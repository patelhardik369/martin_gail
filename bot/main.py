import logging
import time

from .binance_client import get_klines, get_kline_by_open_time
from .config import load_config
from .database import Database
from .martingale import next_bet
from .polymarket_client import get_event_by_slug, parse_prices
from .strategy import predict_direction
from .telegram_notifier import TelegramNotifier
from .utils import (
    current_window_ts,
    fmt_local,
    fmt_ts,
    money,
    money_signed,
    sleep_until,
    streak_label,
)

logger = logging.getLogger(__name__)


def _pretty_reason(reason: str) -> str:
    return " + ".join(p for p in reason.split("+") if p)


def _next_entry(now: int, entry_lead: int) -> tuple[int, int]:
    """Pick the next *trade-eligible* 5-min boundary and the moment to enter.

    The bot only trades windows aligned to 10-min boundaries (multiples of
    600s) — i.e. :00, :10, :20, :30, :40, :50 — and skips the in-between
    windows. This guarantees the previous trade has fully settled before we
    size the next bet, so the martingale step is never stale.

    Ideal entry is `entry_lead` seconds before the window starts. If we're
    slightly late (within entry_lead seconds of window start) we enter
    immediately — still before the candle opens. If the window has already
    begun (now >= W), we skip to the next 10-min boundary.

    Returns (target_window_ts, entry_time)."""
    target = ((now // 600) + 1) * 600
    entry_time = max(now, target - entry_lead)
    return target, entry_time


def _bootstrap_pending(cfg: dict, db: "Database", notifier: "TelegramNotifier", entry_lead: int) -> dict:
    """Wait for the next live entry opportunity and open the first trade.

    Loops until a trade is successfully opened so the main pipeline always
    has a non-None `pending` to anchor its 5-min cadence on. If an attempt
    fails for any reason, we advance past that window before trying again
    instead of immediately retrying the same one."""
    while True:
        now = int(time.time())
        target, entry_time = _next_entry(now, entry_lead)
        if int(time.time()) < entry_time:
            logger.info(
                "Bootstrap: waiting for window %s (entry at %s)",
                fmt_local(target),
                fmt_local(entry_time),
            )
            sleep_until(entry_time)
        pending = trade_open(cfg, db, notifier, target)
        if pending is not None:
            return pending
        # Failed — sleep past this window so the next iteration targets a
        # different one (otherwise _next_entry would pick the same window
        # again until its candle starts).
        sleep_until(target + 1)


def _settle(db: Database, trade: dict, settled_candle: dict) -> tuple[float, float, str, bool]:
    actual = "UP" if settled_candle["close"] >= settled_candle["open"] else "DOWN"
    won = actual == trade["side"]
    pnl = round(
        trade["shares"] * (1 - trade["price"]) if won else -trade["shares"] * trade["price"],
        4,
    )
    new_balance = round(db.get_balance() + pnl, 4)
    db.close_trade(
        trade_id=trade["id"] if "id" in trade else trade["trade_id"],
        close_price=settled_candle["close"],
        actual_outcome=actual,
        pnl=pnl,
        balance_after=new_balance,
        won=won,
    )
    return pnl, new_balance, actual, won


def reconcile_open_trades(db: Database, notifier: TelegramNotifier) -> None:
    now = int(time.time())
    for trade in db.open_trades():
        window_ts = int(trade["window_ts"])
        if now < window_ts + 305:
            continue
        candle = get_kline_by_open_time("BTCUSDT", "5m", window_ts * 1000)
        if not candle:
            logger.warning("No settlement candle for orphan trade #%s", trade["id"])
            continue
        pnl, bal, actual, won = _settle(db, trade, candle)
        notifier.send(
            "\n".join([
                f"🔁 Trade #{trade['id']} — Reconciled",
                "",
                f"Predicted: {trade['side']}",
                f"Actual:    {actual}",
                f"Result:    {'WON' if won else 'LOST'}",
                "",
                f"P&L:     {money_signed(pnl)}",
                f"Balance: {money(bal)}",
            ])
        )


def trade_open(
    cfg: dict, db: Database, notifier: TelegramNotifier, window_ts: int
) -> dict | None:
    """Enter a trade for the upcoming window. Returns trade-summary dict
    (id/window/side/shares/price) used later for settlement, or None if the
    trade was skipped (already taken, insufficient balance, no data, …)."""
    if db.has_trade(window_ts):
        logger.info("Window %s already traded, skipping entry", window_ts)
        return None

    next_window = window_ts + 300

    candles = get_klines(
        symbol="BTCUSDT", interval="5m", limit=30, end_time_ms=window_ts * 1000 - 1
    )
    # Strictly candles BEFORE the window we're betting on. The last item may
    # be the still-running 5-min candle (closes at `window_ts`) - that's OK,
    # it carries the freshest data and is ~99% complete at entry time.
    candles = [c for c in candles if c["open_time"] < window_ts * 1000]
    if len(candles) < 5:
        logger.error("Insufficient candles for window %s: %d", window_ts, len(candles))
        return None

    direction, score, reason = predict_direction(candles)

    slug = f"btc-updown-5m-{window_ts}"
    event = get_event_by_slug(slug)
    up_price, down_price = parse_prices(event)
    price = up_price if direction == "UP" else down_price
    if not (0 < price < 1):
        price = 0.50

    streak_type, streak_count = db.get_streak()
    bet = next_bet(streak_type, streak_count, cfg)
    shares = bet["shares"]
    cost = round(shares * price, 4)

    balance = db.get_balance()
    if cost > balance:
        notifier.send(
            "\n".join([
                "⚠️ Trade Skipped",
                "",
                f"Window:   {fmt_local(window_ts)} → {fmt_local(next_window)}",
                f"Required: {money(cost)}",
                f"Balance:  {money(balance)}",
                "",
                "Reason: bet size exceeds remaining balance.",
            ])
        )
        return None

    # Best estimate of the upcoming window's open: latest available price
    # (close of the candle currently in progress, finalizing at `window_ts`).
    open_price_estimate = candles[-1]["close"]

    trade_id = db.open_trade(
        window_ts=window_ts,
        slug=slug,
        side=direction,
        shares=shares,
        price=price,
        cost=cost,
        open_price=open_price_estimate,
        score=score,
        reason=reason,
        bet_step=bet["step"],
    )

    notifier.send(
        "\n".join([
            f"🎯 Trade #{trade_id} — Opened",
            "",
            "⏰ Window",
            f"Start: {fmt_local(window_ts)}",
            f"End:   {fmt_local(next_window)}",
            "",
            "📊 Position",
            f"Side:   {direction}",
            f"Shares: {shares}",
            f"Price:  ${price:.3f}",
            f"Cost:   {money(cost)}",
            "",
            "💰 Account",
            f"Balance: {money(balance)}",
            "",
            "🎲 Signal",
            f"Score:  {score:+.1f}",
            f"Reason: {_pretty_reason(reason)}",
            "",
            "📈 Martingale",
            f"Step:   {bet['step']}",
            f"Streak: {streak_label(streak_type, streak_count)}",
            "",
            "📍 Market",
            f"BTC now:            {money(open_price_estimate)}",
            f"Polymarket UP/DOWN: ${up_price:.3f} / ${down_price:.3f}",
        ])
    )

    return {
        "trade_id": trade_id,
        "window_ts": window_ts,
        "side": direction,
        "shares": shares,
        "price": price,
    }


def trade_settle(
    cfg: dict, db: Database, notifier: TelegramNotifier, pending: dict
) -> None:
    window_ts = pending["window_ts"]
    settled = get_kline_by_open_time("BTCUSDT", "5m", window_ts * 1000)
    if not settled:
        logger.warning("Settlement candle missing, retrying in 10s")
        time.sleep(10)
        settled = get_kline_by_open_time(
            "BTCUSDT", "5m", window_ts * 1000, search_window_ms=120_000
        )
    if not settled:
        notifier.send(
            "\n".join([
                f"⚠️ Trade #{pending['trade_id']} — Settlement Pending",
                "",
                "Binance candle for this window is not yet available.",
                "Will reconcile automatically on next restart.",
            ])
        )
        return

    trade_row = {
        "id": pending["trade_id"],
        "side": pending["side"],
        "shares": pending["shares"],
        "price": pending["price"],
    }
    pnl, new_balance, actual, won = _settle(db, trade_row, settled)

    streak_type, streak_count = db.get_streak()
    stats = db.stats()
    delta = settled["close"] - settled["open"]
    win_rate = (stats["wins"] / stats["total"] * 100.0) if stats["total"] else 0.0

    notifier.send(
        "\n".join([
            f"{'✅' if won else '❌'} Trade #{pending['trade_id']} — {'WON' if won else 'LOST'}",
            "",
            "🎯 Outcome",
            f"Predicted: {pending['side']}",
            f"Actual:    {actual}",
            "",
            "📍 Price",
            f"BTC open:  {money(settled['open'])}",
            f"BTC close: {money(settled['close'])}",
            f"Change:    {money_signed(delta)}",
            "",
            "💰 P&L",
            f"This trade: {money_signed(pnl)}",
            f"Balance:    {money(new_balance)}",
            "",
            "📈 Streak",
            f"Current: {streak_label(streak_type, streak_count)}",
            "",
            "📊 Record",
            f"Trades:   {stats['total']}",
            f"Wins:     {stats['wins']}",
            f"Losses:   {stats['losses']}",
            f"Win rate: {win_rate:.1f}%",
            f"Net P&L:  {money_signed(stats['total_pnl'])}",
        ])
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    db = Database(cfg["db_path"], initial_balance=cfg["initial_balance"])
    notifier = TelegramNotifier(cfg["telegram_bot_token"], cfg["telegram_chat_id"])

    entry_lead = int(cfg["entry_lead_seconds"])
    settle_buffer = int(cfg["settlement_buffer_seconds"])

    stats = db.stats()
    notifier.send(
        "\n".join([
            "🤖 Bot Started",
            "",
            "💰 Account",
            f"Balance: {money(db.get_balance())}",
            "",
            "📊 History",
            f"Trades:  {stats['total']}",
            f"Wins:    {stats['wins']}",
            f"Losses:  {stats['losses']}",
            f"Net P&L: {money_signed(stats['total_pnl'])}",
            "",
            f"Cadence:    1 trade per 10 min (skips alternate 5-min windows)",
            f"Entry lead: {entry_lead}s before each window",
        ])
    )

    try:
        reconcile_open_trades(db, notifier)
    except Exception as e:
        logger.exception("Reconcile failed: %s", e)

    # Sequential loop, one trade every 10 minutes. Each iteration:
    #   1. Settle `pending` ~settle_buffer seconds after its candle closes.
    #      After this, the DB reflects win/lose, so the next entry's
    #      martingale step is based on a known outcome (no stale streak).
    #   2. Enter trade for the window 10 minutes after `pending`'s window
    #      (i.e. skipping the immediately-following 5-min window). Target
    #      is `pending.window_ts + 600` — NOT from `now` — to keep the
    #      cadence locked to even-aligned 10-min boundaries.
    pending: dict | None = _bootstrap_pending(cfg, db, notifier, entry_lead)

    while True:
        try:
            prev_window_ts = pending["window_ts"]

            settle_at = prev_window_ts + 300 + settle_buffer
            sleep_until(settle_at)
            trade_settle(cfg, db, notifier, pending)
            pending = None  # settled — don't re-settle on shutdown

            next_window = prev_window_ts + 600
            next_entry_time = next_window - entry_lead
            now = int(time.time())
            if now >= next_window:
                logger.warning(
                    "Missed entry for window %s (now past start), re-bootstrapping",
                    fmt_local(next_window),
                )
                pending = _bootstrap_pending(cfg, db, notifier, entry_lead)
                continue
            if now < next_entry_time:
                logger.info(
                    "Waiting %ds for window %s (entry at %s)",
                    next_entry_time - now,
                    fmt_local(next_window),
                    fmt_local(next_entry_time),
                )
                sleep_until(next_entry_time)

            # If the new entry fails (insufficient balance, missing candles,
            # duplicate row, …) re-bootstrap from the next live 10-min
            # window so we don't lose the trading cadence permanently.
            pending = trade_open(cfg, db, notifier, next_window) or \
                _bootstrap_pending(cfg, db, notifier, entry_lead)

        except KeyboardInterrupt:
            logger.info("Shutting down")
            # Settle a still-open trade if its window has already closed
            # (only happens if Ctrl+C lands mid-settle on step 1).
            if pending is not None:
                try:
                    settle_at = pending["window_ts"] + 300 + settle_buffer
                    if int(time.time()) >= settle_at:
                        trade_settle(cfg, db, notifier, pending)
                except Exception:
                    pass
            notifier.send(
                "\n".join([
                    "🛑 Bot Stopped",
                    "",
                    f"Balance: {money(db.get_balance())}",
                ])
            )
            return
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            try:
                notifier.send(
                    "\n".join([
                        "⚠️ Bot Error",
                        "",
                        f"{e}",
                        "",
                        "Retrying in 30s...",
                    ])
                )
            except Exception:
                pass
            time.sleep(30)


if __name__ == "__main__":
    main()
