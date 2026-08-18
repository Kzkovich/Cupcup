from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user, get_game_ctx, require_user
from app.flash import flash
from app.games import game_or_404
from app.games.base import GameConfig
from app.membership import join_team
from app.models import GameProfile, Invite, Team, TeamSlot, User
from app.notify import notify
from app.roster import get_active_slot
from app.routers.teams import get_team_or_404
from app.templating import render

router = APIRouter(tags=["agents"])


def _active_user_ids(db: Session, game: GameConfig) -> set[int]:
    rows = (
        db.query(TeamSlot.user_id)
        .join(Team, Team.id == TeamSlot.team_id)
        .filter(TeamSlot.state == "filled", Team.game == game.key, Team.status != "disbanded")
        .all()
    )
    return {uid for (uid,) in rows}


@router.get("/{game}/agents")
def agents_list(
    request: Request, db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user), game: GameConfig = Depends(get_game_ctx),
    role: str = "", rating_min: str = "", rating_max: str = "",
):
    taken = _active_user_ids(db, game)
    q = db.query(GameProfile).filter(GameProfile.game == game.key, GameProfile.is_looking == True)  # noqa: E712
    profiles = [p for p in q.all() if p.user_id not in taken and (not user or p.user_id != user.id)]

    if role:
        profiles = [p for p in profiles if role in (p.preferred_roles or [])]

    def ordinal_of(raw: str) -> int | None:
        if not raw:
            return None
        if game.rating_kind == "numeric":
            try:
                return int(raw)
            except ValueError:
                return None
        choice = game.rating_choice_by_key(raw)
        return choice.ordinal if choice else None

    min_ord, max_ord = ordinal_of(rating_min), ordinal_of(rating_max)
    if min_ord is not None:
        profiles = [p for p in profiles if p.rating_ordinal >= min_ord]
    if max_ord is not None:
        profiles = [p for p in profiles if p.rating_ordinal <= max_ord]

    profiles.sort(key=lambda p: p.updated_at, reverse=True)

    my_team = None
    my_open_roles = []
    if user:
        slot = get_active_slot(db, user, game)
        if slot and slot.team.captain_id == user.id and slot.team.status == "recruiting":
            my_team = slot.team
            my_open_roles = [game.role_by_key(r) for r in my_team.open_roles]

    return render(
        request, "agents/list.html", user=user, game=game, profiles=profiles,
        role=role, rating_min=rating_min, rating_max=rating_max,
        my_team=my_team, my_open_roles=my_open_roles,
    )


@router.post("/{game}/agents/{target_user_id}/invite")
def invite_agent(
    request: Request, target_user_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_user), game: GameConfig = Depends(get_game_ctx),
    role: str = Form(...), message: str = Form(""),
):
    redirect = RedirectResponse(url=f"/{game.key}/agents", status_code=303)
    slot = get_active_slot(db, user, game)
    if not slot or slot.team.captain_id != user.id:
        flash(request, "Приглашать может только капитан своей команды", "error")
        return redirect
    team = slot.team
    if team.status != "recruiting":
        flash(request, "Команда сейчас не набирает игроков", "error")
        return redirect

    target_slot = next((s for s in team.slots if s.role == role), None)
    if not target_slot or target_slot.state != "open":
        flash(request, "Эта роль сейчас не свободна", "error")
        return redirect

    target_user = db.get(User, target_user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    if get_active_slot(db, target_user, game):
        flash(request, "У этого игрока уже есть команда", "error")
        return redirect

    existing = (
        db.query(Invite)
        .filter(Invite.team_id == team.id, Invite.user_id == target_user_id, Invite.status == "pending")
        .first()
    )
    if existing:
        flash(request, "Приглашение этому игроку уже отправлено", "info")
        return redirect

    db.add(Invite(team_id=team.id, user_id=target_user_id, role=role, message=message.strip() or None))
    notify(db, target_user_id, "invite_received", team_id=team.id, team_name=team.name, role=role, game=game.key)
    db.commit()
    flash(request, "Приглашение отправлено", "success")
    return redirect


def _get_invite_or_404(db: Session, invite_id: int) -> Invite:
    inv = db.get(Invite, invite_id)
    if not inv:
        raise HTTPException(status_code=404, detail="Приглашение не найдено")
    return inv


@router.post("/invites/{invite_id}/accept")
def invite_accept(request: Request, invite_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    inv = _get_invite_or_404(db, invite_id)
    team = db.get(Team, inv.team_id)
    game = game_or_404(team.game)
    redirect = RedirectResponse(url=f"/{game.key}/me", status_code=303)

    if inv.user_id != user.id:
        flash(request, "Это не ваше приглашение", "error")
        return redirect
    if inv.status != "pending":
        flash(request, "Приглашение уже обработано", "error")
        return redirect
    if get_active_slot(db, user, game):
        flash(request, "У вас уже есть команда в этой дисциплине", "error")
        return redirect

    target_slot = next((s for s in team.slots if s.role == inv.role), None)
    if not target_slot or target_slot.state != "open":
        flash(request, "Эта роль в команде уже занята", "error")
        inv.status, inv.decided_at = "expired", datetime.utcnow()
        db.commit()
        return redirect

    join_team(db, game, team, target_slot, user)
    inv.status, inv.decided_at = "accepted", datetime.utcnow()
    notify(db, team.captain_id, "invite_accepted", team_id=team.id, team_name=team.name, role=inv.role, game=game.key)
    db.commit()
    flash(request, f"Вы вступили в команду «{team.name}»", "success")
    return redirect


@router.post("/invites/{invite_id}/decline")
def invite_decline(request: Request, invite_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    inv = _get_invite_or_404(db, invite_id)
    team = db.get(Team, inv.team_id)
    game = game_or_404(team.game)

    if inv.user_id != user.id:
        flash(request, "Это не ваше приглашение", "error")
        return RedirectResponse(url=f"/{game.key}/me", status_code=303)
    if inv.status != "pending":
        flash(request, "Приглашение уже обработано", "error")
        return RedirectResponse(url=f"/{game.key}/me", status_code=303)

    inv.status, inv.decided_at = "declined", datetime.utcnow()
    notify(db, team.captain_id, "invite_declined", team_id=team.id, team_name=team.name, game=game.key)
    db.commit()
    flash(request, "Приглашение отклонено", "success")
    return RedirectResponse(url=f"/{game.key}/me", status_code=303)
