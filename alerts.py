import asyncio
import logging
import sys
from datetime import datetime, timezone

from telegram import Bot
from telegram.error import TelegramError

import config
from monitor import calculate_annualized_yield, calculate_mark_oracle_gap
from supabase_client import log_trade_event

# Rough round-trip cost as % of notional: taker fee on open+close of both
# the perp and spot legs. Used only for the break-even estimate in alerts.
ROUND_TRIP_FEE_PCT = 0.23

logger = logging.getLogger("alerts")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _send(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env), alert not sent")
        return False

    async def _go():
        async with Bot(config.TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)

    try:
        asyncio.run(_go())
        return True
    except TelegramError as e:
        logger.error("Telegram send failed: %s", e)
        return False


def send_message(text: str) -> bool:
    return _send(text)


def send_alert(opportunity: dict) -> bool:
    gap = calculate_mark_oracle_gap(opportunity["mark_price"], opportunity["oracle_price"])
    annualized = calculate_annualized_yield(opportunity["funding_rate"])
    hourly_yield = config.MAX_POSITION_SIZE_USD * abs(opportunity["funding_rate"]) / 100
    fees = config.MAX_POSITION_SIZE_USD * ROUND_TRIP_FEE_PCT / 100
    breakeven_hours = fees / hourly_yield if hourly_yield > 0 else float("inf")

    message = (
        "\U0001f6a8 FUNDING RATE OPPORTUNITY\n"
        "\n"
        f"Market: {opportunity['name']}\n"
        f"Funding Rate: {opportunity['funding_rate']:.5f}% / hour ({annualized:.1f}% annualized)\n"
        f"Mark vs Oracle Gap: {gap:.3f}% (mark {'above' if gap >= 0 else 'below'} oracle)\n"
        f"Open Interest: ${opportunity['open_interest']:,.0f}\n"
        f"24h Volume: ${opportunity['volume_24h']:,.0f}\n"
        "\n"
        f"Est. hourly yield on ${config.MAX_POSITION_SIZE_USD}: ${hourly_yield:.2f}\n"
        f"Est. break-even time: {breakeven_hours:.1f} hours\n"
        "\n"
        f"AUTO_EXECUTE: {'ON' if config.AUTO_EXECUTE else 'OFF'}"
    )
    return _send(message)


def send_execution_alert(market_name: str, spot_size: float, perp_size: float, entry_price: float) -> bool:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    message = (
        "✅ POSITION OPENED (delta neutral)\n"
        "\n"
        f"Market: {market_name}\n"
        f"Spot size: {spot_size}\n"
        f"Perp size: {perp_size}\n"
        f"Entry price: {entry_price}\n"
        f"Time: {timestamp}"
    )
    return _send(message)


def send_exit_alert(market_name: str, funding_collected: float, hold_hours: float, exit_reason: str) -> bool:
    message = (
        "\U0001f3c1 POSITION CLOSED\n"
        "\n"
        f"Market: {market_name}\n"
        f"Funding collected: ${funding_collected:.2f}\n"
        f"Held for: {hold_hours:.1f} hours\n"
        f"Reason: {exit_reason}"
    )
    return _send(message)


def send_error_alert(error_message: str) -> bool:
    log_trade_event("error", None, {"message": error_message})
    return _send(f"⚠️ BOT ERROR\n\n{error_message}")


if __name__ == "__main__":
    mock_opportunity = {
        "name": "cash:CAR",
        "funding_rate": 0.14065,
        "mark_price": 191.0,
        "oracle_price": 186.87,
        "open_interest": 131939,
        "volume_24h": 291389,
    }
    sent = send_alert(mock_opportunity)
    print(f"send_alert returned: {sent}")
    if not sent:
        print("Not sent — check the logs. Most likely TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are empty in .env.")
