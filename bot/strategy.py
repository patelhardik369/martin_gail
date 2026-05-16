"""Next-candle direction predictor for 5-min BTC, with multi-timeframe context.

Combines 5-min signals with a higher-timeframe (1-hour) trend filter to
produce a signed score: positive → UP, negative → DOWN, exactly 0 →
follow the last 5-min candle.

The 1-hour trend gets the biggest single weight (±3) because the 5-min
direction is much closer to a random walk than the 1-hour direction,
and aligning bets with the higher-timeframe trend is the single most
impactful edge available from public price data.

Tunable weights and thresholds are module constants at the top of the
file. Doesn't try to be clever — keeps the bot deterministic and easy
to reason about.
"""

from typing import Iterable

# --- Weights ---------------------------------------------------------------
W_TF_1H = 3.0          # 1-hour EMA(8) vs EMA(20) trend — biggest single signal
W_EMA = 2.0            # 5-min EMA(9) vs EMA(21) crossover
W_MOMENTUM = 2.0       # sum of last-4 candle bodies (extended from 3)
W_LAST_BODY = 1.0      # last 5-min candle color, scaled by body/range ratio
W_VOLUME = 2.0         # volume surge confirmation (raised from 1.0)
W_WICK = 1.0           # long-wick reversal (raised from 0.5)
W_RSI_EXTREME = 0.5    # RSI < 30 → +0.5 (oversold bounce), > 70 → -0.5

# --- Thresholds ------------------------------------------------------------
# Minimum EMA gap (as fraction of price) for the 1h crossover to count.
# Stops noise crossings from flipping the +3 signal.
TF_1H_GAP_PCT = 0.0010   # 0.10%
# Last 5-min candle range must exceed this fraction of ATR(20) for the
# last_body + volume_surge components to count. Below this, the candle
# is "quiet" and those signals are unreliable.
LOW_RANGE_THRESHOLD = 0.5
# RSI period (5-min candles)
RSI_PERIOD = 14


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int) -> float | None:
    """Standard Wilder-smoothed RSI on closing prices."""
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # Seed: simple average of first `period`
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    # Wilder smoothing for the rest
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(candles: list[dict], period: int) -> float | None:
    """Simple ATR: mean of (high - low) over the last `period` candles.
    Good enough for a relative-range filter."""
    if len(candles) < period:
        return None
    window = candles[-period:]
    return sum(c["high"] - c["low"] for c in window) / period


def predict_direction(
    candles_5m: Iterable[dict],
    candles_1h: Iterable[dict] | None = None,
) -> tuple[str, float, str]:
    """Predict the direction of the 5-min candle *immediately after* the
    given 5-min history, optionally informed by 1-hour context.

    `candles_5m` are dicts with float open/high/low/close/volume, oldest
    first. `candles_1h` (optional) is the same shape on hourly candles —
    when provided, an additional ±W_TF_1H component reflects the 1h trend.

    Returns ``(direction, score, reason)``.
    """
    cs = list(candles_5m)
    if not cs:
        return ("UP", 0.0, "no_data")

    last = cs[-1]
    fallback_dir = "UP" if last["close"] >= last["open"] else "DOWN"

    if len(cs) < 21:
        return (fallback_dir, 0.0, "insufficient_data_followlast")

    closes = [c["close"] for c in cs[-30:]]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    rsi = _rsi(closes, RSI_PERIOD)
    atr20 = _atr(cs, 20)

    # Range of the most-recent candle as fraction of ATR — used to gate
    # the noisy last-candle-dependent signals.
    last_range = last["high"] - last["low"]
    candle_quiet = atr20 is not None and atr20 > 0 and last_range < LOW_RANGE_THRESHOLD * atr20

    score = 0.0
    reasons: list[str] = []

    # 1. 1-hour trend — the dominant signal when present
    if candles_1h is not None:
        c1h = list(candles_1h)
        if len(c1h) >= 20:
            h_closes = [c["close"] for c in c1h[-24:]]
            h_ema8 = _ema(h_closes, 8)
            h_ema20 = _ema(h_closes, 20)
            if h_ema8 is not None and h_ema20 is not None and h_ema20 > 0:
                gap = (h_ema8 - h_ema20) / h_ema20
                if gap > TF_1H_GAP_PCT:
                    score += W_TF_1H; reasons.append("1h_bull")
                elif gap < -TF_1H_GAP_PCT:
                    score -= W_TF_1H; reasons.append("1h_bear")
                else:
                    reasons.append("1h_flat")

    # 2. 5-min EMA(9)/EMA(21) trend
    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            score += W_EMA; reasons.append("ema_bull")
        elif ema9 < ema21:
            score -= W_EMA; reasons.append("ema_bear")

    # 3. Short-term momentum: sum of last-4 candle bodies (extended from 3)
    last4 = cs[-4:]
    net = sum(c["close"] - c["open"] for c in last4)
    if net > 0:
        score += W_MOMENTUM; reasons.append("mom_up")
    elif net < 0:
        score -= W_MOMENTUM; reasons.append("mom_dn")

    # 4. Last candle body direction — weighted by body/range ratio so a
    # doji-like candle contributes ~0 while a strong-body candle hits full ±1.
    # Suppressed entirely when the candle is "quiet" (range < 0.5 × ATR).
    if not candle_quiet and last_range > 0:
        body = last["close"] - last["open"]
        ratio = abs(body) / last_range  # 0..1
        weight = W_LAST_BODY * ratio
        if body > 0:
            score += weight; reasons.append(f"last_green({ratio:.2f})")
        elif body < 0:
            score -= weight; reasons.append(f"last_red({ratio:.2f})")
    elif candle_quiet:
        reasons.append("last_quiet")

    # 5. Volume surge confirmation (1.5x 20-period avg) — also suppressed
    # in quiet markets since a volume spike on a tiny candle is noise.
    if not candle_quiet:
        vols = [c["volume"] for c in cs[-21:-1]]
        avg_vol = (sum(vols) / len(vols)) if vols else 0.0
        if avg_vol > 0 and last["volume"] > 1.5 * avg_vol:
            if last["close"] > last["open"]:
                score += W_VOLUME; reasons.append("vol_surge_bull")
            elif last["close"] < last["open"]:
                score -= W_VOLUME; reasons.append("vol_surge_bear")

    # 6. Long-wick reversal
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    if body > 0:
        if lower_wick > body * 2:
            score += W_WICK; reasons.append("long_lower_wick")
        if upper_wick > body * 2:
            score -= W_WICK; reasons.append("long_upper_wick")

    # 7. RSI extreme zones — small mean-reversion bias at overbought/oversold
    if rsi is not None:
        if rsi < 30:
            score += W_RSI_EXTREME; reasons.append(f"rsi_oversold({rsi:.1f})")
        elif rsi > 70:
            score -= W_RSI_EXTREME; reasons.append(f"rsi_overbought({rsi:.1f})")

    if score > 0:
        return ("UP", score, "+".join(reasons))
    if score < 0:
        return ("DOWN", score, "+".join(reasons))
    reasons.append("zero_score_followlast")
    return (fallback_dir, 0.0, "+".join(reasons))
