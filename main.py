"""Telegram bot + FastAPI server for PaperBoat - Demo Trading Bot.

No wallets, no private keys, no real transactions - everything here trades
against a virtual USDC balance stored in Supabase. Solana, BSC, and
Robinhood Chain, auto-detected from the shape (and, for the two EVM chains,
a live DexScreener lookup) of whatever contract address the user sends.
"""

import asyncio
import html
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

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
import pnl_card
import trading
from config import CHAINS, DEFAULT_CHAIN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Base58, 32-44 chars - matches typical Solana addresses.
SOLANA_CA_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
# 0x + 40 hex chars - standard EVM address format, shared by every EVM chain
# this bot supports (BSC, Robinhood Chain), so it can't tell them apart on
# its own - see resolve_chain() below.
EVM_CA_REGEX = re.compile(r"^0x[a-fA-F0-9]{40}$")


async def resolve_chain(token_address: str) -> tuple[Optional[str], Optional[dict]]:
    """Auto-detects which chain a pasted contract address belongs to and
    returns (chain, token_data) - or (None, None) if it doesn't look like a
    supported address, or isn't listed on any candidate chain.

    Solana and EVM addresses have different shapes and never collide, but
    BSC and Robinhood Chain are both EVM chains sharing the same 0x-hex
    format, so the shape alone can't disambiguate them. Instead, every
    "evm"-kind chain is queried on DexScreener and whichever one actually
    has this token listed (i.e. returns a real price) wins.
    """
    if SOLANA_CA_REGEX.match(token_address):
        candidates = [c for c, cfg in CHAINS.items() if cfg["address_kind"] == "solana"]
    elif EVM_CA_REGEX.match(token_address):
        candidates = [c for c, cfg in CHAINS.items() if cfg["address_kind"] == "evm"]
    else:
        return None, None

    for chain in candidates:
        token_data = await market.get_token_data(token_address, chain)
        if token_data and token_data["price_usd"] > 0:
            return chain, token_data
    return None, None

# Take-profit presets, expressed as a multiple of entry market cap (e.g. "2" = 2x).
TP_PRESETS = [2, 3, 5, 10]
# Stop-loss presets, expressed as percent below entry market cap.
SL_PRESETS = [10, 20, 30, 50]


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usd(value) -> str:
    return f"${float(value or 0):,.2f}"


def chain_label(chain: str) -> str:
    return CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])["label"]


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


def pnl_symbol(value) -> str:
    v = float(value or 0)
    return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"


def _seconds_since(iso_timestamp) -> float:
    """Best-effort parse of a Supabase timestamptz string into elapsed
    seconds. Falls back to 0 if the timestamp is missing or unparsable so a
    formatting hiccup never blocks sending the PNL card."""
    if not iso_timestamp:
        return 0.0
    try:
        ts = str(iso_timestamp).replace("Z", "+00:00")
        entry_dt = datetime.fromisoformat(ts)
        if entry_dt.tzinfo is None:
            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - entry_dt).total_seconds())
    except (ValueError, TypeError):
        return 0.0


async def send_pnl_card(bot, chat_id: int, result: dict, reason: str) -> None:
    """Renders a PaperBoat PNL card for a just-closed position and sends it
    to the user via sendPhoto, cleaning up the temp file afterward. Any
    failure here is logged and swallowed so a card issue never blocks the
    rest of the trade flow. `reason` is accepted for logging/future use but
    is not currently rendered on the card itself."""
    trade = {
        "token_name": result["token_name"],
        "token_symbol": result["token_symbol"],
        "entry_market_cap": result["entry_market_cap"],
        "exit_market_cap": result["exit_market_cap"],
        "invested": result["invested"],
        "final_value": result["final_value"],
        "pnl": result["pnl"],
        "pnl_pct": result["pnl_pct"],
        "duration_seconds": _seconds_since(result.get("entry_time")),
        "logo_url": result.get("logo_url", ""),
    }

    path = None
    try:
        path = await asyncio.to_thread(pnl_card.generate_pnl_card, trade)
        with open(path, "rb") as photo:
            await bot.send_photo(chat_id=chat_id, photo=photo)
    except Exception:
        logger.exception("Failed to generate/send PNL card for chat_id=%s", chat_id)
    finally:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                logger.warning("Could not delete temp PNL card file %s", path)


def token_chart_url(token_address: str, chain: str, fallback: str = "") -> str:
    """Best-effort live chart link. Prefers an exact pair/pool URL from the
    API response when available; falls back to the chain's generic explorer
    page template."""
    if fallback:
        return fallback
    cfg = CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])
    return cfg["explorer_url"].format(address=token_address)


async def get_user(update: Update) -> dict:
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)


async def balance_block(user: dict) -> str:
    """Renders the demo balance. It's held in USDC (pegged 1:1 to USD) and
    is chain-agnostic, shared across every chain the user trades on."""
    usd_balance = float(user["balance"])
    return f"💰 Balance: <b>{fmt_usd(usd_balance)} USDC</b>"


def tp_sl_line(entry_price: float, entry_mcap: float, tp_price, sl_price) -> str:
    """TP/SL are stored internally as prices (for precise trigger checks)
    but displayed as market cap, in line with the rest of the UI."""
    if not entry_price:
        return "🎯 TP/SL: Not set"

    parts = []
    if tp_price:
        mult = float(tp_price) / entry_price
        parts.append(f"🎯 TP: {fmt_compact(entry_mcap * mult)} ({mult:.1f}x)")
    if sl_price:
        ratio = float(sl_price) / entry_price
        parts.append(f"🛑 SL: {fmt_compact(entry_mcap * ratio)} ({(ratio - 1) * 100:+.1f}%)")
    if not parts:
        return "🎯 TP/SL: Not set"
    return "  |  ".join(parts)


def format_position_card(
    *,
    name: str,
    symbol: str,
    token_address: str,
    chain: str,
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
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    chart_url = token_chart_url(token_address, chain)
    entry_mcap = float(entry_market_cap or 0)

    return (
        f"📌 <b>{html.escape(name)}</b> ({html.escape(symbol)}) · {html.escape(chain_label(chain))}\n"
        f"Entry: <b>{fmt_compact(entry_mcap)}</b>\n"
        f"Current: <b>{fmt_compact(current_market_cap)}</b>\n"
        f"💵 Invested: {fmt_usd(invested)}\n\n"
        f"{tp_sl_line(entry_price, entry_mcap, tp_price, sl_price)}\n\n"
        f"{pnl_symbol(pnl)} <b>PNL: {fmt_usd(pnl)} ({pnl_pct:+.2f}%)</b>\n\n"
        f"📈 <a href=\"{chart_url}\">View Live Chart</a>\n"
        f"<code>{html.escape(token_address)}</code>\n"
        f"<i>Updated {timestamp}</i>"
    )


def build_position_keyboard(token_address: str, chain: str) -> InlineKeyboardMarkup:
    """Buy (DCA) buttons sit above the sell buttons - buying more of a
    position the user already holds is at least as common an action as
    selling it, and putting it first keeps DCA one tap away instead of
    burying it back on the token-info card."""
    presets = CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])["buy_presets"]
    buy_rows = [
        [
            InlineKeyboardButton(
                f"➕ Buy ${amt:g}", callback_data=f"buy:{chain}:{token_address}:{amt}"
            )
            for amt in presets[i : i + 2]
        ]
        for i in range(0, len(presets), 2)
    ]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{chain}:{token_address}")],
            *buy_rows,
            [InlineKeyboardButton("✏️ Custom Buy", callback_data=f"buycustom:{chain}:{token_address}")],
            [
                InlineKeyboardButton("🎯 Set TP", callback_data=f"tpmenu:{chain}:{token_address}"),
                InlineKeyboardButton("🛑 Set SL", callback_data=f"slmenu:{chain}:{token_address}"),
            ],
            [
                InlineKeyboardButton("💸 Sell 50%", callback_data=f"sell:{chain}:{token_address}:50"),
                InlineKeyboardButton("💯 Sell 100%", callback_data=f"sell:{chain}:{token_address}:100"),
            ],
            [InlineKeyboardButton("📊 Chart", url=token_chart_url(token_address, chain))],
        ]
    )


def build_tp_menu_keyboard(token_address: str, chain: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(TP_PRESETS), 2):
        rows.append(
            [
                InlineKeyboardButton(f"🎯 {m}x", callback_data=f"tpset:{chain}:{token_address}:{m}")
                for m in TP_PRESETS[i : i + 2]
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=f"tpcustom:{chain}:{token_address}")])
    rows.append([InlineKeyboardButton("❌ Clear TP", callback_data=f"tpset:{chain}:{token_address}:0")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"backpos:{chain}:{token_address}")])
    return InlineKeyboardMarkup(rows)


def build_sl_menu_keyboard(token_address: str, chain: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(SL_PRESETS), 2):
        rows.append(
            [
                InlineKeyboardButton(f"🛑 -{p}%", callback_data=f"slset:{chain}:{token_address}:{p}")
                for p in SL_PRESETS[i : i + 2]
            ]
        )
    rows.append([InlineKeyboardButton("✏️ Custom", callback_data=f"slcustom:{chain}:{token_address}")])
    rows.append([InlineKeyboardButton("❌ Clear SL", callback_data=f"slset:{chain}:{token_address}:0")])
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"backpos:{chain}:{token_address}")])
    return InlineKeyboardMarkup(rows)


async def render_position_card(user_id: str, token_address: str, chain: str, live: bool = True):
    """Builds the (text, keyboard) pair for a position card. Returns None if
    the position no longer exists. `live=False` reuses the last-known
    price/mcap from the DB instead of hitting the market data API again - used for
    quick menu navigation where a fresh quote isn't necessary."""
    position = await db.get_position(user_id, token_address, chain)
    if not position:
        return None

    entry_price = float(position["entry_price"])
    entry_mcap = float(position.get("entry_market_cap") or 0)
    amount = float(position["amount"])
    invested = float(position["invested_amount"])

    current_price = float(position.get("current_price") or entry_price)
    current_mcap = float(position.get("current_market_cap") or entry_mcap)

    if live:
        token_data = await market.get_token_data(token_address, chain)
        if token_data and token_data["price_usd"] > 0:
            current_price = token_data["price_usd"]
            current_mcap = float(token_data.get("market_cap") or 0)

    pnl = (amount * current_price) - invested
    pnl_pct = (pnl / invested * 100) if invested else 0.0

    text = format_position_card(
        name=position["token_name"],
        symbol=position["token_symbol"],
        token_address=token_address,
        chain=chain,
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
    return text, build_position_keyboard(token_address, chain)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    balance_line = await balance_block(user)
    await update.message.reply_text(
        "🚤 <b>PAPERBOAT</b> — Demo Trading Bot\n"
        "Practice trading with real market data and zero risk.\n"
        "No wallet. No private keys. No real funds.\n"
        "All trades are simulated with a demo USDC balance.\n\n"
        "⛓ Chains: Solana, BSC & Robinhood Chain (auto-detected)\n\n"
        f"{balance_line}\n\n"
        "📩 Send a token contract address to view live data and trade - "
        "the chain is detected automatically from the address.\n"
        "➕ Already holding a token? Tap Buy again on its position card to "
        "add to it - your average entry updates automatically.\n"
        "🎯 Set Take Profit / Stop Loss and let PaperBoat manage your "
        "positions automatically.\n\n"
        "<b>Commands:</b>\n"
        "/balance — Check balance\n"
        "/positions — Open positions\n"
        "/history — Trade history\n"
        "/start — Show menu\n\n"
        "🛠 Built by @supremeesol",
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


async def positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    overview = (
        "📊 <b>POSITIONS OVERVIEW</b>\n\n"
        f"Open Positions: <b>{len(positions)}</b>\n"
        f"Total Invested: {fmt_usd(total_invested)}\n"
        f"Current Value: {fmt_usd(total_value)}\n\n"
        f"Total PNL: <b>{pnl_symbol(total_pnl)} {fmt_usd(total_pnl)} ({total_pnl_pct:+.2f}%)</b>"
    )
    await update.message.reply_text(overview, parse_mode=ParseMode.HTML)

    for pos in positions:
        chain = pos.get("chain") or DEFAULT_CHAIN
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
            chain=chain,
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
            reply_markup=build_position_keyboard(pos["token_address"], chain),
            disable_web_page_preview=True,
        )


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    trades = await db.get_trades(user["id"], limit=10)

    if not trades:
        await update.message.reply_text("No trades yet. Send a token contract address to get started!")
        return

    lines = ["📜 <b>RECENT TRADES</b>", ""]
    for t in trades:
        side = t["trade_type"]
        side_emoji = "🟢" if side == "BUY" else "🔴"
        pnl_str = f" | PNL: {fmt_usd(t['pnl'])}" if side == "SELL" else ""
        chain_tag = chain_label(t.get("chain") or DEFAULT_CHAIN)
        lines.append(
            f"{side_emoji} <b>{side}</b> {html.escape(t['token_symbol'])} ({chain_tag}) "
            f"— {fmt_usd(t['total_value'])}{pnl_str}"
        )

    total_pnl = await db.get_total_realized_pnl(user["id"])
    lines.append("")
    lines.append(f"<b>Overall Wallet PNL:</b> {pnl_symbol(total_pnl)} {fmt_usd(total_pnl)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Token info + trading flow
# ---------------------------------------------------------------------------

def build_token_info_text(token_data: dict, token_address: str, chain: str) -> str:
    chart_url = token_chart_url(token_address, chain, token_data.get("dex_url", ""))
    pct_1h = float(token_data.get("price_change_1h") or 0)
    change_emoji = "📈" if pct_1h >= 0 else "📉"
    return (
        f"⚡ <b>{html.escape(token_data['name'])}</b> ({html.escape(token_data['symbol'])}) "
        f"· {html.escape(chain_label(chain))}\n\n"
        f"💰 MC: <b>{fmt_compact(token_data['market_cap'])}</b>\n"
        f"📊 24h Vol: {fmt_compact(token_data['volume_24h'])}\n"
        f"{change_emoji} 1h: {pct_1h:+.2f}%\n\n"
        f"📄 CA: <code>{html.escape(token_address)}</code>\n\n"
        f"📊 <a href=\"{chart_url}\">View Live Chart</a>"
    )


def build_token_info_keyboard(token_address: str, chain: str, dex_url: str = "") -> InlineKeyboardMarkup:
    presets = CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])["buy_presets"]
    keyboard = [
        [
            InlineKeyboardButton(f"BUY ${amt:g}", callback_data=f"buy:{chain}:{token_address}:{amt}")
            for amt in presets[i : i + 2]
        ]
        for i in range(0, len(presets), 2)
    ]
    keyboard.append(
        [InlineKeyboardButton("✏️ Custom Amount", callback_data=f"buycustom:{chain}:{token_address}")]
    )
    keyboard.append(
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"refreshtoken:{chain}:{token_address}"),
            InlineKeyboardButton("📊 Chart", url=token_chart_url(token_address, chain, dex_url)),
        ]
    )
    return InlineKeyboardMarkup(keyboard)


async def show_token_info(target, token_address: str, chain: str, token_data: Optional[dict] = None) -> None:
    if token_data is None:
        token_data = await market.get_token_data(token_address, chain)
    if not token_data:
        await target.reply_text(
            f"⚠️ Couldn't find that token on {chain_label(chain)}. Double check the contract address."
        )
        return

    text = build_token_info_text(token_data, token_address, chain)
    keyboard = build_token_info_keyboard(token_address, chain, token_data.get("dex_url", ""))

    await target.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def process_refresh_token(query, chain: str, token_address: str) -> None:
    token_data = await market.get_token_data(token_address, chain)
    if not token_data:
        await query.answer("Couldn't fetch live data right now. Try again shortly.", show_alert=True)
        return

    text = build_token_info_text(token_data, token_address, chain)
    keyboard = build_token_info_keyboard(token_address, chain, token_data.get("dex_url", ""))

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


async def process_buy(update: Update, target, token_address: str, usdc_amount: float, chain: str,
                       delete_chat_id=None, delete_message_id=None) -> None:
    user = await get_user(update)
    try:
        result = await trading.execute_buy(user, token_address, usdc_amount, chain)
    except trading.TradingError as exc:
        await target.reply_text(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Unexpected error during buy")
        await target.reply_text("⚠️ Something went wrong processing that trade. Please try again.")
        return

    # Re-read the position so an add-to-existing-position buy still reflects
    # any TP/SL that was already set on it.
    position_row = await db.get_position(user["id"], token_address, chain)
    tp_price = position_row.get("tp_price") if position_row else None
    sl_price = position_row.get("sl_price") if position_row else None

    text = format_position_card(
        name=result["token_name"],
        symbol=result["token_symbol"],
        token_address=token_address,
        chain=chain,
        entry_price=result["avg_entry_price"],
        entry_market_cap=result["avg_entry_market_cap"],
        current_market_cap=result["entry_market_cap"],
        tokens=result["total_amount"],
        invested=result["total_invested"],
        pnl=0.0,
        pnl_pct=0.0,
        tp_price=tp_price,
        sl_price=sl_price,
    )
    await target.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=build_position_keyboard(token_address, chain),
        disable_web_page_preview=True,
    )

    # Clean up the original "Buy" prompt (token card with buy buttons, or the
    # "send a custom amount" message) now that the trade confirmation is shown.
    if delete_chat_id is not None and delete_message_id is not None:
        try:
            await telegram_app.bot.delete_message(chat_id=delete_chat_id, message_id=delete_message_id)
        except Exception:
            logger.debug(
                "Could not delete origin buy message %s in chat %s",
                delete_message_id, delete_chat_id, exc_info=True,
            )


async def process_tp_menu(query, chain: str, token_address: str) -> None:
    try:
        await query.edit_message_text(
            "🎯 <b>Set Take Profit</b>\n\n"
            "Choose a target as a multiple of your entry market cap "
            "(e.g. 2x auto-sells once the cap doubles).",
            parse_mode=ParseMode.HTML,
            reply_markup=build_tp_menu_keyboard(token_address, chain),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await query.answer()


async def process_sl_menu(query, chain: str, token_address: str) -> None:
    try:
        await query.edit_message_text(
            "🛑 <b>Set Stop Loss</b>\n\n"
            "Choose a target as a percent below your entry market cap "
            "(e.g. -20% auto-sells if the cap drops that far).",
            parse_mode=ParseMode.HTML,
            reply_markup=build_sl_menu_keyboard(token_address, chain),
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise
    await query.answer()


async def apply_tp(user_id: str, token_address: str, chain: str, multiple: float):
    position = await db.get_position(user_id, token_address, chain)
    if not position:
        return None
    entry_price = float(position["entry_price"])
    tp_price = entry_price * multiple if multiple > 0 else None
    sl_price = position.get("sl_price")
    await db.set_position_tp_sl(position["id"], tp_price, sl_price)
    return True


async def apply_sl(user_id: str, token_address: str, chain: str, percent: float):
    position = await db.get_position(user_id, token_address, chain)
    if not position:
        return None
    entry_price = float(position["entry_price"])
    sl_price = entry_price * (1 - percent / 100) if percent > 0 else None
    tp_price = position.get("tp_price")
    await db.set_position_tp_sl(position["id"], tp_price, sl_price)
    return True


async def process_tp_set(query, chain: str, token_address: str, multiple: float) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    applied = await apply_tp(user["id"], token_address, chain, multiple)
    if applied is None:
        await query.answer("This position is no longer open.", show_alert=True)
        return

    await show_position_card(query, user["id"], token_address, chain, live=False)
    await query.answer("Take Profit set" if multiple > 0 else "Take Profit cleared")


async def process_sl_set(query, chain: str, token_address: str, percent: float) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)

    applied = await apply_sl(user["id"], token_address, chain, percent)
    if applied is None:
        await query.answer("This position is no longer open.", show_alert=True)
        return

    await show_position_card(query, user["id"], token_address, chain, live=False)
    await query.answer("Stop Loss set" if percent > 0 else "Stop Loss cleared")


async def show_position_card(query, user_id: str, token_address: str, chain: str, live: bool = True) -> None:
    rendered = await render_position_card(user_id, token_address, chain, live=live)
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


async def process_refresh(query, chain: str, token_address: str) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)
    await show_position_card(query, user["id"], token_address, chain, live=True)
    await query.answer("Updated")


async def process_back_to_position(query, chain: str, token_address: str) -> None:
    tg_user = query.from_user
    user = await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)
    await show_position_card(query, user["id"], token_address, chain, live=False)
    await query.answer()


async def process_sell(
    update: Update, target, token_address: str, percent: float, chain: str, loading_query=None
) -> None:
    """Executes a sell. When triggered from a Sell button (`loading_query`
    set), the originating message is swapped to a "Selling..." placeholder
    first so the group sees immediate feedback while the trade + PNL card
    render, instead of the buttons just sitting there looking unresponsive."""
    user = await get_user(update)

    if loading_query is not None:
        try:
            placeholder = "🖼️ Sending PNL Card..." if percent >= 100 else f"⏳ Selling {percent:g}%..."
            await loading_query.edit_message_text(placeholder)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("Could not show sell-loading placeholder", exc_info=True)

    async def _fail(message: str) -> None:
        if loading_query is not None:
            try:
                await loading_query.edit_message_text(message)
                return
            except BadRequest:
                pass
        await target.reply_text(message)

    try:
        result = await trading.execute_sell(user, token_address, percent, chain)
    except trading.TradingError as exc:
        await _fail(f"⚠️ {exc}")
        return
    except Exception:
        logger.exception("Unexpected error during sell")
        await _fail("⚠️ Something went wrong processing that trade. Please try again.")
        return

    if result.get("position_closed"):
        # Full close: the PNL card image is the confirmation. Drop it in the
        # chat, then remove the loading placeholder now that it's served its purpose.
        await send_pnl_card(telegram_app.bot, update.effective_chat.id, result, reason="manual")
        if loading_query is not None:
            try:
                await telegram_app.bot.delete_message(
                    chat_id=loading_query.message.chat_id,
                    message_id=loading_query.message.message_id,
                )
            except Exception:
                logger.debug("Could not delete sell-loading placeholder", exc_info=True)
        return

    text = (
        "💸 <b>SELL EXECUTED</b>\n\n"
        f"{html.escape(result['token_name'])} ({html.escape(result['token_symbol'])}) · {chain_label(chain)}\n\n"
        f"Entry MCap: {fmt_compact(result['entry_market_cap'])}\n"
        f"Exit MCap: {fmt_compact(result['exit_market_cap'])}\n"
        f"Received: {fmt_usd(result['proceeds'])}\n\n"
        f"PNL: <b>{pnl_symbol(result['pnl'])} {fmt_usd(result['pnl'])} "
        f"({result['pnl_pct']:+.2f}%)</b>\n\n"
        f"💰 Balance: {fmt_usd(result['new_balance'])} USDC"
    )
    if loading_query is not None:
        try:
            await loading_query.edit_message_text(text, parse_mode=ParseMode.HTML)
            return
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
            return
    await target.reply_text(text, parse_mode=ParseMode.HTML)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()

    pending_ca = context.user_data.get("awaiting_custom_buy")
    if pending_ca:
        context.user_data.pop("awaiting_custom_buy", None)
        origin = context.user_data.pop("awaiting_custom_buy_origin", None)
        try:
            amount = float(text)
        except ValueError:
            await update.message.reply_text("Please send a valid number, e.g. 0.75")
            return
        await process_buy(
            update, update.message, pending_ca["token_address"], amount, pending_ca["chain"],
            delete_chat_id=origin.get("chat_id") if origin else None,
            delete_message_id=origin.get("message_id") if origin else None,
        )
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
        applied = await apply_tp(user["id"], pending_tp["token_address"], pending_tp["chain"], multiple)
        if applied is None:
            await update.message.reply_text("That position is no longer open.")
            return
        rendered = await render_position_card(user["id"], pending_tp["token_address"], pending_tp["chain"], live=False)
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
        applied = await apply_sl(user["id"], pending_sl["token_address"], pending_sl["chain"], percent)
        if applied is None:
            await update.message.reply_text("That position is no longer open.")
            return
        rendered = await render_position_card(user["id"], pending_sl["token_address"], pending_sl["chain"], live=False)
        if rendered:
            text_out, keyboard = rendered
            await update.message.reply_text(
                text_out, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True
            )
        return

    if SOLANA_CA_REGEX.match(text) or EVM_CA_REGEX.match(text):
        chain, token_data = await resolve_chain(text)
        if chain:
            await show_token_info(update.message, text, chain, token_data)
        else:
            await update.message.reply_text(
                "⚠️ Couldn't find that token on Solana, BSC, or Robinhood Chain. "
                "Double check the contract address."
            )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""

    # Each branch answers the callback query exactly once - Telegram rejects
    # a second answer() call on the same query, so several branches handle
    # their own (with a toast/alert) instead of us answering here up front.
    if action == "buy" and len(parts) == 4:
        await query.answer()
        chain, token_address, amount_str = parts[1], parts[2], parts[3]
        await process_buy(
            update, query.message, token_address, float(amount_str), chain,
            delete_chat_id=query.message.chat_id,
            delete_message_id=query.message.message_id,
        )
    elif action == "buycustom" and len(parts) == 3:
        await query.answer()
        chain, token_address = parts[1], parts[2]
        context.user_data["awaiting_custom_buy"] = {"chain": chain, "token_address": token_address}
        context.user_data["awaiting_custom_buy_origin"] = {
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
        }
        await query.message.reply_text("Send the amount of USDC you'd like to spend, e.g. 100")
    elif action == "sell" and len(parts) == 4:
        await query.answer()
        chain, token_address, percent_str = parts[1], parts[2], parts[3]
        await process_sell(
            update, query.message, token_address, float(percent_str), chain, loading_query=query
        )
    elif action == "refresh" and len(parts) == 3:
        await process_refresh(query, parts[1], parts[2])
    elif action == "refreshtoken" and len(parts) == 3:
        await process_refresh_token(query, parts[1], parts[2])
    elif action == "tpmenu" and len(parts) == 3:
        await process_tp_menu(query, parts[1], parts[2])
    elif action == "slmenu" and len(parts) == 3:
        await process_sl_menu(query, parts[1], parts[2])
    elif action == "tpset" and len(parts) == 4:
        await process_tp_set(query, parts[1], parts[2], float(parts[3]))
    elif action == "slset" and len(parts) == 4:
        await process_sl_set(query, parts[1], parts[2], float(parts[3]))
    elif action == "tpcustom" and len(parts) == 3:
        await query.answer()
        context.user_data["awaiting_custom_tp"] = {"chain": parts[1], "token_address": parts[2]}
        await query.message.reply_text("Send your TP as a multiple of entry market cap, e.g. 4 for 4x")
    elif action == "slcustom" and len(parts) == 3:
        await query.answer()
        context.user_data["awaiting_custom_sl"] = {"chain": parts[1], "token_address": parts[2]}
        await query.message.reply_text("Send your SL as a percent below entry market cap, e.g. 25 for -25%")
    elif action == "backpos" and len(parts) == 3:
        await process_back_to_position(query, parts[1], parts[2])
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

        chain = pos.get("chain") or DEFAULT_CHAIN
        try:
            result = await trading.execute_sell(user, pos["token_address"], 100, chain)
        except trading.TradingError:
            continue
        except Exception:
            logger.exception("Auto TP/SL sell failed for position %s", pos.get("id"))
            continue

        await send_pnl_card(
            telegram_app.bot,
            user["telegram_id"],
            result,
            reason="tp" if hit_tp else "sl",
        )


# ---------------------------------------------------------------------------
# Telegram application setup
# ---------------------------------------------------------------------------

telegram_app: Application = Application.builder().token(config.TELEGRAM_TOKEN).build()

telegram_app.add_handler(CommandHandler("start", start_handler))
telegram_app.add_handler(CommandHandler("balance", balance_handler))
telegram_app.add_handler(CommandHandler("positions", positions_handler))
telegram_app.add_handler(CommandHandler("history", history_handler))
telegram_app.add_handler(CallbackQueryHandler(callback_handler))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

BOT_COMMANDS = [
    BotCommand("start", "Welcome & how it works"),
    BotCommand("balance", "Check your demo balance"),
    BotCommand("positions", "View your open positions"),
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
