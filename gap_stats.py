"""Gap variance report over the Supabase gap_history table (written by
monitor.scan_markets).

gap_history holds the spot-blocked markets — those that passed funding+gap+OI
but have no Hyperliquid spot hedge. That is exactly the cross-venue candidate
set, so this report does two things:

1. Per-market gap variance (avg/min/max |gap|, % tight). Stats are on |gap|
   because entry cost scales with magnitude; a signed mean shows direction bias.
2. Per-asset-class rollup (crypto / equity / commodity / fx / index / etf /
   basket) with the candidate hedge venue — to answer where the persistent,
   clean-gap, high-funding opportunity actually concentrates, and therefore
   which hedge integration (if any) is worth building first.

Run after the bot has accumulated samples:  python gap_stats.py
"""
import sys
from collections import defaultdict

import config
from asset_class import HEDGE_VENUE, classify

TIGHT_GAP_THRESHOLD = 0.2
PAGE_SIZE = 1000


def _fetch_all_rows() -> list[dict]:
    """Pull every row from gap_history, paging past PostgREST's 1000-row cap."""
    from supabase import create_client
    client = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
    rows: list[dict] = []
    start = 0
    while True:
        # order by the monotonic PK so paging stays stable while the bot
        # inserts concurrently — without it, OFFSET pages can skip/duplicate rows
        page = (client.table("gap_history")
                .select("market,oracle_gap,funding_rate")
                .order("id")
                .range(start, start + PAGE_SIZE - 1)
                .execute().data)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        print("Supabase not configured: set SUPABASE_URL and SUPABASE_KEY in .env.")
        return

    try:
        rows = _fetch_all_rows()
    except Exception as e:
        print(f"Failed to query Supabase gap_history: {e}")
        return

    # market -> list of (gap, funding) samples
    samples: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        try:
            samples[row["market"]].append((float(row["oracle_gap"]), float(row["funding_rate"])))
        except (KeyError, TypeError, ValueError):
            continue

    if not samples:
        print("gap_history has no usable rows yet. Run the bot (python main.py) to accumulate samples.")
        return

    total_samples = sum(len(s) for s in samples.values())
    print(f"Spot-blocked dataset: {total_samples} samples across {len(samples)} markets\n")

    # ---- per-asset-class rollup (the cross-venue decision view) ----
    class_markets: dict[str, set] = defaultdict(set)
    class_gaps: dict[str, list[float]] = defaultdict(list)
    class_fund: dict[str, list[float]] = defaultdict(list)
    for market, pts in samples.items():
        cls, _ = classify(market)
        class_markets[cls].add(market)
        class_gaps[cls].extend(abs(g) for g, _ in pts)
        class_fund[cls].extend(abs(f) for _, f in pts)

    print("Where the opportunity concentrates (by asset class):")
    print(f"{'Class':>10} {'Markets':>8} {'Samples':>8} {'Avg|gap|':>9} "
          f"{'<0.2%':>6} {'Avg|fund|':>10}  Hedge venue")
    for cls in sorted(class_markets, key=lambda c: len(class_gaps[c]), reverse=True):
        gaps, fund = class_gaps[cls], class_fund[cls]
        n = len(gaps)
        avg_gap = sum(gaps) / n
        tight = sum(1 for g in gaps if g < TIGHT_GAP_THRESHOLD) / n * 100
        avg_fund = sum(fund) / n
        print(f"{cls:>10} {len(class_markets[cls]):>8} {n:>8} {avg_gap:>8.3f}% "
              f"{tight:>5.0f}% {avg_fund:>9.4f}%  {HEDGE_VENUE[cls]}")

    # member markets for every non-crypto class, so heuristic miscategorisations
    # are visible and the classifier sets can be corrected
    print("\nMembers of each non-crypto class (audit the heuristic):")
    for cls in sorted(class_markets):
        if cls == "crypto":
            continue
        print(f"  {cls:>10}: {', '.join(sorted(class_markets[cls]))}")

    # ---- per-market gap variance ----
    print(f"\nPer-market gap variance (stats on |gap|; <{TIGHT_GAP_THRESHOLD}% = cheap-entry cycles):")
    print(f"{'Market':>16} {'Class':>10} {'Samples':>8} {'Mean':>8} {'Avg|g|':>8} "
          f"{'Min|g|':>8} {'Max|g|':>8} {'<' + str(TIGHT_GAP_THRESHOLD) + '%':>7}")
    for market, pts in sorted(samples.items(), key=lambda kv: len(kv[1]), reverse=True):
        gaps = [g for g, _ in pts]
        absg = [abs(g) for g in gaps]
        n = len(gaps)
        mean_signed = sum(gaps) / n
        tight_pct = sum(1 for g in absg if g < TIGHT_GAP_THRESHOLD) / n * 100
        cls, _ = classify(market)
        print(f"{market:>16} {cls:>10} {n:>8} {mean_signed:>+7.3f}% {sum(absg) / n:>7.3f}% "
              f"{min(absg):>7.3f}% {max(absg):>7.3f}% {tight_pct:>6.0f}%")


if __name__ == "__main__":
    main()
