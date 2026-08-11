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
DEXSCREENER_API_BASE_URL = "https://api.dexscreener.com"

# Maps our internal chain code to DexScreener's chainId slug. Used both for
# the /tokens/v1 API lookup below and for the guessed logo-CDN fallback -
# DexScreener uses the same slugs for both.
_DEXSCREENER_CHAIN_SLUGS = {"sol": "solana", "bsc": "bsc", "robinhood": "robinhood"}


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
    zero." Also used when summary.24h.volume_usd or the pool-level 1h
    price change is missing/zero, which happen independently of price.

    Important: /search splits its response into three arrays, and only
    two of them carry market data. data["tokens"] is identity-only (name,
    symbol, decimals, fdv - no price/volume/liquidity at all). The actual
    price_usd/volume_usd numbers live on data["pools"], keyed per pool, so
    a pool only "matches" our token if our token address appears in that
    pool's own `tokens` list. Reading price/volume off a token dict (as
    if /search worked like /tokens/{address}) silently returns nothing,
    which is why volume was defaulting to 0 for otherwise-healthy tokens.

    This merges the token's identity fields with the market data from
    whichever matching pool has the highest volume_usd (the pool
    DexPaprika's own site would treat as the token's primary pool).
    Returns None if nothing in either array matches."""
    network = _chain_cfg(chain)["dexpaprika_network_id"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{DEXPAPRIKA_BASE_URL}/search", params={"query": token_address})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("DexPaprika /search fallback failed (%s/%s): %s", chain, token_address, exc)
        return None

    token_meta = None
    for token in data.get("tokens", []):
        if token.get("chain") == network and str(token.get("id", "")).lower() == token_address.lower():
            token_meta = token
            break

    matching_pools = [
        pool for pool in data.get("pools", [])
        if pool.get("chain") == network
        and any(str(t.get("id", "")).lower() == token_address.lower() for t in pool.get("tokens", []))
    ]
    best_pool = max(matching_pools, key=lambda p: float(p.get("volume_usd") or 0), default=None)

    if token_meta is None and best_pool is None:
        return None

    merged = dict(token_meta or {})
    if best_pool:
        # Only fill price from the pool if the token entry didn't already
        # have one (it never does today, but this keeps identity fields
        # authoritative over pool fields if that ever changes).
        merged.setdefault("price_usd", best_pool.get("price_usd"))
        merged["volume_usd"] = best_pool.get("volume_usd")
        merged["last_price_change_usd_1h"] = best_pool.get("last_price_change_usd_1h")
    return merged


def _dexscreener_chain_slug(chain: str) -> str:
    return _DEXSCREENER_CHAIN_SLUGS.get(chain, chain)


async def _fetch_dexscreener_logo(token_address: str, chain: str) -> Optional[str]:
    """Queries DexScreener's own GET /tokens/v1/{chainId}/{tokenAddress}
    endpoint for a verified logo. Unlike DexPaprika (which only exposes a
    has_image boolean, no URL) DexScreener's pair objects carry a real
    info.imageUrl field. A token can have multiple pairs/pools, each
    potentially with its own imageUrl, so we pick the imageUrl from
    whichever pair has the most USD liquidity - that's the pair
    DexScreener's own UI would surface first, and the one most likely to
    have curated art attached.

    Returns None (never raises) if the token has no pairs, no pair carries
    an image, or the request fails - a miss here just means the caller
    falls back to the guessed CDN URL, and pnl_card.py falls back further
    to a letter badge, so nothing downstream breaks."""
    chain_slug = _dexscreener_chain_slug(chain)
    url = f"{DEXSCREENER_API_BASE_URL}/tokens/v1/{chain_slug}/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=6) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            pairs = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("DexScreener /tokens/v1 lookup failed (%s/%s): %s", chain, token_address, exc)
        return None

    if not isinstance(pairs, list) or not pairs:
        return None

    candidates = [p for p in pairs if _dig(p, "info.imageUrl", default=None)]
    if not candidates:
        return None

    best_pair = max(candidates, key=lambda p: float(_dig(p, "liquidity.usd", default=0) or 0))
    return _dig(best_pair, "info.imageUrl", default=None)


def _guessed_logo_url(token_address: str, chain: str) -> str:
    """Last-resort fallback: guesses a URL on DexScreener's public
    token-image CDN by convention, without confirming it actually exists.
    Used only when _fetch_dexscreener_logo() above can't find a verified
    imageUrl (e.g. the token has no indexed pairs yet, or the API call
    failed) - pnl_card.py's _fetch_logo() will 404/fail gracefully into a
    letter badge if this guess turns out to be wrong.
    """
    chain_slug = _dexscreener_chain_slug(chain)
    return f"https://dd.dexscreener.com/ds-data/tokens/{chain_slug}/{token_address}.png"


async def _resolve_logo_url(token_address: str, chain: str) -> str:
    """Best-effort logo resolution, verified source first: try
    DexScreener's /tokens/v1 API (real info.imageUrl) before falling back
    to guessing the CDN path directly. Always returns *some* URL string -
    downstream, pnl_card.py treats a bad/missing image as "no logo" and
    draws a letter badge instead, so a wrong guess is never fatal."""
    verified = await _fetch_dexscreener_logo(token_address, chain)
    if verified:
        return verified
    return _guessed_logo_url(token_address, chain)


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
    # Pick the highest-volume pool that has a non-null 1h change. Left as
    # None (not defaulted to 0 yet) if nothing is found here - the /search
    # fallback below gets a chance to fill it before we give up on it.
    if price_change_1h is None:
        pools = result.get("pools") or []
        candidates = [p for p in pools if p.get("last_price_change_usd_1h") is not None]
        if candidates:
            best_pool = max(candidates, key=lambda p: float(p.get("volume_usd") or 0))
            price_change_1h = best_pool.get("last_price_change_usd_1h")

    if price_usd <= 0 or not volume_24h or price_change_1h is None:
        # Summary endpoint came back empty on price, volume, and/or 1h
        # change - try the /search fallback before giving up (see
        # _query_dexpaprika_search docstring). These are independent gaps:
        # a token can have a valid price but a missing 1h change (or vice
        # versa), so this triggers on any one of them rather than requiring
        # all three to be missing.
        fallback = await _query_dexpaprika_search(token_address, chain)
        if fallback:
            if price_usd <= 0:
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
            if price_change_1h is None:
                price_change_1h = fallback.get("last_price_change_usd_1h")
            if price_usd > 0 or volume_24h:
                logger.info(
                    "DexPaprika summary lacked price/volume/1h-change for %s/%s - used /search fallback instead.",
                    chain, token_address,
                )

    if price_change_1h is None:
        price_change_1h = 0

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
        # a usable image URL, so this comes from DexScreener instead - see
        # _resolve_logo_url() for the verified-first, guessed-fallback logic.
        "logo_url": await _resolve_logo_url(token_address, chain),
    }


async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None