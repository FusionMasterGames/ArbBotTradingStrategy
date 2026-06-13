import logging
import sys
from datetime import datetime, timezone

import config

logger = logging.getLogger("supabase_client")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_client = None
_unconfigured_logged = False


def get_client():
    """Lazily create and cache one Supabase client for the whole bot. Returns
    None if creds are missing (logged once) or the client can't be built —
    every caller must handle None and never let a failure propagate."""
    global _client, _unconfigured_logged
    if _client is not None:
        return _client
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        if not _unconfigured_logged:
            logger.error("Supabase not configured (set SUPABASE_URL and SUPABASE_KEY in .env), "
                         "persistence disabled")
            _unconfigured_logged = True
        return None
    try:
        from supabase import create_client
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    except Exception as e:
        logger.error("Failed to create Supabase client: %s", e)
        return None
    return _client


def log_trade_event(event_type: str, market: str | None = None, details: dict | None = None) -> None:
    """Fire-and-forget insert into trade_events. Any failure (no creds, network,
    schema) is logged and swallowed so the bot loop can never crash on it."""
    client = get_client()
    if client is None:
        return
    try:
        client.table("trade_events").insert({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "market": market,
            "details": details or {},
        }).execute()
    except Exception as e:
        logger.error("Failed to log trade_event '%s'%s: %s",
                     event_type, f" ({market})" if market else "", e)
