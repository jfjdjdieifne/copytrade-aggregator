"""Telegram I/O — sending, formatting, and command parsing.

This module is deliberately ignorant of business logic: it takes strings and
returns transport results. All the intelligence lives above it, which is what
keeps it testable and keeps Telegram outages from affecting the radar.
"""

from __future__ import annotations

import json
from typing import Any, Optional

TG_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE = 4096


def _chunks(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on line boundaries so a long message is never cut mid-sentence."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            if current:
                out.append(current)
            while len(line) > limit:
                out.append(line[:limit])
                line = line[limit:]
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        out.append(current)
    return out


class Telegram:
    def __init__(self, fetch, token: str, default_chat_id: Optional[str] = None):
        self.fetch = fetch
        self.token = token
        self.default_chat_id = default_chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, payload: dict[str, Any]) -> dict:
        url = TG_API.format(token=self.token, method=method)
        try:
            resp = await self.fetch(
                url,
                {
                    "method": "POST",
                    "headers": {"content-type": "application/json"},
                    "body": json.dumps(payload),
                },
            )
            status = getattr(resp, "status", 200)
            if status != 200:
                return {"ok": False, "error": f"HTTP {status}"}
            data = resp.json()
            if hasattr(data, "__await__"):
                data = await data
            return data if isinstance(data, dict) else {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}

    async def send(
        self,
        text: str,
        chat_id: Optional[str] = None,
        keyboard: Optional[list[list[dict]]] = None,
        disable_preview: bool = True,
    ) -> dict:
        target = chat_id or self.default_chat_id
        if not target or not self.configured:
            return {"ok": False, "error": "telegram not configured"}

        last: dict = {"ok": False}
        for part in _chunks(text):
            payload: dict[str, Any] = {
                "chat_id": target,
                "text": part,
                "parse_mode": "Markdown",
                "disable_web_page_preview": disable_preview,
            }
            if keyboard and part is not None:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            last = await self.call("sendMessage", payload)

            # Markdown can fail on stray characters; retry as plain text once.
            if not last.get("ok"):
                payload.pop("parse_mode", None)
                last = await self.call("sendMessage", payload)
        return last

    async def typing(self, chat_id: Optional[str] = None) -> None:
        target = chat_id or self.default_chat_id
        if not target or not self.configured:
            return
        await self.call("sendChatAction", {"chat_id": target, "action": "typing"})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not self.configured:
            return
        await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        data = await self.call("getUpdates", payload)
        return data.get("result", []) if data.get("ok") else []

    async def set_webhook(self, url: str, secret: str = "") -> dict:
        payload: dict[str, Any] = {"url": url, "allowed_updates": ["message", "callback_query"]}
        if secret:
            payload["secret_token"] = secret
        return await self.call("setWebhook", payload)

    async def delete_webhook(self) -> dict:
        return await self.call("deleteWebhook", {})

    async def get_webhook_info(self) -> dict:
        """Read back what Telegram thinks the webhook is.

        Without this, a failed `setWebhook` is invisible: the bot simply goes
        quiet and there is no way to tell a wrong URL from a wrong token from a
        webhook that was never registered at all.
        """
        return await self.call("getWebhookInfo", {})


# ── keyboards ────────────────────────────────────────────────────────────────
def signal_keyboard(signal_id: str) -> list[list[dict]]:
    return [
        [
            {"text": "📊 تفاصيل المتداولين", "callback_data": f"det:{signal_id}"},
            {"text": "❓ لماذا هذه الدرجة", "callback_data": f"why:{signal_id}"},
        ],
        [
            {"text": "📈 كل المراكز", "callback_data": "positions"},
            {"text": "📉 أداء الإشارات", "callback_data": "stats"},
        ],
    ]


def main_keyboard() -> list[list[dict]]:
    return [
        [
            {"text": "🔍 آخر الإشارات", "callback_data": "latest"},
            {"text": "📈 كل المراكز", "callback_data": "positions"},
        ],
        [
            {"text": "👥 قائمة المراقبة", "callback_data": "watchlist"},
            {"text": "📉 الأداء", "callback_data": "stats"},
        ],
        [{"text": "⚙️ حالة النظام", "callback_data": "health"}],
    ]


# ── update parsing ───────────────────────────────────────────────────────────
def parse_update(update: dict) -> Optional[dict]:
    """Normalise a Telegram update into {chat_id, text, callback_id, user}."""
    if not isinstance(update, dict):
        return None

    cb = update.get("callback_query")
    if cb:
        message = cb.get("message") or {}
        chat = message.get("chat") or {}
        user = cb.get("from") or {}
        return {
            "kind": "callback",
            "chat_id": str(chat.get("id", "")),
            "text": cb.get("data", "") or "",
            "callback_id": cb.get("id", ""),
            "user": user.get("username") or user.get("first_name") or str(user.get("id", "")),
        }

    msg = update.get("message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    user = msg.get("from") or {}
    return {
        "kind": "message",
        "chat_id": str(chat.get("id", "")),
        "text": (msg.get("text") or "").strip(),
        "callback_id": "",
        "user": user.get("username") or user.get("first_name") or str(user.get("id", "")),
    }


HELP_TEXT = """🤖 *رادار — مساعد اتفاق المتداولين*

أنا أرصد متداولين محترفين مستقلين على منصات مجانية، وأخبرك عندما يتفقون على نفس العملة والاتجاه.

*الأوامر:*
/latest — آخر الإشارات
/positions — المراكز الحالية للمراقبة
/watchlist — المتداولون المحترفون
/stats — أداء الإشارات فعلياً
/why — لماذا رُفضت إشارة
/health — حالة النظام
/help — هذه القائمة

أو اكتب سؤالك بالعربي مباشرة — سأفهمه وأشرح من بيانات النظام.

⚠️ كل ما يصلك تحليل آلي (HYPOTHETICAL_) وليس نصيحة مالية."""
