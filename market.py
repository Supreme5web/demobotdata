"""Solana Tracker Data API integration for token price/market data.

Docs: https://docs.solanatracker.io/data-api/tokens/token-info
GET https://data.solanatracker.io/tokens/{address}, auth via 'x-api-key' header.

The response nests everything under `token` (name/symbol/mint/image) and
`pools` (one entry per DEX pool, each with its own price/marketCap/liquidity).
We pick the most liquid pool as the "current" one, same approach as the old
DexScreener integration. Field paths are read defensively with _dig() so a
renamed/missing field degrades to 0 instead of raising - the API's exact
schema can shift between plans/versions and this should never crash a trade.
"""

import logging
from typing import Optional

import httpx

from config import SOL_MINT_ADDRESS, DEFAULT_SOL_PRICE_FALLBACK, SOLANATRACKER_API_KEY

logger = logging.getLogger(__name__)

SOLANATRACKER_BASE_URL = "https://data.solanatracker.io"
_HEADERS = {"x-api-key": SOLANATRACKER_API_KEY}


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


async def get_token_data(token_address: str) -> Optional[dict]:
    """Fetch token info from the Solana Tracker Data API for a given
    contract address. Returns the most liquid pool's data, or None if the
    token can't be found / the request fails.
    """
    url = f"{SOLANATRACKER_BASE_URL}/tokens/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=_HEADERS)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Market data request failed for %s: %s", token_address, exc)
        return None

    token_info = data.get("token") or {}
    pools = data.get("pools") or []
    if not pools:
        return None

    # Pick the pool with the highest USD liquidity as the most reliable source.
    pool = max(pools, key=lambda p: _dig(p, "liquidity.usd", default=0) or 0)

    try:
        price_usd = float(_dig(pool, "price.usd", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    market_cap = _dig(pool, "marketCap.usd", "marketCapUsd", default=0)
    liquidity_usd = _dig(pool, "liquidity.usd", "liquidityUsd", default=0)

    # Volume/price-change live at the top level of the response (per-token,
    # aggregated across pools), keyed by timeframe.
    volume_24h = _dig(data, "txns.24h.volume", "volume.24h", default=0)
    price_change_1h = _dig(data, "events.1h.priceChangePercentage", "priceChange.1h", default=0)

    return {
        "token_address": token_address,
        "name": token_info.get("name") or "Unknown Token",
        "symbol": token_info.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": market_cap or 0,
        "liquidity_usd": liquidity_usd or 0,
        "volume_24h": volume_24h or 0,
        "price_change_1h": price_change_1h or 0,
        "pair_address": pool.get("poolId") or pool.get("poolAddress") or "",
        "dex_url": "",  # Solana Tracker doesn't return a ready-made chart link;
                         # main.py falls back to a generated Solana Tracker page.
        "logo_url": token_info.get("image") or "",
    }


async def get_current_price(token_address: str) -> Optional[float]:
    data = await get_token_data(token_address)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None


async def get_sol_price() -> float:
    """Fetch the current SOL/USD price, used to convert BUY button amounts
    (denominated in SOL) into demo USD balance deductions."""
    data = await get_token_data(SOL_MINT_ADDRESS)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    logger.warning("Could not fetch live SOL price, using fallback of $%s", DEFAULT_SOL_PRICE_FALLBACK)
    return DEFAULT_SOL_PRICE_FALLBACK
