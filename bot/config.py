import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Shared defaults inherited by every bot unless overridden in its entry of
# the top-level "bots" array in config.json. Per-bot keys (name, db_path,
# phase_offset_seconds, telegram_token_env, telegram_chat_env) live there.
DEFAULTS = {
    "initial_balance": 1000.0,
    "base_shares": 5,
    "bet_multiplier": 2,
    "max_doubles": 6,
    "settlement_buffer_seconds": 5,
    # Seconds BEFORE the next 5-min window starts to enter the trade.
    "entry_lead_seconds": 10,
    # Soft budget for Polymarket to resolve.
    "resolution_budget_seconds": 240,
}


def load_configs(path: str = "config.json") -> list[dict]:
    """Load config.json and produce one self-contained config dict per bot.

    The file must contain a top-level ``bots`` array, each entry providing
    at minimum ``name``, ``db_path``, ``phase_offset_seconds``,
    ``telegram_token_env`` and ``telegram_chat_env``. Shared parameters
    (martingale, timing) live at the top level and are merged into every
    bot's dict; a bot entry can override any of them.
    """
    raw = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            raw.update(json.load(f))

    bots_spec = raw.pop("bots", None)
    if not bots_spec:
        raise RuntimeError(
            f"{path} must define a 'bots' array with one entry per bot."
        )

    configs: list[dict] = []
    seen_phases: set[int] = set()
    seen_names: set[str] = set()
    for spec in bots_spec:
        merged = dict(raw)
        merged.update(spec)

        for required in ("name", "db_path", "phase_offset_seconds"):
            if required not in merged:
                raise RuntimeError(
                    f"bot entry is missing required field '{required}': {spec}"
                )
        if merged["name"] in seen_names:
            raise RuntimeError(f"duplicate bot name: {merged['name']}")
        if merged["phase_offset_seconds"] in seen_phases:
            raise RuntimeError(
                f"duplicate phase_offset_seconds: {merged['phase_offset_seconds']}"
            )
        seen_names.add(merged["name"])
        seen_phases.add(int(merged["phase_offset_seconds"]))

        token_env = merged.pop("telegram_token_env", "TELEGRAM_BOT_TOKEN")
        chat_env = merged.pop("telegram_chat_env", "TELEGRAM_CHAT_ID")
        merged["telegram_bot_token"] = os.getenv(token_env, "")
        merged["telegram_chat_id"] = os.getenv(chat_env, "")

        configs.append(merged)

    return configs
