"""Supabase connection and data access functions.

All Supabase calls are synchronous under the hood, so each function here
wraps its call in asyncio.to_thread to avoid blocking the event loop.
"""

import asyncio
import logging
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_KEY, STARTING_BALANCE

logger = logging.getLogger(__name__)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


async def _run(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_or_create_user(telegram_id: int, username: Optional[str]) -> dict:
    def _op():
        res = (
            supabase.table("users")
            .select("*")
            .eq("telegram_id", telegram_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0]

        new_user = {
            "telegram_id": telegram_id,
            "username": username or "",
            "balance": STARTING_BALANCE,
        }
        insert_res = supabase.table("users").insert(new_user).execute()
        logger.info("Created new user for telegram_id=%s", telegram_id)
        return insert_res.data[0]

    return await _run(_op)


async def update_balance(user_id: str, new_balance: float) -> None:
    def _op():
        supabase.table("users").update({"balance": new_balance}).eq("id", user_id).execute()

    await _run(_op)


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

async def get_position(user_id: str, token_address: str) -> Optional[dict]:
    def _op():
        res = (
            supabase.table("positions")
            .select("*")
            .eq("user_id", user_id)
            .eq("token_address", token_address)
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
