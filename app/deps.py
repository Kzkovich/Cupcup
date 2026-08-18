from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.games import GAMES, GameConfig, game_or_404
from app.models import User

LAST_GAME_COOKIE = "last_game"


class AuthRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


class AdminRequired(Exception):
    pass


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if user is None or user.banned_at is not None:
        request.session.pop("user_id", None)
        return None
    return user


def require_user(request: Request, user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise AuthRequired(next_path=request.url.path)
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise AdminRequired()
    return user


def get_game_ctx(game: str, response: Response) -> GameConfig:
    cfg = game_or_404(game)
    response.set_cookie(LAST_GAME_COOKIE, cfg.key, max_age=180 * 24 * 3600, samesite="lax")
    return cfg


def get_last_game(request: Request) -> GameConfig:
    """Для страниц без {game} в пути (уведомления, личные данные, админ-разделы
    вне конкретной дисциплины) — чтобы шапка не теряла пункты меню."""
    key = request.cookies.get(LAST_GAME_COOKIE, "dota2")
    return GAMES.get(key, GAMES["dota2"])
