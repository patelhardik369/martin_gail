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


def _first_market(event: dict | None) -> dict | None:
    if not event:
        return None
    markets = event.get("markets") or []
    return markets[0] if markets else None


def _outcome_index(outcomes: list, label_options: tuple[str, ...]) -> int | None:
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() in label_options:
            return i
    return None


def parse_prices(event: dict | None) -> tuple[float, float]:
    """Return (up_price, down_price) for the event's first market.
    Polymarket's outcomes/outcomePrices arrive as JSON-encoded strings."""
    if not event:
        return (0.50, 0.50)
    try:
        m = _first_market(event)
        if not m:
            return (0.50, 0.50)
        outcomes = _maybe_parse(m.get("outcomes"))
        prices = _maybe_parse(m.get("outcomePrices"))
        if not (outcomes and prices and len(outcomes) == len(prices) == 2):
            return (0.50, 0.50)
        up_idx = _outcome_index(outcomes, ("up", "yes"))
        if up_idx is None:
            up_idx = 0
        down_idx = 1 - up_idx
        up = float(prices[up_idx])
        down = float(prices[down_idx])
        if not (0 < up < 1 and 0 < down < 1):
            return (0.50, 0.50)
        return (up, down)
    except Exception as e:
        logger.warning("parse_prices failed: %s", e)
        return (0.50, 0.50)


def parse_token_ids(event: dict | None) -> tuple[str | None, str | None]:
    """Return (up_token_id, down_token_id) — CLOB asset ids for WS subscription.
    None if unavailable. Polymarket encodes clobTokenIds as a JSON string."""
    m = _first_market(event)
    if not m:
        return (None, None)
    try:
        outcomes = _maybe_parse(m.get("outcomes"))
        tokens = _maybe_parse(m.get("clobTokenIds"))
        if not (outcomes and tokens and len(outcomes) == len(tokens) == 2):
            return (None, None)
        up_idx = _outcome_index(outcomes, ("up", "yes"))
        if up_idx is None:
            up_idx = 0
        down_idx = 1 - up_idx
        return (str(tokens[up_idx]), str(tokens[down_idx]))
    except Exception as e:
        logger.warning("parse_token_ids failed: %s", e)
        return (None, None)


def parse_resolution(event: dict | None) -> tuple[str | None, bool]:
    """Inspect outcomePrices to decide if the market is resolved.
    Returns (winner, resolved). Winner is 'UP' or 'DOWN' when resolved=True.

    A binary market is considered resolved when:
      - `closed` is True (no more trading), AND
      - outcomePrices collapse to [1, 0] or [0, 1] (one side fully wins).
    """
    m = _first_market(event)
    if not m:
        return (None, False)
    if not m.get("closed"):
        return (None, False)
    try:
        outcomes = _maybe_parse(m.get("outcomes"))
        prices = _maybe_parse(m.get("outcomePrices"))
        if not (outcomes and prices and len(outcomes) == len(prices) == 2):
            return (None, False)
        p0, p1 = float(prices[0]), float(prices[1])
        # Resolved iff one side is 1.0 and the other is 0.0 (tolerate tiny float noise).
        if abs(p0 - 1.0) < 1e-6 and abs(p1) < 1e-6:
            winner_idx = 0
        elif abs(p1 - 1.0) < 1e-6 and abs(p0) < 1e-6:
            winner_idx = 1
        else:
            return (None, False)
        up_idx = _outcome_index(outcomes, ("up", "yes"))
        if up_idx is None:
            up_idx = 0
        return (("UP" if winner_idx == up_idx else "DOWN"), True)
    except Exception as e:
        logger.warning("parse_resolution failed: %s", e)
        return (None, False)
