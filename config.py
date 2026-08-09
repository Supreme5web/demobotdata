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
