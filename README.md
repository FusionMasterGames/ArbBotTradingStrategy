# xyz-arb-bot

A funding-rate arbitrage bot for [Hyperliquid](https://hyperliquid.xyz). It scans all perp markets (native + HIP-3 builder DEXes like Trade.xyz, cash, km) for elevated funding rates, verifies a delta-neutral hedge is actually possible (a USDC spot pair whose price tracks the perp oracle), estimates entry costs and break-even time, and alerts via Telegram. With `AUTO_EXECUTE` enabled it opens the position itself: short the perp, long the spot, collect funding, exit when the rate decays or the max hold time is reached.

## How it works

```
main.py        — poll loop: scan -> alert -> (execute) -> accrue funding -> check exits -> summary
monitor.py     — opportunity filters, spot-hedge check, exit logic, position tracking, summaries
executor.py    — position sizing, entry cost estimation, order placement, rollback safety
hl_api.py      — Hyperliquid REST API layer (perps, HIP-3 DEXes, spot, account state)
alerts.py      — Telegram notifications
config.py      — all tunable parameters (secrets loaded from .env)
positions.json — open position state (do not edit while running)
trades.log     — full activity log
scan_report.py — standalone utility: current tradeable set with economics
```

A market is a **tradeable opportunity** when ALL of these pass:

1. `|funding rate| >= MIN_FUNDING_RATE` (%/hr)
2. `|mark vs oracle gap| <= MAX_MARK_ORACLE_GAP` (%)
3. `open interest >= MIN_OPEN_INTEREST` (USD)
4. A USDC spot pair exists **and** its price is within 5% of the perp oracle (guards against ticker-squatted tokens — spot TRUMP/USDC is not the TRUMP perp!)
5. At execution time: estimated break-even < 12 hours (fees + slippage + gap cost vs hourly funding yield)

## Setup

```bash
git clone <your-repo>
cd xyz-arb-bot
pip install -r requirements.txt
```

Create `.env` in the project root (never commit it — it is in `.gitignore`):

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-token
TELEGRAM_CHAT_ID=123456789
WALLET_ADDRESS=0xYourHyperliquidWallet
PRIVATE_KEY=0xYourPrivateKey
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-service-role-key
```

`WALLET_ADDRESS`/`PRIVATE_KEY` are only required for live trading. `SUPABASE_URL`/`SUPABASE_KEY` are only required for gap-history persistence (see below) — without them the bot still runs and logs everything to `trades.log`, it just skips the database insert. The bot runs fine in simulation mode with just the Telegram credentials.

### Set up Supabase gap-history persistence

The spot-hedge-blocked gap dataset is stored in Supabase so it survives redeploys (Railway's filesystem is ephemeral).

1. Create a project at [supabase.com](https://supabase.com)
2. Open **SQL Editor → New query**, paste the contents of [`supabase_schema.sql`](supabase_schema.sql), and run it to create the `gap_history` table
3. From **Project Settings → API**, copy the **Project URL** into `SUPABASE_URL` and a key into `SUPABASE_KEY` (the `service_role` key is simplest for a private backend bot — it bypasses Row Level Security)

Then `python gap_stats.py` reads the table and prints per-market gap variance stats.

### Get a Telegram bot token (BotFather)

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, choose a display name and a unique username ending in `bot`
3. BotFather replies with the bot token — put it in `.env` as `TELEGRAM_BOT_TOKEN`

### Find your Telegram chat ID

1. Send any message to your new bot (this is required — bots can't message you first)
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
3. Find `"chat":{"id":123456789,...}` in the JSON — that number is your `TELEGRAM_CHAT_ID`

## Running

```bash
python main.py          # the bot (simulation mode unless AUTO_EXECUTE = True)
python scan_report.py   # one-off: show current tradeable opportunities with economics
python monitor.py       # one-off: full market/filter diagnostic
```

## Test in simulation mode FIRST

`AUTO_EXECUTE = False` (the default) means **no orders are ever sent**. Every execution call logs `SIMULATION MODE — no orders placed` and records what it *would* have done in `trades.log`. Positions opened in simulation are tagged `"simulated": true` in `positions.json`.

Before ever flipping `AUTO_EXECUTE` to `True`:

1. Run the bot in simulation for at least a few days
2. Watch `trades.log` — confirm entries, exits, and funding accrual look right
3. Confirm Telegram alerts arrive (opportunity, execution, exit, errors, 6-hourly summary)
4. Check the rejection breakdown in the summaries to understand what the bot is passing on
5. For the first live run, lower `MAX_POSITION_SIZE_USD` to ~$15 and watch closely

The live order path additionally requires `WALLET_ADDRESS` and `PRIVATE_KEY` in `.env` and a funded Hyperliquid account.

## Deploy to Railway

1. Push this repo to GitHub. **Verify `.env`, `positions.json`, and `trades.log` are NOT committed** (`.gitignore` handles this — check with `git status`).
2. Go to [railway.app](https://railway.app) → New Project → **Deploy from GitHub repo** → select the repo.
3. In the service → **Variables**, add each secret from your `.env`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `WALLET_ADDRESS` (live trading only)
   - `PRIVATE_KEY` (live trading only)
   - `SUPABASE_URL` (gap-history persistence)
   - `SUPABASE_KEY` (gap-history persistence)
   Never commit these to the repo — Railway injects them as environment variables and `python-dotenv` falls through to real env vars when no `.env` file exists.
4. In **Settings → Deploy**, set the start command:
   ```
   python main.py
   ```
5. Deploy. Logs stream in the Railway dashboard; the bot also confirms startup via Telegram.

Note: Railway's filesystem is ephemeral — `positions.json` is lost on redeploy. Run in simulation until you're comfortable, and avoid redeploying while a live position is open (or add a volume / external store for `positions.json` first).

## Configuration (`config.py`)

| Parameter | Default | Meaning |
|---|---|---|
| `MIN_FUNDING_RATE` | `0.015` | Minimum \|funding rate\| in %/hr to flag an opportunity (0.015%/hr ≈ 131%/yr) |
| `MAX_MARK_ORACLE_GAP` | `2.0` | Maximum \|mark − oracle\| difference in % — a wide gap is a crowded, risky entry |
| `MIN_OPEN_INTEREST` | `50000` | Minimum market open interest in USD — liquidity floor |
| `AUTO_EXECUTE` | `False` | `False` = alert only, simulate orders. `True` = place real orders |
| `MAX_POSITION_SIZE_USD` | `200` | Hard cap per position. Also capped at 5% of market OI and 50% of balance across all positions |
| `LEVERAGE` | `2` | Perp leverage (isolated margin) |
| `EXIT_FUNDING_THRESHOLD` | `0.01` | Close the position when \|funding\| decays below this (%/hr) |
| `MAX_HOLD_HOURS` | `48` | Force-close after this many hours regardless of funding |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between scan cycles |
| `TELEGRAM_BOT_TOKEN` | from `.env` | Bot API token from BotFather |
| `TELEGRAM_CHAT_ID` | from `.env` | Your chat ID for alerts |
| `WALLET_ADDRESS` | from `.env` | Hyperliquid wallet (live trading + balance checks) |
| `PRIVATE_KEY` | from `.env` | Wallet private key (live trading only — keep it safe) |
| `SUPABASE_URL` | from `.env` | Supabase project URL for gap-history persistence |
| `SUPABASE_KEY` | from `.env` | Supabase API key (service_role recommended for a backend bot) |

Hardcoded safety constants in `executor.py`: taker fee estimate `0.035%` per leg, max break-even `12h`, position caps (5% OI / 50% balance), order slippage tolerance `1%`. In `monitor.py`: spot-hedge price divergence `5%`, gap-flip alert threshold `0.25%`, summary interval `6h`.

## Telegram alerts you'll receive

- 🤖 Startup confirmation with mode and config
- 🚨 Opportunity detected (rate, gap, OI, est. yield, break-even) — max once per 6h per market
- ✅ Position opened (both legs, sizes, entry price)
- 🏁 Position closed (funding collected, hold time, reason)
- ⚠️ Errors (failed legs, gap direction flips, loop crashes — throttled to one per 15 min)
- 📊 Position + rejection summary every 6 hours

## Safety properties

- No partial hedges: if the spot leg fails after the perp short fills, the perp is closed immediately and you're alerted
- Spot hedge is price-validated against the perp oracle (±5%) at scan AND entry time
- Funding accrual is time-scaled — poll frequency doesn't affect totals
- A gap direction flip on an open position triggers a manual-review alert (not an auto-close)
- All execution functions check `AUTO_EXECUTE` individually — simulation can't leak orders
