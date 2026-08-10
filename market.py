"""DexScreener (dexscreener.com) API integration for token price/market data.

Docs: https://docs.dexscreener.com/api/reference - REST API at
https://api.dexscreener.com. This is a free, public API: no API key, no
account, no auth header required.

Multi-chain: every lookup here is keyed on (DexScreener chain slug, token
address), where the slug comes from CHAINS[chain]["dexscreener_chain_id"]
(e.g. "solana", "bsc", "robinhood"). `GET /tokens/v1/{slug}/{address}`
returns a plain JSON array of every trading pair (across every DEX) for
that token - we pick the pair with the highest USD liquidity as the
"primary" one, same approach as picking the top pool from Codex/Solana
Tracker previously.

The bot's demo trading currency is USDC, which is pegged 1:1 to USD, so
unlike a chain's native gas token there's no price to fetch or convert -
trading.py just treats a USDC amount as an equal USD amount directly.

Field paths are read defensively with _dig() so a renamed/missing field
degrades to 0 instead of raising - this should never crash a trade.
"""

import logging
from typing import Optional

import httpx

from config import CHAINS, DEFAULT_CHAIN

logger = logging.getLogger(__name__)

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"


def _chain_cfg(chain: str) -> dict:
    return CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])


def _dig(d: dict, *paths, default=0):
    """Tries each dotted path (e.g. 'liquidity.usd') against `d` in order
    and returns the first value found that isn't None. Never raises."""
    for path in paths:
        node = d
        ok = True
        for key in path.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return default


async def _get_pairs(token_address: str, chain: str) -> list:
    """GET /tokens/v1/{dexscreener_chain_id}/{address} - returns a bare JSON
    array of pair objects (one per DEX pool this token trades on), or an
    empty list if the token isn't found / the request fails."""
    slug = _chain_cfg(chain)["dexscreener_chain_id"]
    url = f"{DEXSCREENER_BASE_URL}/tokens/v1/{slug}/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("DexScreener API request failed (%s/%s): %s", chain, token_address, exc)
        return []
    return data if isinstance(data, list) else []


def _best_pair(pairs: list) -> Optional[dict]:
    """Picks the highest-liquidity pair out of every DEX pool returned for
    a token - mirrors how Codex/Solana Tracker surfaced a single "primary"
    pool per token."""
    if not pairs:
        return None
    return max(pairs, key=lambda p: float(_dig(p, "liquidity.usd", default=0) or 0))


async def get_token_data(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[dict]:
    """Fetch token info from DexScreener for a given token address on the
    given chain (see CHAINS in config.py for supported chains). Returns
    None if the token can't be found / the request fails."""
    pairs = await _get_pairs(token_address, chain)
    pair = _best_pair(pairs)
    if not pair:
        return None

    try:
        price_usd = float(_dig(pair, "priceUsd", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    market_cap = _dig(pair, "marketCap", "fdv", default=0)
    liquidity_usd = _dig(pair, "liquidity.usd", default=0)
    volume_24h = _dig(pair, "volume.h24", default=0)
    price_change_1h = _dig(pair, "priceChange.h1", default=0)

    base_token = pair.get("baseToken") or {}

    return {
        "token_address": token_address,
        "chain": chain,
        "name": base_token.get("name") or "Unknown Token",
        "symbol": base_token.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": market_cap or 0,
        "liquidity_usd": liquidity_usd or 0,
        "volume_24h": volume_24h or 0,
        "price_change_1h": price_change_1h or 0,
        "pair_address": pair.get("pairAddress") or "",
        "dex_url": pair.get("url") or "",
        "logo_url": _dig(pair, "info.imageUrl", default=""),
    }


async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None
