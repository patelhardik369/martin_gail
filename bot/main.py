import logging
import time

from .binance_client import get_klines, get_kline_by_open_time
from .config import load_config
from .database import Database
from .martingale import next_bet
from .polymarket_client import (
    get_event_by_slug,
    parse_prices,
    parse_token_ids,
)
from .polymarket_resolver import wait_for_resolution
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
    has a non-None `pending` to anchor its 10-min cadence on. If an attempt
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


def _settle_with_winner(
    db: Database, trade: dict, winner: str, candle: dict | None
) -> tuple[float, float, bool]:
    """Apply Polymarket-declared winner to a trade row. The Binance settlement
    candle (if available) supplies the true OHLC for the bet window — it's
    informational only, the winner is purely Polymarket's call. Returns
    (pnl, new_balance, won)."""
    won = winner == trade["side"]
    pnl = round(
        trade["shares"] * (1 - trade["price"]) if won else -trade["shares"] * trade["price"],
        4,
    )
    new_balance = round(db.get_balance() + pnl, 4)
    db.close_trade(
        trade_id=trade["id"] if "id" in trade else trade["trade_id"],
        actual_outcome=winner,
        pnl=pnl,
        balance_after=new_balance,
        won=won,
        resolved_at=int(time.time()),
        open_price=candle["open"] if candle else None,
        close_price=candle["close"] if candle else None,
        window_high=candle["high"] if candle else None,
        window_low=candle["low"] if candle else None,
    )
    return pnl, new_balance, won


def reconcile_open_trades(
    cfg: dict, db: Database, notifier: TelegramNotifier
) -> None:
    """For any orphan open trade whose 5-min window already closed, wait for
    Polymarket to declare a winner (no Binance fallback). Uses the same
    soft-budget logic as live settle so the user is told if it's stuck."""
    budget = int(cfg["resolution_budget_seconds"])
    for trade in db.open_trades():
        window_ts = int(trade["window_ts"])
        # If the candle hasn't even closed yet, leave it alone for the main loop.
        if int(time.time()) < window_ts + 300:
            continue
        slug = trade["slug"]
        notifier.send(
            "\n".join([
                f"🔁 Trade #{trade['id']} — Reconciling",
                "",
                "Bot restarted with an unresolved trade.",
                f"Window: {fmt_local(window_ts)}",
                "Waiting for Polymarket resolution…",
            ])
        )

        def _overrun_msg(tid: int = trade["id"]) -> None:
            notifier.send(
                "\n".join([
                    f"⏳ Trade #{tid} — Resolution Pending",
                    "",
                    f"Polymarket has not resolved within {budget}s.",
                    "Still waiting — no further action until it does.",
                ])
            )

        winner = wait_for_resolution(slug, budget_s=budget, on_budget_exceeded=_overrun_msg)
        # Best-effort: pull the Binance OHLC for display + audit on the row.
        candle = get_kline_by_open_time("BTCUSDT", "5m", window_ts * 1000)
        pnl, bal, won = _settle_with_winner(
            db, {**trade, "id": trade["id"]}, winner, candle
        )
        notifier.send(
            "\n".join([
                f"{'✅' if won else '❌'} Trade #{trade['id']} — Reconciled {'WON' if won else 'LOST'}",
                "",
                f"Predicted: {trade['side']}",
                f"Polymarket winner: {winner}",
                "",
                f"P&L:     {money_signed(pnl)}",
                f"Balance: {money(bal)}",
            ])
        )


def trade_open(
    cfg: dict, db: Database, notifier: TelegramNotifier, window_ts: int
) -> dict | None:
    """Enter a trade for the upcoming window. Returns trade-summary dict
    (id/window/side/shares/price/slug) used later for settlement, or None if
    the trade was skipped (already taken, insufficient balance, no data, …)."""
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

    direction, score, reason, regime = predict_direction(candles)

    slug = f"btc-updown-5m-{window_ts}"
    event = get_event_by_slug(slug)
    up_price, down_price = parse_prices(event)
    up_token, down_token = parse_token_ids(event)
    price = up_price if direction == "UP" else down_price
    if not (0 < price < 1):
        price = 0.50

    streak_type, streak_count = db.get_streak()
    bet = next_bet(streak_type, streak_count, cfg)
    shares = bet["shares"]
    # Soft chop-no-double guard: don't double into a choppy market. The
    # martingale step (and underlying streak) still advances on win/loss as
    # normal, so doubling resumes the moment the regime flips back to trend.
    # This caps the bleed at base × streak_length instead of base × 2^step.
    base_shares = int(cfg["base_shares"])
    clamped = regime == "chop" and bet["step"] >= 1 and shares > base_shares
    if clamped:
        shares = base_shares
        reason = f"{reason}+chop_no_double"
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
        up_token_id=up_token,
        down_token_id=down_token,
    )

    martingale_lines = [
        f"Step:   {bet['step']}",
        f"Streak: {streak_label(streak_type, streak_count)}",
    ]
    if clamped:
        martingale_lines.append(
            f"⚠️ Chop detected — clamped to base ({base_shares}) instead of {bet['shares']}"
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
            f"Regime: {regime}",
            f"Score:  {score:+.1f}",
            f"Reason: {_pretty_reason(reason)}",
            "",
            "📈 Martingale",
            *martingale_lines,
            "",
            "📍 Market",
            f"BTC now:            {money(open_price_estimate)}",
            f"Polymarket UP/DOWN: ${up_price:.3f} / ${down_price:.3f}",
        ])
    )

    return {
        "trade_id": trade_id,
        "window_ts": window_ts,
        "slug": slug,
        "side": direction,
        "shares": shares,
        "price": price,
    }


def trade_settle(
    cfg: dict, db: Database, notifier: TelegramNotifier, pending: dict
) -> bool:
    """Wait for Polymarket to resolve `pending`'s market and settle the trade.

    Returns True if resolution happened inside the soft budget (next entry
    should fire on the originally-planned 10-min boundary), False if it
    overran (the next round was skipped and the caller should pick the
    next 10-min boundary >= now+entry_lead instead).
    """
    window_ts = pending["window_ts"]
    slug = pending["slug"]
    budget = int(cfg["resolution_budget_seconds"])
    overran = {"flag": False}

    def _overrun_msg() -> None:
        overran["flag"] = True
        notifier.send(
            "\n".join([
                f"⏳ Trade #{pending['trade_id']} — Resolution Pending",
                "",
                f"Polymarket has not resolved within {budget}s.",
                "Skipping next round — still waiting on this one.",
            ])
        )

    winner = wait_for_resolution(slug, budget_s=budget, on_budget_exceeded=_overrun_msg)

    # Best-effort Binance close just for the Telegram display — does not
    # affect win/lose, which comes solely from Polymarket.
    settled = get_kline_by_open_time("BTCUSDT", "5m", window_ts * 1000)

    trade_row = {
        "id": pending["trade_id"],
        "side": pending["side"],
        "shares": pending["shares"],
        "price": pending["price"],
    }
    pnl, new_balance, won = _settle_with_winner(
        db, trade_row, winner, settled
    )

    streak_type, streak_count = db.get_streak()
    stats = db.stats()
    win_rate = (stats["wins"] / stats["total"] * 100.0) if stats["total"] else 0.0

    lines = [
        f"{'✅' if won else '❌'} Trade #{pending['trade_id']} — {'WON' if won else 'LOST'}",
        "",
        "🎯 Outcome",
        f"Predicted:        {pending['side']}",
        f"Polymarket winner: {winner}",
    ]
    if settled is not None:
        delta = settled["close"] - settled["open"]
        binance_dir = "UP" if delta >= 0 else "DOWN"
        diverged = " ⚠️ diverged" if binance_dir != winner else ""
        lines += [
            f"Binance:           {binance_dir} ({money_signed(delta)}){diverged}",
            "",
            "📍 Price",
            f"BTC open:  {money(settled['open'])}",
            f"BTC close: {money(settled['close'])}",
        ]
    lines += [
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
    ]
    notifier.send("\n".join(lines))

    return not overran["flag"]


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
    resolution_budget = int(cfg["resolution_budget_seconds"])

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
            f"Resolution: Polymarket only (budget {resolution_budget}s, skip on overrun)",
        ])
    )

    try:
        reconcile_open_trades(cfg, db, notifier)
    except Exception as e:
        logger.exception("Reconcile failed: %s", e)

    # Sequential loop, one trade every 10 minutes. Each iteration:
    #   1. Settle `pending` by waiting on Polymarket. Inside the soft
    #      budget the next entry stays on schedule (W+600). On overrun,
    #      we keep waiting indefinitely and the next entry slides to the
    #      next free 10-min boundary after settlement.
    #   2. Enter the next trade for that 10-min boundary.
    pending: dict | None = _bootstrap_pending(cfg, db, notifier, entry_lead)

    while True:
        try:
            prev_window_ts = pending["window_ts"]

            settle_at = prev_window_ts + 300 + settle_buffer
            sleep_until(settle_at)
            on_schedule = trade_settle(cfg, db, notifier, pending)
            pending = None  # settled — don't re-settle on shutdown

            if on_schedule:
                next_window = prev_window_ts + 600
                next_entry_time = next_window - entry_lead
                now = int(time.time())
                if now >= next_window:
                    # Lost the slot during settlement (unlikely but possible
                    # with clock skew). Re-bootstrap to the next live boundary.
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
                pending = trade_open(cfg, db, notifier, next_window) or \
                    _bootstrap_pending(cfg, db, notifier, entry_lead)
            else:
                # Settle overran the budget — skip whatever 10-min slot(s)
                # passed while we waited, pick up at the next live boundary.
                logger.info("Settle overran budget — resuming on next 10-min boundary")
                pending = _bootstrap_pending(cfg, db, notifier, entry_lead)

        except KeyboardInterrupt:
            logger.info("Shutting down")
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
