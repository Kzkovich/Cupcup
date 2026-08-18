from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, get_game_ctx, require_user
from app.flash import flash
from app.games.base import GameConfig
from app.models import Application, Team, TeamSlot, User
from app.notify import notify
from app.roster import build_roster_view, get_active_slot, team_avg_rating_label
from app.routers.profile import get_own_profile
from app.templating import render

router = APIRouter(prefix="/{game}/teams", tags=["teams"])

BUMP_COOLDOWN = timedelta(hours=2)


def get_team_or_404(db: Session, team_id: int, game: GameConfig) -> Team:
    team = db.get(Team, team_id)
    if not team or team.game != game.key:
        raise HTTPException(status_code=404, detail="Команда не найдена")
    return team


def require_captain(team: Team, user: User) -> bool:
    return team.captain_id == user.id


def sync_team_status(team: Team) -> None:
    if team.status in ("recruiting", "full"):
        team.status = "full" if team.filled_count >= len(team.slots) else "recruiting"


def _rating_ordinal_from_query(game: GameConfig, raw: str | None) -> int | None:
    if not raw:
        return None
    if game.rating_kind == "numeric":
        try:
            return int(raw)
        except ValueError:
            return None
    choice = game.rating_choice_by_key(raw)
    return choice.ordinal if choice else None


def _parse_wanted_rating(game: GameConfig, raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        _, ordinal = game.parse_rating(raw)
        return ordinal
    except (ValueError, TypeError):
        return None


@router.get("")
def team_list(
    request: Request, db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user), game: GameConfig = Depends(get_game_ctx),
    role: str = "", rating_min: str = "", rating_max: str = "", open_only: bool = False,
):
    teams = (
        db.query(Team)
        .filter(Team.game == game.key, Team.status != "disbanded")
        .order_by(Team.bumped_at.desc())
        .all()
    )

    min_ord = _rating_ordinal_from_query(game, rating_min)
    max_ord = _rating_ordinal_from_query(game, rating_max)

    rows = []
    for team in teams:
        if role and role not in [s.role for s in team.slots if s.state == "open"]:
            continue
        if open_only and team.filled_count >= len(team.slots):
            continue
        if min_ord is not None and team.rating_max_wanted is not None and team.rating_max_wanted < min_ord:
            continue
        if max_ord is not None and team.rating_min_wanted is not None and team.rating_min_wanted > max_ord:
            continue
        view = build_roster_view(db, team, game)
        rows.append({
            "team": team,
            "avg_label": team_avg_rating_label(view, game),
            "open_roles": [game.role_by_key(r) for r in team.open_roles],
        })

    my_active_team = None
    if user:
        slot = get_active_slot(db, user, game)
        my_active_team = slot.team if slot else None

    return render(
        request, "teams/list.html", user=user, game=game, rows=rows,
        role=role, rating_min=rating_min, rating_max=rating_max, open_only=open_only,
        my_active_team=my_active_team,
    )


@router.get("/new")
def team_new_form(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    own_profile = get_own_profile(db, user, game)
    if not own_profile:
        flash(request, "Сначала заполните игровой профиль — ник и рейтинг", "info")
        return RedirectResponse(url=f"/{game.key}/profile", status_code=303)
    if get_active_slot(db, user, game):
        flash(request, "У вас уже есть команда в этой дисциплине", "error")
        return RedirectResponse(url=f"/{game.key}/teams", status_code=303)
    return render(request, "teams/new.html", user=user, game=game, own_profile=own_profile)


@router.post("/new")
async def team_new_submit(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    own_profile = get_own_profile(db, user, game)
    if not own_profile:
        flash(request, "Сначала заполните игровой профиль", "error")
        return RedirectResponse(url=f"/{game.key}/profile", status_code=303)
    if get_active_slot(db, user, game):
        flash(request, "У вас уже есть команда в этой дисциплине", "error")
        return RedirectResponse(url=f"/{game.key}/teams", status_code=303)

    form = await request.form()
    name = str(form.get("name", "")).strip()
    tag = str(form.get("tag", "")).strip()
    description = str(form.get("description", "")).strip()
    rating_min_wanted = _parse_wanted_rating(game, str(form.get("rating_min_wanted", "")))
    rating_max_wanted = _parse_wanted_rating(game, str(form.get("rating_max_wanted", "")))

    errors = []
    if not name:
        errors.append("Укажите название команды")

    slot_plan: list[tuple[str, str, str]] = []
    mine_count = 0
    for r in game.roles:
        kind = str(form.get(f"role_{r.key}", "open"))
        note = str(form.get(f"note_{r.key}", "")).strip()
        if kind not in ("mine", "open", "reserved"):
            kind = "open"
        if kind == "mine":
            mine_count += 1
        if kind == "reserved" and not note:
            errors.append(f"Укажите заметку для роли «{r.label}» (например, имя друга)")
        slot_plan.append((r.key, kind, note))

    if mine_count != 1:
        errors.append("Выберите ровно одну роль, на которой играете вы")

    if errors:
        for e in errors:
            flash(request, e, "error")
        return render(
            request, "teams/new.html", status_code=400, user=user, game=game, own_profile=own_profile,
            form={"name": name, "tag": tag, "description": description, "slot_plan": dict((k, (kd, n)) for k, kd, n in slot_plan)},
        )

    team = Team(
        game=game.key, name=name, tag=tag or None, captain_id=user.id,
        description=description or None,
        rating_min_wanted=rating_min_wanted, rating_max_wanted=rating_max_wanted,
    )
    db.add(team)
    db.flush()

    for role_key, kind, note in slot_plan:
        if kind == "mine":
            db.add(TeamSlot(team_id=team.id, role=role_key, state="filled", user_id=user.id))
        elif kind == "reserved":
            db.add(TeamSlot(team_id=team.id, role=role_key, state="reserved", note=note))
        else:
            db.add(TeamSlot(team_id=team.id, role=role_key, state="open"))

    db.commit()
    flash(request, "Команда создана", "success")
    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)


@router.get("/{team_id}")
def team_detail(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    view = build_roster_view(db, team, game)
    is_captain = bool(user and require_captain(team, user))

    my_application = None
    my_active_team = None
    is_member = False
    if user:
        my_application = (
            db.query(Application)
            .filter(Application.team_id == team.id, Application.user_id == user.id, Application.status == "pending")
            .first()
        )
        slot = get_active_slot(db, user, game)
        my_active_team = slot.team if slot else None
        is_member = any(v["slot"].user_id == user.id for v in view)

    can_see_contacts = is_captain or is_member or bool(user and user.is_admin)

    return render(
        request, "teams/detail.html", user=user, game=game, team=team, view=view,
        avg_label=team_avg_rating_label(view, game), is_captain=is_captain,
        my_application=my_application, my_active_team=my_active_team,
        can_see_contacts=can_see_contacts,
    )


@router.get("/{team_id}/edit")
def team_edit_form(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Редактировать команду может только капитан", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)
    return render(request, "teams/edit.html", user=user, game=game, team=team)


@router.post("/{team_id}/edit")
def team_edit_submit(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    name: str = Form(...), tag: str = Form(""), description: str = Form(""),
    rating_min_wanted: str = Form(""), rating_max_wanted: str = Form(""),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Редактировать команду может только капитан", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    name = name.strip()
    if not name:
        flash(request, "Укажите название команды", "error")
        return render(request, "teams/edit.html", status_code=400, user=user, game=game, team=team)

    team.name = name
    team.tag = tag.strip() or None
    team.description = description.strip() or None
    team.rating_min_wanted = _parse_wanted_rating(game, rating_min_wanted)
    team.rating_max_wanted = _parse_wanted_rating(game, rating_max_wanted)
    db.commit()
    flash(request, "Изменения сохранены", "success")
    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)


@router.post("/{team_id}/slots/{slot_id}")
def team_slot_update(
    request: Request, team_id: int, slot_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    action: str = Form(...), note: str = Form(""),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Доступно только капитану", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    slot = db.get(TeamSlot, slot_id)
    if not slot or slot.team_id != team.id:
        raise HTTPException(status_code=404, detail="Слот не найден")

    if action == "kick":
        if slot.state != "filled" or slot.user_id == team.captain_id:
            flash(request, "Этого игрока нельзя исключить так", "error")
        else:
            kicked_id = slot.user_id
            slot.state, slot.user_id, slot.note = "open", None, None
            sync_team_status(team)
            notify(db, kicked_id, "kicked_from_team", team_id=team.id, team_name=team.name, game=game.key)
            db.commit()
            flash(request, "Игрок исключён из команды", "success")
    elif action == "set_reserved":
        if slot.state == "filled":
            flash(request, "Слот занят игроком — сначала исключите его", "error")
        elif not note.strip():
            flash(request, "Укажите заметку (например, имя друга)", "error")
        else:
            slot.state, slot.note = "reserved", note.strip()
            db.commit()
            flash(request, "Слот отмечен как занятый", "success")
    elif action == "set_open":
        if slot.state == "filled":
            flash(request, "Слот занят игроком — сначала исключите его", "error")
        else:
            slot.state, slot.note = "open", None
            sync_team_status(team)
            db.commit()
            flash(request, "Слот открыт для заявок", "success")
    else:
        flash(request, "Неизвестное действие", "error")

    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)


@router.post("/{team_id}/bump")
def team_bump(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Доступно только капитану", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    if datetime.utcnow() - team.bumped_at < BUMP_COOLDOWN:
        flash(request, "Поднимать команду можно раз в 2 часа", "error")
    else:
        team.bumped_at = datetime.utcnow()
        db.commit()
        flash(request, "Команда поднята в списке", "success")
    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)


@router.post("/{team_id}/toggle-status")
def team_toggle_status(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Доступно только капитану", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    if team.status == "locked":
        sync_team_status(team)
        flash(request, "Набор возобновлён", "success")
    else:
        team.status = "locked"
        flash(request, "Набор приостановлен", "success")
    db.commit()
    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)


@router.post("/{team_id}/disband")
def team_disband(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Доступно только капитану", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    member_ids = [s.user_id for s in team.slots if s.user_id and s.user_id != user.id]
    team.status = "disbanded"
    for uid in member_ids:
        notify(db, uid, "team_disbanded", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Команда расформирована", "success")
    return RedirectResponse(url=f"/{game.key}/teams", status_code=303)


@router.post("/{team_id}/leave")
def team_leave(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    team = get_team_or_404(db, team_id, game)
    if require_captain(team, user):
        flash(request, "Капитан не может просто выйти — передайте капитанство или расформируйте команду", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    slot = next((s for s in team.slots if s.user_id == user.id), None)
    if not slot:
        flash(request, "Вы не состоите в этой команде", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    slot.state, slot.user_id, slot.note = "open", None, None
    sync_team_status(team)
    notify(db, team.captain_id, "member_left", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Вы покинули команду", "success")
    return RedirectResponse(url=f"/{game.key}/teams", status_code=303)


@router.post("/{team_id}/transfer-captain")
def team_transfer_captain(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    new_captain_user_id: int = Form(...),
):
    team = get_team_or_404(db, team_id, game)
    if not require_captain(team, user):
        flash(request, "Доступно только капитану", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    target_slot = next((s for s in team.slots if s.user_id == new_captain_user_id and s.state == "filled"), None)
    if not target_slot:
        flash(request, "Выберите действующего участника команды", "error")
        return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    team.captain_id = new_captain_user_id
    notify(db, new_captain_user_id, "made_captain", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Капитанство передано", "success")
    return RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)
