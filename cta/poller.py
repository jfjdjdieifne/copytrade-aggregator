"""Telegram without a webhook — the cron pulls messages instead.

Why this exists
---------------
Every failure in this project so far has been the same failure in a new costume:

    the Worker returns something the platform will not accept
    -> the webhook fails
    -> the bot goes silent
    -> nothing is visible, because the bot *is* the surface

A webhook is a single point of failure that reports nothing. If the Worker
returns 500 for any reason, Telegram records "Wrong response from the webhook"
and the message is retried a few times and then lost. The operator sees silence.

Polling inverts that. The cron already runs every minute. Instead of waiting to
be called, it calls `getUpdates` and processes the messages itself. Consequences:

* a bad response cannot break the channel — there is no channel to break
* a deploy that crashes cannot lose messages; they stay in Telegram's queue
  until a working Worker collects them (Telegram holds them for 24 hours)
* the failure surface collapses: if the cron runs, the bot works
* `getUpdates` doubles as a receipt — `pending_update_count` tells us whether
  anything is stuck, without relying on a webhook's error field

`getUpdates` and an active webhook are mutually exclusive: Telegram refuses
polling while one is registered, so the cron deletes it once. That is the only
switch involved, and it is automatic.

The trade-off is latency: replies arrive within a minute rather than instantly.
For a radar that reports rare events and answers questions, that is the right
trade — reliability over speed.
"""

from __future__ import annotations

import json

from .state import KEY_OWNER_CHAT, KEY_TG_OFFSET, KEY_TG_POLL_ERRORS, Store

# Telegram's own retention limit for undelivered updates.
BATCH_LIMIT = 25


async def _say(tg, store: Store, chat_id: str, text: str, keyboard=None) -> dict:
    """Send, and if that fails, remember why — polling must not die on one send."""
    result = await tg.send(text, chat_id, keyboard=keyboard)
    if not result.get("ok"):
        try:
            await store.append(
                KEY_TG_POLL_ERRORS,
                {
                    "dir": "out",
                    "chat_id": chat_id,
                    "error": str(result.get("error", ""))[:200],
                },
                cap=10,
            )
        except Exception:
            pass
    return result


async def poll_updates_async(env, fetch, limit: int = BATCH_LIMIT) -> dict:
    """Collect and answer Telegram messages from the cron. Never raises.

    Returns a small report so `/health` and `/setup` can show the truth:
    how many messages were taken, how many replies went out, and the next
    offset that will be requested.
    """
    from .bot import Bot
    from .config import Config
    from .http_bridge import as_fetcher
    from .state import store_from_env
    from .telegram import Telegram

    fetch = as_fetcher(fetch)
    token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
    if not token:
        return {"ok": False, "reason": "no token"}

    store = store_from_env(env)
    tg = Telegram(fetch, token, getattr(env, "TELEGRAM_CHAT_ID", ""))

    try:
        offset = int(await store.get(KEY_TG_OFFSET, 0) or 0)
    except (TypeError, ValueError):
        offset = 0

    report = {"ok": True, "taken": 0, "replied": 0, "offset_before": offset}

    # One-time switch: polling and a webhook cannot coexist.
    if offset == 0:
        try:
            info = await tg.get_webhook_info()
            registered = str((info.get("result") or {}).get("url", "") or "")
            if registered:
                await tg.delete_webhook()
                report["webhook_removed"] = registered
        except Exception as exc:
            report["webhook_error"] = str(exc)[:200]

    # Call getUpdates directly rather than through `get_updates`, which returns
    # `[]` on failure. That would make an unreachable Telegram indistinguishable
    # from a Telegram with nothing to say — the exact silent-zero pattern that
    # hid every other bug in this system.
    payload = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
    if offset:
        payload["offset"] = offset

    try:
        raw = await tg.call("getUpdates", payload)
    except Exception as exc:
        report["ok"] = False
        report["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return report

    if not raw.get("ok"):
        report["ok"] = False
        report["error"] = str(raw.get("error") or raw.get("description") or "getUpdates failed")[:200]
        return report

    updates = raw.get("result") or []
    if not updates:
        report["idle"] = True
        return report

    bot = Bot(tg, store, env=env, cfg=Config.from_env(env))
    highest = offset
    for update in updates:
        try:
            update_id = int(update.get("update_id", 0))
        except (TypeError, ValueError):
            update_id = 0

        try:
            result = await bot.handle_update(update)
            if result.get("delivered") or result.get("kind") in ("whoami", "callback"):
                report["replied"] = int(report["replied"]) + 1
        except Exception as exc:
            # A single bad message must never stop the queue.
            report.setdefault("errors", []).append(str(exc)[:160])

        # Advance past this update whatever happened: retrying a message that
        # cannot be handled just blocks every message behind it.
        highest = max(highest, update_id + 1)

    report["taken"] = len(updates)
    report["offset"] = highest

    try:
        if highest != offset:
            await store.set(KEY_TG_OFFSET, highest)
        await store.set("last_poll_at", _now_iso())
    except Exception as exc:
        report["ok"] = False
        report["store_error"] = str(exc)[:200]

    return report


async def poll_status_async(env, fetch) -> dict:
    """Report the queue without consuming it — for `/setup` and `/health`."""
    from .http_bridge import as_fetcher
    from .state import store_from_env
    from .telegram import Telegram

    fetch = as_fetcher(fetch)
    token = str(getattr(env, "TELEGRAM_BOT_TOKEN", "") or "")
    if not token:
        return {"ok": False, "reason": "no token"}

    store = store_from_env(env)
    tg = Telegram(fetch, token)

    info = await tg.get_webhook_info()
    detail = info.get("result", {}) if info.get("ok") else {}

    owner = ""
    try:
        owner = str(await store.get(KEY_OWNER_CHAT, "") or "")
    except Exception:
        pass
    try:
        last_poll = await store.get("last_poll_at", "")
    except Exception:
        last_poll = ""
    try:
        errors = await store.get(KEY_TG_POLL_ERRORS, []) or []
    except Exception:
        errors = []

    return {
        "ok": bool(info.get("ok")),
        "mode": "polling" if not detail.get("url") else "webhook",
        "registered_url": detail.get("url", ""),
        "pending": detail.get("pending_update_count", 0),
        "last_error": detail.get("last_error_message", ""),
        "offset": await _offset(store),
        "owner": owner,
        "last_poll_at": last_poll,
        "errors": errors[-3:],
    }


async def _offset(store) -> int:
    try:
        return int(await store.get(KEY_TG_OFFSET, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)
