"""Опциональная привязка Telegram для дублирования уведомлений.

Работает только если задан TG_BOT_TOKEN — иначе весь модуль неактивен
и в интерфейсе не появляется. Поллинг запускается в фоновой задаче
того же процесса uvicorn (без отдельного systemd-сервиса).
"""
import asyncio
import logging
import secrets
import time
from datetime import datetime

import httpx

from app.config import get_settings
from app.db import SessionLocal
from app.models import User

logger = logging.getLogger("telegram_bot")

_LINK_CODE_TTL = 5 * 60
_link_codes: dict[str, tuple[int, float]] = {}  # code -> (user_id, expires_at)


def generate_link_code(user_id: int) -> str:
    code = secrets.token_hex(4)
    _link_codes[code] = (user_id, time.time() + _LINK_CODE_TTL)
    return code


def _pop_valid_code(code: str) -> int | None:
    entry = _link_codes.pop(code, None)
    if not entry:
        return None
    user_id, expires_at = entry
    if time.time() > expires_at:
        return None
    return user_id


def deep_link(code: str) -> str:
    settings = get_settings()
    return f"https://t.me/{settings.tg_bot_username}?start={code}"


def _handle_start(chat_id: int, tg_username: str | None, code: str) -> str:
    user_id = _pop_valid_code(code)
    if not user_id:
        return "Код устарел или неверный. Вернитесь на сайт и запросите новую ссылку."
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user:
            return "Аккаунт не найден."
        user.tg_chat_id = str(chat_id)
        user.tg_username = tg_username
        db.commit()
    return "Готово! Уведомления AlfaCyberCup теперь дублируются сюда."


async def _process_update(client: httpx.AsyncClient, token: str, update: dict) -> None:
    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or not text.startswith("/start"):
        return
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        reply = "Перейдите на сайт AlfaCyberCup и нажмите «Подключить Telegram», чтобы получить персональную ссылку."
    else:
        reply = _handle_start(chat_id, message.get("from", {}).get("username"), parts[1].strip())
    try:
        await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": reply})
    except Exception:
        logger.exception("Не удалось отправить ответ в Telegram")


async def poll_loop() -> None:
    settings = get_settings()
    token = settings.tg_bot_token
    offset = 0
    async with httpx.AsyncClient(timeout=35.0) as client:
        logger.info("Telegram polling started")
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={"timeout": 25, "offset": offset},
                )
                resp.raise_for_status()
                data = resp.json()
                for update in data.get("result", []):
                    offset = max(offset, update["update_id"] + 1)
                    await _process_update(client, token, update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram polling error, retry in 5s")
                await asyncio.sleep(5)
