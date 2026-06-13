import logging
import sys

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
REQUEST_TIMEOUT = 10

logger = logging.getLogger("hyperliquid")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _post_info(payload: dict):
    try:
        resp = requests.post(INFO_URL, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.error("Hyperliquid info request failed (type=%s): %s", payload.get("type"), e)
        return None


def _parse_perp_markets(data) -> list[dict]:
    if not data:
        return []
    meta, ctxs = data
    markets = []
    for asset, ctx in zip(meta["universe"], ctxs):
        if asset.get("isDelisted"):
            continue
        try:
            mark_price = float(ctx["markPx"])
            markets.append({
                "name": asset["name"],
                # API returns funding as a decimal fraction per hour
                "funding_rate": float(ctx["funding"]) * 100,
                "mark_price": mark_price,
                "oracle_price": float(ctx["oraclePx"]),
                # openInterest is in coin units; convert to USD at mark
                "open_interest": float(ctx["openInterest"]) * mark_price,
                "volume_24h": float(ctx["dayNtlVlm"]),
            })
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Skipping perp market %s, bad data: %s", asset.get("name"), e)
    return markets


def fetch_native_markets() -> list[dict]:
    return _parse_perp_markets(_post_info({"type": "metaAndAssetCtxs"}))


def fetch_hip3_markets() -> list[dict]:
    # perpDexs returns [null, dex1, dex2, ...] — null is the native DEX.
    # perpDexStatus only returns totalNetDeposit, so per-DEX market data
    # comes from metaAndAssetCtxs with the "dex" parameter instead.
    dexs = _post_info({"type": "perpDexs"})
    if not dexs:
        return []
    markets = []
    for dex in dexs:
        if not dex or not dex.get("name"):
            continue
        dex_data = _post_info({"type": "metaAndAssetCtxs", "dex": dex["name"]})
        markets.extend(_parse_perp_markets(dex_data))
    return markets


def fetch_all_markets() -> list[dict]:
    return fetch_native_markets() + fetch_hip3_markets()


def fetch_spot_markets() -> list[dict]:
    data = _post_info({"type": "spotMetaAndAssetCtxs"})
    if not data:
        return []
    meta, ctxs = data
    token_names = {t["index"]: t["name"] for t in meta["tokens"]}
    # universe and ctxs are NOT index-aligned (universe has entries with no
    # ctx), so match on the ctx "coin" field instead of zip order
    ctx_by_coin = {c.get("coin"): c for c in ctxs}
    markets = []
    for pair in meta["universe"]:
        ctx = ctx_by_coin.get(pair["name"])
        if not ctx or ctx.get("markPx") is None:
            continue
        base, quote = pair["tokens"]
        markets.append({
            "name": f"{token_names[base]}/{token_names[quote]}",
            "price": float(ctx["markPx"]),
        })
    return markets


def get_open_positions(wallet_address: str) -> list[dict]:
    data = _post_info({"type": "clearinghouseState", "user": wallet_address})
    if not data:
        return []
    positions = []
    for ap in data.get("assetPositions", []):
        pos = ap["position"]
        positions.append({
            "name": pos["coin"],
            "size": float(pos["szi"]),
            "entry_price": float(pos["entryPx"]) if pos.get("entryPx") else None,
            "unrealized_pnl": float(pos["unrealizedPnl"]),
        })
    return positions


if __name__ == "__main__":
    from pprint import pprint

    from config import WALLET_ADDRESS

    native = fetch_native_markets()
    hip3 = fetch_hip3_markets()
    perps = fetch_all_markets()
    print(f"--- Perp markets: {len(perps)} total ({len(native)} native + {len(hip3)} HIP-3) ---")
    names = {m["name"] for m in perps}
    for expected in ["BTC", "ETH", "xyz:CAR", "xyz:SPCX"]:
        print(f"  {expected}: {'FOUND' if expected in names else 'MISSING'}")
    print("HIP-3 markets:")
    pprint(sorted(hip3, key=lambda m: abs(m["funding_rate"]), reverse=True)[:10])
    print("Top 10 overall by absolute funding rate (%/hr):")
    pprint(sorted(perps, key=lambda m: abs(m["funding_rate"]), reverse=True)[:10])

    spots = fetch_spot_markets()
    print(f"\n--- Spot markets: {len(spots)} ---")
    print("First 10:")
    pprint(spots[:10])

    wallet = WALLET_ADDRESS or "0x0000000000000000000000000000000000000000"
    print(f"\n--- Open positions for {wallet} ---")
    if not WALLET_ADDRESS:
        print("(WALLET_ADDRESS not set in .env — querying zero address as a smoke test)")
    pprint(get_open_positions(wallet))
