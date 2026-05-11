import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


DEFAULTS = {
    "initial_balance": 1000.0,
    "base_shares": 5,
    "bet_multiplier": 2,
    "max_doubles": 6,
    "db_path": "data/trades.db",
    "settlement_buffer_seconds": 5,
    # Seconds BEFORE the next 5-min window starts to enter the trade.
    # Polymarket odds are roughly 50/50 just before the candle begins; they
    # start drifting hard once the candle is live, so entering early matters.
    # 10s leaves room for order placement/acceptance latency — a limit order
    # may not fill if the odds have already moved by the time it's accepted.
    "entry_lead_seconds": 10,
}


def load_config(path: str = "config.json") -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    cfg["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    cfg["telegram_chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")
    return cfg
