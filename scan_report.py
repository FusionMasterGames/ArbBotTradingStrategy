import config
from executor import calculate_breakeven_hours, estimate_entry_cost
from monitor import calculate_annualized_yield, scan_markets

opps = scan_markets()
print(f"TRADEABLE opportunities at MIN_FUNDING_RATE={config.MIN_FUNDING_RATE}: {len(opps)}\n")
for m in opps:
    size = config.MAX_POSITION_SIZE_USD
    costs = estimate_entry_cost(m, size)
    hourly = size * abs(m["funding_rate"]) / 100
    breakeven = calculate_breakeven_hours(hourly, costs["total"])
    print(f"{m['name']:>12}  funding {m['funding_rate']:+.5f}%/hr  "
          f"annualized {calculate_annualized_yield(m['funding_rate']):+.1f}%")
    print(f"{'':>12}  OI ${m['open_interest']:,.0f}  "
          f"hourly yield on ${size}: ${hourly:.4f}  "
          f"breakeven: {breakeven:.1f}h  (entry cost ${costs['total']:.2f})\n")
