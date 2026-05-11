import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        if not self.enabled:
            logger.warning(
                "Telegram disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set). "
                "Messages will be logged only."
            )

    def send(self, text: str) -> None:
        if not self.enabled:
            logger.info("[telegram] %s", text.replace("\n", " | "))
            return
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": text},
                timeout=10,
            )
            if r.status_code != 200:
                logger.warning(
                    "Telegram send failed: %s %s", r.status_code, r.text[:200]
                )
        except Exception as e:
            logger.warning("Telegram send exception: %s", e)
