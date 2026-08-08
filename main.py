"""Telegram bot + FastAPI server for the demo (paper) trading bot.

No wallets, no private keys, no real transactions - everything here trades
against a virtual USD balance stored in Supabase.
"""

import asyncio
import html
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import database as db
import market
import trading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Base58, 32-44 chars - matches typical Solana addresses.
SOLANA_CA_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

DIVIDER = "━━━━━━━━━━━━━━━━━━━━"

# Preset TP/SL combos offered right after a buy. Each is
# (label, take_profit_multiple, stop_loss_percent).
TP_SL_PRESETS = [
    ("🎯 2x  /  🛑 -20%", 2, 20),
    ("🎯 3x  /  🛑 -30%", 3, 30),
    ("🎯 5x  /  🛑 -50%", 5, 50),
]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usd(value) -> str:
    return f"${float(value or 0):,.2f}"


def fmt_sol(value) -> str:
    return f"{float(value or 0):,.4f} SOL"


def fmt_price(value) -> str:
    price = float(value or 0)
    if price == 0:
        return "$0.00"
    if price >= 1:
        return f"${price:,.4f}"
    if price >= 0.01:
        return f"${price:.6f}"
    # Very small prices (typical for new memecoins) - show significant digits.
    formatted = f"{price:.10f}".rstrip("0")
    if formatted.endswith("."):
        formatted += "0"
    return f"${formatted}"


def fmt_compact(value) -> str:
    v = float(value or 0)
    if v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


def fmt_pct(value) -> str:
    v = float(value or 0)
    arrow = "▲" if v > 0 else "▼" if v < 0 else "•"
    return f"{arrow} {v:+.2f}%"


def dex_chart_url(token_address: str, fallback: str = "") -> str:
    """Best-effort DexScreener chart link. Prefers the exact pair URL from
    the API response; falls back to the generic token-address route, which
    DexScreener resolves to the most liquid pair."""
    return fallback or f"https://dexscreener.com/solana/{token_address}"


async def get_user(update: Update) -> dict:
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)


async def balance_block(user: dict) -> str:
    """Renders the demo balance in both USD and its live SOL equivalent."""
    usd_balance = float(user["balance"])
    sol_price = await market.get_sol_price()
    sol_equiv = usd_balance / sol_price if sol_price else 0.0
    return f"💰 <b>Balance:</b> {fmt_usd(usd_balance)}  <i>(≈ {fmt_sol(sol_equiv)})</i>"


def tp_sl_line(entry_price: float, tp_price, sl_price) -> str:
    parts = []
    if tp_price:
        mult = float(tp_price) / entry_price if entry_price else 0
        parts.append(f"🎯 TP: {fmt_price(tp_price)} ({mult:.1f}x)")
    if sl_price:
        pct = ((float(sl_price) / entry_price) - 1) * 100 if entry_price else 0
        parts.append(f"🛑 SL: {fmt_price(sl_price)} ({pct:+.1f}%)")
    if not parts:
        return "⚙️ TP/SL: not set"
    return "  |  ".join(parts)


def format_position_card(
    *,
    name: str,
    symbol: str,
    token_address: str,
    entry_price: float,
    entry_market_cap,
    current_market_cap,
    tokens: float,
    invested: float,
    pnl: float,
    pnl_pct: float,
    tp_price=None,
    sl_price=None,
) -> str:
    """Render a clean, refreshable position summary keyed on market cap."""
    if pnl > 0:
        emoji = "🟢"
    elif pnl < 0:
        emoji = "🔴"
    else:
        emoji = "⚪"

    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    chart_url = dex_chart_url(token_address)

    return (
        f"📌 <b>{html.escape(name)}</b> ({html.escape(symbol)})\n"
        f"<code>{html.escape(token_address)}</code>\n"
        f"{DIVIDER}\n"
        f"Entry MCap: <b>{fmt_compact(entry_market_cap)}</b>\n"
        f"Current MCap: <b>{fmt_compact(current_market_cap)}</b>\n"
        f"Tokens Held: {tokens:,.0f}\n"
        f"Invested: {fmt_usd(invested)}\n"
        f"{tp_sl_line(entry_price, tp_price, sl_price)}\n"
        f"{DIVIDER}\n"
        f"{emoji} <b>PNL: {fmt_usd(pnl)} ({pnl_pct:+.2f}%)</b>\n\n"
        f"📊 <a href=\"{chart_url}\">View Live Chart on DexScreener</a>\n"
        f"<i>Updated {timestamp}</i>"
    )


def build_position_keyboard(token_address: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{token_address}")],
            [
                InlineKeyboardButton("💸 Sell 50%", callback_data=f"sell:{token_address}:50"),
                InlineKeyboardButton("💯 Sell 100%", callback_data=f"sell:{token_address}:100"),
            ],
            [InlineKeyboardButton("📊 DexScreener", url=dex_chart_url(token_address))],
        ]
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    balance_line = await balance_block(user)
    await update.message.reply_text(
        "⚡ <b>TRADEEMDEMO</b> — Solana Paper Trading\n"
        f"{DIVIDER}\n"
        "Practice trading Solana tokens with real live prices and zero risk. "
        "No wallet, no private keys, no real funds — every trade here is simulated "
        "against a demo balance.\n\n"
        f"{balance_line}\n\n"
        "<b>How it works:</b>\n"
        "📩 Send any Solana token contract address to pull up live price, "
        "market cap, liquidity and volume, then buy straight from the chat.\n\n"
        "<b>Commands</b>\n"
        "/balance — check your demo balance\n"
        "/portfolio — view your open positions\n"
        "/history — view your recent trades\n"
        "/start — show this menu again",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    balance_line = await balance_block(user)
    await update.message.reply_text(
        f"{balance_line}",
        parse_mode=ParseMode.HTML,
    )


async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    positions = await db.get_positions(user["id"])

    if not positions:
        await update.message.reply_text(
            "📭 You have no open positions yet.\n\n"
            "Send a token contract address to pull up live data and start trading!"
        )
        return

    total_invested = 0.0
    total_value = 0.0
    for pos in positions:
        amount = float(pos["amount"])
        invested = float(pos["invested_amount"])
        current_price = float(pos.get("current_price") or pos["entry_price"])
        total_invested += invested
        total_value += amount * current_price

    total_pnl = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0
    overview_emoji = "🟢" if total_pnl > 0 else "🔴" if total_pnl < 0 else "⚪"

    overview = (
        "📊 <b>PORTFOLIO OVERVIEW</b>\n"
        f"{DIVIDER}\n"
        f"Open Positions: <b>{len(positions)}</b>\n"
        f"Total Invested: {fmt_usd(total_invested)}\n"
        f"Current Value: {fmt_usd(total_value)}\n"
        f"{overview_emoji} <b>Total PNL: {fmt_usd(total_pnl)} ({total_pnl_pct:+.2f}%)</b>"
    )
    await update.message.reply_text(overview, parse_mode=ParseMode.HTML)

    for pos in positions:
        amount = float(pos["amount"])
        invested = float(pos["invested_amount"])
        entry_price = float(pos["entry_price"])
        entry_mcap = float(pos.get("entry_market_cap") or 0)
        current_mcap = float(pos.get("current_market_cap") or entry_mcap)
        current_price = float(pos.get("current_price") or entry_price)
        current_value = amount * current_price
        pnl = current_value - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0

        text = format_position_card(
            name=pos["token_name"],
            symbol=pos["token_symbol"],
            token_address=pos["token_address"],
            entry_price=entry_price,
            entry_market_cap=entry_mcap,
            current_market_cap=current_mcap,
            tokens=amount,
            invested=invested,
            pnl=pnl,
            pnl_pct=pnl_pct,
            tp_price=pos.get("tp_price"),
            sl_price=pos.get("sl_price"),
        )
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_position_keyboard(pos["token_address"]),
            disable_web_page_preview=True,
        )


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    trades = await db.get_trades(user["id"], limit=10)

    if not trades:
        await update.message.reply_text("No trades yet. Send a token contract address to get started!")
        return

    lines = [f"📜 <b>RECENT TRADES</b>\n{DIVIDER}"]
    for t in trades:
        emoji = "🟢" if t["trade_type"] == "BUY" else "🔴"
        pnl_str = f" | PNL: {fmt_usd(t['pnl'])}" if t["trade_type"] == "SELL" else ""
        lines.append(
            f"{emoji} <b>{t['trade_type']}</b> {html.escape(t['token_symbol'])} "
            f"— {fmt_usd(t['total_value'])}{pnl_str}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Token info + trading flow
# ---------------------------------------------------------------------------

def build_token_info_text(token_data: dict, token_address: str) -> str:
    chart_url = dex_chart_url(token_address, token_data.get("dex_url", ""))
    return (
        f"⚡ <b>{html.escape(token_data['name'])}</b> "
        f"(<b>{html.escape(token_data['symbol'])}</b>)\n"
        f"{DIVIDER}\n"
        f"💰 Price: <b>{fmt_price(token_data['price_usd'])}</b>\n"
        f"📊 Market Cap: {fmt_compact(token_data['market_cap'])}\n"
        f"💧 Liquidity: {fmt_compact(token_data['liquidity_usd'])}\n"
        f"📈 Volume (24h): {fmt_compact(token_data['volume_24h'])}\n"
        f"📉 Change (24h): {fmt_pct(token_data['price_change_24h'])}\n"
        f"{DIVIDER}\n"
        f"📄 CA: <code>{html.escape(token_address)}</code>\n\n"
        f"📊 <a href=\"{chart_url}\">View Live Chart on DexScreener</a>"
    )


def build_token_info_keyboard(token_address: str, dex_url: str = "") -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("BUY 0.1", callback_data=f"buy:{token_address}:0.1"),
            InlineKeyboardButton("BUY 0.5", callback_data=f"buy:{token_address}:0.5"),
        ],
        [
            InlineKeyboardButton("BUY 0.6", callback_data=f"buy:{token_address}:0.6"),
            InlineKeyboardButton("BUY 1", callback_data=f"buy:{token_address}:1"),
        ],
        [InlineKeyboardButton("✏️ Custom Amount", callback_data=f"buycustom:{token_address}")],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refreshtoken:{token_address}"),
            InlineKeyboardButton("📊 DexScreener", url=dex_chart_url(token_address, dex_url)),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


async def show_token_info(update: Update, token_address: str) -> None:
    token_data = await market.get_token_data(token_address)
    if not token_data:
        await update.message.reply_text(
            "⚠️ Couldn't find that token on DexScreener. Double check the contract address."
        )
        return

    text = build_token_info_text(token_data, token_address)
    keyboard = build_token_info_keyboard(token_address, token_data.get("dex_url", ""))

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def process_refresh_token(query, token_address: str) -> None:
    token_data = await market.get_token_data(token_address)
    if not token_data:
        await query.answer("Couldn't fetch live data right now. Try again shortly.", show_alert=True)
        return

    text = build_token_info_text(token_data, token_address)
    keyboard = build_token_info_keyboard(token_address, token_data.get("dex_url", ""))

    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise

    await query.answer("Updated")


async def process_buy(update: Update, target, token_address: str, sol_amount: float) -> None:
    user = await get_user(update)
    try:
        result = await trading.execute_buy(user, token_address, sol_amount)
    except trading.TradingError as exc:
        await target.reply_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Unexpected error during buy")
        await target.reply_text("⚠️ Something went wrong processing that trade. Please try again.")
        return

    sol_price = await market.get_sol_price()
    new_balance_sol = result["new_balance"] / sol_price if sol_price else 0.0

    confirm_text = (
        "✅ <b>DEMO BUY EXECUTED</b>\n"
        f"{DIVIDER}\n"
        f"Token: <b>{html.escape(result['token_name'])}</b> "
        f"({html.escape(result['token_symbol'])})\n"
        f"Spent: {result['sol_amount']:.4f} SOL ({fmt_usd(result['usd_amount'])})\n"
        f"Entry Price: {fmt_price(result['entry_price'])}\n"
        f"Tokens: {result['tokens_bought']:,.0f}\n\n"
        f"💰 Balance: {fmt_usd(result['new_balance'])} "
        f"<i>(≈ {fmt_sol(new_balance_sol)})</i>\n\n"
        "🎯 <b>Set Take Profit / Stop Loss?</b>\n"
        "Auto-sells this position the moment price hits your target — "
        "no need to watch the chart."
    )
    keyboard_rows = [
        [InlineKeyboardButton(label, callback_data=f"tpsl:{token_address}:{tp}:{sl}")]
        for label, tp, sl in TP_SL_PRESETS
    ]
    keyboard_rows.append(
        [InlineKeyboardButton("⏭️ Skip (manage manually)", callback_data=f"tpsl:{token_address}:0:0")]
    )

    await target.reply_text(
        confirm_text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def process_tp_sl_selection(query, token_address: str, tp_mult: float, sl_pct: float) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    position = await db.get_position(user["id"], token_address)
    if not position:
        await query.answer("This position is no longer open.", show_alert=True)
        return

    entry_price = float(position["entry_price"])
    tp_price = entry_price * tp_mult if tp_mult > 0 else None
    sl_price = entry_price * (1 - sl_pct / 100) if sl_pct > 0 else None

    await db.set_position_tp_sl(position["id"], tp_price, sl_price)

    amount = float(position["amount"])
    invested = float(position["invested_amount"])
    entry_mcap = float(position.get("entry_market_cap") or 0)

    text = format_position_card(
        name=position["token_name"],
        symbol=position["token_symbol"],
        token_address=token_address,
        entry_price=entry_price,
        entry_market_cap=entry_mcap,
        current_market_cap=entry_mcap,
        tokens=amount,
        invested=invested,
        pnl=0.0,
        pnl_pct=0.0,
        tp_price=tp_price,
        sl_price=sl_price,
    )

    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_position_keyboard(token_address),
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise

    await query.answer("TP/SL set" if (tp_price or sl_price) else "Skipped — trade set to manual")


async def process_refresh(query, token_address: str) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    position = await db.get_position(user["id"], token_address)
    if not position:
        await query.answer("This position has been closed.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return

    token_data = await market.get_token_data(token_address)
    if not token_data or token_data["price_usd"] <= 0:
        await query.answer("Couldn't fetch a live price right now. Try again shortly.", show_alert=True)
        return

    amount = float(position["amount"])
    invested = float(position["invested_amount"])
    entry_price = float(position["entry_price"])
    current_price = token_data["price_usd"]
    current_mcap = float(token_data.get("market_cap") or 0)
    entry_mcap = float(position.get("entry_market_cap") or current_mcap)
    pnl = (amount * current_price) - invested
    pnl_pct = (pnl / invested * 100) if invested else 0.0

    text = format_position_card(
        name=position["token_name"],
        symbol=position["token_symbol"],
        token_address=token_address,
        entry_price=entry_price,
        entry_market_cap=entry_mcap,
        current_market_cap=current_mcap,
        tokens=amount,
        invested=invested,
        pnl=pnl,
        pnl_pct=pnl_pct,
        tp_price=position.get("tp_price"),
        sl_price=position.get("sl_price"),
    )

    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_position_keyboard(token_address),
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise

    await query.answer("Updated")


async def process_sell(update: Update, target, token_address: str, percent: float) -> None:
    user = await get_user(update)
    try:
        result = await trading.execute_sell(user, token_address, percent)
    except trading.TradingError as exc:
        await target.reply_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Unexpected error during sell")
        await target.reply_text("⚠️ Something went wrong processing that trade. Please try again.")
        return

    sol_price = await market.get_sol_price()
    new_balance_sol = result["new_balance"] / sol_price if sol_price else 0.0

    emoji = "🟢" if result["pnl"] >= 0 else "🔴"
    text = (
        "💸 <b>DEMO SELL EXECUTED</b>\n"
        f"{DIVIDER}\n"
        f"Token: <b>{html.escape(result['token_name'])}</b> "
        f"({html.escape(result['token_symbol'])})\n"
        f"Entry: {fmt_price(result['entry_price'])}\n"
        f"Exit: {fmt_price(result['exit_price'])}\n"
        f"Received: {fmt_usd(result['proceeds'])}\n"
        f"{emoji} <b>PNL: {fmt_usd(result['pnl'])} ({result['pnl_pct']:+.2f}%)</b>\n\n"
        f"💰 Balance: {fmt_usd(result['new_balance'])} "
        f"<i>(≈ {fmt_sol(new_balance_sol)})</i>"
    )
    await target.reply_text(text, parse_mode=ParseMode.HTML)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    pending_ca = context.user_data.get("awaiting_custom_buy")
    if pending_ca:
        context.user_data.pop("awaiting_custom_buy", None)
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("Please send a valid number, e.g. 0.75")
            return
        await process_buy(update, update.message, pending_ca, amount)
        return

    if SOLANA_CA_REGEX.match(text):
        await show_token_info(update, text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""

    # Each branch answers the callback query exactly once - Telegram rejects
    # a second answer() call on the same query, so `refresh`/`refreshtoken`/
    # `tpsl` handle their own (with a toast/alert) instead of us answering
    # here up front.
    if action == "buy" and len(parts) == 3:
        await query.answer()
        token_address, amount_str = parts[1], parts[2]
        await process_buy(update, query.message, token_address, float(amount_str))
    elif action == "buycustom" and len(parts) == 2:
        await query.answer()
        token_address = parts[1]
        context.user_data["awaiting_custom_buy"] = token_address
        await query.message.reply_text("Send the amount of SOL you'd like to spend, e.g. 0.75")
    elif action == "sell" and len(parts) == 3:
        await query.answer()
        token_address, percent_str = parts[1], parts[2]
        await process_sell(update, query.message, token_address, float(percent_str))
    elif action == "refresh" and len(parts) == 2:
        token_address = parts[1]
        await process_refresh(query, token_address)
    elif action == "refreshtoken" and len(parts) == 2:
        token_address = parts[1]
        await process_refresh_token(query, token_address)
    elif action == "tpsl" and len(parts) == 4:
        token_address, tp_str, sl_str = parts[1], parts[2], parts[3]
        await process_tp_sl_selection(query, token_address, float(tp_str), float(sl_str))
    else:
        await query.answer()


# ---------------------------------------------------------------------------
# TP/SL auto-execution monitor
# ---------------------------------------------------------------------------

async def check_tp_sl_triggers() -> None:
    """Scans open positions for a hit take-profit or stop-loss target and
    auto-sells the full position, notifying the user in Telegram."""
    positions = await db.get_all_positions()
    for pos in positions:
        tp_price = pos.get("tp_price")
        sl_price = pos.get("sl_price")
        if not tp_price and not sl_price:
            continue

        current_price = pos.get("current_price")
        if not current_price:
            continue
        current_price = float(current_price)

        hit_tp = bool(tp_price) and current_price >= float(tp_price)
        hit_sl = bool(sl_price) and current_price <= float(sl_price)
        if not (hit_tp or hit_sl):
            continue

        user = await db.get_user_by_id(pos["user_id"])
        if not user:
            continue

        try:
            result = await trading.execute_sell(user, pos["token_address"], 100)
        except trading.TradingError:
            continue
        except Exception:
            logger.exception("Auto TP/SL sell failed for position %s", pos.get("id"))
            continue

        sol_price = await market.get_sol_price()
        new_balance_sol = result["new_balance"] / sol_price if sol_price else 0.0

        reason = "🎯 TAKE PROFIT HIT" if hit_tp else "🛑 STOP LOSS HIT"
        emoji = "🟢" if result["pnl"] >= 0 else "🔴"
        text = (
            f"{reason}\n"
            f"{DIVIDER}\n"
            f"Token: <b>{html.escape(result['token_name'])}</b> "
            f"({html.escape(result['token_symbol'])})\n"
            f"Entry: {fmt_price(result['entry_price'])}\n"
            f"Exit: {fmt_price(result['exit_price'])}\n"
            f"Received: {fmt_usd(result['proceeds'])}\n"
            f"{emoji} <b>PNL: {fmt_usd(result['pnl'])} ({result['pnl_pct']:+.2f}%)</b>\n\n"
            f"💰 Balance: {fmt_usd(result['new_balance'])} "
            f"<i>(≈ {fmt_sol(new_balance_sol)})</i>\n\n"
            "🤖 Position auto-closed by your TP/SL order."
        )
        try:
            await telegram_app.bot.send_message(
                chat_id=user["telegram_id"], text=text, parse_mode=ParseMode.HTML
            )
        except Exception:
            logger.exception("Failed to notify user %s of TP/SL trigger", user.get("telegram_id"))


# ---------------------------------------------------------------------------
# Telegram application setup
# ---------------------------------------------------------------------------

telegram_app: Application = Application.builder().token(config.TELEGRAM_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start_handler))
telegram_app.add_handler(CommandHandler("balance", balance_handler))
telegram_app.add_handler(CommandHandler("portfolio", portfolio_handler))
telegram_app.add_handler(CommandHandler("history", history_handler))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

BOT_COMMANDS = [
    BotCommand("start", "Welcome & how it works"),
    BotCommand("balance", "Check your demo balance"),
    BotCommand("portfolio", "View your open positions"),
    BotCommand("history", "View your recent trades"),
]


async def price_update_loop() -> None:
    """Background task: refresh open position prices/PNL every 30s, then
    check for any take-profit/stop-loss targets that were hit."""
    while True:
        try:
            await trading.refresh_all_positions()
            await check_tp_sl_triggers()
        except Exception:
            logger.exception("Price update loop failed")
        await asyncio.sleep(config.PRICE_UPDATE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
    await telegram_app.bot.set_my_commands(BOT_COMMANDS)
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    price_task = asyncio.create_task(price_update_loop())
    logger.info("Telegram bot started and polling for updates.")

    try:
        yield
    finally:
        price_task.cancel()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("Telegram bot stopped.")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "running"}
