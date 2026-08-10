"""Environment configuration for the demo trading bot."""

import os

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# App constants
STARTING_BALANCE_SOL = 10.0
PRICE_UPDATE_INTERVAL_SECONDS = 30
SOL_MINT_ADDRESS = "So11111111111111111111111111111111111111112"
DEFAULT_SOL_PRICE_FALLBACK = 150.0

# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------
# Market data now comes from DexScreener's public multi-chain API
# (https://api.dexscreener.com/tokens/v1/{chainId}/{address}), which needs no
# API key - so the old SOLANATRACKER_API_KEY requirement is gone. Every chain
# below is looked up with the same DexScreener endpoint, just a different
# `dex_chain_id` slug.
#
# `code` is the short id used everywhere internally (DB rows, callback_data,
# regex dispatch) - kept to 3 chars so Telegram's 64-byte callback_data limit
# never gets tight once a token address is appended.
#
# `native_price_address` is the wrapped-native-token contract DexScreener
# uses to price that chain's gas token in USD (WSOL / WETH / WBNB). Robinhood
# Chain settles in ETH but doesn't have a confirmed canonical WETH contract
# baked in here, so it borrows Ethereum's ETH/USD price instead of pricing a
# token on Robinhood Chain directly - see `native_price_chain` below.
CHAINS = {
    "sol": {
        "label": "Solana",
        "native_symbol": "SOL",
        "dex_chain_id": "solana",
        "native_price_address": SOL_MINT_ADDRESS,
        "native_price_chain": None,
        "buy_presets": [0.1, 0.5, 0.6, 1],
        "fallback_native_price": 150.0,
        "explorer_url": "https://www.solanatracker.io/token/{address}",
    },
    "eth": {
        "label": "Ethereum",
        "native_symbol": "ETH",
        "dex_chain_id": "ethereum",
        "native_price_address": "0xC02aaA39b223fE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
        "native_price_chain": None,
        "buy_presets": [0.01, 0.05, 0.1, 0.25],
        "fallback_native_price": 3000.0,
        "explorer_url": "https://dexscreener.com/ethereum/{address}",
    },
    "bsc": {
        "label": "BNB Chain",
        "native_symbol": "BNB",
        "dex_chain_id": "bsc",
        "native_price_address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095",  # WBNB
        "native_price_chain": None,
        "buy_presets": [0.05, 0.25, 0.5, 1],
        "fallback_native_price": 600.0,
        "explorer_url": "https://dexscreener.com/bsc/{address}",
    },
    "rbh": {
        "label": "Robinhood Chain",
        "native_symbol": "ETH",
        "dex_chain_id": "robinhood",
        "native_price_address": None,
        # Reuse Ethereum's WETH pricing for Robinhood Chain's native ETH gas token.
        "native_price_chain": "eth",
        "buy_presets": [0.01, 0.05, 0.1, 0.25],
        "fallback_native_price": 3000.0,
        "explorer_url": "https://dexscreener.com/robinhood/{address}",
    },
}

DEFAULT_CHAIN = "sol"

REQUIRED_VARS = {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
}

missing = [name for name, value in REQUIRED_VARS.items() if not value]
if missing:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Set them in your .env file or Render environment settings."
    )
