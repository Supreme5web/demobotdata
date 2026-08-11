"""Environment configuration for the demo trading bot."""

import os

from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CODEX_API_KEY = os.getenv("CODEX_API_KEY")

# App constants
# Starting demo balance for a new user, and the currency it (and every buy)
# is denominated in - see the USDC note below.
STARTING_BALANCE_USDC = 800.0
PRICE_UPDATE_INTERVAL_SECONDS = 30

# Buy-button presets, in USDC. USDC is pegged 1:1 to USD, so - unlike a
# chain's native gas token (SOL/BNB/ETH), whose USD value moves - these
# numbers don't need a live price lookup to convert into demo USD balance
# deductions; trading.py just treats them as USD amounts directly.
USDC_BUY_PRESETS = [50, 100, 250, 500]

# ---------------------------------------------------------------------------
# Chains
# ---------------------------------------------------------------------------
# Market data comes from Codex.io's GraphQL API (https://graph.codex.io/graphql,
# docs: https://docs.codex.io) - requires an API key (CODEX_API_KEY above),
# sent as the raw value of the `Authorization` header (no "Bearer " prefix).
# Codex identifies tokens by (networkId, address) rather than a chain slug,
# so each chain below carries its own "codex_network_id" used as the
# `networkId` GraphQL variable.
#
# NOTE on "robinhood": Codex's network coverage is generated live from their
# API and Robinhood Chain is very new (mainnet chain ID 4663), so it may not
# be indexed yet. If lookups for this chain come back empty, that's likely
# why - worth confirming against `getNetworks` in the Codex API directly,
# or falling back to another data source for this chain specifically.
#
# "address_kind" is used by main.py to auto-detect which chain a pasted
# contract address belongs to: Solana addresses are base58 while every EVM
# chain (BSC, Robinhood Chain, ...) shares the same 0x-hex format, so two
# chains can share a kind. When that happens the address alone can't tell
# them apart - main.py resolves it by querying Codex for the address on
# each candidate chain and using whichever one actually has the token
# listed.
#
# trading.py, database.py, and main.py all thread a `chain` argument through
# their calls and key DB rows on (user, token, chain), so adding a chain here
# is enough to make it tradable everywhere else.
CHAINS = {
    "sol": {
        "label": "Solana",
        "native_symbol": "SOL",
        "buy_presets": USDC_BUY_PRESETS,
        "explorer_url": "https://dexscreener.com/solana/{address}",
        "codex_network_id": 1399811149,
        "address_kind": "solana",
    },
    "bsc": {
        "label": "BSC",
        "native_symbol": "BNB",
        "buy_presets": USDC_BUY_PRESETS,
        "explorer_url": "https://dexscreener.com/bsc/{address}",
        "codex_network_id": 56,
        "address_kind": "evm",
    },
    "robinhood": {
        "label": "Robinhood Chain",
        "native_symbol": "ETH",
        "buy_presets": USDC_BUY_PRESETS,
        "explorer_url": "https://dexscreener.com/robinhood/{address}",
        "codex_network_id": 4663,
        "address_kind": "evm",
    },
}

DEFAULT_CHAIN = "sol"

REQUIRED_VARS = {
    "SUPABASE_URL": SUPABASE_URL,
    "SUPABASE_KEY": SUPABASE_KEY,
    "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
    "CODEX_API_KEY": CODEX_API_KEY,
}

missing = [name for name, value in REQUIRED_VARS.items() if not value]
if missing:
    raise RuntimeError(
        f"Missing required environment variable(s): {', '.join(missing)}. "
        "Set them in your .env file or Render environment settings."
    )
