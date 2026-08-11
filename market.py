"""DexPaprika (dexpaprika.com) REST API integration for token price/market data.

Docs: https://docs.dexpaprika.com - base URL https://api.dexpaprika.com.
No API key is required.

Multi-chain: every lookup here is keyed on (DexPaprika network slug, token
address), where the slug comes from CHAINS[chain]["dexpaprika_network_id"]
(e.g. "solana", "bsc"). We call GET /networks/{network}/tokens/{address},
which returns DexPaprika's already-aggregated, liquidity-weighted view of
the token (price, fdv, liquidity, volume) in a single call - no need to
fetch every pool and pick the best one ourselves.

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

DEXPAPRIKA_BASE_URL = "https://api.dexpaprika.com"


def _chain_cfg(chain: str) -> dict:
    return CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])


def _dig(d: dict, *paths, default=0):
    """Tries each dotted path (e.g. 'summary.24h.volume_usd') against `d`
    in order and returns the first value found that isn't None. Never
    raises."""
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


async def _query_dexpaprika(token_address: str, chain: str) -> Optional[dict]:
    """GETs /networks/{network}/tokens/{address} and returns the parsed
    JSON body, or None if the token isn't found / the request fails."""
    network = _chain_cfg(chain)["dexpaprika_network_id"]
    url = f"{DEXPAPRIKA_BASE_URL}/networks/{network}/tokens/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                # Token not indexed on this network - not an error, just a miss.
                return None
            resp.raise_for_status()
            return resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("DexPaprika API request failed (%s/%s): %s", chain, token_address, exc)
        return None


async def _query_dexpaprika_search(token_address: str, chain: str) -> Optional[dict]:
    """Fallback for tokens whose /tokens/{address} 'summary' object hasn't
    been fully populated yet - DexPaprika's own site flags this explicitly
    for very new chains (e.g. Robinhood Chain, live since July 2026):
    "Summary is present but summary.price_usd is missing, non-finite, or
    zero." The flat /search endpoint has been observed to carry real
    price/liquidity numbers for the same token even when the summary
    endpoint doesn't yet, so it's worth a second try before giving up.
    Returns the matching flat token dict, or None if no match / failure."""
    network = _chain_cfg(chain)["dexpaprika_network_id"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{DEXPAPRIKA_BASE_URL}/search", params={"query": token_address})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("DexPaprika /search fallback failed (%s/%s): %s", chain, token_address, exc)
        return None

    for token in data.get("tokens", []):
        if token.get("chain") == network and str(token.get("id", "")).lower() == token_address.lower():
            return token
    return None



def _logo_url(token_address: str, chain: str) -> str:
    """Return a stable token-logo URL for chains supported by DexScreener's
    public token-image CDN. DexPaprika provides has_image but not the image URL.
    """
    chain_slug = {"sol": "solana", "bsc": "bsc", "robinhood": "robinhood"}.get(chain, chain)
    return f"https://dd.dexscreener.com/ds-data/tokens/{chain_slug}/{token_address}.png"


async def get_token_data(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[dict]:
    """Fetch token info from DexPaprika for a given token address on the
    given chain (see CHAINS in config.py for supported chains). Returns
    None if the token can't be found / the request fails."""
    result = await _query_dexpaprika(token_address, chain)
    if not result:
        return None

    try:
        price_usd = float(_dig(result, "summary.price_usd", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    # DexPaprika doesn't expose a separate circulating market cap field -
    # "fdv" (fully diluted valuation = price * total supply) is the closest
    # equivalent and is what most memecoin trackers show as "MCap" anyway.
    market_cap = _dig(result, "summary.fdv", default=0)
    liquidity_usd = _dig(result, "summary.liquidity_usd", default=0)
    volume_24h = _dig(result, "summary.24h.volume_usd", default=0)
    price_change_1h = _dig(result, "summary.1h.last_price_usd_change", "summary.1h.price_change", default=None)

    # DexPaprika also exposes 1h percentage changes on individual pools.
    # Pick the highest-volume pool that has a non-null 1h change.
    if price_change_1h is None:
        pools = result.get("pools") or []
        candidates = [p for p in pools if p.get("last_price_change_usd_1h") is not None]
        if candidates:
            best_pool = max(candidates, key=lambda p: float(p.get("volume_usd") or 0))
            price_change_1h = best_pool.get("last_price_change_usd_1h")
    if price_change_1h is None:
        price_change_1h = 0

    if price_usd <= 0:
        # Summary endpoint came back empty on price - try the /search
        # fallback before giving up (see _query_dexpaprika_search docstring).
        fallback = await _query_dexpaprika_search(token_address, chain)
        if fallback:
            try:
                price_usd = float(fallback.get("price_usd") or 0)
            except (TypeError, ValueError):
                price_usd = 0.0
            if not market_cap:
                market_cap = fallback.get("fdv_usd") or fallback.get("fdv") or 0
            if not liquidity_usd:
                liquidity_usd = fallback.get("liquidity_usd") or 0
            if not volume_24h:
                volume_24h = fallback.get("volume_usd") or fallback.get("volume_usd_24h") or 0
            if price_usd > 0:
                logger.info(
                    "DexPaprika summary lacked price for %s/%s - used /search fallback instead.",
                    chain, token_address,
                )

    return {
        "token_address": token_address,
        "chain": chain,
        "name": result.get("name") or "Unknown Token",
        "symbol": result.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": market_cap or 0,
        "liquidity_usd": liquidity_usd or 0,
        "volume_24h": volume_24h or 0,
        "price_change_1h": price_change_1h or 0,
        "pair_address": "",
        "dex_url": "",
        # DexPaprika's token endpoint only returns a `has_image` boolean, not
        # a usable image URL, so this stays empty - pnl_card.py already
        # falls back to a letter badge when no logo is available.
        "logo_url": _logo_url(token_address, chain),
    }


async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None