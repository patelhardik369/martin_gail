# Polymarket BTC 5-min Up/Down — Paper Trading Bot

A Python bot that paper-trades Polymarket's 5-minute Bitcoin up/down markets
using a martingale (double-on-loss) strategy. Predicts the direction of the
next 5-min BTC candle from recent Binance candles, simulates the trade
against the live Polymarket UP/DOWN price, settles after the window closes,
and reports every trade to Telegram. Balance and streak persist in SQLite.

> Paper trading only. No funds at risk. See `CLAUDE.md` for the full design.

## Setup

```powershell
pip install -r requirements.txt
copy config.example.json config.json
copy .env.example .env
```

Edit `.env` and paste your Telegram credentials:

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=987654321
```

- Get a bot token from [@BotFather](https://t.me/BotFather).
- Get your chat ID from [@userinfobot](https://t.me/userinfobot).

## Run

```powershell
python -m bot.main
```

The bot waits for the next 5-minute UTC boundary, then loops forever:
1. Pull last 30 five-min BTC candles from Binance
2. Predict UP/DOWN for the next candle
3. Open a paper trade at the live Polymarket price
4. Wait 5 minutes, settle vs Binance close, update P&L
5. Send a Telegram message

Stop with Ctrl+C — state is safe in `data/trades.db`.

## Configuration (`config.json`)

| Key | Default | Meaning |
|---|---|---|
| `initial_balance` | 1000 | Starting paper balance in USD |
| `base_shares` | 5 | Shares per trade on a fresh start / after a win |
| `bet_multiplier` | 2 | Multiplier per consecutive loss |
| `max_doubles` | 6 | Cap on martingale steps (limits max single bet) |
| `db_path` | `data/trades.db` | SQLite file |
| `settlement_buffer_seconds` | 5 | Wait this long after window close before settling |

## Inspect trade history

```powershell
sqlite3 data/trades.db "SELECT id, side, shares, price, actual_outcome, pnl, balance_after FROM trades ORDER BY id DESC LIMIT 20"
```

## Files
See `CLAUDE.md` for the full project map and design notes.
