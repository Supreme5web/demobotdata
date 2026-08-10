-- Run this in the Supabase SQL editor before starting the bot.

create extension if not exists "pgcrypto";

create table if not exists users (
    id uuid primary key default gen_random_uuid(),
    telegram_id bigint unique not null,
    username text,
    balance numeric not null default 10000,
    created_at timestamptz not null default now()
);

create table if not exists positions (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,
    token_address text not null,
    token_symbol text not null,
    token_name text not null,
    amount numeric not null,
    entry_price numeric not null,
    entry_market_cap numeric,
    invested_amount numeric not null,
    current_price numeric,
    current_market_cap numeric,
    unrealized_pnl numeric default 0,
    tp_price numeric,
    sl_price numeric,
    created_at timestamptz not null default now()
);

-- Safe to re-run: adds TP/SL columns to an existing deployment's positions
-- table without touching any other data.
alter table if exists positions add column if not exists tp_price numeric;
alter table if exists positions add column if not exists sl_price numeric;

-- Safe to re-run: adds multi-chain support. Existing rows (all Solana,
-- pre-dating this column) default to 'sol' so old positions keep working.
alter table if exists positions add column if not exists chain text not null default 'sol';

create table if not exists trades (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,
    token_address text not null,
    token_symbol text not null,
    trade_type text not null check (trade_type in ('BUY', 'SELL')),
    amount numeric not null,
    price numeric not null,
    total_value numeric not null,
    pnl numeric default 0,
    created_at timestamptz not null default now()
);

alter table if exists trades add column if not exists chain text not null default 'sol';

-- A user can hold the same token_address as an unrelated token on a
-- different chain, so positions are unique per (user, token, chain) rather
-- than per (user, token).
drop index if exists idx_positions_token_address;
create unique index if not exists idx_positions_user_token_chain
    on positions(user_id, token_address, chain);

create index if not exists idx_positions_user_id on positions(user_id);
create index if not exists idx_trades_user_id on trades(user_id);

-- Safe to re-run: adds /settings support (custom buy buttons + auto TP/SL
-- defaults applied to new positions). NULL means "use the app default".
alter table if exists users add column if not exists buy_presets jsonb;
alter table if exists users add column if not exists default_tp_multiple numeric;
alter table if exists users add column if not exists default_sl_percent numeric;

-- Safe to re-run: tracks the chat/message id of the currently-pinned
-- position card, so it can be unpinned automatically when the position closes.
alter table if exists positions add column if not exists pinned_chat_id bigint;
alter table if exists positions add column if not exists pinned_message_id bigint;
