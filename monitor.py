import logging
import sys
from datetime import datetime, timedelta, timezone

import config
from hl_api import fetch_all_markets, fetch_spot_markets
from supabase_client import get_client, log_trade_event

SPOT_HEDGE_MAX_DIVERGENCE = 0.05
GAP_FLIP_ALERT_PCT = 0.25
SUMMARY_INTERVAL_HOURS = 6

REJECT_FUNDING = "Failed funding threshold"
REJECT_GAP = "Failed gap filter"
REJECT_OI = "Failed open interest filter"
REJECT_SPOT = "Failed spot hedge check"
REJECT_BREAKEVEN = "Failed breakeven check"
REJECTION_REASONS = [REJECT_FUNDING, REJECT_GAP, REJECT_OI, REJECT_SPOT, REJECT_BREAKEVEN]

_rejection_tracker: dict[str, set] = {r: set() for r in REJECTION_REASONS}
_last_summary_time: datetime | None = None

logger = logging.getLogger("monitor")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def calculate_mark_oracle_gap(mark_price: float, oracle_price: float) -> float:
    return (mark_price - oracle_price) / oracle_price * 100


def calculate_annualized_yield(funding_rate_per_hour: float) -> float:
    return funding_rate_per_hour * 24 * 365


def filter_opportunity(market: dict) -> bool:
    gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    return (
        abs(market["funding_rate"]) >= config.MIN_FUNDING_RATE
        and abs(gap) <= config.MAX_MARK_ORACLE_GAP
        and market["open_interest"] >= config.MIN_OPEN_INTEREST
    )


def has_spot_market(market: dict, spot_prices: dict[str, float] | None = None) -> bool:
    """True if Hyperliquid has a USDC spot pair whose price tracks this perp's oracle.

    A name match alone is not enough — tickers collide (spot TRUMP/USDC is a
    different asset than the TRUMP perp), so the spot price must be within
    SPOT_HEDGE_MAX_DIVERGENCE of the perp oracle to count as a hedge.
    Pass spot_prices (name -> price, from fetch_spot_markets) to avoid an API
    call per market.
    """
    if spot_prices is None:
        spot_prices = {m["name"]: m["price"] for m in fetch_spot_markets()}
    price = spot_prices.get(f"{market['name'].split(':')[-1]}/USDC")
    if price is None:
        return False
    return abs(price - market["oracle_price"]) / market["oracle_price"] <= SPOT_HEDGE_MAX_DIVERGENCE


def _classify_rejection(market: dict, spot_prices: dict[str, float]) -> str | None:
    if abs(market["funding_rate"]) < config.MIN_FUNDING_RATE:
        return REJECT_FUNDING
    gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    if abs(gap) > config.MAX_MARK_ORACLE_GAP:
        return REJECT_GAP
    if market["open_interest"] < config.MIN_OPEN_INTEREST:
        return REJECT_OI
    if not has_spot_market(market, spot_prices):
        return REJECT_SPOT
    from executor import MAX_BREAKEVEN_HOURS, calculate_breakeven_hours, estimate_entry_cost
    costs = estimate_entry_cost(market, config.MAX_POSITION_SIZE_USD)
    hourly = config.MAX_POSITION_SIZE_USD * abs(market["funding_rate"]) / 100
    if calculate_breakeven_hours(hourly, costs["total"]) >= MAX_BREAKEVEN_HOURS:
        return REJECT_BREAKEVEN
    return None


def _format_usd_compact(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:.0f}K"
    return f"${value:.0f}"


def _record_gap_history(timestamp: str, market: dict, gap: float) -> None:
    """Insert one row per spot-blocked market per cycle into the Supabase
    gap_history table, building a gap variance dataset (see gap_stats.py).
    Any failure is logged and swallowed — this must never crash the bot loop."""
    client = get_client()
    if client is None:
        return
    try:
        client.table("gap_history").insert({
            "timestamp": timestamp,
            "market": market["name"],
            "funding_rate": round(market["funding_rate"], 5),
            "oracle_gap": round(gap, 4),
        }).execute()
    except Exception as e:
        logger.error("Failed to insert gap history for %s: %s", market["name"], e)


def _log_opportunity_detected(market: dict) -> None:
    """Emit an opportunity_detected event with full economics for a market that
    passed every filter (funding, gap, OI, spot hedge, breakeven)."""
    from executor import calculate_breakeven_hours, estimate_entry_cost
    size = config.MAX_POSITION_SIZE_USD
    costs = estimate_entry_cost(market, size)
    hourly = size * abs(market["funding_rate"]) / 100
    breakeven = calculate_breakeven_hours(hourly, costs["total"])
    gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    log_trade_event("opportunity_detected", market["name"], {
        "funding_rate": round(market["funding_rate"], 5),
        "annualized_yield": round(calculate_annualized_yield(market["funding_rate"]), 1),
        "gap": round(gap, 4),
        "open_interest": round(market["open_interest"], 2),
        "hourly_yield_usd": round(hourly, 4),
        "entry_cost_usd": round(costs["total"], 4),
        "breakeven_hours": round(breakeven, 2),
    })


def _log_spot_blocked(market: dict) -> None:
    """Log full would-be economics for markets blocked only by the missing
    spot hedge, so consistently-clean blocked trades show up over time."""
    from executor import calculate_breakeven_hours, estimate_entry_cost
    size = config.MAX_POSITION_SIZE_USD
    costs = estimate_entry_cost(market, size)
    hourly = size * abs(market["funding_rate"]) / 100
    breakeven = calculate_breakeven_hours(hourly, costs["total"])
    gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    logger.info(
        "Spot-blocked: %s | %+.4f%%/hr | %+.0f%% ann | gap %+.2f%% | OI %s | "
        "yield $%.2f/hr | cost $%.2f | breakeven %.1fh",
        market["name"], market["funding_rate"],
        calculate_annualized_yield(market["funding_rate"]), gap,
        _format_usd_compact(market["open_interest"]), hourly, costs["total"], breakeven,
    )
    _record_gap_history(datetime.now(timezone.utc).isoformat(), market, gap)


def scan_markets() -> list[dict]:
    markets = fetch_all_markets()
    spot_prices = {m["name"]: m["price"] for m in fetch_spot_markets()}
    opportunities = []
    clean = 0
    cycle_rejections = {r: 0 for r in REJECTION_REASONS}
    for m in markets:
        reason = _classify_rejection(m, spot_prices)
        if reason is not None:
            cycle_rejections[reason] += 1
            _rejection_tracker[reason].add(m["name"])
            if reason == REJECT_SPOT:
                _log_spot_blocked(m)
        else:
            clean += 1
            _log_opportunity_detected(m)
        # Breakeven is enforced (and logged) at execution time — keep
        # returning those markets so the executor makes the final call.
        if reason is None or reason == REJECT_BREAKEVEN:
            opportunities.append(m)
    opportunities.sort(key=lambda m: m["funding_rate"], reverse=True)
    logger.info("Scanned %d markets, found %d hedgeable opportunities", len(markets), len(opportunities))
    log_trade_event("scan_summary", None, {
        "markets_scanned": len(markets),
        "opportunities": clean,
        "returned_to_executor": len(opportunities),
        "rejections": cycle_rejections,
    })
    return opportunities


# ---------------- position tracking and exits ----------------

def load_positions() -> dict[str, dict]:
    from executor import _read_positions
    return {p["name"]: p for p in _read_positions()}


def update_funding_collected(market_name: str, funding_rate: float, position_size_usd: float) -> float:
    """Accrue funding since the last update. Funding is %/hr, so the amount
    is scaled by elapsed time — calling more often does not over-count."""
    from executor import _read_positions, _write_positions
    positions = _read_positions()
    now = datetime.now(timezone.utc)
    earned = 0.0
    for p in positions:
        if p["name"] == market_name:
            last = datetime.fromisoformat(p.get("last_updated") or p["entry_time"])
            elapsed_hours = max(0.0, (now - last).total_seconds() / 3600)
            earned = abs(funding_rate) / 100 * position_size_usd * elapsed_hours
            p["total_funding_collected"] = p.get("total_funding_collected", 0.0) + earned
            p["last_updated"] = now.isoformat()
    _write_positions(positions)
    return earned


def _update_position(market_name: str, updates: dict) -> None:
    from executor import _read_positions, _write_positions
    positions = _read_positions()
    for p in positions:
        if p["name"] == market_name:
            p.update(updates)
    _write_positions(positions)


def _check_gap_flip(position: dict, market: dict) -> None:
    if position.get("gap_flip_alerted"):
        return
    if "entry_mark_price" not in position or "entry_oracle_price" not in position:
        return
    entry_gap = calculate_mark_oracle_gap(position["entry_mark_price"], position["entry_oracle_price"])
    current_gap = calculate_mark_oracle_gap(market["mark_price"], market["oracle_price"])
    if entry_gap * current_gap < 0 and abs(current_gap) >= GAP_FLIP_ALERT_PCT:
        from alerts import send_error_alert
        logger.warning(
            "Gap direction flipped for %s: %+.3f%% at entry -> %+.3f%% now — flagged for manual review",
            position["name"], entry_gap, current_gap,
        )
        send_error_alert(
            f"Gap direction flipped for {position['name']}: "
            f"{entry_gap:+.3f}% at entry -> {current_gap:+.3f}% now.\n"
            f"Position NOT auto-closed — review manually."
        )
        _update_position(position["name"], {"gap_flip_alerted": True})


def get_exit_reason(position: dict) -> str | None:
    markets = {m["name"]: m for m in fetch_all_markets()}
    market = markets.get(position["name"])
    if market is not None:
        _check_gap_flip(position, market)
        if abs(market["funding_rate"]) < config.EXIT_FUNDING_THRESHOLD:
            reason = (f"funding {market['funding_rate']:+.5f}%/hr below "
                      f"exit threshold {config.EXIT_FUNDING_THRESHOLD}%/hr")
            logger.info("Exit signal for %s: %s", position["name"], reason)
            return reason
    opened_at = datetime.fromisoformat(position["opened_at"])
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    hours_open = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600
    if hours_open > config.MAX_HOLD_HOURS:
        reason = f"held {hours_open:.1f}h, max is {config.MAX_HOLD_HOURS}h"
        logger.info("Exit signal for %s: %s", position["name"], reason)
        return reason
    return None


def should_exit(position: dict) -> bool:
    return get_exit_reason(position) is not None


def check_all_exits() -> list[dict]:
    from executor import close_delta_neutral
    closed = []
    for name, position in load_positions().items():
        reason = get_exit_reason(position)
        if reason:
            logger.info("check_all_exits: closing %s (%s)", name, reason)
            result = close_delta_neutral(name, reason)
            if result:
                closed.append(result)
        else:
            logger.info("check_all_exits: holding %s", name)
    return closed


def generate_position_summary() -> str:
    from executor import get_account_balance
    positions = load_positions()
    markets = {m["name"]: m for m in fetch_all_markets()}
    now = datetime.now(timezone.utc)

    lines = ["\U0001f4ca OPEN POSITIONS SUMMARY", ""]
    total_funding = 0.0
    total_notional = 0.0
    for name, p in positions.items():
        entry = datetime.fromisoformat(p["entry_time"])
        hold_hours = (now - entry).total_seconds() / 3600
        current = markets.get(name)
        rate_str = f"{current['funding_rate']:+.4f}%/hr" if current else "unknown"
        total_funding += p.get("total_funding_collected", 0.0)
        total_notional += p.get("size_usd", 0.0)
        lines += [
            f"Market: {name}",
            f"Entry: {entry.strftime('%Y-%m-%d %H:%M UTC')}",
            f"Hold time: {hold_hours:.1f} hours",
            f"Funding collected: ${p.get('total_funding_collected', 0.0):.2f}",
            f"Current funding rate: {rate_str}",
            f"Exit trigger: rate < {config.EXIT_FUNDING_THRESHOLD}% OR after {config.MAX_HOLD_HOURS}hrs",
            "",
        ]
    if not positions:
        lines += ["No open positions.", ""]

    lines.append(f"Total funding collected today: ${total_funding:.2f}")
    lines.append(f"Active positions: {len(positions)}")
    balance = get_account_balance(config.WALLET_ADDRESS) if config.WALLET_ADDRESS else 0.0
    if balance > 0:
        lines.append(f"Account utilization: {total_notional / balance * 100:.0f}%")
    else:
        lines.append("Account utilization: n/a (no wallet balance)")

    lines.append("")
    lines.append("Rejected since last summary:")
    for reason in REJECTION_REASONS:
        names = _rejection_tracker[reason]
        detail = f" ({', '.join(sorted(names))})" if names and len(names) <= 6 else ""
        lines.append(f"- {reason}: {len(names)}{detail}")
    return "\n".join(lines)


def maybe_send_position_summary(force: bool = False) -> bool:
    """Send the summary via Telegram at most every SUMMARY_INTERVAL_HOURS.
    Call once per poll cycle from the main loop. Rejection counters reset
    after each successful send."""
    global _last_summary_time
    now = datetime.now(timezone.utc)
    if not force and _last_summary_time is not None and \
            now - _last_summary_time < timedelta(hours=SUMMARY_INTERVAL_HOURS):
        return False
    from alerts import send_message
    sent = send_message(generate_position_summary())
    if sent:
        _last_summary_time = now
        for names in _rejection_tracker.values():
            names.clear()
        logger.info("Position summary sent")
    else:
        logger.error("Position summary send failed")
    return sent


if __name__ == "__main__":
    def _row(m):
        gap = calculate_mark_oracle_gap(m["mark_price"], m["oracle_price"])
        return (f"{m['name']:>14}  funding {m['funding_rate']:+.5f}%/hr "
                f"({calculate_annualized_yield(m['funding_rate']):+.1f}%/yr)  "
                f"gap {gap:+.3f}%  OI ${m['open_interest']:,.0f}")

    markets = fetch_all_markets()
    spot_prices = {m["name"]: m["price"] for m in fetch_spot_markets()}
    hedgeable = [m for m in markets if has_spot_market(m, spot_prices)]
    filtered = [m for m in markets if filter_opportunity(m)]
    tradeable = sorted((m for m in filtered if has_spot_market(m, spot_prices)),
                       key=lambda m: m["funding_rate"], reverse=True)

    print(f"Markets: {len(markets)} total, {len(hedgeable)} hedgeable (have USDC spot pair)")
    print(f"(filters: |funding| >= {config.MIN_FUNDING_RATE}%/hr, "
          f"|gap| <= {config.MAX_MARK_ORACLE_GAP}%, OI >= ${config.MIN_OPEN_INTEREST:,}, spot pair exists)")

    print(f"\n--- TRADEABLE opportunities (all filters + spot check): {len(tradeable)} ---")
    for m in tradeable:
        print(_row(m))

    unhedgeable = [m for m in filtered if not has_spot_market(m, spot_prices)]
    print(f"\n--- Pass financial filters but NO spot market (excluded): {len(unhedgeable)} ---")
    for m in sorted(unhedgeable, key=lambda m: abs(m["funding_rate"]), reverse=True):
        print(_row(m))

    print("\n--- Top 10 hedgeable by |funding| (for context, unfiltered) ---")
    for m in sorted(hedgeable, key=lambda m: abs(m["funding_rate"]), reverse=True)[:10]:
        passes = "PASS" if filter_opportunity(m) else "fail"
        print(f"{_row(m)}  [{passes}]")
