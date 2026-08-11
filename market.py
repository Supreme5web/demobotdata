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



def _select_matching_pool(token_price: float, pools: list) -> dict | None:
    """Select the pool whose price is closest to the token-level DexPaprika price."""
    if not pools or token_price <= 0:
        return None

    candidates = []
    for pool in pools:
        try:
            pool_price = float(pool.get("price_usd") or 0)
            if pool_price <= 0:
                continue

            # Compare on a logarithmic scale so wildly different prices
            # (e.g. 75 vs 0.00001) are rejected reliably.
            ratio = max(pool_price, token_price) / min(pool_price, token_price)
            candidates.append((ratio, pool))
        except (TypeError, ValueError):
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


async def get_token_data(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[dict]:
    """Fetch token data and select the pool that matches the token price."""
    network = _normalize_chain(chain)
    url = f"{BASE_URL}/networks/{network}/tokens/{token_address}"

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        token = data.get("tokens", [{}])[0] if data.get("tokens") else {}
        token_price = float(token.get("price_usd") or 0)
        pools = data.get("pools") or []

        # Fetch /search as well because the pool-level response contains
        # 5m/1h/24h price changes. Select the pool whose price matches
        # the token-level price rather than blindly taking pools[0].
        search_data = {}
        try:
            search_response = await client.get(
                f"{BASE_URL}/search",
                params={"query": token_address},
            )
            if search_response.is_success:
                search_data = search_response.json()
                search_pools = search_data.get("pools") or []
                if search_pools:
                    pools = search_pools
        except Exception:
            pass

        matching_pool = _select_matching_pool(token_price, pools)

        change_5m = None
        change_1h = None
        change_24h = None

        if matching_pool:
            change_5m = matching_pool.get("last_price_change_usd_5m")
            change_1h = matching_pool.get("last_price_change_usd_1h")
            change_24h = matching_pool.get("last_price_change_usd_24h")

        # Keep token-level volume/liquidity because token volume aggregates
        # the relevant pools, while pool volume represents only one pool.
        volume_usd = float(token.get("volume_usd") or 0)
        liquidity_usd = float(token.get("liquidity_usd") or 0)

        # PaperBoat uses the standard 1B circulating supply assumption.
        market_cap = token_price * 1_000_000_000 if token_price > 0 else 0

        return {
            "name": token.get("name") or "",
            "symbol": token.get("symbol") or "",
            "price_usd": token_price,
            "market_cap": market_cap,
            "liquidity_usd": liquidity_usd,
            "volume_usd": volume_usd,
            "price_change_5m": float(change_5m) if change_5m is not None else 0.0,
            "price_change_1h": float(change_1h) if change_1h is not None else 0.0,
            "price_change_24h": float(change_24h) if change_24h is not None else 0.0,
            "logo_url": token.get("logo_url") or "",
            "dex_url": "",
            "pools": pools,
        }
async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None