"""Telegram bot + FastAPI server for PaperBoat - Demo Trading Bot.

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

# Take-profit presets, expressed as a multiple of entry market cap (e.g. "2" = 2x).
TP_PRESETS = [2, 3, 5, 10]
# Stop-loss presets, expressed as percent below entry market cap.
SL_PRESETS = [10, 20, 30, 50]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usd(value) -> str:
    return f"${float(value or 0):,.2f}"


def fmt_sol(value) -> str:
    return f"{float(value or 0):,.4f} SOL"


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


def tp_sl_line(entry_price: float, entry_mcap: float, tp_price, sl_price) -> str:
    """TP/SL are stored internally as prices (for precise trigger checks)
    but displayed as market cap, in line with the rest of the UI."""
    if not entry_price:
        return "⚙️ TP/SL: not set"

    parts = []
    if tp_price:
        mult = float(tp_price) / entry_price
        parts.append(f"🎯 TP: {fmt_compact(entry_mcap * mult)} ({mult:.1f}x)")
    if sl_price:
        ratio = float(sl_price) / entry_price
        parts.append(f"🛑 SL: {fmt_compact(entry_mcap * ratio)} ({(ratio - 1) * 100:+.1f}%)")
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
    entry_mcap = float(entry_market_cap or 0)

    return (
        f"📌 <b>{html.escape(name)}</b> ({html.escape(symbol)})\n"
        f"<code>{html.escape(token_address)}</code>\n"
        f"{DIVIDER}\n"
        f"Entry MCap: <b>{fmt_compact(entry_mcap)}</b>\n"
        f"Current MCap: <b>{fmt_compact(current_market_cap)}</b>\n"
        f"Tokens Held: {tokens:,.0f}\n"
        f"Invested: {fmt_usd(invested)}\n"
        f"{tp_sl_line(entry_price, entry_mcap, tp_price, sl_price)}\n"
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
                InlineKeyboardButton("🎯 Set TP", callback_data=f"tpmenu:{token_address}"),
                InlineKeyboardButton("🛑 Set SL", callback_data=f"slmenu:{token_address}"),
            ],
            [
                InlineKeyboardButton("💸 Sell 50%", callback_data=f"sell:{token_address}:50"),
                InlineKeyboardButton("💯 Sell 100%", callback_data=f"sell:{token_address}:100"),
            ],
            [InlineKeyboardButton("📊 DexScreener", url=dex_chart_url(token_address))],
        ]
    )


def build_tp_menu_keyboard(token_address: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(TP_PRESETS), 2):
        rows.append(
            [
                InlineKeyboardButton(f"🎯 {m}x", callback_data=f"tpset:{token_address}:{m}")
                for m in TP_PRESETS[i : i + 2]
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=f"tpcustom:{token_address}")])
    rows.append([InlineKeyboardButton("❌ Clear TP", callback_data=f"tpset:{token_address}:0")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"backpos:{token_address}")])
    return InlineKeyboardMarkup(rows)


def build_sl_menu_keyboard(token_address: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(SL_PRESETS), 2):
        rows.append(
            [
                InlineKeyboardButton(f"🛑 -{p}%", callback_data=f"slset:{token_address}:{p}")
                for p in SL_PRESETS[i : i + 2]
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=f"slcustom:{token_address}")])
    rows.append([InlineKeyboardButton("❌ Clear SL", callback_data=f"slset:{token_address}:0")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"backpos:{token_address}")])
    return InlineKeyboardMarkup(rows)


async def render_position_card(user_id: str, token_address: str, live: bool = True):
    """Builds the (text, keyboard) pair for a position card. Returns None if
    the position no longer exists. `live=False` reuses the last-known
    price/mcap from the DB instead of hitting DexScreener again - used for
    quick menu navigation where a fresh quote isn't necessary."""
    position = await db.get_position(user_id, token_address)
    if not position:
        return None

    entry_price = float(position["entry_price"])
    entry_mcap = float(position.get("entry_market_cap") or 0)
    amount = float(position["amount"])
    invested = float(position["invested_amount"])

    current_price = float(position.get("current_price") or entry_price)
    current_mcap = float(position.get("current_market_cap") or entry_mcap)

    if live:
        token_data = await market.get_token_data(token_address)
        if token_data and token_data["price_usd"] > 0:
            current_price = token_data["price_usd"]
            current_mcap = float(token_data.get("market_cap") or 0)

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
    return text, build_position_keyboard(token_address)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    balance_line = await balance_block(user)
    await update.message.reply_text(
        "⛵ <b>PAPERBOAT</b> — Demo Trading Bot\n"
        f"{DIVIDER}\n"
        "Practice trading Solana tokens with real live market data and zero "
        "risk. No wallet, no private keys, no real funds — every trade here "
        "is simulated against a demo balance.\n\n"
        f"{balance_line}\n\n"
        "<b>How it works</b>\n"
        "📩 Send any Solana token contract address to pull up its live "
        "market cap, volume and 1h change, then buy straight from the chat. "
        "Set a Take Profit / Stop Loss on any open position and PaperBoat "
        "will auto-close it the moment your target is hit.\n\n"
        "<b>Commands</b>\n"
        "/balance — check your demo balance\n"
        "/portfolio — view your open positions\n"
        "/history — view your recent trades\n"
        "/start — show this menu again\n\n"
        "🛠️ <i>Built by @supremeesol</i>",
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
        f"📊 Market Cap: <b>{fmt_compact(token_data['market_cap'])}</b>\n"
        f"📈 Volume (24h): {fmt_compact(token_data['volume_24h'])}\n"
        f"📉 Change (1h): {fmt_pct(token_data['price_change_1h'])}\n"
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

    # Re-read the position so an add-to-existing-position buy still reflects
    # any TP/SL that was already set on it.
    position_row = await db.get_position(user["id"], token_address)
    tp_price = position_row.get("tp_price") if position_row else None
    sl_price = position_row.get("sl_price") if position_row else None

    text = format_position_card(
        name=result["token_name"],
        symbol=result["token_symbol"],
        token_address=token_address,
        entry_price=result["entry_price"],
        entry_market_cap=result["entry_market_cap"],
        current_market_cap=result["entry_market_cap"],
        tokens=result["tokens_bought"],
        invested=result["usd_amount"],
        pnl=0.0,
        pnl_pct=0.0,
        tp_price=tp_price,
        sl_price=sl_price,
    )
    await target.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_position_keyboard(token_address),
        disable_web_page_preview=True,
    )


async def process_tp_menu(query, token_address: str) -> None:
    try:
        await query.edit_message_text(
            "🎯 <b>Set Take Profit</b>\n"
            f"{DIVIDER}\n"
            "Choose a target as a multiple of your entry market cap "
            "(e.g. 2x auto-sells once the cap doubles).",
            parse_mode=ParseMode.HTML,
            reply_markup=build_tp_menu_keyboard(token_address),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await query.answer()


async def process_sl_menu(query, token_address: str) -> None:
    try:
        await query.edit_message_text(
            "🛑 <b>Set Stop Loss</b>\n"
            f"{DIVIDER}\n"
            "Choose a target as a percent below your entry market cap "
            "(e.g. -20% auto-sells if the cap drops that far).",
            parse_mode=ParseMode.HTML,
            reply_markup=build_sl_menu_keyboard(token_address),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await query.answer()


async def apply_tp(user_id: str, token_address: str, multiple: float):
    position = await db.get_position(user_id, token_address)
    if not position:
        return None
    entry_price = float(position["entry_price"])
    tp_price = entry_price * multiple if multiple > 0 else None
    sl_price = position.get("sl_price")
    await db.set_position_tp_sl(position["id"], tp_price, sl_price)
    return True


async def apply_sl(user_id: str, token_address: str, percent: float):
    position = await db.get_position(user_id, token_address)
    if not position:
        return None
    entry_price = float(position["entry_price"])
    sl_price = entry_price * (1 - percent / 100) if percent > 0 else None
    tp_price = position.get("tp_price")
    await db.set_position_tp_sl(position["id"], tp_price, sl_price)
    return True


async def process_tp_set(query, token_address: str, multiple: float) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    applied = await apply_tp(user["id"], token_address, multiple)
    if applied is None:
        await query.answer("This position is no longer open.", show_alert=True)
        return

    await show_position_card(query, user["id"], token_address, live=False)
    await query.answer("Take Profit set" if multiple > 0 else "Take Profit cleared")


async def process_sl_set(query, token_address: str, percent: float) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    applied = await apply_sl(user["id"], token_address, percent)
    if applied is None:
        await query.answer("This position is no longer open.", show_alert=True)
        return

    await show_position_card(query, user["id"], token_address, live=False)
    await query.answer("Stop Loss set" if percent > 0 else "Stop Loss cleared")


async def show_position_card(query, user_id: str, token_address: str, live: bool = True) -> None:
    rendered = await render_position_card(user_id, token_address, live=live)
    if not rendered:
        await query.answer("This position has been closed.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return

    text, keyboard = rendered
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


async def process_refresh(query, token_address: str) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)
    await show_position_card(query, user["id"], token_address, live=True)
    await query.answer("Updated")


async def process_back_to_position(query, token_address: str) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)
    await show_position_card(query, user["id"], token_address, live=False)
    await query.answer()


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
        f"Entry MCap: {fmt_compact(result['entry_market_cap'])}\n"
        f"Exit MCap: {fmt_compact(result['exit_market_cap'])}\n"
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

    pending_tp = context.user_data.get("awaiting_custom_tp")
    if pending_tp:
        context.user_data.pop("awaiting_custom_tp", None)
        try:
            multiple = float(text)
            if multiple <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid multiple, e.g. 4 for 4x")
            return
        user = await get_user(update)
        applied = await apply_tp(user["id"], pending_tp, multiple)
        if applied is None:
            await update.message.reply_text("That position is no longer open.")
            return
        rendered = await render_position_card(user["id"], pending_tp, live=False)
        if rendered:
            text_out, keyboard = rendered
            await update.message.reply_text(
                text_out, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True
            )
        return

    pending_sl = context.user_data.get("awaiting_custom_sl")
    if pending_sl:
        context.user_data.pop("awaiting_custom_sl", None)
        try:
            percent = float(text)
            if percent <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please send a valid percent, e.g. 25 for -25%")
            return
        user = await get_user(update)
        applied = await apply_sl(user["id"], pending_sl, percent)
        if applied is None:
            await update.message.reply_text("That position is no longer open.")
            return
        rendered = await render_position_card(user["id"], pending_sl, live=False)
        if rendered:
            text_out, keyboard = rendered
            await update.message.reply_text(
                text_out, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True
            )
        return

    if SOLANA_CA_REGEX.match(text):
        await show_token_info(update, text)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""

    # Each branch answers the callback query exactly once - Telegram rejects
    # a second answer() call on the same query, so several branches handle
    # their own (with a toast/alert) instead of us answering here up front.
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
        await process_refresh(query, parts[1])
    elif action == "refreshtoken" and len(parts) == 2:
        await process_refresh_token(query, parts[1])
    elif action == "tpmenu" and len(parts) == 2:
        await process_tp_menu(query, parts[1])
    elif action == "slmenu" and len(parts) == 2:
        await process_sl_menu(query, parts[1])
    elif action == "tpset" and len(parts) == 3:
        await process_tp_set(query, parts[1], float(parts[2]))
    elif action == "slset" and len(parts) == 3:
        await process_sl_set(query, parts[1], float(parts[2]))
    elif action == "tpcustom" and len(parts) == 2:
        await query.answer()
        context.user_data["awaiting_custom_tp"] = parts[1]
        await query.message.reply_text("Send your TP as a multiple of entry market cap, e.g. 4 for 4x")
    elif action == "slcustom" and len(parts) == 2:
        await query.answer()
        context.user_data["awaiting_custom_sl"] = parts[1]
        await query.message.reply_text("Send your SL as a percent below entry market cap, e.g. 25 for -25%")
    elif action == "backpos" and len(parts) == 2:
        await process_back_to_position(query, parts[1])
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
            f"Entry MCap: {fmt_compact(result['entry_market_cap'])}\n"
            f"Exit MCap: {fmt_compact(result['exit_market_cap'])}\n"
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
    """Background task: refresh open position prices/mcap/PNL every 30s,
    then check for any take-profit/stop-loss targets that were hit."""
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
    logger.info("PaperBoat bot started and polling for updates.")

    try:
        yield
    finally:
        price_task.cancel()
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()
        logger.info("PaperBoat bot stopped.")


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return {"status": "running", "bot": "PaperBoat - Demo Trading Bot"}
