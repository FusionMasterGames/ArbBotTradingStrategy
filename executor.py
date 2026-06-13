import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from alerts import send_error_alert, send_execution_alert, send_exit_alert
from hl_api import _post_info, fetch_all_markets, fetch_spot_markets
from monitor import calculate_mark_oracle_gap, filter_opportunity
from supabase_client import log_trade_event

TAKER_FEE_RATE = 0.00035
MAX_BREAKEVEN_HOURS = 12
MAX_OI_FRACTION = 0.05
MAX_BALANCE_FRACTION = 0.50
DEFAULT_SLIPPAGE = 0.01

POSITIONS_FILE = Path(__file__).parent / "positions.json"

logger = logging.getLogger("executor")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# ---------------- sizing and cost estimation ----------------

def get_account_balance(wallet_address: str) -> float:
    data = _post_info({"type": "clearinghouseState", "user": wallet_address})
    if not data:
        return 0.0
    return float(data.get("withdrawable", 0.0))


def _read_positions() -> list[dict]:
    try:
        return json.loads(POSITIONS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def _write_positions(positions: list[dict]) -> None:
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2))


def _open_position_notional() -> float:
    return sum(float(p.get("size_usd", 0.0)) for p in _read_positions())


def calculate_position_size(market: dict, account_balance: float) -> float:
    candidates = {
        "MAX_POSITION_SIZE_USD": float(config.MAX_POSITION_SIZE_USD),
        "5% of open interest": MAX_OI_FRACTION * market["open_interest"],
        "50% of balance minus open positions": MAX_BALANCE_FRACTION * account_balance - _open_position_notional(),
    }
    binding, size = min(candidates.items(), key=lambda kv: kv[1])
    if size <= 0:
        logger.info("No safe position size for %s (binding constraint: %s)", market["name"], binding)
        return 0.0
    logger.info("Position size for %s: $%.2f (binding constraint: %s)", market["name"], size, binding)
    return size


def estimate_entry_cost(market: dict, position_size_usd: float) -> dict:
    gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    spot_fee = position_size_usd * TAKER_FEE_RATE
    perp_fee = position_size_usd * TAKER_FEE_RATE
    slippage = position_size_usd * (position_size_usd / market["open_interest"]) * 0.5
    gap_cost = position_size_usd * abs(gap / 100)
    return {
        "spot_fee": spot_fee,
        "perp_fee": perp_fee,
        "slippage": slippage,
        "gap_cost": gap_cost,
        "total": spot_fee + perp_fee + slippage + gap_cost,
    }


def calculate_breakeven_hours(hourly_yield: float, total_entry_cost: float) -> float:
    if hourly_yield <= 0:
        return float("inf")
    return total_entry_cost / hourly_yield


def passes_execution_checks(market: dict, account_balance: float) -> bool:
    size = calculate_position_size(market, account_balance)
    if size <= 0:
        logger.info("Execution check failed for %s: no safe position size", market["name"])
        return False

    costs = estimate_entry_cost(market, size)
    hourly_yield = size * abs(market["funding_rate"]) / 100
    breakeven = calculate_breakeven_hours(hourly_yield, costs["total"])
    if breakeven >= MAX_BREAKEVEN_HOURS:
        logger.info(
            "Execution check failed for %s: breakeven %.1fh >= %dh (entry cost $%.2f, hourly yield $%.4f)",
            market["name"], breakeven, MAX_BREAKEVEN_HOURS, costs["total"], hourly_yield,
        )
        return False

    if not filter_opportunity(market):
        logger.info("Execution check failed for %s: no longer passes opportunity filters", market["name"])
        return False

    logger.info(
        "Execution checks PASSED for %s: size $%.2f, entry cost $%.2f, breakeven %.1fh",
        market["name"], size, costs["total"], breakeven,
    )
    return True


# ---------------- order placement ----------------

_exchange = None


def _get_exchange():
    global _exchange
    if _exchange is None:
        if not config.WALLET_ADDRESS or not config.PRIVATE_KEY:
            raise RuntimeError("WALLET_ADDRESS and PRIVATE_KEY must be set in .env for live trading")
        from eth_account import Account
        from hyperliquid.exchange import Exchange
        from hyperliquid.utils import constants
        wallet = Account.from_key(config.PRIVATE_KEY)
        _exchange = Exchange(wallet, constants.MAINNET_API_URL, account_address=config.WALLET_ADDRESS)
    return _exchange


def _assert_ok(result, what: str) -> None:
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise RuntimeError(f"Order failed ({what}): {result}")
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    errors = [s["error"] for s in statuses if isinstance(s, dict) and "error" in s]
    if errors:
        raise RuntimeError(f"Order failed ({what}): {errors}")


def _get_market(market_name: str) -> dict:
    for m in fetch_all_markets():
        if m["name"] == market_name:
            return m
    raise ValueError(f"Unknown perp market: {market_name}")


def _sz_decimals(market_name: str) -> int:
    payload = {"type": "meta"}
    if ":" in market_name:
        payload["dex"] = market_name.split(":")[0]
    meta = _post_info(payload)
    if meta:
        for asset in meta["universe"]:
            if asset["name"] == market_name:
                return asset["szDecimals"]
    raise ValueError(f"Could not resolve szDecimals for {market_name}")


def _spot_pair(market_name: str) -> tuple[str, float]:
    """Map a perp name to its USDC spot pair; raises if Hyperliquid has no spot market for it."""
    base = market_name.split(":")[-1]
    pair = f"{base}/USDC"
    for m in fetch_spot_markets():
        if m["name"] == pair:
            return pair, m["price"]
    raise ValueError(f"No spot market {pair} on Hyperliquid — cannot hedge {market_name}")


def place_perp_short(market_name: str, size_usd: float, leverage: int) -> dict:
    market = _get_market(market_name)
    size_coins = size_usd / market["mark_price"]

    if not config.AUTO_EXECUTE:
        print(f"SIMULATION MODE — no orders placed (perp short {market_name})")
        logger.info(
            "SIMULATED perp short %s: sell %.6f coins (~$%.2f) at mark %.6g, %dx isolated",
            market_name, size_coins, size_usd, market["mark_price"], leverage,
        )
        return {"status": "simulated", "market": market_name, "side": "sell",
                "size_coins": size_coins, "size_usd": size_usd, "price": market["mark_price"]}

    size_coins = round(size_coins, _sz_decimals(market_name))
    exchange = _get_exchange()
    exchange.update_leverage(leverage, market_name, is_cross=False)
    result = exchange.market_open(market_name, False, size_coins, None, DEFAULT_SLIPPAGE)
    _assert_ok(result, f"perp short {market_name}")
    logger.info("LIVE perp short placed: %s %.6f coins (~$%.2f) %dx isolated", market_name, size_coins, size_usd, leverage)
    return result


def place_spot_long(market_name: str, size_usd: float) -> dict:
    pair, price = _spot_pair(market_name)
    # Catches wrong-pair lookups / stale prices before they become a bad hedge
    perp = _get_market(market_name)
    if abs(price - perp["oracle_price"]) / perp["oracle_price"] > 0.05:
        raise ValueError(
            f"Spot price {price} diverges >5% from perp oracle {perp['oracle_price']} "
            f"for {market_name} — refusing to hedge"
        )
    size_coins = size_usd / price

    if not config.AUTO_EXECUTE:
        print(f"SIMULATION MODE — no orders placed (spot long {pair})")
        logger.info("SIMULATED spot long %s: buy %.6f (~$%.2f) at %.6g", pair, size_coins, size_usd, price)
        return {"status": "simulated", "market": pair, "side": "buy",
                "size_coins": size_coins, "size_usd": size_usd, "price": price}

    exchange = _get_exchange()
    result = exchange.market_open(pair, True, size_coins, None, DEFAULT_SLIPPAGE)
    _assert_ok(result, f"spot long {pair}")
    logger.info("LIVE spot long placed: %s %.6f (~$%.2f)", pair, size_coins, size_usd)
    return result


def _close_perp(market_name: str) -> None:
    if not config.AUTO_EXECUTE:
        print(f"SIMULATION MODE — no orders placed (close perp {market_name})")
        logger.info("SIMULATED perp close %s", market_name)
        return
    _assert_ok(_get_exchange().market_close(market_name), f"perp close {market_name}")
    logger.info("LIVE perp close placed: %s", market_name)


def enter_delta_neutral(market: dict) -> dict | None:
    balance = get_account_balance(config.WALLET_ADDRESS) if config.WALLET_ADDRESS else 0.0
    if not config.AUTO_EXECUTE and balance <= 0:
        balance = 1000.0
        logger.info("Simulation: no wallet balance available, using mock $1000")

    size = calculate_position_size(market, balance)
    if size <= 0:
        logger.info("enter_delta_neutral aborted for %s: no safe position size", market["name"])
        return None
    if not passes_execution_checks(market, balance):
        logger.info("enter_delta_neutral aborted for %s: failed execution checks", market["name"])
        return None

    perp = place_perp_short(market["name"], size, config.LEVERAGE)
    try:
        spot = place_spot_long(market["name"], size)
    except Exception as e:
        logger.error("Spot leg failed for %s after perp short — closing perp immediately: %s", market["name"], e)
        try:
            _close_perp(market["name"])
            rollback_msg = "Perp short closed — no partial hedge left open."
        except Exception as close_err:
            rollback_msg = (f"CRITICAL: closing the perp short ALSO failed ({close_err}) — "
                            f"a naked perp short may be open. Intervene manually NOW.")
            logger.critical("Rollback failed for %s: %s", market["name"], close_err)
        send_error_alert(f"Spot leg failed for {market['name']} after perp short: {e}\n{rollback_msg}")
        return None

    now = datetime.now(timezone.utc)
    position = {
        "name": market["name"],
        "size_usd": size,
        "entry_time": now.isoformat(),
        "opened_at": now.isoformat(),
        "spot_size": spot["size_coins"] if "size_coins" in spot else size / market["mark_price"],
        "perp_size": -(perp["size_coins"] if "size_coins" in perp else size / market["mark_price"]),
        "entry_funding_rate": market["funding_rate"],
        "entry_mark_price": market["mark_price"],
        "entry_oracle_price": market["oracle_price"],
        "total_funding_collected": 0.0,
        "max_hold_until": (now + timedelta(hours=config.MAX_HOLD_HOURS)).isoformat(),
        "simulated": not config.AUTO_EXECUTE,
    }
    positions = _read_positions()
    positions.append(position)
    _write_positions(positions)
    logger.info(
        "ENTERED delta neutral %s%s: $%.2f, perp %.6f short / spot %.6f long, funding %.5f%%/hr, max hold until %s",
        market["name"], " (SIMULATED)" if position["simulated"] else "",
        size, abs(position["perp_size"]), position["spot_size"],
        market["funding_rate"], position["max_hold_until"],
    )
    send_execution_alert(market["name"], position["spot_size"], position["perp_size"], market["mark_price"])
    log_trade_event("position_opened", market["name"], {
        "size_usd": size,
        "spot_size": position["spot_size"],
        "perp_size": position["perp_size"],
        "entry_price": market["mark_price"],
        "entry_funding_rate": market["funding_rate"],
        "simulated": position["simulated"],
    })
    return position


def close_delta_neutral(market_name: str, exit_reason: str) -> dict | None:
    positions = _read_positions()
    position = next((p for p in positions if p["name"] == market_name), None)
    if position is None:
        logger.error("close_delta_neutral: no tracked position for %s", market_name)
        return None

    if not config.AUTO_EXECUTE:
        print(f"SIMULATION MODE — no orders placed (close delta neutral {market_name})")
        logger.info(
            "SIMULATED close %s: buy back %.6f perp, sell %.6f spot",
            market_name, abs(position["perp_size"]), position["spot_size"],
        )
    else:
        _close_perp(market_name)
        pair, _ = _spot_pair(market_name)
        result = _get_exchange().market_open(pair, False, position["spot_size"], None, DEFAULT_SLIPPAGE)
        _assert_ok(result, f"spot close {pair}")
        logger.info("LIVE spot close placed: %s %.6f", pair, position["spot_size"])

    now = datetime.now(timezone.utc)
    entry_time = datetime.fromisoformat(position["entry_time"])
    hold_hours = (now - entry_time).total_seconds() / 3600
    # total_funding_collected is kept current by update_funding_collected();
    # only the residual since the last update is estimated here (at entry rate)
    # to avoid double-counting.
    last_update = datetime.fromisoformat(position.get("last_updated") or position["entry_time"])
    residual_hours = max(0.0, (now - last_update).total_seconds() / 3600)
    funding_collected = (position.get("total_funding_collected", 0.0)
                         + position["size_usd"] * abs(position["entry_funding_rate"]) / 100 * residual_hours)

    _write_positions([p for p in positions if p["name"] != market_name])
    logger.info(
        "CLOSED %s%s after %.1fh (%s): est. funding collected $%.2f",
        market_name, " (SIMULATED)" if not config.AUTO_EXECUTE else "",
        hold_hours, exit_reason, funding_collected,
    )
    send_exit_alert(market_name, funding_collected, hold_hours, exit_reason)
    log_trade_event("position_closed", market_name, {
        "funding_collected": round(funding_collected, 4),
        "hold_hours": round(hold_hours, 2),
        "exit_reason": exit_reason,
        "simulated": not config.AUTO_EXECUTE,
    })
    return {"name": market_name, "funding_collected": funding_collected,
            "hold_hours": hold_hours, "exit_reason": exit_reason}


if __name__ == "__main__":
    assert not config.AUTO_EXECUTE, "Run this test with AUTO_EXECUTE = False"

    if config.WALLET_ADDRESS:
        balance = get_account_balance(config.WALLET_ADDRESS)
        print(f"Account balance (withdrawable): ${balance:,.2f}")
    else:
        balance = 1000.0
        print("WALLET_ADDRESS not set in .env — using mock $1,000 balance")

    markets = {m["name"]: m for m in fetch_all_markets()}

    print("\n========== SIM TEST 1: km:USOIL (live opportunity, no spot market) ==========")
    print("Expect: checks pass, perp short simulated, spot leg fails, perp rolled back, error alert sent")
    result = enter_delta_neutral(markets["km:USOIL"])
    print(f"returned: {result}")
    print(f"positions.json after: {_read_positions()}")

    print("\n========== SIM TEST 2: mock HYPE @ 0.06%/hr (has real spot pair) ==========")
    print("Expect: full success path — both legs simulated, position written, execution alert sent")
    mock = dict(markets["HYPE"])
    mock["funding_rate"] = 0.06
    result = enter_delta_neutral(mock)
    print(f"position written: {result is not None}")
    print(f"positions.json after enter: {json.dumps(_read_positions(), indent=2)}")

    print("\n========== SIM TEST 3: close_delta_neutral(HYPE) ==========")
    closed = close_delta_neutral("HYPE", "simulation lifecycle test")
    print(f"closed: {closed}")
    print(f"positions.json after close: {_read_positions()}")
