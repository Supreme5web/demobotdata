-- Migration for: Weekly Reset + Limit Orders
-- Run this in the Supabase SQL editor before deploying the updated bot.

-- ---------------------------------------------------------------------
-- Weekly Reset
-- ---------------------------------------------------------------------
-- Tracks when a user last used /reset, so main.py's _reset_status() can
-- enforce "Fridays only, once per 7 days".
alter table users
    add column if not exists last_reset_at timestamptz;

-- ---------------------------------------------------------------------
-- Limit Orders
-- ---------------------------------------------------------------------
-- A queued buy: fires once the token's market cap crosses target_market_cap,
-- in the direction recorded at creation time ('below' = buy the dip,
-- 'above' = buy the breakout). Rows are kept after fill/cancel (status
-- updated in place) rather than deleted, as a lightweight order history.
create table if not exists limit_orders (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references users(id) on delete cascade,
    token_address text not null,
    chain text not null,
    token_symbol text,
    token_name text,
    usdc_amount numeric not null,
    target_market_cap numeric not null,
    direction text not null check (direction in ('above', 'below')),
    status text not null default 'open' check (status in ('open', 'filled', 'cancelled')),
    created_at timestamptz not null default now()
);

create index if not exists limit_orders_open_idx
    on limit_orders (status)
    where status = 'open';

create index if not exists limit_orders_user_idx
    on limit_orders (user_id);
