"""Wait for a Polymarket binary market to resolve.

Two transports run cooperatively:

* **WebSocket** (wss://ws-subscriptions-clob.polymarket.com/ws/market) —
  primary. Subscribes to the market's two token ids with
  `custom_feature_enabled=true` so the server emits `market_resolved`
  events. Push-based, sub-second latency.
* **HTTP gamma-api** — fallback / safety net. Polled before WS connect (in
  case resolution already happened) and every ~15s while the WS is open
  (in case it disconnects silently or we missed the event).

Behaviour:

* Returns the winner (`'UP'` or `'DOWN'`) as soon as either transport
  sees a resolution. Never returns a Binance-derived guess — Polymarket
  is the only source of truth.
* If the soft `budget_s` elapses without resolution, `on_budget_exceeded`
  is invoked **once** (used by the caller to notify Telegram that the
  next round is being skipped). Resolution polling continues
  indefinitely after that — there is no hard timeout.
* Ctrl+C / KeyboardInterrupt propagates out so the bot stays
  responsive to shutdown.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import websocket  # type: ignore[import-untyped]

from .polymarket_client import get_event_by_slug, parse_resolution, parse_token_ids

logger = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# How often to re-check the HTTP gamma-api alongside the open WS connection.
# Belt-and-braces: catches cases where the WS misses the event or stalls.
HTTP_FALLBACK_INTERVAL_S = 15

# Reconnect delay if the WS drops.
WS_RECONNECT_DELAY_S = 2


def _resolve_via_http(slug: str) -> str | None:
    """One-shot HTTP check. Returns winner or None if not yet resolved."""
    event = get_event_by_slug(slug)
    winner, resolved = parse_resolution(event)
    return winner if resolved else None


class _WsListener:
    """Background WS connection that publishes the winner to a thread-safe
    holder. Runs until stop() is called or it sees the winner."""

    def __init__(self, up_token: str, down_token: str) -> None:
        self.up_token = up_token
        self.down_token = down_token
        self.winner: str | None = None
        self._stop = threading.Event()
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None

    # --- thread-loop side --------------------------------------------------

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        sub = {
            "assets_ids": [self.up_token, self.down_token],
            "type": "market",
            "initial_dump": False,
            "custom_feature_enabled": True,
        }
        try:
            ws.send(json.dumps(sub))
            logger.info("WS subscribed to UP=%s DOWN=%s",
                        self.up_token[:8], self.down_token[:8])
        except Exception as e:
            logger.warning("WS subscribe send failed: %s", e)

    def _on_message(self, ws: websocket.WebSocketApp, msg: str) -> None:
        try:
            data = json.loads(msg)
        except Exception:
            return
        # Polymarket sometimes batches events in a list; handle both shapes.
        events = data if isinstance(data, list) else [data]
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if ev.get("event_type") != "market_resolved":
                continue
            winning = str(ev.get("winning_asset_id") or "")
            if winning == self.up_token:
                self.winner = "UP"
            elif winning == self.down_token:
                self.winner = "DOWN"
            else:
                # Resolution for a different asset id (shouldn't happen since
                # we only subscribed to two). Treat as noise.
                logger.warning("WS market_resolved for unknown asset %s", winning[:12])
                continue
            self._stop.set()
            try:
                ws.close()
            except Exception:
                pass
            return

    def _on_error(self, ws: websocket.WebSocketApp, err: Exception) -> None:
        logger.warning("WS error: %s", err)

    def _on_close(self, ws: websocket.WebSocketApp, code, msg) -> None:
        logger.info("WS closed (code=%s)", code)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                logger.warning("WS run_forever crashed: %s", e)
            if self._stop.is_set():
                return
            time.sleep(WS_RECONNECT_DELAY_S)

    # --- caller side -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="polymarket-ws", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def wait_for_resolution(
    slug: str,
    budget_s: int,
    on_budget_exceeded: Callable[[], None] | None = None,
    poll_interval_s: float = 0.5,
) -> str:
    """Block until Polymarket resolves market `slug`. Return 'UP' or 'DOWN'.

    Strategy:
      1. HTTP primer: if already resolved, return immediately (cheap,
         avoids spinning up a WS for a market that resolved while we
         were sleeping).
      2. Fetch token ids from gamma-api; if available, start a WS
         listener subscribed to both.
      3. Loop: every `poll_interval_s` check the WS-published winner.
         Every HTTP_FALLBACK_INTERVAL_S, also do an HTTP gamma-api
         check (covers a stalled/disconnected WS).
      4. After `budget_s` elapses without resolution, fire
         `on_budget_exceeded` exactly once. Keep waiting indefinitely.
    """
    deadline_soft = time.time() + budget_s
    notified_overrun = False

    # 1) Primer.
    early = _resolve_via_http(slug)
    if early is not None:
        return early

    # 2) Look up token ids + start WS.
    event = get_event_by_slug(slug)
    up_token, down_token = parse_token_ids(event)
    listener: _WsListener | None = None
    if up_token and down_token:
        listener = _WsListener(up_token, down_token)
        listener.start()
    else:
        logger.warning(
            "Polymarket clobTokenIds missing for slug=%s — falling back to HTTP-only polling",
            slug,
        )

    last_http_check = time.time()
    try:
        while True:
            # WS-side winner?
            if listener and listener.winner is not None:
                return listener.winner

            # HTTP fallback tick.
            if time.time() - last_http_check >= HTTP_FALLBACK_INTERVAL_S:
                last_http_check = time.time()
                winner = _resolve_via_http(slug)
                if winner is not None:
                    return winner
                # If we didn't have token ids before, try once more — the
                # event may have just been populated.
                if listener is None:
                    event = get_event_by_slug(slug)
                    up_token, down_token = parse_token_ids(event)
                    if up_token and down_token:
                        listener = _WsListener(up_token, down_token)
                        listener.start()

            # Budget tick.
            if not notified_overrun and time.time() >= deadline_soft:
                notified_overrun = True
                if on_budget_exceeded is not None:
                    try:
                        on_budget_exceeded()
                    except Exception as e:
                        logger.warning("on_budget_exceeded raised: %s", e)

            time.sleep(poll_interval_s)
    finally:
        if listener is not None:
            listener.stop()
