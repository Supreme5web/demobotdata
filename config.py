"""Environment configuration for the demo trading bot."""

import os

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SOLANATRACKER_API_KEY = os.getenv("SOLANATRACKER_API_KEY")

# App constants
STARTING_BALANCE_SOL = 10.0
PRICE_UPDATE_INTERVAL_SECONDS = 30
SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
DEFAULT_SOL_PRICE_FALLBACK = 150.0

# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------
# This bot is Solana-only. Market data comes from Solana Tracker's Data API
# (https://data.solanatracker.io, docs: https://docs.solanatracker.io),
# SOLANATRACKER_API_KEY required, which identifies tokens by mint address.
#
# A single-entry CHAINS dict is kept (rather than inlining these values)
# because trading.py, database.py, and main.py all thread a `chain` argument
# through their calls and key DB rows on (user, token, chain) - keeping the
# same shape here means those call sites don't need to change even though
# "sol" is now the only option.
CHAINS = {
    "sol": {
        "label": "Solana",
        "native_symbol": "SOL",
        "buy_presets": [0.1, 0.5, 0.6, 1],
        "fallback_native_price": DEFAULT_SOL_PRICE_FALLBACK,
        "explorer_url": "https://www.solanatracker.io/token/{address}",
    },
}

DEFAULT_CHAIN = "sol"

REQUIRED_VARS = {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "SOLANATRACKER_API_KEY": SOLANATRACKER_API_KEY,
}

missing = [name for name, value in REQUIRED_VARS.items() if not value]
if missing:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Set them in your .env file or Render environment settings."
    )
