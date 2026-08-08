"""Buy, sell, portfolio and PNL logic for the demo trading bot."""

import logging

import database as db
import market

logger = logging.getLogger(__name__)


class TradingError(Exception):
    """Raised for user-facing trading failures (insufficient balance, bad price, etc.)."""


async def execute_buy(user: dict, token_address: str, sol_amount: float) -> dict:
    """Buy a token using a SOL-denominated amount, converted to demo USD."""
    if sol_amount <= 0:
        raise TradingError("Amount must be greater than zero.")

    token_data = await market.get_token_data(token_address)
    if not token_data or token_data["price_usd"] <= 0:
        raise TradingError("Could not fetch a valid price for this token right now.")

    sol_price = await market.get_sol_price()
    usd_amount = sol_amount * sol_price

    balance = float(user["balance"])
    if usd_amount > balance:
        raise TradingError(
            f"Insufficient demo balance. You have ${balance:,.2f}, "
            f"this trade needs ~${usd_amount:,.2f}."
        )

    entry_price = token_data["price_usd"]
    entry_market_cap = float(token_data.get("market_cap") or 0)
    tokens_bought = usd_amount / entry_price
    new_balance = balance - usd_amount

    await db.update_balance(user["id"], new_balance)

    existing = await db.get_position(user["id"], token_address)
    if existing:
        old_amount = float(existing["amount"])
        old_invested = float(existing["invested_amount"])
        old_entry_mcap = float(existing.get("entry_market_cap") or entry_market_cap)
        new_amount = old_amount + tokens_bought
        new_invested = old_invested + usd_amount
        new_entry_price = new_invested / new_amount if new_amount else entry_price
        # Weight the blended entry market cap by invested amount, same approach as entry price.
        new_entry_mcap = (
            ((old_entry_mcap * old_invested) + (entry_market_cap * usd_amount)) / new_invested
            if new_invested
            else entry_market_cap
        )
        await db.update_position(
            existing["id"],
            {
                "amount": new_amount,
                "invested_amount": new_invested,
                "entry_price": new_entry_price,
                "entry_market_cap": new_entry_mcap,
                "current_price": entry_price,
                "current_market_cap": entry_market_cap,
            },
        )
    else:
        await db.create_position(
            {
                "user_id": user["id"],
                "token_address": token_address,
                "token_symbol": token_data["symbol"],
                "token_name": token_data["name"],
                "amount": tokens_bought,
                "entry_price": entry_price,
                "entry_market_cap": entry_market_cap,
                "invested_amount": usd_amount,
                "current_price": entry_price,
                "current_market_cap": entry_market_cap,
                "unrealized_pnl": 0,
            }
        )

    await db.add_trade(
        {
            "user_id": user["id"],
            "token_address": token_address,
            "token_symbol": token_data["symbol"],
            "trade_type": "BUY",
            "amount": tokens_bought,
            "price": entry_price,
            "total_value": usd_amount,
            "pnl": 0,
        }
    )

    return {
        "token_name": token_data["name"],
        "token_symbol": token_data["symbol"],
        "token_address": token_address,
        "sol_amount": sol_amount,
        "usd_amount": usd_amount,
        "entry_price": entry_price,
        "entry_market_cap": entry_market_cap,
        "tokens_bought": tokens_bought,
        "new_balance": new_balance,
    }


async def execute_sell(user: dict, token_address: str, percent: float) -> dict:
    """Sell a percentage (1-100) of an existing position."""
    if not (0 < percent <= 100):
        raise TradingError("Invalid sell percentage.")

    position = await db.get_position(user["id"], token_address)
    if not position:
        raise TradingError("You don't have an open position in this token.")

    token_data = await market.get_token_data(token_address)
    if not token_data or token_data["price_usd"] <= 0:
        raise TradingError("Could not fetch a valid price for this token right now.")

    exit_price = token_data["price_usd"]
    exit_market_cap = float(token_data.get("market_cap") or 0)
    total_amount = float(position["amount"])
    sell_amount = total_amount * (percent / 100)
    entry_price = float(position["entry_price"])
    entry_market_cap = float(position.get("entry_market_cap") or 0)

    proceeds = sell_amount * exit_price
    cost_basis = sell_amount * entry_price
    pnl = proceeds - cost_basis
    pnl_pct = (pnl / cost_basis * 100) if cost_basis > 0 else 0.0

    balance = float(user["balance"])
    new_balance = balance + proceeds
    await db.update_balance(user["id"], new_balance)

    remaining_amount = total_amount - sell_amount
    position_closed = remaining_amount <= 1e-9
    if position_closed:
        await db.delete_position(position["id"])
    else:
        remaining_invested = float(position["invested_amount"]) * (remaining_amount / total_amount)
        await db.update_position(
            position["id"],
            {
                "amount": remaining_amount,
                "invested_amount": remaining_invested,
                "current_price": exit_price,
            },
        )

    await db.add_trade(
        {
            "user_id": user["id"],
            "token_address": token_address,
            "token_symbol": position["token_symbol"],
            "trade_type": "SELL",
            "amount": sell_amount,
            "price": exit_price,
            "total_value": proceeds,
            "pnl": pnl,
        }
    )

    return {
        "token_name": position["token_name"],
        "token_symbol": position["token_symbol"],
        "token_address": token_address,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_market_cap": entry_market_cap,
        "exit_market_cap": exit_market_cap,
        "proceeds": proceeds,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "new_balance": new_balance,
        # Added for PNL card generation - doesn't affect trading logic above.
        "invested": cost_basis,
        "final_value": proceeds,
        "position_closed": position_closed,
        "entry_time": position.get("created_at"),
        "logo_url": token_data.get("logo_url", ""),
    }


async def refresh_all_positions() -> None:
    """Refresh current price, market cap, and unrealized PNL for every open position."""
    positions = await db.get_all_positions()
    for pos in positions:
        try:
            token_data = await market.get_token_data(pos["token_address"])
            if not token_data or token_data["price_usd"] <= 0:
                continue
            price = token_data["price_usd"]
            market_cap = float(token_data.get("market_cap") or 0)
            amount = float(pos["amount"])
            invested = float(pos["invested_amount"])
            unrealized_pnl = (price * amount) - invested
            await db.update_position(
                pos["id"],
                {
                    "current_price": price,
                    "current_market_cap": market_cap,
                    "unrealized_pnl": unrealized_pnl,
                },
            )
        except Exception as exc:  # noqa: BLE001 - keep the loop alive on any single failure
            logger.error("Failed to refresh position %s: %s", pos.get("id"), exc)
