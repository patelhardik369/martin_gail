"""Backtest the new regime-aware strategy on stored trade history.

Honest scope:
  * Regime detector (chop flips/trend_ratio) is applied using the prior 6
    TRADE WINDOWS' OHLC — these are real market data and are unaffected by
    strategy choice, so chop classification is deterministic.
  * Side selection: chop → invert the original score's side; trend → keep it.
  * Sizing: full martingale on streaks computed from the NEW outcomes, with
    chop-no-double clamp (shares clamped to base when regime=chop and step>=1).
  * What is NOT applied: EMA-gap and body-momentum gates — those need the
    30-candle history each trade fetched live, which is not stored.

Reads from data/trades.db, writes nothing.
"""
import sqlite3
from datetime import datetime, timezone

DB = "data/trades.db"
BASE_SHARES = 5
MULTIPLIER = 2
MAX_DOUBLES = 6
INITIAL_BALANCE = 1000.0

# Regime thresholds — same as bot/strategy.py module constants.
REGIME_LOOKBACK = 6
CHOP_FLIPS = 4
CHOP_TREND_RATIO = 0.25


def fetch_rows():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, window_ts, side, actual_outcome, score, reason,
                  open_price, window_high, window_low, close_price,
                  price, status
           FROM trades
           WHERE status IN ('won','lost')
           ORDER BY window_ts"""
    ).fetchall()
    conn.close()
    return rows


def detect_regime(prior_rows):
    """Replicates bot/strategy.detect_regime, fed with the OHLC of prior trade
    windows. Same thresholds as production."""
    if len(prior_rows) < REGIME_LOOKBACK:
        return {"regime": "unknown", "flips": 0, "trend_ratio": 0.0}
    window = prior_rows[-REGIME_LOOKBACK:]
    colors = ["G" if r["close_price"] >= r["open_price"] else "R" for r in window]
    flips = sum(1 for i in range(1, len(colors)) if colors[i] != colors[i - 1])
    high = max(r["window_high"] for r in window)
    low = min(r["window_low"] for r in window)
    span = high - low
    net = abs(window[-1]["close_price"] - window[0]["open_price"])
    trend_ratio = (net / span) if span > 0 else 0.0
    is_chop = flips >= CHOP_FLIPS or trend_ratio < CHOP_TREND_RATIO
    return {
        "regime": "chop" if is_chop else "trend",
        "flips": flips,
        "trend_ratio": trend_ratio,
    }


def compute_streak(history):
    """Given a sequence of past 'won'/'lost' strings in chronological order,
    return (streak_type, streak_count) for the most recent run."""
    if not history:
        return ("none", 0)
    last = history[-1]
    cnt = 0
    for r in reversed(history):
        if r == last:
            cnt += 1
        else:
            break
    return ("win" if last == "won" else "loss", cnt)


def main():
    rows = fetch_rows()
    print(f"# Loaded {len(rows)} resolved trades from {DB}")
    print()

    orig_balance = INITIAL_BALANCE
    new_balance = INITIAL_BALANCE
    orig_history = []
    new_history = []

    table = []
    n_inverted = n_clamped = n_chop = 0

    for i, r in enumerate(rows):
        info = detect_regime(rows[:i])
        regime = info["regime"]
        is_chop = regime == "chop"
        if is_chop:
            n_chop += 1

        # --- ORIGINAL strategy replay ---
        orig_side = r["side"]
        orig_outcome = r["actual_outcome"]
        ostreak_t, ostreak_n = compute_streak(orig_history)
        ostep = min(ostreak_n, MAX_DOUBLES) if ostreak_t == "loss" else 0
        oshares = BASE_SHARES * (MULTIPLIER ** ostep)
        ocost = round(oshares * r["price"], 4)
        if ocost > orig_balance:
            ostatus = "skip"
            opnl = 0.0
        else:
            owon = orig_side == orig_outcome
            ostatus = "won" if owon else "lost"
            opnl = round(
                oshares * (1 - r["price"]) if owon else -oshares * r["price"], 4
            )
            orig_balance = round(orig_balance + opnl, 4)
            orig_history.append(ostatus)

        # --- NEW strategy: chop-invert + chop-no-double ---
        new_side = orig_side
        if is_chop:
            new_side = "DOWN" if orig_side == "UP" else "UP"
            n_inverted += 1
        new_price = r["price"] if new_side == orig_side else round(1 - r["price"], 4)

        nstreak_t, nstreak_n = compute_streak(new_history)
        nstep_natural = min(nstreak_n, MAX_DOUBLES) if nstreak_t == "loss" else 0
        nshares_natural = BASE_SHARES * (MULTIPLIER ** nstep_natural)
        clamped = is_chop and nstep_natural >= 1 and nshares_natural > BASE_SHARES
        nshares = BASE_SHARES if clamped else nshares_natural
        if clamped:
            n_clamped += 1
        ncost = round(nshares * new_price, 4)

        if ncost > new_balance:
            nstatus = "skip"
            npnl = 0.0
        else:
            nwon = new_side == orig_outcome
            nstatus = "won" if nwon else "lost"
            npnl = round(
                nshares * (1 - new_price) if nwon else -nshares * new_price, 4
            )
            new_balance = round(new_balance + npnl, 4)
            new_history.append(nstatus)

        ts = datetime.fromtimestamp(r["window_ts"], tz=timezone.utc).strftime(
            "%m-%d %H:%M"
        )
        move_pct = (
            (r["close_price"] - r["open_price"]) / r["open_price"] * 100
            if r["open_price"]
            else 0.0
        )

        table.append({
            "i": i + 1, "ts": ts, "regime": regime, "flips": info["flips"],
            "tr": info["trend_ratio"], "outcome": orig_outcome,
            "move_pct": move_pct, "orig_side": orig_side, "orig_step": ostep,
            "orig_shares": oshares, "orig_status": ostatus, "orig_pnl": opnl,
            "orig_balance": orig_balance, "new_side": new_side,
            "new_step": nstep_natural, "new_shares": nshares,
            "new_price": new_price, "new_status": nstatus, "new_pnl": npnl,
            "new_balance": new_balance, "clamped": clamped,
            "inverted": new_side != orig_side,
        })

    def longest_loss(hist):
        cur = mx = 0
        for s in hist:
            if s == "lost":
                cur += 1; mx = max(mx, cur)
            else:
                cur = 0
        return mx

    orig_w = sum(1 for s in orig_history if s == "won")
    orig_l = sum(1 for s in orig_history if s == "lost")
    new_w = sum(1 for s in new_history if s == "won")
    new_l = sum(1 for s in new_history if s == "lost")

    print("# Per-trade results (ORIG = old strategy replay, NEW = chop-aware)")
    print()
    hdr = (
        f"{'#':>3} {'time':<12} {'regime':<7} {'flp':>3} {'tr':>5} "
        f"{'mv%':>6} {'out':<4}  "
        f"{'oSd':<4} {'oStp':>4} {'oSh':>4} {'oRes':<4} {'oPnL':>8} {'oBal':>9}  "
        f"{'nSd':<4} {'nStp':>4} {'nSh':>4} {'nRes':<4} {'nPnL':>8} {'nBal':>9} {'tag'}"
    )
    print(hdr)
    print("-" * len(hdr))
    for t in table:
        tag = []
        if t["inverted"]: tag.append("INV")
        if t["clamped"]: tag.append("CLAMP")
        print(
            f"{t['i']:>3} {t['ts']:<12} {t['regime']:<7} {t['flips']:>3} "
            f"{t['tr']:>5.2f} {t['move_pct']:>+6.3f} {t['outcome']:<4}  "
            f"{t['orig_side']:<4} {t['orig_step']:>4} {t['orig_shares']:>4} "
            f"{t['orig_status']:<4} {t['orig_pnl']:>+8.2f} {t['orig_balance']:>9.2f}  "
            f"{t['new_side']:<4} {t['new_step']:>4} {t['new_shares']:>4} "
            f"{t['new_status']:<4} {t['new_pnl']:>+8.2f} {t['new_balance']:>9.2f} "
            f"{','.join(tag)}"
        )

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"Trades total:           {len(rows)}")
    print(f"Chop windows detected:  {n_chop}  ({n_chop/len(rows)*100:.1f}%)")
    print(f"Inversions applied:     {n_inverted}")
    print(f"No-double clamps fired: {n_clamped}")
    print()
    print(f"{'':<20}{'ORIGINAL':>12}{'NEW':>12}{'delta':>12}")
    print(f"{'wins':<20}{orig_w:>12}{new_w:>12}{new_w - orig_w:>+12}")
    print(f"{'losses':<20}{orig_l:>12}{new_l:>12}{new_l - orig_l:>+12}")
    print(
        f"{'win rate':<20}{orig_w/max(1,orig_w+orig_l)*100:>11.1f}%"
        f"{new_w/max(1,new_w+new_l)*100:>11.1f}%"
        f"{(new_w/max(1,new_w+new_l) - orig_w/max(1,orig_w+orig_l))*100:>+11.1f}%"
    )
    print(
        f"{'longest loss streak':<20}{longest_loss(orig_history):>12}"
        f"{longest_loss(new_history):>12}"
        f"{longest_loss(new_history)-longest_loss(orig_history):>+12}"
    )
    print(
        f"{'final balance ($)':<20}{orig_balance:>12.2f}{new_balance:>12.2f}"
        f"{new_balance-orig_balance:>+12.2f}"
    )
    print(
        f"{'net P&L ($)':<20}{orig_balance-INITIAL_BALANCE:>+12.2f}"
        f"{new_balance-INITIAL_BALANCE:>+12.2f}"
        f"{(new_balance-orig_balance):>+12.2f}"
    )


if __name__ == "__main__":
    main()
