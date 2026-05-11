import json
import logging
import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


def get_event_by_slug(slug: str) -> dict | None:
    """Fetch a Polymarket event by slug. Returns None on any failure so the
    caller can gracefully fall back to a default price."""
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("id"):
            return data
        return None
    except Exception as e:
        logger.warning("Polymarket event fetch failed for slug=%s: %s", slug, e)
        return None


def _maybe_parse(s):
    if isinstance(s, str):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return None
    return s


def parse_prices(event: dict | None) -> tuple[float, float]:
    """Return (up_price, down_price) for the event's first market.
    Polymarket's outcomes/outcomePrices arrive as JSON-encoded strings."""
    if not event:
        return (0.50, 0.50)
    try:
        markets = event.get("markets") or []
        if not markets:
            return (0.50, 0.50)
        m = markets[0]
        outcomes = _maybe_parse(m.get("outcomes"))
        prices = _maybe_parse(m.get("outcomePrices"))
        if not (outcomes and prices and len(outcomes) == len(prices) == 2):
            return (0.50, 0.50)
        up_idx = next(
            (i for i, o in enumerate(outcomes) if str(o).strip().lower() in ("up", "yes")),
            0,
        )
        down_idx = 1 - up_idx
        up = float(prices[up_idx])
        down = float(prices[down_idx])
        if not (0 < up < 1 and 0 < down < 1):
            return (0.50, 0.50)
        return (up, down)
    except Exception as e:
        logger.warning("parse_prices failed: %s", e)
        return (0.50, 0.50)
