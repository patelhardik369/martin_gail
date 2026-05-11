import logging
import requests

logger = logging.getLogger(__name__)

BINANCE_BASE = "https://api.binance.com"


def _parse_kline(k: list) -> dict:
    return {
        "open_time": int(k[0]),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "close_time": int(k[6]),
    }


def get_klines(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    limit: int = 30,
    end_time_ms: int | None = None,
    start_time_ms: int | None = None,
) -> list[dict]:
    """Fetch klines (candles) from Binance public REST.

    Returns oldest-first list of dicts: open_time (ms), open, high, low,
    close, volume, close_time (ms). All numeric fields parsed to float / int.
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_time_ms is not None:
        params["endTime"] = end_time_ms
    if start_time_ms is not None:
        params["startTime"] = start_time_ms

    r = requests.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=10)
    r.raise_for_status()
    return [_parse_kline(k) for k in r.json()]


def get_kline_by_open_time(
    symbol: str, interval: str, open_time_ms: int, search_window_ms: int = 60_000
) -> dict | None:
    """Fetch one specific kline by its open_time. Looks slightly past the
    expected close in case the candle hasn't finalized yet."""
    klines = get_klines(
        symbol=symbol,
        interval=interval,
        limit=5,
        end_time_ms=open_time_ms + search_window_ms,
    )
    for k in klines:
        if k["open_time"] == open_time_ms:
            return k
    return None
