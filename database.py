"""Supabase connection and data access functions.

All Supabase calls are synchronous under the hood, so each function here
wraps its call in asyncio.to_thread to avoid blocking the event loop.
"""

import asyncio
import logging
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, STARTING_BALANCE_USDC

logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int, username: Optional[str]) -> dict:
    def _lookup():
        res = (
            supabase.table("users")
            .select("*")
            .eq("telegram_id", telegram_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    existing = await _run(_lookup)
    if existing:
        return existing

    # New user: seed their demo balance with a flat starting amount of the
    # bot's demo trading currency, USDC (pegged 1:1 to USD, so no price
    # lookup is needed here).
    def _insert():
        new_user = {
            "telegram_id": telegram_id,
            "username": username or "",
            "balance": STARTING_BALANCE_USDC,
        }
        insert_res = supabase.table("users").insert(new_user).execute()
        logger.info(
            "Created new user for telegram_id=%s with starting balance %.2f USDC",
            telegram_id, STARTING_BALANCE_USDC,
        )
        return insert_res.data[0]

    return await _run(_insert)


async def update_balance(user_id: str, new_balance: float) -> None:
    def _op():
        supabase.table("users").update({"balance": new_balance}).eq("id", user_id).execute()

    await _run(_op)


async def get_user_by_id(user_id: str) -> Optional[dict]:
    def _op():
        res = supabase.table("users").select("*").eq("id", user_id).limit(1).execute()
        return res.data[0] if res.data else None

    return await _run(_op)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
# Positions are keyed on (user_id, token_address, chain) rather than just
# (user_id, token_address) - the same address string can be an unrelated
# token on a different EVM chain (eth / bsc / rbh), so chain disambiguates.

async def get_position(user_id: str, token_address: str, chain: str) -> Optional[dict]:
    def _op():
        res = (
            supabase.table("positions")
            .select("*")
            .eq("user_id", user_id)
            .eq("token_address", token_address)
            .eq("chain", chain)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    return await _run(_op)


async def get_positions(user_id: str) -> list:
    def _op():
        res = supabase.table("positions").select("*").eq("user_id", user_id).execute()
        return res.data or []

    return await _run(_op)


async def get_all_positions() -> list:
    def _op():
        res = supabase.table("positions").select("*").execute()
        return res.data or []

    return await _run(_op)


async def create_position(position: dict) -> dict:
    def _op():
        res = supabase.table("positions").insert(position).execute()
        return res.data[0]

    return await _run(_op)


async def update_position(position_id: str, fields: dict) -> None:
    def _op():
        supabase.table("positions").update(fields).eq("id", position_id).execute()

    await _run(_op)


async def set_position_tp_sl(
    position_id: str, tp_price: Optional[float], sl_price: Optional[float]
) -> None:
    def _op():
        supabase.table("positions").update(
            {"tp_price": tp_price, "sl_price": sl_price}
        ).eq("id", position_id).execute()

    await _run(_op)


async def delete_position(position_id: str) -> None:
    def _op():
        supabase.table("positions").delete().eq("id", position_id).execute()

    await _run(_op)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

async def add_trade(trade: dict) -> None:
    def _op():
        supabase.table("trades").insert(trade).execute()

    await _run(_op)


async def get_trades(user_id: str, limit: int = 10) -> list:
    def _op():
        res = (
            supabase.table("trades")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []

    return await _run(_op)


async def get_total_realized_pnl(user_id: str) -> float:
    """Sums PNL across every SELL trade the user has ever made (all-time,
    not limited by the /history display cap)."""
    def _op():
        res = (
            supabase.table("trades")
            .select("pnl")
            .eq("user_id", user_id)
            .eq("trade_type", "SELL")
            .execute()
        )
        return sum(float(row.get("pnl") or 0) for row in (res.data or []))

    return await _run(_op)
