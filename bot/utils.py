import time
from datetime import datetime, timedelta, timezone

WINDOW = 300  # seconds

# All user-facing timestamps (Telegram messages, logs) are rendered in
# Indian Standard Time so the operator (who is in India) doesn't have to
# do mental conversion regardless of which timezone the VPS happens to
# run in. Internal storage stays in UTC unix-seconds.
IST = timezone(timedelta(hours=5, minutes=30))


def current_window_ts(now: int | None = None) -> int:
    """Start (UTC unix-sec) of the 5-min window containing `now`."""
    if now is None:
        now = int(time.time())
    return (now // WINDOW) * WINDOW


def next_window_ts(now: int | None = None) -> int:
    return current_window_ts(now) + WINDOW


def sleep_until(ts: int | float) -> None:
    """Sleep until wall-clock time `ts` (UTC unix-sec), chunked so the
    process stays responsive to Ctrl+C."""
    while True:
        delta = ts - time.time()
        if delta <= 0:
            return
        time.sleep(min(delta, 30))


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_local(ts: int) -> str:
    """IST datetime in dd/mm/yy - hh:mm format, e.g. '11/05/26 - 18:20'."""
    return datetime.fromtimestamp(ts, tz=IST).strftime("%d/%m/%y - %H:%M")


def fmt_local_time(ts: int) -> str:
    """Just the IST time portion, e.g. '18:25'."""
    return datetime.fromtimestamp(ts, tz=IST).strftime("%H:%M")


def money(amount: float) -> str:
    """Unsigned dollar amount, e.g. '$1,000.00'."""
    return f"${amount:,.2f}"


def money_signed(amount: float) -> str:
    """Signed dollar amount, e.g. '+$2.43' or '-$1.27'."""
    sign = "+" if amount >= 0 else "-"
    return f"{sign}${abs(amount):,.2f}"


def streak_label(streak_type: str, streak_count: int) -> str:
    """Human label like 'fresh', '3 wins', '5 losses'."""
    if streak_type == "none" or streak_count == 0:
        return "fresh"
    if streak_type == "win":
        return f"{streak_count} win" + ("" if streak_count == 1 else "s")
    return f"{streak_count} loss" + ("" if streak_count == 1 else "es")
