"""Next-candle direction predictor for 5-min BTC.

Combines a small set of classic indicators into a single signed score
(positive → UP, negative → DOWN, exactly 0 → follow the last candle).

A simple regime detector looks at the most recent _REGIME_LOOKBACK closed
5-min candles and classifies the market as ``trend`` or ``chop`` based on
color-flip count and ``|net move| / total range``. Historically in chop the
trend-following score is anti-predictive (≈32% win on |score|≥3), so the
final score is **inverted** when chop is detected — i.e. we mean-revert in
range markets and trend-follow in directional markets.

Two extra gates filter contributions that were generating false-confidence
scores in chop: the EMA cross requires a minimum gap, and the last-3
momentum requires its body sum to exceed a fraction of the recent ATR.
"""

from typing import Iterable


# --- Regime detection -------------------------------------------------------
# Lookback of 6 closed 5-min candles = 30 min of price action. Chop in this
# project's losing streaks typically lasts 60-90 min, so a 30-min view catches
# it early.
_REGIME_LOOKBACK = 6
# Color-flip threshold: with 6 candles there are at most 5 flips. ≥4 means
# the candles are mostly alternating green/red — classic whipsaw.
_CHOP_FLIPS = 4
# trend_ratio = |close[last] - open[first]| / (max(high) - min(low)).
# <0.25 means the market traversed a wide range but ended near where it
# started — a sideways oscillation, no directional commitment.
_CHOP_TREND_RATIO = 0.25

# --- Signal gates -----------------------------------------------------------
# EMA(9)/EMA(21) cross gives ±2; require the gap to exceed 0.02% of price
# so micro-crossings on a flat market don't swing the score by 4 points.
_EMA_GATE_PCT = 0.0002
# Sum of last-3 candle bodies; require |sum| to exceed 0.5 × ATR(6) so a
# trio of tiny drift-bodies doesn't trigger the momentum signal in chop.
_BODY_MOMENTUM_GATE = 0.5


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _avg_range(candles: list[dict]) -> float:
    if not candles:
        return 0.0
    return sum(c["high"] - c["low"] for c in candles) / len(candles)


def detect_regime(candles: list[dict]) -> dict:
    """Classify the last _REGIME_LOOKBACK candles as ``trend`` or ``chop``.

    Returns ``{regime, flips, trend_ratio, span_pct}``. Returns ``regime
    = 'unknown'`` if there isn't enough history.
    """
    if len(candles) < _REGIME_LOOKBACK:
        return {"regime": "unknown", "flips": 0, "trend_ratio": 0.0, "span_pct": 0.0}

    window = candles[-_REGIME_LOOKBACK:]
    colors = ["G" if c["close"] >= c["open"] else "R" for c in window]
    flips = sum(1 for i in range(1, len(colors)) if colors[i] != colors[i - 1])

    high = max(c["high"] for c in window)
    low = min(c["low"] for c in window)
    span = high - low
    net = abs(window[-1]["close"] - window[0]["open"])
    trend_ratio = (net / span) if span > 0 else 0.0
    span_pct = (span / window[-1]["close"]) if window[-1]["close"] else 0.0

    is_chop = flips >= _CHOP_FLIPS or trend_ratio < _CHOP_TREND_RATIO
    return {
        "regime": "chop" if is_chop else "trend",
        "flips": flips,
        "trend_ratio": trend_ratio,
        "span_pct": span_pct,
    }


def predict_direction(candles: Iterable[dict]) -> tuple[str, float, str, str]:
    """Predict the direction of the candle *immediately after* the given list.

    `candles` are dicts with float open/high/low/close/volume, oldest first.
    Returns ``(direction, score, reason, regime)`` where regime is one of
    ``trend``, ``chop``, ``unknown``.
    """
    cs = list(candles)
    if not cs:
        return ("UP", 0.0, "no_data", "unknown")

    last = cs[-1]
    fallback_dir = "UP" if last["close"] >= last["open"] else "DOWN"

    if len(cs) < 21:
        return (fallback_dir, 0.0, "insufficient_data_followlast", "unknown")

    info = detect_regime(cs)
    regime = info["regime"]

    closes = [c["close"] for c in cs[-30:]]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    score = 0.0
    reasons: list[str] = []

    # 1. EMA(9) vs EMA(21) trend — gated by minimum separation
    if ema9 is not None and ema21 is not None:
        gap = abs(ema9 - ema21)
        gate = _EMA_GATE_PCT * last["close"]
        if gap > gate:
            if ema9 > ema21:
                score += 2; reasons.append("ema_bull")
            else:
                score -= 2; reasons.append("ema_bear")
        else:
            reasons.append("ema_flat")

    # 2. Short-term momentum: sum of last-3 bodies, gated by ATR(6)
    last3 = cs[-3:]
    net_body = sum(c["close"] - c["open"] for c in last3)
    atr6 = _avg_range(cs[-6:])
    body_gate = _BODY_MOMENTUM_GATE * atr6
    if abs(net_body) > body_gate:
        if net_body > 0:
            score += 2; reasons.append("mom_up")
        else:
            score -= 2; reasons.append("mom_dn")
    else:
        reasons.append("mom_weak")

    # 3. Last candle body direction
    if last["close"] > last["open"]:
        score += 1; reasons.append("last_green")
    elif last["close"] < last["open"]:
        score -= 1; reasons.append("last_red")

    # 4. Volume surge confirmation (1.5x 20-period avg)
    vols = [c["volume"] for c in cs[-21:-1]]
    avg_vol = (sum(vols) / len(vols)) if vols else 0.0
    if avg_vol > 0 and last["volume"] > 1.5 * avg_vol:
        if last["close"] > last["open"]:
            score += 1; reasons.append("vol_surge_bull")
        elif last["close"] < last["open"]:
            score -= 1; reasons.append("vol_surge_bear")

    # 5. Wick reversal signal
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    if body > 0:
        if lower_wick > body * 2:
            score += 0.5; reasons.append("long_lower_wick")
        if upper_wick > body * 2:
            score -= 0.5; reasons.append("long_upper_wick")

    # 6. Regime-aware side selection.
    # In chop the trend score is anti-predictive — mean-revert by flipping it.
    regime_tag = f"{regime}(f={info['flips']},tr={info['trend_ratio']:.2f})"
    if regime == "chop":
        score = -score
        reasons.append("chop_invert")
    reasons.append(regime_tag)

    if score > 0:
        return ("UP", score, "+".join(reasons), regime)
    if score < 0:
        return ("DOWN", score, "+".join(reasons), regime)
    reasons.append("zero_score_followlast")
    return (fallback_dir, 0.0, "+".join(reasons), regime)
