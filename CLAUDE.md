# Martin Gail — Polymarket BTC 5-Minute Up/Down Bot

## What this project is
A paper-trading Python bot that bets on the **direction of every upcoming
5-minute Bitcoin candle** on Polymarket using a **martingale** (double-on-loss)
bet-sizing strategy. The bot analyzes a window of recently-closed Binance
5-min BTC candles to predict whether the next 5-min window will close up or
down, then simulates a buy of the matching Polymarket UP/DOWN share. After the
window closes it settles the trade against the Binance close, updates P&L,
adjusts the bet size, and posts the result to Telegram.

It is **paper-only** today (no funded wallet, no real orders). State persists
in SQLite so the bot can be restarted without losing balance/streak.

## How a single trade cycle works
The loop is **pipelined** so the bot trades every 5-min window with no gaps.
At any time at most one trade is "pending settlement". Each iteration enters
the next window's trade just BEFORE that candle starts (while Polymarket
odds are still ~50/50), then settles the previous window's trade shortly
after its candle closes.

For each upcoming window `W` (Unix-time multiple of 300):
1. Sleep until `W − entry_lead_seconds` (default 10s before the candle starts —
   leaves room for order placement / acceptance latency so a limit order
   actually fills before odds drift). If the bot is slightly late, it still
   enters immediately as long as the candle hasn't started yet (`now < W`).
2. Fetch the last 30 closed 5-min BTCUSDT candles from Binance (the candle
   still in progress is INCLUDED — it carries the freshest data and is
   ~99% complete at entry time).
3. Run `strategy.predict_direction(candles)` → `UP` or `DOWN` + signal score.
4. Build the Polymarket slug `btc-updown-5m-{W}` and call gamma-api to pull
   the current UP / DOWN share prices. Fallback to `$0.50 / $0.50` on failure.
5. Compute bet size from martingale state (`base_shares × multiplier^step`,
   capped at `max_doubles`).
6. Record the trade as `open` in `data/trades.db` and notify Telegram.
7. Sleep until `prev_window + 300 + settlement_buffer_seconds` (settle the
   PREVIOUS window's trade, not the one we just opened).
8. Fetch the candle whose `open_time == prev_window` and decide outcome:
   `UP` wins when `close >= open`, else `DOWN` wins.
9. Update the trade row with outcome / P&L / new balance and notify Telegram.
10. Loop — the next iteration's step-1 is roughly 5 minutes later.

Timing example (entry_lead=10, settle_buffer=5):
```
T=W-10      open trade for window W
T=W+0       window W candle starts (we already hold the position)
T=W+290     open trade for window W+300
T=W+305     settle window W
T=W+590     open trade for window W+600
T=W+605     settle window W+300
…
```

PnL per trade:
- Win: `+shares × (1 − entry_price)` (each share pays $1, you paid `price`)
- Loss: `−shares × entry_price`

## Strategy (next-candle direction)
Inputs: last ~30 closed 5-min BTCUSDT candles (OHLCV from Binance public REST).

Signal score (positive → UP, negative → DOWN, 0 → follow last candle):
| Component | Weight | Notes |
|---|---|---|
| EMA(9) vs EMA(21) crossover | ±2 | Trend |
| Sum of last-3 candle bodies | ±2 | Short-term momentum |
| Last candle body color | ±1 | Continuation |
| Volume surge (last vol > 1.5× 20-period avg) | ±1 | Amplifies last-candle direction |
| Long wick reversal (wick > 2× body) | ±0.5 | Lower → bullish, upper → bearish |

Tunable in `bot/strategy.py`. Doesn't try to be clever — keeps the bot
deterministic and easy to reason about.

## Bet sizing (martingale)
- `base_shares` (default 5) on a fresh start / right after a win.
- After each **loss**, multiply by `bet_multiplier` (default 2):
  `5 → 10 → 20 → 40 → 80 → 160 → 320` …
- Cap at `max_doubles` (default 6) — anything past that, hold at the cap.
- Reset to base on the first **win**.
- Skip the trade if `shares × price` > current balance.

The risk is well-known (long losing streaks bankrupt martingale). The cap
limits exposure. With `max_doubles=6` and base=5 shares @ $0.50, the worst
single-trade stake is `5 × 2⁶ × 0.5 = $160`; the cumulative loss across the
7-step streak is `$2.5 + $5 + $10 + $20 + $40 + $80 + $160 = $317.5`, well
inside a $1000 bankroll.

## APIs
| Service | Auth | Used for | Endpoint |
|---|---|---|---|
| Binance | none | 5-min BTC klines | `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m` |
| Polymarket Gamma | none | Event-by-slug (UP/DOWN price) | `GET https://gamma-api.polymarket.com/events?slug={slug}` |
| Telegram Bot | bot token | Notifications | `POST https://api.telegram.org/bot{token}/sendMessage` |

WebSockets aren't used yet — trades are bounded by 5-min boundaries so polling
is sufficient and more robust. A WS upgrade (binance kline stream + polymarket
market socket) is a stretch goal for tighter entry timing and real trading.

## File layout
```
martin_gail/
├── CLAUDE.md                # This file
├── README.md                # Quick start
├── requirements.txt         # requests + python-dotenv (that's it)
├── config.example.json      # Strategy params
├── .env.example             # TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
├── .gitignore
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entry point, scheduler, trade cycle
│   ├── config.py            # Load config.json + .env
│   ├── utils.py             # window_ts, sleep_until, fmt_ts
│   ├── binance_client.py    # Fetch 5-min klines (REST)
│   ├── polymarket_client.py # Fetch event by slug, parse UP/DOWN prices
│   ├── strategy.py          # Trend analysis → UP/DOWN prediction
│   ├── martingale.py        # Bet sizing
│   ├── database.py          # SQLite: trades table + state KV
│   └── telegram_notifier.py # send_message via HTTP
└── data/
    └── trades.db            # Created on first run
```

## Database schema
- `trades` — one row per 5-min window. UNIQUE(window_ts) prevents duplicates
  on restart. Columns: `id, window_ts, slug, side, shares, price, cost,
  open_price, close_price, actual_outcome, pnl, balance_after, bet_step,
  score, reason, status (open|won|lost), opened_at, closed_at`.
- `state` — key/value KV. Today only `balance` lives here; streak is derived
  from the trades table (`get_streak` walks back from the latest resolved row).

## Run
```bash
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
# Edit .env — paste your TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
python -m bot.main
```

The bot starts, posts a "Bot started" summary to Telegram, waits for the next
5-min boundary, and begins trading. Ctrl+C to stop — state is safe on disk.

## Recovery on restart
On startup `main.py` calls `reconcile_open_trades()` which finds any rows left
in `status='open'` whose window has already closed and settles them using the
real Binance close. This makes the bot crash-safe.

## Stretch goals (NOT BUILT)
- Real trading via Polymarket CLOB (`py-clob-client`) with a funded wallet
- WebSocket price feeds (Binance kline socket + Polymarket market socket) for
  last-second entry timing à la the well-known reference bot
- Adaptive bet sizing (compute exact share count to cover prior loss given
  the current UP/DOWN price, instead of flat doubling)
- Multi-symbol (ETH/SOL 5-min markets alongside BTC)
- Web dashboard for trade history / equity curve

## Conventions in this codebase
- All timestamps are UTC seconds (`window_ts = int`). Binance kline ms-times
  are converted at the edge.
- No async — every API call is plain `requests` with a 10s timeout. The 5-min
  cadence makes threading/asyncio unnecessary.
- No third-party Binance/Polymarket/Telegram SDKs — raw HTTP keeps the dep
  surface tiny (`requests`, `python-dotenv`) and avoids version churn.
- Floats for money are fine at this scale; everything is rounded to 4 dp at
  the storage boundary.
