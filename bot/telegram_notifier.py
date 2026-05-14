import json
import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        # Bot's own @username — needed to build the no-op URL on the live
        # P&L button (taps open this bot's chat = nothing visibly happens).
        self.username: str | None = None
        # Optional callback fired after every send(). Used to bump a floating
        # P&L pill so it stays the most-recent message regardless of which
        # call site triggered the send.
        self._after_send_hook = None
        if self.enabled:
            self.username = self._fetch_username()
        else:
            logger.warning(
                "Telegram disabled (bot_token / chat_id not set). "
                "Messages will be logged only."
            )

    def set_after_send_hook(self, fn) -> None:
        """Register a callable to run after every send(). The hook should use
        send_pnl_button / delete_message (not send) to avoid re-entrant loops."""
        self._after_send_hook = fn

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _fetch_username(self) -> str | None:
        try:
            r = requests.get(self._url("getMe"), timeout=10)
            if r.status_code == 200 and r.json().get("ok"):
                return r.json()["result"].get("username")
            logger.warning("getMe failed: %s %s", r.status_code, r.text[:200])
        except Exception as e:
            logger.warning("getMe exception: %s", e)
        return None

    def send(self, text: str) -> None:
        if not self.enabled:
            logger.info("[telegram] %s", text.replace("\n", " | "))
            self._run_after_send_hook()
            return
        try:
            r = requests.post(
                self._url("sendMessage"),
                data={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(
                    "Telegram send failed: %s %s", r.status_code, r.text[:200]
                )
        except Exception as e:
            logger.warning("Telegram send exception: %s", e)
        self._run_after_send_hook()

    def _run_after_send_hook(self) -> None:
        if self._after_send_hook is None:
            return
        try:
            self._after_send_hook()
        except Exception as e:
            logger.warning("Telegram after_send hook failed: %s", e)

    def send_pnl_button(self, body: str, button_label: str) -> int | None:
        """Send a message styled as a single full-width inline-keyboard button.

        The button's URL points at this bot's own chat, so tapping it just
        re-opens the chat you're already in — visibly a no-op, no spinner.
        Returns the sent message_id (so the caller can delete it later), or
        None if Telegram is disabled or getMe failed at construction time.
        """
        if not self.enabled or not self.username:
            return None
        markup = {
            "inline_keyboard": [[
                {"text": button_label, "url": f"https://t.me/{self.username}"}
            ]]
        }
        try:
            r = requests.post(
                self._url("sendMessage"),
                data={
                    "chat_id": self.chat_id,
                    "text": body,
                    "reply_markup": json.dumps(markup),
                },
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(
                    "Telegram pill send failed: %s %s",
                    r.status_code, r.text[:200],
                )
                return None
            return int(r.json()["result"]["message_id"])
        except Exception as e:
            logger.warning("Telegram pill send exception: %s", e)
            return None

    def delete_message(self, message_id: int) -> None:
        """Best-effort delete. Silently ignores errors (message already gone,
        too old to delete, etc.) since stale pills don't hurt correctness."""
        if not self.enabled:
            return
        try:
            requests.post(
                self._url("deleteMessage"),
                data={"chat_id": self.chat_id, "message_id": message_id},
                timeout=10,
            )
        except Exception as e:
            logger.debug("Telegram delete exception: %s", e)
