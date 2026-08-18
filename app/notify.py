from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.games import GAMES
from app.models import Notification, User

_TEXT = {
    "kicked_from_team": ("Вас исключили из команды «{team_name}»", "team"),
    "team_disbanded": ("Команда «{team_name}» расформирована", "team"),
    "member_left": ("Игрок покинул команду «{team_name}»", "team"),
    "made_captain": ("Вас назначили капитаном команды «{team_name}»", "team"),
    "application_received": ("{applicant} подал(а) заявку в «{team_name}»", "me"),
    "application_accepted": ("Вас приняли в команду «{team_name}» на роль {role}", "team"),
    "application_rejected": ("Ваша заявка в «{team_name}» отклонена", "me"),
    "application_auto_withdrawn": ("Заявка в «{team_name}» отозвана автоматически — игрок нашёл другую команду", "me"),
    "application_withdrawn": ("Заявка в «{team_name}» отозвана игроком", "me"),
    "invite_received": ("Команда «{team_name}» приглашает вас на роль {role}", "me"),
    "invite_accepted": ("Игрок принял приглашение в «{team_name}»", "team"),
    "invite_declined": ("Игрок отклонил приглашение в «{team_name}»", "me"),
    "invite_expired": ("Приглашение в «{team_name}» истекло — игрок вступил в другую команду", "me"),
}


def describe_payload(type_: str, payload: dict) -> dict:
    template, link_kind = _TEXT.get(type_, (type_, "me"))
    text = template.format(**payload)
    game = payload.get("game")
    if link_kind == "team" and payload.get("team_id"):
        link = f"/{game}/teams/{payload['team_id']}"
    else:
        link = f"/{game}/me" if game else "/"
    game_label = GAMES[game].label if game in GAMES else ""
    return {"text": text, "link": link, "game_label": game_label}


def describe(n: Notification) -> dict:
    return describe_payload(n.type, n.payload)


def _send_telegram(chat_id: str, text: str) -> tuple[bool, str | None]:
    settings = get_settings()
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{settings.tg_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=3.0,
        )
        r.raise_for_status()
        return True, None
    except Exception as e:  # сеть/телеграм не должны валить основной флоу
        return False, str(e)


def notify(db: Session, user_id: int, type: str, **payload) -> Notification:
    n = Notification(user_id=user_id, type=type, payload=payload)
    db.add(n)

    settings = get_settings()
    if not settings.telegram_enabled:
        return n

    user = db.get(User, user_id)
    if not user or not user.tg_chat_id:
        return n

    text = describe_payload(type, payload)["text"]
    ok, err = _send_telegram(user.tg_chat_id, text)
    n.tg_sent_at = datetime.utcnow() if ok else None
    n.tg_error = None if ok else err
    return n
