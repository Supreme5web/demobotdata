"""Solana Tracker (solanatracker.io) Data API integration for token price/market data.

Docs: https://docs.solanatracker.io - REST API at https://data.solanatracker.io,
auth via an `x-api-key: <SOLANATRACKER_API_KEY>` header.

Solana-only: this bot no longer trades other chains, so every lookup here is
keyed on a Solana mint address. `GET /tokens/{address}` returns full token
metadata plus an array of `pools` (one per DEX/market the token trades on) -
we use `pools[0]`, which the API returns as the primary/highest-liquidity
pool. `GET /price?token=<mint>` is a lighter-weight single-value lookup, used
for the frequent "what's SOL worth right now" calls (native-amount -> USD
conversion) instead of pulling the full token payload each time.

Field paths are read defensively with _dig() so a renamed/missing field
degrades to 0 instead of raising - this should never crash a trade.
"""

import logging
from typing import Optional

import httpx

from config import SOLANATRACKER_API_KEY, SOL_MINT_ADDRESS, DEFAULT_SOL_PRICE_FALLBACK

logger = logging.getLogger(__name__)

SOLANATRACKER_BASE_URL = "https://data.solanatracker.io"
_HEADERS = {"x-api-key": SOLANATRACKER_API_KEY}


def _dig(d: dict, *paths, default=0):
    """Tries each dotted path (e.g. 'pools.0.price.usd') against `d` in
    order and returns the first value found that isn't None. Never raises."""
    for path in paths:
        node = d
        ok = True
        for key in path.split("."):
            if isinstance(node, list):
                try:
                    node = node[int(key)]
                except (ValueError, IndexError):
                    ok = False
                    break
            elif isinstance(node, dict) and key in node:
                node = node[key]
            else:
                ok = False
                break
        if ok and node is not None:
            return node
    return default


async def _get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{SOLANATRACKER_BASE_URL}{path}",
                headers=_HEADERS,
                params=params,
            )
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Solana Tracker API request failed (%s): %s", path, exc)
        return None


async def get_token_data(token_address: str, chain: str = "sol") -> Optional[dict]:
    """Fetch token info from Solana Tracker for a given mint address.
    Returns None if the token can't be found / the request fails.

    `chain` is accepted for call-site compatibility with the rest of the
    codebase (which still threads a `chain` argument through everywhere)
    but is otherwise unused - this bot is Solana-only.
    """
    data = await _get(f"/tokens/{token_address}")
    if not data:
        return None

    # pools[0] is the primary (highest-liquidity) pool for this token.
    if not _dig(data, "pools.0"):
        return None

    try:
        price_usd = float(_dig(data, "pools.0.price.usd", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    market_cap = _dig(data, "pools.0.marketCap.usd", default=0)
    liquidity_usd = _dig(data, "pools.0.liquidity.usd", default=0)
    volume_24h = _dig(data, "pools.0.txns.volume24h", "pools.0.txns.volume", default=0)
    price_change_1h = _dig(data, "events.1h.priceChangePercentage", default=0)

    token = data.get("token") or {}

    return {
        "token_address": token_address,
        "chain": "sol",
        "name": token.get("name") or "Unknown Token",
        "symbol": token.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": market_cap or 0,
        "liquidity_usd": liquidity_usd or 0,
        "volume_24h": volume_24h or 0,
        "price_change_1h": price_change_1h or 0,
        "pair_address": _dig(data, "pools.0.poolId", default=""),
        "dex_url": "",  # Solana Tracker doesn't return a ready-made chart
                         # link; main.py falls back to the chain's explorer page.
        "logo_url": token.get("image") or "",
    }


async def get_current_price(token_address: str, chain: str = "sol") -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None


async def get_native_price(chain: str = "sol") -> float:
    """Fetch the current USD price of SOL, used to convert BUY button
    amounts (denominated in SOL) into demo USD balance deductions. Falls
    back to a hardcoded estimate if the live lookup fails, so a market-data
    hiccup never blocks a trade outright.

    `chain` is accepted for call-site compatibility (see get_token_data)
    but is otherwise unused - this bot only ever prices SOL.
    """
    data = await _get("/price", params={"token": SOL_MINT_ADDRESS})
    if data:
        try:
            price = float(data.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            return price

    logger.warning(
        "Could not fetch live SOL price, using fallback of $%s",
        DEFAULT_SOL_PRICE_FALLBACK,
    )
    return DEFAULT_SOL_PRICE_FALLBACK


# Backwards-compatible alias - existing callers can keep calling this.
async def get_sol_price() -> float:
    return await get_native_price("sol")
