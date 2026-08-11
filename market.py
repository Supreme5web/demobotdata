"""Codex.io (codex.io) GraphQL API integration for token price/market data.

Docs: https://docs.codex.io - GraphQL endpoint at https://graph.codex.io/graphql.
Requires an API key (CODEX_API_KEY in config.py), sent as the raw value of
the `Authorization` header (no "Bearer " prefix).

Multi-chain: every lookup here is keyed on (Codex networkId, token address),
where the networkId comes from CHAINS[chain]["codex_network_id"] (e.g.
1399811149 for Solana, 56 for BSC). We use the `filterTokens` query with an
exact-match `phrase` (the token address) scoped to a single network, which
returns Codex's already-aggregated, liquidity-weighted view of the token
(price, market cap, liquidity, volume) in a single call - no need to fetch
every pool and pick the best one ourselves like DexScreener required.

The bot's demo trading currency is USDC, which is pegged 1:1 to USD, so
unlike a chain's native gas token there's no price to fetch or convert -
trading.py just treats a USDC amount as an equal USD amount directly.

Field paths are read defensively with _dig() so a renamed/missing field
degrades to 0 instead of raising - this should never crash a trade.
"""

import logging
from typing import Optional

import httpx

from config import CHAINS, CODEX_API_KEY, DEFAULT_CHAIN

logger = logging.getLogger(__name__)

CODEX_GRAPHQL_URL = "https://graph.codex.io/graphql"

FILTER_TOKENS_QUERY = """
query FilterTokensByAddress($phrase: String, $network: [Int]) {
  filterTokens(phrase: $phrase, filters: { network: $network }, limit: 1) {
    results {
      priceUSD
      marketCap
      liquidity
      volume24
      change1
      token {
        name
        symbol
        address
        info {
          imageThumbUrl
        }
      }
    }
  }
}
"""


def _chain_cfg(chain: str) -> dict:
    return CHAINS.get(chain, CHAINS[DEFAULT_CHAIN])


def _dig(d: dict, *paths, default=0):
    """Tries each dotted path (e.g. 'token.info.imageThumbUrl') against `d`
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


async def _query_codex(token_address: str, chain: str) -> Optional[dict]:
    """POSTs the filterTokens query scoped to a single network and returns
    the first (and only) result dict, or None if the token isn't found /
    the request fails."""
    network_id = _chain_cfg(chain)["codex_network_id"]
    payload = {
        "query": FILTER_TOKENS_QUERY,
        "variables": {"phrase": token_address, "network": [network_id]},
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": CODEX_API_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(CODEX_GRAPHQL_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.error("Codex API request failed (%s/%s): %s", chain, token_address, exc)
        return None

    if data.get("errors"):
        logger.error("Codex API returned errors (%s/%s): %s", chain, token_address, data["errors"])
        return None

    results = _dig(data, "data.filterTokens.results", default=[])
    if not results:
        return None
    return results[0]


async def get_token_data(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[dict]:
    """Fetch token info from Codex for a given token address on the given
    chain (see CHAINS in config.py for supported chains). Returns None if
    the token can't be found / the request fails."""
    result = await _query_codex(token_address, chain)
    if not result:
        return None

    try:
        price_usd = float(_dig(result, "priceUSD", default=0) or 0)
    except (TypeError, ValueError):
        price_usd = 0.0

    market_cap = _dig(result, "marketCap", default=0)
    liquidity_usd = _dig(result, "liquidity", default=0)
    volume_24h = _dig(result, "volume24", default=0)
    price_change_1h = _dig(result, "change1", default=0)

    token = result.get("token") or {}

    return {
        "token_address": token_address,
        "chain": chain,
        "name": token.get("name") or "Unknown Token",
        "symbol": token.get("symbol") or "???",
        "price_usd": price_usd,
        "market_cap": market_cap or 0,
        "liquidity_usd": liquidity_usd or 0,
        "volume_24h": volume_24h or 0,
        "price_change_1h": price_change_1h or 0,
        "pair_address": "",
        "dex_url": "",
        "logo_url": _dig(token, "info.imageThumbUrl", default=""),
    }


async def get_current_price(token_address: str, chain: str = DEFAULT_CHAIN) -> Optional[float]:
    data = await get_token_data(token_address, chain)
    if data and data["price_usd"] > 0:
        return data["price_usd"]
    return None
