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

## Architecture: two bots in parallel
The project runs **two independent bots** in one process, one thread each.
Together they cover every 5-min window; individually each still trades on a
10-min cadence so its previous bet has settled before sizing the next.

| Bot | Windows traded | DB file | Telegram env vars |
|---|---|---|---|
| **A** (phase 0)   | :00, :10, :20, :30, :40, :50 | `data/trades_a.db` | `TELEGRAM_BOT_TOKEN_A` / `TELEGRAM_CHAT_ID_A` |
| **B** (phase 300) | :05, :15, :25, :35, :45, :55 | `data/trades_b.db` | `TELEGRAM_BOT_TOKEN_B` / `TELEGRAM_CHAT_ID_B` |

Each bot has its own balance, streak, martingale state, and trade history —
no shared mutable state between them. The bot list lives in `config.json`
under `bots[]`; shared params (martingale, timing) sit at the top level and
each bot inherits them. Spawned via `threading.Thread` from `bot.main.main()`;
Ctrl+C sets a shared `stop_event` and both threads finish their current
iteration then send a "Bot Stopped" Telegram message before exiting.

## How a single trade cycle works
Each bot trades windows aligned to its phase: `(W − phase) % 600 == 0`.
For phase=0 that's :00/:10/:20/…; for phase=300 it's :05/:15/:25/….
Both bots are 10 minutes between their *own* trades.

This spacing is deliberate. With back-to-back 5-min trades inside a single
bot, the bot would size bet N+1 ~10s before bet N's candle even closed —
meaning bet N+1's martingale step would be based on a stale streak. By
skipping the in-between window for each bot, every new entry happens AFTER
that bot's previous trade has fully settled, so the martingale step is
always based on a known win/loss outcome. (Bot B's in-between window is
bot A's trade window, and vice versa — they don't share state, so neither
pollutes the other's streak.)

Each bot's loop is **sequential** (not pipelined): at any time at most one
trade is open per bot. Each iteration first settles the previous trade,
then enters the next one in that bot's next phase-aligned slot.

For each trade window `W` (Unix-time multiple of 600):
1. *(start of iteration: previous trade for window `W−600` is open)*
   Sleep until `W − 600 + 300 + settlement_buffer_seconds`, then **wait
   for Polymarket** to declare the winner via `bot/polymarket_resolver.py`
   (WebSocket primary, gamma-api HTTP fallback). The Binance candle is
   not used for settlement — Polymarket is the single source of truth.
   - Inside `resolution_budget_seconds` (default 240s): settle, then
     stay on the planned 10-min cadence and enter the next window.
   - On overrun: send a "skipping next round" Telegram message and keep
     waiting indefinitely. When resolution finally arrives, settle and
     re-enter at the next live 10-min boundary.
2. Sleep until `W − entry_lead_seconds` (default 10s before the candle
   starts — leaves room for order placement / acceptance latency so a
   limit order actually fills before odds drift). If slightly late, enter
   immediately as long as the candle hasn't started yet (`now < W`).
3. Fetch the last 30 5-min BTCUSDT candles from Binance (the candle still
   in progress is INCLUDED — it carries the freshest data and is ~99%
   complete at entry time).
4. Run `strategy.predict_direction(candles)` → `UP` or `DOWN` + score.
5. Build the Polymarket slug `btc-updown-5m-{W}` and call gamma-api for
   the current UP / DOWN share prices. Fallback to `$0.50 / $0.50` on
   failure.
6. Compute bet size from martingale state (`base_shares × multiplier^step`,
   capped at `max_doubles`). Streak is read from the DB and is now
   guaranteed correct because step 1 already settled the previous trade.
7. Record the trade as `open` in `data/trades.db` and notify Telegram.
8. Loop — the next iteration's step-1 fires ~5 minutes later, when this
   trade's candle closes.

Timing example (entry_lead=10, settle_buffer=5, resolution_budget=240):
```
T=W-10      open trade for window W
T=W+0       window W candle starts
T=W+300     window W candle closes
T=W+305     begin waiting on Polymarket
T=W+305-545 Polymarket resolves (typical) → settle, on-schedule
T=W+545     budget elapsed → notify "skipping next round", keep waiting
T=W+590     open trade for window W+600                ← only if Polymarket already resolved
T=…         on overrun, the next entry is the next 10-min boundary >= now
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
| Binance | none | 5-min BTC klines (signal + display) | `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m` |
| Polymarket Gamma | none | Event-by-slug (UP/DOWN price, clobTokenIds, resolution check) | `GET https://gamma-api.polymarket.com/events?slug={slug}` |
| Polymarket Market WS | none | `market_resolved` push events (winner) | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Telegram Bot | bot token | Notifications | `POST https://api.telegram.org/bot{token}/sendMessage` |

The Polymarket market WS is the primary settlement channel — subscribed with
`custom_feature_enabled=true` so the server emits `market_resolved` events the
moment the price oracle posts. The gamma-api HTTP endpoint is polled as a
fallback (primer before WS connect; every 15s while connected) in case the WS
drops or we subscribed after resolution fired.

## File layout
```
martin_gail/
├── CLAUDE.md                # This file
├── README.md                # Quick start
├── requirements.txt         # requests + python-dotenv + websocket-client
├── config.example.json      # Shared params + bots[] array
├── .env.example             # 4 telegram vars (one pair per bot)
├── .gitignore
├── bot/
│   ├── __init__.py
│   ├── main.py              # Entry point, thread per bot, scheduler, trade cycle
│   ├── config.py            # Load config.json → list[bot_cfg]
│   ├── utils.py             # window_ts, sleep_until, fmt_ts
│   ├── binance_client.py    # Fetch 5-min klines (REST)
│   ├── polymarket_client.py # Fetch event by slug, parse UP/DOWN prices, parse resolution
│   ├── polymarket_resolver.py # Wait for winner via WS (primary) + HTTP (fallback)
│   ├── strategy.py          # Trend-following UP/DOWN prediction
│   ├── martingale.py        # Bet sizing
│   ├── database.py          # SQLite: trades table + state KV
│   └── telegram_notifier.py # send_message via HTTP, optional prefix
├── scripts/
│   ├── export_db.py
│   └── backtest_new_strategy.py
└── data/
    ├── trades_a.db          # Bot A history (created on first run)
    └── trades_b.db          # Bot B history (created on first run)
```

## Database schema
Each bot has its own SQLite file (no `bot_id` column — physical isolation).
- `trades` — one row per traded window. UNIQUE(window_ts) prevents duplicates
  on restart. Columns: `id, window_ts, slug, side, shares, price, cost,
  open_price, window_high, window_low, close_price, actual_outcome, pnl,
  balance_after, bet_step, score, reason, up_token_id, down_token_id,
  resolved_at, status (open|won|lost), opened_at, closed_at`.
  `actual_outcome` is the Polymarket-declared winner (`UP`/`DOWN`);
  `close_price` is the Binance close at the same moment, kept for display
  /audit only.
- `state` — key/value KV. Today only `balance` lives here; streak is derived
  from the trades table (`get_streak` walks back from the latest resolved row).

## Run
```bash
pip install -r requirements.txt
cp config.example.json config.json
cp .env.example .env
# Edit .env — paste 4 vars: TELEGRAM_BOT_TOKEN_A/CHAT_ID_A and _B/_B
python -m bot.main
```

`bot.main.main()` loads `config.json`, spawns one thread per entry in `bots[]`,
each with its own DB and Telegram credentials. Both bots post their own "Bot
Started" summary, then wait for their next phase-aligned window. Ctrl+C is
caught by the main thread, which sets a shared stop event; both bots finish
their current iteration, send "Bot Stopped", and exit. State is safe on disk.

## Recovery on restart
On startup `main.py` calls `reconcile_open_trades()` which finds any rows left
in `status='open'` whose window has already closed and waits for Polymarket
to resolve them (same WS+HTTP path as live settle). Because Polymarket retains
historical resolutions forever, this works even if the bot was down for hours.
There is no Binance fallback — if a market is genuinely unresolved (e.g. UMA
dispute), the bot keeps waiting and announces the delay on Telegram.

## Stretch goals (NOT BUILT)
- Real trading via Polymarket CLOB (`py-clob-client`) with a funded wallet
- Binance kline WS for last-second entry timing à la the well-known reference
  bot (Polymarket WS is already wired for settlement)
- Adaptive bet sizing (compute exact share count to cover prior loss given
  the current UP/DOWN price, instead of flat doubling)
- Multi-symbol (ETH/SOL 5-min markets alongside BTC)
- Web dashboard for trade history / equity curve

## Conventions in this codebase
- All timestamps are UTC seconds (`window_ts = int`). Binance kline ms-times
  are converted at the edge.
- No async — every API call is plain `requests` with a 10s timeout. The
  Polymarket WS runs on a single background thread inside `polymarket_resolver`
  and pushes the winner back through a thread-safe holder; the main loop
  stays synchronous.
- No third-party Binance/Polymarket/Telegram SDKs — raw HTTP + bare
  `websocket-client` keeps the dep surface tiny (`requests`, `python-dotenv`,
  `websocket-client`) and avoids version churn.
- Floats for money are fine at this scale; everything is rounded to 4 dp at
  the storage boundary.
