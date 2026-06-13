-- Run once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Structured event log the bot writes to via supabase_client.log_trade_event.

create table if not exists public.trade_events (
    id          bigint generated always as identity primary key,
    timestamp   timestamptz not null default now(),
    -- one of: scan_summary, opportunity_detected, position_opened,
    --         position_closed, error
    -- (intentionally NOT a CHECK constraint: inserts are fire-and-forget and
    --  errors are swallowed, so a rejected row would be silently lost — better
    --  to store an unexpected type than to drop the event)
    event_type  text not null,
    market      text,                                   -- null for non-market events (scan_summary, error)
    details     jsonb not null default '{}'::jsonb      -- flexible per-event payload
);

-- Query patterns: filter by event_type (often newest-first), filter by market
-- (newest-first), and global newest-first. Composite indexes cover the leading
-- column for plain filtering AND the timestamp ordering in one structure.
create index if not exists trade_events_type_ts_idx   on public.trade_events (event_type, timestamp desc);
create index if not exists trade_events_market_ts_idx on public.trade_events (market, timestamp desc);
create index if not exists trade_events_ts_idx        on public.trade_events (timestamp desc);
