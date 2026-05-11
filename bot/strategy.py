"""Next-candle direction predictor for 5-min BTC.

Combines a small set of classic indicators into a single signed score:
positive → UP, negative → DOWN, exactly 0 → follow the last candle direction.
Tunable by editing the weights below.
"""

from typing import Iterable


def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def predict_direction(candles: Iterable[dict]) -> tuple[str, float, str]:
    """Predict the direction of the candle *immediately after* the given list.

    `candles` are dicts with float open/high/low/close/volume, oldest first.
    Returns (direction, score, reason).
    """
    cs = list(candles)
    if not cs:
        return ("UP", 0.0, "no_data")

    last = cs[-1]
    fallback_dir = "UP" if last["close"] >= last["open"] else "DOWN"

    if len(cs) < 21:
        return (fallback_dir, 0.0, "insufficient_data_followlast")

    closes = [c["close"] for c in cs[-30:]]
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)

    score = 0.0
    reasons: list[str] = []

    # 1. EMA(9) vs EMA(21) trend
    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            score += 2; reasons.append("ema_bull")
        elif ema9 < ema21:
            score -= 2; reasons.append("ema_bear")

    # 2. Short-term momentum: sum of last-3 candle bodies
    last3 = cs[-3:]
    net = sum(c["close"] - c["open"] for c in last3)
    if net > 0:
        score += 2; reasons.append("mom_up")
    elif net < 0:
        score -= 2; reasons.append("mom_dn")

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

    if score > 0:
        return ("UP", score, "+".join(reasons))
    if score < 0:
        return ("DOWN", score, "+".join(reasons))
    reasons.append("zero_score_followlast")
    return (fallback_dir, 0.0, "+".join(reasons))
