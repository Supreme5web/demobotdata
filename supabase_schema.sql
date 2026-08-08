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
    invested_amount numeric not null,
    current_price numeric,
    unrealized_pnl numeric default 0,
    created_at timestamptz not null default now()
);

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

create index if not exists idx_positions_user_id on positions(user_id);
create index if not exists idx_positions_token_address on positions(token_address);
create index if not exists idx_trades_user_id on trades(user_id);
