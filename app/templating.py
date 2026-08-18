from datetime import datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.flash import pop_flashed
from app.games import GAMES

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["GAMES"] = GAMES
templates.env.globals["APP_SETTINGS"] = get_settings()


def _dt(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if value is None:
        return "—"
    return value.strftime(fmt)


def _timeago(value: datetime | None) -> str:
    if value is None:
        return "—"
    delta = datetime.utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "только что"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    if days < 30:
        return f"{days} дн назад"
    return _dt(value, "%d.%m.%Y")


templates.env.filters["dt"] = _dt
templates.env.filters["timeago"] = _timeago


def render(request: Request, name: str, status_code: int = 200, **context):
    ctx = {
        "request": request,
        "flashed": pop_flashed(request),
    }
    ctx.update(context)

    if ctx.get("user") and "notif_unread_count" not in ctx:
        from app.db import SessionLocal
        from app.models import Notification

        with SessionLocal() as s:
            ctx["notif_unread_count"] = (
                s.query(Notification)
                .filter(Notification.user_id == ctx["user"].id, Notification.read_at.is_(None))
                .count()
            )

    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
