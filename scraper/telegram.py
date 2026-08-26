"""Minimal Telegram Bot API client for posting job alerts."""

from __future__ import annotations

import html
import time

import requests

API_BASE = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4096          # Telegram's hard limit for a single message
SEND_INTERVAL = 1.2     # seconds between messages, well inside Telegram's limits


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise TelegramError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set"
            )
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()
        self._last_send = 0.0

    def _call(self, method: str, payload: dict) -> dict:
        url = API_BASE.format(token=self.token, method=method)
        for attempt in range(5):
            resp = self.session.post(url, json=payload, timeout=30)
            if resp.status_code == 429:
                retry_after = int(
                    resp.json().get("parameters", {}).get("retry_after", 5)
                )
                time.sleep(retry_after + 1)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            body = resp.json()
            if not body.get("ok"):
                raise TelegramError(f"{method} failed: {body.get('description')}")
            return body["result"]
        raise TelegramError(f"{method} failed after retries")

    def send(self, text: str, *, disable_preview: bool = True) -> None:
        """Send one HTML message, throttled and truncated to Telegram's limit."""
        if len(text) > MAX_LEN:
            text = text[: MAX_LEN - 20].rstrip() + "\n…"
        wait = SEND_INTERVAL - (time.monotonic() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": disable_preview},
            },
        )
        self._last_send = time.monotonic()

    def check(self) -> str:
        return self._call("getMe", {}).get("username", "unknown")


def esc(value: str) -> str:
    """Escape text for Telegram's HTML parse mode."""
    return html.escape(value or "", quote=False)
