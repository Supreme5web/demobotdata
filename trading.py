"""Buy, sell, portfolio and PNL logic for the demo trading bot."""

import logging

import database as db
import market
from config import DEFAULT_CHAIN

logger = logging.getLogger(__name__)


class TradingError(Exception):
    """Raised for user-facing trading failures (insufficient balance, bad price, etc.)."""


async def execute_buy(user: dict, token_address: str, usdc_amount: float, chain: str = DEFAULT_CHAIN) -> dict:
    """Buy a token using an amount denominated in USDC, the bot's demo
    trading currency. USDC is pegged 1:1 to USD, so - unlike a chain's
    native gas token (SOL/BNB/ETH), whose USD value moves - the amount
    needs no price lookup or conversion; it's spent as-is.

    If the user already holds this token on this chain, this DCAs into the
    existing position: the new buy's cost is added to invested_amount, and
    entry_price / entry_market_cap are recomputed as the invested-amount-
    weighted average across the old and new fills, so a second (or third,
    or Nth) buy always reflects the true blended average entry rather than
    just the latest fill price.
    """
    if usdc_amount <= 0:
        raise TradingError("Amount must be greater than zero.")

    token_data = await market.get_token_data(token_address, chain)
    if not token_data or token_data["price_usd"] <= 0:
        raise TradingError("Could not fetch a valid price for this token right now.")

    usd_amount = usdc_amount  # USDC is 1:1 with USD - no conversion needed.

    balance = float(user["balance"])
    if usd_amount > balance:
        raise TradingError(
            f"Insufficient demo balance. You have {balance:,.2f} USDC, "
            f"this trade needs {usd_amount:,.2f} USDC."
        )

    entry_price = token_data["price_usd"]
    entry_market_cap = float(token_data.get("market_cap") or 0)
    tokens_bought = usd_amount / entry_price
    new_balance = balance - usd_amount

    await db.update_balance(user["id"], new_balance)

    existing = await db.get_position(user["id"], token_address, chain)
    if existing:
        # --- DCA into the existing position -------------------------------
        old_amount = float(existing["amount"])
        old_invested = float(existing["invested_amount"])
        old_entry_mcap = float(existing.get("entry_market_cap") or entry_market_cap)
        new_amount = old_amount + tokens_bought
        new_invested = old_invested + usd_amount
        # Average entry price = total $ invested / total tokens held - the
        # standard cost-basis formula, so repeated buys always land on the
        # correct blended entry regardless of how many fills went into it.
        new_entry_price = new_invested / new_amount if new_amount else entry_price
        # Entry market cap is weighted the same way (by $ invested at each
        # fill) so it stays consistent with the blended entry price above.
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
        avg_entry_price = new_entry_price
        avg_entry_mcap = new_entry_mcap
        total_amount = new_amount
        total_invested = new_invested
        is_dca = True
    else:
        # Apply the user's /settings auto TP/SL defaults (if any) to a
        # brand-new position only - DCA buys above keep whatever TP/SL was
        # already set on the existing position untouched.
        default_tp_mult = user.get("default_tp_multiple")
        default_sl_pct = user.get("default_sl_percent")
        default_tp_price = entry_price * float(default_tp_mult) if default_tp_mult else None
        default_sl_price = entry_price * (1 - float(default_sl_pct) / 100) if default_sl_pct else None

        await db.create_position(
            {
                "user_id": user["id"],
                "token_address": token_address,
                "chain": chain,
                "token_symbol": token_data["symbol"],
                "token_name": token_data["name"],
                "amount": tokens_bought,
                "entry_price": entry_price,
                "entry_market_cap": entry_market_cap,
                "invested_amount": usd_amount,
                "current_price": entry_price,
                "current_market_cap": entry_market_cap,
                "unrealized_pnl": 0,
                "tp_price": default_tp_price,
                "sl_price": default_sl_price,
            }
        )
        avg_entry_price = entry_price
        avg_entry_mcap = entry_market_cap
        total_amount = tokens_bought
        total_invested = usd_amount
        is_dca = False

    await db.add_trade(
        {
            "user_id": user["id"],
            "token_address": token_address,
            "chain": chain,
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
        "chain": chain,
        "usdc_amount": usdc_amount,
        "usd_amount": usd_amount,
        "entry_price": entry_price,
        "entry_market_cap": entry_market_cap,
        "tokens_bought": tokens_bought,
        "new_balance": new_balance,
        # Position-level (post-fill) figures - what the card should display
        # so a DCA buy shows the true blended average, not just this fill.
        "avg_entry_price": avg_entry_price,
        "avg_entry_market_cap": avg_entry_mcap,
        "total_amount": total_amount,
        "total_invested": total_invested,
        "is_dca": is_dca,
    }


async def execute_sell(user: dict, token_address: str, percent: float, chain: str = DEFAULT_CHAIN) -> dict:
    """Sell a percentage (1-100) of an existing position."""
    if not (0 < percent <= 100):
        raise TradingError("Invalid sell percentage.")

    position = await db.get_position(user["id"], token_address, chain)
    if not position:
        raise TradingError("You don't have an open position in this token.")

    token_data = await market.get_token_data(token_address, chain)
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
            "chain": chain,
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
        "chain": chain,
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
        "username": user.get("username") or "",
    }


async def refresh_all_positions() -> None:
    """Refresh current price, market cap, and unrealized PNL for every open position."""
    positions = await db.get_all_positions()
    for pos in positions:
        try:
            chain = pos.get("chain") or DEFAULT_CHAIN
            token_data = await market.get_token_data(pos["token_address"], chain)
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
