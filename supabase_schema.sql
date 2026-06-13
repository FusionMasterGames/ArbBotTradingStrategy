-- Run this once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).
-- Creates the gap_history table the bot writes to via monitor._record_gap_history.

create table if not exists public.gap_history (
    id           bigint generated always as identity primary key,
    timestamp    timestamptz  not null,
    market       text         not null,
    funding_rate double precision,
    oracle_gap   double precision
);

-- gap_stats.py groups by market; this keeps the per-market scan fast as the
-- table grows (~15-20k rows/day).
create index if not exists gap_history_market_idx on public.gap_history (market);

-- The bot uses the service_role or anon key from .env. If you keep Row Level
-- Security enabled (the Supabase default) with the anon key, add policies that
-- allow insert + select; using the service_role key bypasses RLS entirely.
-- Simplest for a private backend bot: use the service_role key in SUPABASE_KEY.
