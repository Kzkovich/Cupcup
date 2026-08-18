from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_game_ctx, require_user
from app.flash import flash
from app.games.base import GameConfig
from app.models import GameProfile, User
from app.templating import render

router = APIRouter(prefix="/{game}", tags=["profile"])


def get_own_profile(db: Session, user: User, game: GameConfig) -> GameProfile | None:
    return (
        db.query(GameProfile)
        .filter(GameProfile.user_id == user.id, GameProfile.game == game.key)
        .first()
    )


@router.get("/profile")
def profile_form(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    profile = get_own_profile(db, user, game)
    return render(request, "profile/edit.html", user=user, game=game, profile=profile)


@router.post("/profile")
def profile_submit(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    nickname: str = Form(...),
    account_id: str = Form(""),
    region: str = Form(""),
    profile_url: str = Form(""),
    rating: str = Form(...),
    preferred_roles: list[str] = Form([]),
    is_looking: bool = Form(False),
    about: str = Form(""),
):
    nickname = nickname.strip()
    errors = []
    if not nickname:
        errors.append("Укажите игровой ник")
    if game.region_choices and region not in game.region_choices:
        errors.append("Выберите регион из списка")
    valid_roles = {r.key for r in game.roles}
    preferred_roles = [r for r in preferred_roles if r in valid_roles]

    try:
        rating_raw, rating_ordinal = game.parse_rating(rating)
    except (ValueError, TypeError):
        errors.append("Некорректный рейтинг")
        rating_raw, rating_ordinal = "0", 0

    if errors:
        for e in errors:
            flash(request, e, "error")
        fake_profile = GameProfile(
            nickname=nickname, account_id=account_id, region=region,
            profile_url=profile_url, rating_raw=rating, preferred_roles=preferred_roles,
            is_looking=is_looking, about=about,
        )
        return render(request, "profile/edit.html", status_code=400, user=user, game=game, profile=fake_profile)

    profile = get_own_profile(db, user, game)
    is_new = profile is None
    if profile is None:
        profile = GameProfile(user_id=user.id, game=game.key)
        db.add(profile)

    rating_changed = profile.rating_raw != rating_raw if not is_new else True

    profile.nickname = nickname
    profile.account_id = account_id.strip() or None
    profile.region = region or None
    profile.profile_url = profile_url.strip() or None
    profile.rating_raw = rating_raw
    profile.rating_ordinal = rating_ordinal
    profile.preferred_roles = preferred_roles
    profile.is_looking = is_looking
    profile.about = about.strip() or None

    if rating_changed and profile.verify_status != "none":
        profile.verify_status = "none"
        profile.verified_raw = None
        profile.verified_label = None
        profile.verified_at = None

    db.commit()
    flash(request, "Профиль сохранён", "success")
    return RedirectResponse(url=f"/{game.key}/profile", status_code=303)


@router.post("/profile/verify")
def profile_verify(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    if not game.has_auto_verify or game.verify_fn is None:
        flash(request, "Для этой дисциплины автопроверки нет — рейтинг проверит организатор вручную", "info")
        return RedirectResponse(url=f"/{game.key}/profile", status_code=303)

    profile = get_own_profile(db, user, game)
    if not profile or not profile.account_id:
        flash(request, "Сначала укажите Steam ID / ссылку на профиль", "error")
        return RedirectResponse(url=f"/{game.key}/profile", status_code=303)

    result = game.verify_fn(profile.account_id, profile.rating_raw)
    profile.verify_status = result.status
    profile.verified_raw = result.verified_raw
    profile.verified_label = result.verified_label
    profile.verified_at = datetime.utcnow()
    db.commit()

    messages = {
        "ok": ("Рейтинг подтверждён", "success"),
        "mismatch": (result.detail or "Есть расхождение с заявленным рейтингом", "error"),
        "private": (result.detail or "Профиль скрыт — проверить не получилось", "info"),
        "failed": (result.detail or "Проверить не удалось, попробуйте позже", "info"),
    }
    text, level = messages.get(result.status, ("Проверено", "info"))
    flash(request, text, level)
    return RedirectResponse(url=f"/{game.key}/profile", status_code=303)
