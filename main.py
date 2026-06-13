import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from alerts import send_alert, send_error_alert, send_message
from executor import enter_delta_neutral, get_account_balance, passes_execution_checks
from hl_api import fetch_all_markets
from monitor import (check_all_exits, load_positions, maybe_send_position_summary,
                     scan_markets, update_funding_collected)

# A persistent opportunity would otherwise re-alert every poll cycle
ALERT_COOLDOWN = timedelta(hours=6)
ERROR_ALERT_COOLDOWN = timedelta(minutes=15)

_alerted: dict[str, datetime] = {}
_last_error_alert: datetime | None = None

logger = logging.getLogger("main")
if not logger.handlers:
    _handler = logging.FileHandler(Path(__file__).parent / "trades.log")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def log_startup_message() -> None:
    mode = "LIVE TRADING" if config.AUTO_EXECUTE else "SIMULATION"
    logger.info(
        "Bot started [%s]: MIN_FUNDING_RATE=%s%%/hr, MAX_MARK_ORACLE_GAP=%s%%, "
        "MIN_OPEN_INTEREST=$%s, MAX_POSITION_SIZE_USD=$%s, LEVERAGE=%sx, "
        "EXIT_FUNDING_THRESHOLD=%s%%/hr, MAX_HOLD_HOURS=%s, POLL_INTERVAL_SECONDS=%s",
        mode, config.MIN_FUNDING_RATE, config.MAX_MARK_ORACLE_GAP,
        config.MIN_OPEN_INTEREST, config.MAX_POSITION_SIZE_USD, config.LEVERAGE,
        config.EXIT_FUNDING_THRESHOLD, config.MAX_HOLD_HOURS, config.POLL_INTERVAL_SECONDS,
    )
    print(f"xyz-arb-bot started in {mode} mode (poll every {config.POLL_INTERVAL_SECONDS}s)")


def send_alert_on_startup() -> None:
    mode = "⚠️ LIVE TRADING" if config.AUTO_EXECUTE else "SIMULATION (no real orders)"
    send_message(
        "\U0001f916 xyz-arb-bot is live\n"
        "\n"
        f"Mode: {mode}\n"
        f"Poll interval: {config.POLL_INTERVAL_SECONDS}s\n"
        f"Min funding: {config.MIN_FUNDING_RATE}%/hr\n"
        f"Max position: ${config.MAX_POSITION_SIZE_USD}\n"
        f"Max hold: {config.MAX_HOLD_HOURS}h"
    )


def _report_error(e: Exception) -> None:
    global _last_error_alert
    logger.exception("Main loop error: %s", e)
    now = datetime.now(timezone.utc)
    if _last_error_alert is None or now - _last_error_alert >= ERROR_ALERT_COOLDOWN:
        send_error_alert(f"Main loop error: {e}")
        _last_error_alert = now


def run_cycle() -> None:
    opportunities = scan_markets()
    positions = load_positions()
    now = datetime.now(timezone.utc)

    for opp in opportunities:
        name = opp["name"]
        if name in positions:
            continue

        last_alert = _alerted.get(name)
        if last_alert is None or now - last_alert >= ALERT_COOLDOWN:
            send_alert(opp)
            _alerted[name] = now

        if config.AUTO_EXECUTE:
            balance = get_account_balance(config.WALLET_ADDRESS)
            if passes_execution_checks(opp, balance):
                enter_delta_neutral(opp)

    funding_rates = {m["name"]: m["funding_rate"] for m in fetch_all_markets()}
    for market_name, position in load_positions().items():
        rate = funding_rates.get(market_name)
        if rate is not None:
            update_funding_collected(market_name, rate, position["size_usd"])

    check_all_exits()
    maybe_send_position_summary()

    print(f"[{now.strftime('%H:%M:%S')}] cycle done: {len(opportunities)} opportunities, "
          f"{len(load_positions())} open positions", flush=True)


def main(max_cycles: int | None = None) -> None:
    log_startup_message()
    send_alert_on_startup()

    cycles = 0
    while True:
        try:
            run_cycle()
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            print("Stopped.")
            return
        except Exception as e:
            _report_error(e)

        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            logger.info("Reached max_cycles=%d, exiting", max_cycles)
            return
        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
