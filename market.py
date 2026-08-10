"""DexScreener multi-chain token/price data integration.

Docs: GET https://api.dexscreener.com/tokens/v1/{chainId}/{tokenAddresses}
No API key required. Returns a JSON array of matching pairs directly (not
wrapped in an object) - one entry per DEX pool the token trades on. We pick
the most liquid pool as the "current" one, same approach as the old
Solana-only integration this replaces.

`chain` is always one of the short codes in config.CHAINS ("sol", "eth",
"bsc", "rbh"). Field paths are read defensively with _dig() so a
renamed/missing field degrades to 0 instead of raising - DexScreener's exact
schema can shift slightly between chains/pools and this should never crash
a trade.
"""

import logging
from typing import Optional

import httpx

from config import CHAINS, DEFAULT_CHAIN

logger = logging.getLogger(__name__)

DEXSCREENER_BASE_URL = "https://api.dexscreener.com"


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


def _chain_config(chain: str) -> dict:
    return CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])


async def _fetch_pairs(dex_chain_id: str, token_address: str) -> list:
    url = f"{DEXSCREENER_BASE_URL}/tokens/v1/{dex_chain_id}/{token_address}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Market data request failed for %s on %s: %s", token_address, dex_chain_id, exc)
        return []

    # DexScreener returns a bare list from tokens/v1; guard against a shape
    # change (e.g. an {"pairs": [...]} wrapper) so this never crashes.
    if isinstance(data, dict):
        data = data.get("pairs") or []
    return data if isinstance(data, list) else []


async def get_token_data(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[dict]:
    """Fetch token info from DexScreener for a given contract address on the
    given chain. Returns the most liquid pool's data, or None if the token
    can't be found / the request fails.
    """
    cfg = _chain_config(chain)
    pairs = await _fetch_pairs(cfg["dex_chain_id"], token_address)
    if not pairs:
        return None

    # Pick the pool with the highest USD liquidity as the most reliable source.
    pool = max(pairs, key=lambda p: float(_dig(p, "liquidity.usd", default=0) or 0))

    try:
        price_usd = float(_dig(pool, "priceUsd", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    market_cap = _dig(pool, "marketCap", "fdv", default=0)
    liquidity_usd = _dig(pool, "liquidity.usd", default=0)
    volume_24h = _dig(pool, "volume.h24", default=0)
    price_change_1h = _dig(pool, "priceChange.h1", default=0)

    base_token = pool.get("baseToken") or {}

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
        "pair_address": pool.get("pairAddress") or "",
        "dex_url": pool.get("url") or "",
        "logo_url": _dig(pool, "info.imageUrl", default=""),
    }


async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None


async def get_native_price(chain: str = DEFAULT_CHAIN) -> float:
    """Fetch the current USD price of `chain`'s native gas token (SOL / ETH /
    BNB), used to convert BUY button amounts (denominated in the native
    token) into demo USD balance deductions. Falls back to a hardcoded
    estimate if the live lookup fails, so a market-data hiccup never blocks
    a trade outright."""
    cfg = _chain_config(chain)

    # Some chains (Robinhood Chain) borrow another chain's native price
    # instead of pricing their own wrapped-native token directly - see the
    # comment on native_price_chain in config.py.
    price_chain = cfg.get("native_price_chain") or chain
    price_cfg = _chain_config(price_chain)
    price_address = price_cfg["native_price_address"]

    if price_address:
        data = await get_token_data(price_address, price_chain)
        if data and data["price_usd"] > 0:
            return data["price_usd"]

    logger.warning(
        "Could not fetch live %s price, using fallback of $%s",
        cfg["native_symbol"], cfg["fallback_native_price"],
    )
    return cfg["fallback_native_price"]


# Backwards-compatible alias - existing callers that only ever traded Solana
# can keep calling this.
async def get_sol_price() -> float:
    return await get_native_price("sol")
