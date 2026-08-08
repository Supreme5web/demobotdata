"""DexScreener API integration for token price/market data."""

import logging
from typing import Optional

import httpx

from config import SOL_MINT_ADDRESS, DEFAULT_SOL_PRICE_FALLBACK

logger = logging.getLogger(__name__)

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"


async def get_token_data(token_address: str) -> Optional[dict]:
    """Fetch token info from DexScreener for a given contract address.

    Returns the most liquid pair's data, or None if the token can't be found.
    """
    url = DEXSCREENER_TOKEN_URL.format(token_address)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("DexScreener request failed for %s: %s", token_address, exc)
        return None

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Pick the pair with the highest USD liquidity as the most reliable source.
    pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0)
    base = pair.get("baseToken", {}) or {}

    try:
        price_usd = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    return {
        "token_address": token_address,
        "name": base.get("name") or "Unknown Token",
        "symbol": base.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": pair.get("marketCap") or pair.get("fdv") or 0,
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd") or 0,
        "volume_24h": (pair.get("volume") or {}).get("h24") or 0,
        "price_change_1h": (pair.get("priceChange") or {}).get("h1") or 0,
        "pair_address": pair.get("pairAddress", ""),
        "dex_url": pair.get("url", ""),
        "logo_url": (pair.get("info") or {}).get("imageUrl", ""),
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
