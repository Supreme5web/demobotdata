"""Telegram bot + FastAPI server for the demo (paper) trading bot.

No wallets, no private keys, no real transactions - everything here trades
against a virtual USD balance stored in Supabase.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
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


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_usd(value) -> str:
    return f"${float(value or 0):,.2f}"


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


async def get_user(update: Update) -> dict:
    tg_user = update.effective_user
    return await db.get_or_create_user(tg_user.id, tg_user.username or tg_user.first_name)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    await update.message.reply_text(
        "🚀 *Welcome to TradeEmDemo*\n\n"
        "Paper trading for Solana tokens - no wallet, no private keys, "
        "no real transactions. Everything here is simulated.\n\n"
        f"💰 *Demo Balance:* {fmt_usd(user['balance'])}\n\n"
        "Send me a Solana token contract address to see live data and trade it, "
        "or use:\n"
        "/balance — check your demo balance\n"
        "/portfolio — view your open positions\n"
        "/history — view your recent trades",
        parse_mode=ParseMode.MARKDOWN,
    )


async def balance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    await update.message.reply_text(
        f"💰 *Demo Balance:* {fmt_usd(user['balance'])}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def portfolio_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    positions = await db.get_positions(user["id"])

    if not positions:
        await update.message.reply_text(
            "You have no open positions yet. Send a token contract address to start trading!"
        )
        return

    for pos in positions:
        amount = float(pos["amount"])
        entry = float(pos["entry_price"])
        current = float(pos["current_price"] or entry)
        invested = float(pos["invested_amount"])
        current_value = amount * current
        pnl = current_value - invested
        pnl_pct = (pnl / invested * 100) if invested else 0.0
        emoji = "🟢" if pnl >= 0 else "🔴"

        text = (
            f"*{pos['token_name']}* ({pos['token_symbol']})\n"
            f"Amount: {amount:,.2f}\n"
            f"Entry: {fmt_price(entry)}\n"
            f"Current: {fmt_price(current)}\n"
            f"{emoji} PNL: {fmt_usd(pnl)} ({pnl_pct:+.2f}%)"
        )
        keyboard = [
            [
                InlineKeyboardButton("Sell 50%", callback_data=f"sell:{pos['token_address']}:50"),
                InlineKeyboardButton("Sell 100%", callback_data=f"sell:{pos['token_address']}:100"),
            ]
        ]
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await get_user(update)
    trades = await db.get_trades(user["id"], limit=10)

    if not trades:
        await update.message.reply_text("No trades yet. Send a token contract address to get started!")
        return

    lines = ["📜 *Recent Trades*\n"]
    for t in trades:
        emoji = "🟢" if t["trade_type"] == "BUY" else "🔴"
        pnl_str = f" | PNL: {fmt_usd(t['pnl'])}" if t["trade_type"] == "SELL" else ""
        lines.append(f"{emoji} {t['trade_type']} {t['token_symbol']} — {fmt_usd(t['total_value'])}{pnl_str}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Token info + trading flow
# ---------------------------------------------------------------------------

async def show_token_info(update: Update, token_address: str) -> None:
    token_data = await market.get_token_data(token_address)
    if not token_data:
        await update.message.reply_text(
            "Couldn't find that token on DexScreener. Double check the contract address."
        )
        return

    text = (
        "🚀 *TOKEN INFO*\n\n"
        f"Name: {token_data['name']}\n"
        f"Symbol: {token_data['symbol']}\n\n"
        f"💰 Price: {fmt_price(token_data['price_usd'])}\n"
        f"📊 Market Cap: {fmt_compact(token_data['market_cap'])}\n"
        f"💧 Liquidity: {fmt_compact(token_data['liquidity_usd'])}\n"
        f"📈 Volume (24h): {fmt_compact(token_data['volume_24h'])}\n"
        f"📉 Change (24h): {float(token_data['price_change_24h'] or 0):+.2f}%\n\n"
        f"📄 CA: `{token_address}`"
    )
    keyboard = [
        [
            InlineKeyboardButton("BUY 0.1", callback_data=f"buy:{token_address}:0.1"),
            InlineKeyboardButton("BUY 0.5", callback_data=f"buy:{token_address}:0.5"),
        ],
        [
            InlineKeyboardButton("BUY 0.6", callback_data=f"buy:{token_address}:0.6"),
            InlineKeyboardButton("BUY 1", callback_data=f"buy:{token_address}:1"),
        ],
        [InlineKeyboardButton("CUSTOM", callback_data=f"buycustom:{token_address}")],
    ]
    if token_data.get("dex_url"):
        keyboard.append([InlineKeyboardButton("View on DexScreener", url=token_data["dex_url"])])

    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard)
    )


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

    text = (
        "🟢 *DEMO BUY*\n\n"
        f"Token: {result['token_name']} ({result['token_symbol']})\n"
        f"Invested: {result['sol_amount']:g} SOL (~{fmt_usd(result['usd_amount'])})\n"
        f"Entry: {fmt_price(result['entry_price'])}\n"
        f"Tokens: {result['tokens_bought']:,.0f}\n"
        f"Balance: {fmt_usd(result['new_balance'])}"
    )
    await target.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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

    emoji = "🟢" if result["pnl"] >= 0 else "🔴"
    text = (
        "🔴 *DEMO SELL*\n\n"
        f"Token: {result['token_name']} ({result['token_symbol']})\n"
        f"Entry: {fmt_price(result['entry_price'])}\n"
        f"Exit: {fmt_price(result['exit_price'])}\n"
        f"Received: {fmt_usd(result['proceeds'])}\n"
        f"{emoji} PNL: {fmt_usd(result['pnl'])} ({result['pnl_pct']:+.2f}%)\n"
        f"Balance: {fmt_usd(result['new_balance'])}"
    )
    await target.reply_text(text, parse_mode=ParseMode.MARKDOWN)


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
    await query.answer()
    data = query.data or ""
    parts = data.split(":")
    action = parts[0] if parts else ""

    if action == "buy" and len(parts) == 3:
        token_address, amount_str = parts[1], parts[2]
        await process_buy(update, query.message, token_address, float(amount_str))
    elif action == "buycustom" and len(parts) == 2:
        token_address = parts[1]
        context.user_data["awaiting_custom_buy"] = token_address
        await query.message.reply_text("Send the amount of SOL you'd like to spend, e.g. 0.75")
    elif action == "sell" and len(parts) == 3:
        token_address, percent_str = parts[1], parts[2]
        await process_sell(update, query.message, token_address, float(percent_str))


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


async def price_update_loop() -> None:
    """Background task: refresh open position prices/PNL every 30s."""
    while True:
        try:
            await trading.refresh_all_positions()
        except Exception:
            logger.exception("Price update loop failed")
        await asyncio.sleep(config.PRICE_UPDATE_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    await telegram_app.initialize()
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
