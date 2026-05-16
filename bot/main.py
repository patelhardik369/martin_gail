import logging
import threading
import time

from .binance_client import get_klines, get_kline_by_open_time
from .config import load_configs
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
    fmt_local,
    money,
    money_signed,
    sleep_until,
    streak_label,
)

logger = logging.getLogger(__name__)


def _pretty_reason(reason: str) -> str:
    return " + ".join(p for p in reason.split("+") if p)


_PILL_STATE_KEY = "last_pnl_msg_id"


def _refresh_pnl_pill(db: "Database", notifier: "TelegramNotifier") -> None:
    """Re-post the floating P&L button so it stays the latest message.

    Sequence: read prior message_id from state, delete it (best-effort),
    send a fresh single-button message with running balance + P&L + win
    rate, store the new message_id. The button is a URL-link to this
    bot's own chat, so tapping it is a silent no-op.

    The displayed balance/P&L are "marked to worst case": any currently-open
    trade's full cost is deducted up front, so the moment a trade opens you
    see it reflected in the pill. On settle, a win reverses the deduction
    and adds the payoff; a loss leaves the displayed number unchanged
    (since we already showed the worst-case at open).
    """
    if not notifier.enabled or not notifier.username:
        return
    balance = db.get_balance()
    stats = db.stats()
    open_costs = sum(float(t["cost"]) for t in db.open_trades())
    eff_balance = balance - open_costs
    eff_pnl = stats["total_pnl"] - open_costs
    win_rate = (stats["wins"] / stats["total"] * 100.0) if stats["total"] else 0.0
    label = (
        f"💰 {money(eff_balance)}   "
        f"📈 {money_signed(eff_pnl)}   "
        f"✓ {win_rate:.1f}%"
    )
    prior = db.get_state(_PILL_STATE_KEY)
    if prior:
        try:
            notifier.delete_message(int(prior))
        except ValueError:
            pass
    new_id = notifier.send_pnl_button("📊 Live P&L", label)
    if new_id is not None:
        db.set_state(_PILL_STATE_KEY, str(new_id))


def _next_entry(now: int, entry_lead: int, phase: int) -> tuple[int, int]:
    """Pick the next *trade-eligible* 5-min boundary and the moment to enter.

    A bot only trades windows whose unix-second start satisfies
    ``(W - phase) % 600 == 0`` — i.e. with phase=0 it trades :00/:10/:20…
    and with phase=300 it trades :05/:15/:25…. Either way each bot still
    waits 10 min between its own trades, so the previous trade has fully
    settled before sizing the next bet (martingale step is never stale).

    Ideal entry is ``entry_lead`` seconds before the window starts. If we're
    slightly late (within entry_lead seconds of window start) we enter
    immediately — still before the candle opens. If the window has already
    begun (now >= W), we skip to the next eligible boundary.

    Returns (target_window_ts, entry_time)."""
    # First W > now with (W - phase) % 600 == 0.
    target = ((now - phase) // 600 + 1) * 600 + phase
    entry_time = max(now, target - entry_lead)
    return target, entry_time


def _bootstrap_pending(
    cfg: dict,
    db: "Database",
    notifier: "TelegramNotifier",
    entry_lead: int,
    log: logging.LoggerAdapter,
) -> dict:
    """Wait for the next live entry opportunity and open the first trade.

    Loops until a trade is successfully opened so the main pipeline always
    has a non-None `pending` to anchor its 10-min cadence on. If an attempt
    fails for any reason, we advance past that window before trying again
    instead of immediately retrying the same one."""
    phase = int(cfg["phase_offset_seconds"])
    while True:
        now = int(time.time())
        target, entry_time = _next_entry(now, entry_lead, phase)
        if int(time.time()) < entry_time:
            log.info(
                "Bootstrap: waiting for window %s (entry at %s)",
                fmt_local(target),
                fmt_local(entry_time),
            )
            sleep_until(entry_time)
        pending = trade_open(cfg, db, notifier, target, log)
        if pending is not None:
            return pending
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
    cfg: dict,
    db: Database,
    notifier: TelegramNotifier,
    log: logging.LoggerAdapter,
) -> None:
    """For any orphan open trade whose 5-min window already closed, wait for
    Polymarket to declare a winner (no Binance fallback). Uses the same
    soft-budget logic as live settle so the user is told if it's stuck."""
    budget = int(cfg["resolution_budget_seconds"])
    for trade in db.open_trades():
        window_ts = int(trade["window_ts"])
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
    cfg: dict,
    db: Database,
    notifier: TelegramNotifier,
    window_ts: int,
    log: logging.LoggerAdapter,
) -> dict | None:
    """Enter a trade for the upcoming window. Returns trade-summary dict
    (id/window/side/shares/price/slug) used later for settlement, or None if
    the trade was skipped (already taken, insufficient balance, no data, …)."""
    if db.has_trade(window_ts):
        log.info("Window %s already traded, skipping entry", window_ts)
        return None

    next_window = window_ts + 300

    candles = get_klines(
        symbol="BTCUSDT", interval="5m", limit=30, end_time_ms=window_ts * 1000 - 1
    )
    candles = [c for c in candles if c["open_time"] < window_ts * 1000]
    if len(candles) < 5:
        log.error("Insufficient candles for window %s: %d", window_ts, len(candles))
        return None

    direction, score, reason = predict_direction(candles)

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
        "slug": slug,
        "side": direction,
        "shares": shares,
        "price": price,
    }


def trade_settle(
    cfg: dict,
    db: Database,
    notifier: TelegramNotifier,
    pending: dict,
    log: logging.LoggerAdapter,
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


def _cadence_label(phase: int) -> str:
    """Human description of which minute-of-hour slots this phase trades."""
    base_minutes = [phase // 60 + 10 * i for i in range(6)]
    base_minutes = [m % 60 for m in base_minutes]
    return ", ".join(f":{m:02d}" for m in sorted(base_minutes))


def _run_bot(cfg: dict, stop_event: threading.Event) -> None:
    """One bot's full lifecycle. Runs in its own thread.

    Each bot has its own DB file, its own Telegram credentials, and a phase
    offset that determines which 5-min windows it trades. Two bots with
    phases 0 and 300 cover every 5-min window between them while each
    individually stays on a 10-min cadence.
    """
    name = cfg["name"]
    log = logging.LoggerAdapter(logger, {})  # placeholder; real prefix below
    # Use a per-bot logger so log lines get prefixed without polluting
    # message strings.
    log = _make_bot_logger(name)

    db = Database(cfg["db_path"], initial_balance=cfg["initial_balance"])
    notifier = TelegramNotifier(
        cfg["telegram_bot_token"], cfg["telegram_chat_id"]
    )
    # Every notifier.send() will now auto-refresh the P&L pill, so the pill
    # is always the last message in the chat no matter which call site
    # triggered the send (open/settle/reconcile/error/etc).
    notifier.set_after_send_hook(lambda: _refresh_pnl_pill(db, notifier))

    entry_lead = int(cfg["entry_lead_seconds"])
    settle_buffer = int(cfg["settlement_buffer_seconds"])
    resolution_budget = int(cfg["resolution_budget_seconds"])
    phase = int(cfg["phase_offset_seconds"])

    stats = db.stats()
    notifier.send(
        "\n".join([
            "🤖 Bot Started",
            "",
            f"Name: {name}",
            f"DB:   {cfg['db_path']}",
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
            f"Cadence:    1 trade per 10 min on {_cadence_label(phase)}",
            f"Entry lead: {entry_lead}s before each window",
            f"Resolution: Polymarket only (budget {resolution_budget}s, skip on overrun)",
        ])
    )

    try:
        reconcile_open_trades(cfg, db, notifier, log)
    except Exception as e:
        log.exception("Reconcile failed: %s", e)

    pending: dict | None = _bootstrap_pending(cfg, db, notifier, entry_lead, log)

    while not stop_event.is_set():
        try:
            prev_window_ts = pending["window_ts"]

            settle_at = prev_window_ts + 300 + settle_buffer
            sleep_until(settle_at)
            if stop_event.is_set():
                break
            on_schedule = trade_settle(cfg, db, notifier, pending, log)
            pending = None

            if on_schedule:
                next_window = prev_window_ts + 600
                next_entry_time = next_window - entry_lead
                now = int(time.time())
                if now >= next_window:
                    pending = _bootstrap_pending(cfg, db, notifier, entry_lead, log)
                    continue
                if now < next_entry_time:
                    log.info(
                        "Waiting %ds for window %s (entry at %s)",
                        next_entry_time - now,
                        fmt_local(next_window),
                        fmt_local(next_entry_time),
                    )
                    sleep_until(next_entry_time)
                if stop_event.is_set():
                    break
                pending = trade_open(cfg, db, notifier, next_window, log) or \
                    _bootstrap_pending(cfg, db, notifier, entry_lead, log)
            else:
                log.info("Settle overran budget — resuming on next 10-min boundary")
                pending = _bootstrap_pending(cfg, db, notifier, entry_lead, log)

        except Exception as e:
            log.exception("Main loop error: %s", e)
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
            # Sleep in 1s chunks so a stop_event lands quickly.
            for _ in range(30):
                if stop_event.is_set():
                    break
                time.sleep(1)

    notifier.send(
        "\n".join([
            "🛑 Bot Stopped",
            "",
            f"Balance: {money(db.get_balance())}",
        ])
    )


def _make_bot_logger(name: str) -> logging.LoggerAdapter:
    """Per-bot LoggerAdapter that prefixes every record with [name]."""
    class _Adapter(logging.LoggerAdapter):
        def process(self, msg, kwargs):
            return f"[{self.extra['name']}] {msg}", kwargs
    return _Adapter(logger, {"name": name})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    configs = load_configs()
    logger.info(
        "Starting %d bot(s): %s",
        len(configs),
        ", ".join(c["name"] for c in configs),
    )

    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    for cfg in configs:
        t = threading.Thread(
            target=_run_bot,
            args=(cfg, stop_event),
            name=f"bot-{cfg['name']}",
            daemon=False,
        )
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested — signalling bots to stop")
        stop_event.set()
        for t in threads:
            t.join(timeout=30)
        logger.info("All bots stopped")


if __name__ == "__main__":
    main()
