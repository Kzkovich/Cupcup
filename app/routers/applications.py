from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_game_ctx, require_user
from app.flash import flash
from app.games import game_or_404
from app.games.base import GameConfig
from app.membership import join_team
from app.models import Application, GameProfile, Invite, Team, User
from app.notify import notify
from app.roster import get_active_slot
from app.routers.profile import get_own_profile
from app.routers.teams import get_team_or_404
from app.templating import render

router = APIRouter(tags=["applications"])


@router.post("/{game}/teams/{team_id}/apply")
def apply_to_team(
    request: Request, team_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    roles: list[str] = Form([]), message: str = Form(""),
):
    team = get_team_or_404(db, team_id, game)
    redirect = RedirectResponse(url=f"/{game.key}/teams/{team.id}", status_code=303)

    if team.captain_id == user.id:
        flash(request, "Вы капитан этой команды", "info")
        return redirect
    if get_active_slot(db, user, game):
        flash(request, "У вас уже есть команда в этой дисциплине", "error")
        return redirect

    own_profile = get_own_profile(db, user, game)
    if not own_profile:
        flash(request, "Сначала заполните игровой профиль", "error")
        return RedirectResponse(url=f"/{game.key}/profile", status_code=303)

    if team.status != "recruiting":
        flash(request, "Команда сейчас не набирает игроков", "error")
        return redirect

    open_roles = {s.role for s in team.slots if s.state == "open"}
    chosen = [r for r in roles if r in open_roles]
    if not chosen:
        flash(request, "Выберите хотя бы одну открытую роль", "error")
        return redirect

    existing = (
        db.query(Application)
        .filter(Application.team_id == team.id, Application.user_id == user.id, Application.status == "pending")
        .first()
    )
    if existing:
        flash(request, "Вы уже подали заявку в эту команду", "info")
        return redirect

    db.add(Application(team_id=team.id, user_id=user.id, roles=chosen, message=message.strip() or None))
    notify(
        db, team.captain_id, "application_received",
        team_id=team.id, team_name=team.name, applicant=own_profile.nickname, game=game.key,
    )
    db.commit()
    flash(request, "Заявка отправлена", "success")
    return redirect


def _get_application_or_404(db: Session, application_id: int) -> Application:
    app_row = db.get(Application, application_id)
    if not app_row:
        raise HTTPException(status_code=404, detail="Заявка не найдена")
    return app_row


@router.post("/applications/{application_id}/accept")
def application_accept(
    request: Request, application_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    app_row = _get_application_or_404(db, application_id)
    team = db.get(Team, app_row.team_id)
    game = game_or_404(team.game)
    redirect = RedirectResponse(url=f"/{game.key}/me", status_code=303)

    if team.captain_id != user.id:
        flash(request, "Доступно только капитану", "error")
        return redirect
    if app_row.status != "pending":
        flash(request, "Заявка уже обработана", "error")
        return redirect

    open_roles = {s.role for s in team.slots if s.state == "open"}
    chosen_role = next((r for r in app_row.roles if r in open_roles), None)
    if chosen_role is None:
        flash(request, "Все выбранные в заявке роли уже заняты", "error")
        return redirect

    slot = next(s for s in team.slots if s.role == chosen_role)
    applicant = app_row.applicant
    join_team(db, game, team, slot, applicant)
    app_row.status, app_row.decided_at, app_row.decided_by = "accepted", datetime.utcnow(), user.id

    notify(
        db, app_row.user_id, "application_accepted",
        team_id=team.id, team_name=team.name, role=chosen_role, game=game.key,
    )
    db.commit()
    flash(request, "Игрок принят в команду", "success")
    return redirect


@router.post("/applications/{application_id}/reject")
def application_reject(
    request: Request, application_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    app_row = _get_application_or_404(db, application_id)
    team = db.get(Team, app_row.team_id)
    game = game_or_404(team.game)
    redirect = RedirectResponse(url=f"/{game.key}/me", status_code=303)

    if team.captain_id != user.id:
        flash(request, "Доступно только капитану", "error")
        return redirect
    if app_row.status != "pending":
        flash(request, "Заявка уже обработана", "error")
        return redirect

    app_row.status, app_row.decided_at, app_row.decided_by = "rejected", datetime.utcnow(), user.id
    notify(db, app_row.user_id, "application_rejected", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Заявка отклонена", "success")
    return redirect


@router.post("/applications/{application_id}/withdraw")
def application_withdraw(
    request: Request, application_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    app_row = _get_application_or_404(db, application_id)
    team = db.get(Team, app_row.team_id)
    game = game_or_404(team.game)

    if app_row.user_id != user.id:
        flash(request, "Это не ваша заявка", "error")
        return RedirectResponse(url=f"/{game.key}/me", status_code=303)
    if app_row.status != "pending":
        flash(request, "Заявка уже обработана", "error")
        return RedirectResponse(url=f"/{game.key}/me", status_code=303)

    app_row.status, app_row.decided_at = "withdrawn", datetime.utcnow()
    notify(db, team.captain_id, "application_withdrawn", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Заявка отозвана", "success")
    return RedirectResponse(url=request.headers.get("referer") or f"/{game.key}/teams/{team.id}", status_code=303)


@router.get("/{game}/me")
def my_dashboard(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
):
    own_profile = get_own_profile(db, user, game)
    active_slot = get_active_slot(db, user, game)
    active_team = active_slot.team if active_slot else None
    is_captain = bool(active_team and active_team.captain_id == user.id)

    outgoing = (
        db.query(Application)
        .join(Team, Team.id == Application.team_id)
        .filter(Application.user_id == user.id, Team.game == game.key)
        .order_by(Application.created_at.desc())
        .limit(30)
        .all()
    )

    incoming = []
    if is_captain:
        pending = (
            db.query(Application)
            .filter(Application.team_id == active_team.id, Application.status == "pending")
            .order_by(Application.created_at)
            .all()
        )
        applicant_ids = [a.user_id for a in pending]
        profiles = {}
        if applicant_ids:
            rows = db.query(GameProfile).filter(GameProfile.user_id.in_(applicant_ids), GameProfile.game == game.key).all()
            profiles = {p.user_id: p for p in rows}
        for a in pending:
            incoming.append({"app": a, "profile": profiles.get(a.user_id), "user": a.applicant})

    my_invites = []
    if not active_team:
        my_invites = (
            db.query(Invite)
            .join(Team, Team.id == Invite.team_id)
            .filter(Invite.user_id == user.id, Invite.status == "pending", Team.game == game.key)
            .order_by(Invite.created_at.desc())
            .all()
        )

    return render(
        request, "applications/me.html", user=user, game=game,
        own_profile=own_profile, active_team=active_team, is_captain=is_captain,
        outgoing=outgoing, incoming=incoming, my_invites=my_invites,
    )
