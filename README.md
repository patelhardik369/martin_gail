# Polymarket BTC 5-min Up/Down — Paper Trading Bot

A Python paper-trading bot that bets on Polymarket's 5-minute Bitcoin up/down
markets with a martingale (double-on-loss) strategy. **Two bots run in
parallel** in a single process, offset by 5 minutes, so together they cover
every 5-min window while each individually stays on a 10-min cadence (so
its previous bet has settled before sizing the next).

| Bot | Trades at | DB |
|---|---|---|
| A | :00, :10, :20, :30, :40, :50 | `data/trades_a.db` |
| B | :05, :15, :25, :35, :45, :55 | `data/trades_b.db` |

Each bot has its own balance, streak, martingale state, and Telegram channel.
Strategy is a simple trend-follower combining EMA cross, last-3 momentum,
last-candle color, volume surge, and wick reversal into a signed score
(see `bot/strategy.py`).

> Paper trading only. No funds at risk. See `CLAUDE.md` for the full design.

## Setup

```powershell
pip install -r requirements.txt
copy config.example.json config.json
copy .env.example .env
```

Edit `.env` and paste **two pairs** of Telegram credentials (one per bot):

```
TELEGRAM_BOT_TOKEN_A=123456:ABC-bot-A-token
TELEGRAM_CHAT_ID_A=987654321
TELEGRAM_BOT_TOKEN_B=234567:DEF-bot-B-token
TELEGRAM_CHAT_ID_B=876543210
```

- Create two bots in [@BotFather](https://t.me/BotFather) so each has its
  own token and chat.
- Get your chat IDs from [@userinfobot](https://t.me/userinfobot) (start
  each Telegram bot once first so it can DM you).

## Run

```powershell
python -m bot.main
```

`bot.main.main()` loads `config.json`, spawns one thread per entry in `bots[]`,
each with its own DB and Telegram credentials. Both bots post their own
"Bot Started" summary, then wait for their next phase-aligned window.

Stop with Ctrl+C — both bots finish their current iteration, send
"Bot Stopped", and exit. State is safe on disk.

## Configuration (`config.json`)

Shared parameters live at the top level and are inherited by every bot:

| Key | Default | Meaning |
|---|---|---|
| `initial_balance` | 1000 | Starting paper balance per bot in USD |
| `base_shares` | 5 | Shares per trade on a fresh start / after a win |
| `bet_multiplier` | 2 | Multiplier per consecutive loss |
| `max_doubles` | 6 | Cap on martingale steps |
| `settlement_buffer_seconds` | 5 | Wait this long after window close before settling |
| `entry_lead_seconds` | 10 | Seconds before window start to enter |
| `resolution_budget_seconds` | 240 | Soft timeout for Polymarket resolution |

Per-bot entries live under `bots[]`:

| Key | Meaning |
|---|---|
| `name` | Short label used in logs + Telegram prefix |
| `db_path` | SQLite file for this bot's trades + balance |
| `phase_offset_seconds` | 0 for :00/:10/…, 300 for :05/:15/… |
| `telegram_token_env` | env-var name that holds this bot's Telegram bot token |
| `telegram_chat_env` | env-var name that holds this bot's Telegram chat ID |

## Inspect trade history

```powershell
sqlite3 data/trades_a.db "SELECT id, side, shares, price, actual_outcome, pnl, balance_after FROM trades ORDER BY id DESC LIMIT 20"
sqlite3 data/trades_b.db "SELECT id, side, shares, price, actual_outcome, pnl, balance_after FROM trades ORDER BY id DESC LIMIT 20"
```

## Files
See `CLAUDE.md` for the full project map and design notes.
