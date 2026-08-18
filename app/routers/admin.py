import csv
import io
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_game_ctx, require_admin
from app.flash import flash
from app.games.base import GameConfig
from app.models import Application, GameProfile, ResetRequest, ResetToken, Team, TeamSlot, User
from app.roster import build_roster_view, team_avg_rating_label
from app.security import generate_reset_token, hash_token
from app.templating import render

router = APIRouter(prefix="/admin", tags=["admin"])


# Статические маршруты /admin/reset-requests и /admin/users/... регистрируются
# ПЕРЕД динамическим /admin/{game} — иначе FastAPI матчит "reset-requests" как game.

@router.get("/reset-requests")
def admin_reset_requests(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), q: str = ""):
    requests_q = db.query(ResetRequest).filter(ResetRequest.status == "pending").order_by(ResetRequest.created_at)
    pending = requests_q.all()

    found_users = []
    if q:
        found_users = (
            db.query(User)
            .filter(User.email.ilike(f"%{q}%") | User.display_name.ilike(f"%{q}%"))
            .limit(20)
            .all()
        )
        nick_matches = (
            db.query(GameProfile)
            .filter(GameProfile.nickname.ilike(f"%{q}%"))
            .limit(20)
            .all()
        )
        seen = {u.id for u in found_users}
        for p in nick_matches:
            if p.user_id not in seen:
                u = db.get(User, p.user_id)
                if u:
                    found_users.append(u)
                    seen.add(u.id)

    issued_link = request.session.pop("issued_reset_link", None)
    return render(
        request, "admin/reset_requests.html", user=user, game=None,
        pending=pending, q=q, found_users=found_users, issued_link=issued_link,
    )


@router.post("/reset-requests/{request_id}/dismiss")
def admin_dismiss_reset_request(request: Request, request_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rr = db.get(ResetRequest, request_id)
    if not rr:
        raise HTTPException(status_code=404)
    rr.status, rr.resolved_at, rr.resolved_by = "dismissed", datetime.utcnow(), user.id
    db.commit()
    flash(request, "Заявка закрыта", "success")
    return RedirectResponse(url="/admin/reset-requests", status_code=303)


@router.post("/users/{target_user_id}/issue-reset-link")
def admin_issue_reset_link(
    request: Request, target_user_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_admin), request_id: str = Form(""),
):
    target = db.get(User, target_user_id)
    if not target:
        raise HTTPException(status_code=404)

    raw_token = generate_reset_token()
    db.add(ResetToken(
        user_id=target.id, token_hash=hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(hours=24),
        created_by=user.id,
    ))
    if request_id.isdigit():
        rr = db.get(ResetRequest, int(request_id))
        if rr:
            rr.status, rr.resolved_at, rr.resolved_by = "resolved", datetime.utcnow(), user.id
    db.commit()

    link = str(request.base_url).rstrip("/") + f"/auth/reset/{raw_token}"
    request.session["issued_reset_link"] = {"email": target.email, "link": link}
    flash(request, "Ссылка сгенерирована — скопируйте и отправьте игроку", "success")
    return RedirectResponse(url="/admin/reset-requests", status_code=303)


# --- маршруты по конкретной дисциплине (/admin/{game}/...) ---

@router.get("/{game}")
def admin_dashboard(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx),
):
    total_users = db.query(User).count()
    profiles = db.query(GameProfile).filter(GameProfile.game == game.key).all()
    teams = db.query(Team).filter(Team.game == game.key).all()
    active_teams = [t for t in teams if t.status != "disbanded"]

    active_member_ids = {
        uid for (uid,) in db.query(TeamSlot.user_id)
        .join(Team, Team.id == TeamSlot.team_id)
        .filter(TeamSlot.state == "filled", Team.game == game.key, Team.status != "disbanded")
        .all()
    }
    applied_or_captain_ids = {t.captain_id for t in teams} | {
        uid for (uid,) in db.query(Application.user_id)
        .join(Team, Team.id == Application.team_id)
        .filter(Team.game == game.key)
        .all()
    }

    stats = {
        "total_users": total_users,
        "with_profile": len(profiles),
        "engaged": len(applied_or_captain_ids),
        "in_team": len(active_member_ids),
        "teams_total": len(active_teams),
        "teams_recruiting": len([t for t in active_teams if t.status == "recruiting"]),
        "teams_full": len([t for t in active_teams if t.status == "full"]),
        "teams_locked": len([t for t in active_teams if t.status == "locked"]),
    }

    ordinals = [p.rating_ordinal for p in profiles]
    histogram = game.histogram_buckets(ordinals)
    max_bucket = max((c for _, c in histogram), default=0) or 1

    role_deficit = []
    for r in game.roles:
        open_slots = sum(
            1 for t in active_teams for s in t.slots if s.role == r.key and s.state == "open"
        )
        waiting = sum(
            1 for p in profiles
            if p.is_looking and r.key in (p.preferred_roles or []) and p.user_id not in active_member_ids
        )
        role_deficit.append({"role": r, "open_slots": open_slots, "waiting_players": waiting})

    waiting_profiles = sorted(
        (p for p in profiles if p.is_looking and p.user_id not in active_member_ids),
        key=lambda p: p.created_at,
    )[:20]

    return render(
        request, "admin/dashboard.html", user=user, game=game, stats=stats,
        histogram=histogram, max_bucket=max_bucket, role_deficit=role_deficit,
        waiting_profiles=waiting_profiles,
    )


@router.get("/{game}/teams")
def admin_teams(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx), q: str = "",
):
    query = db.query(Team).filter(Team.game == game.key)
    if q:
        query = query.filter(Team.name.ilike(f"%{q}%"))
    teams = query.order_by(Team.created_at.desc()).all()
    rows = []
    for t in teams:
        view = build_roster_view(db, t, game)
        rows.append({"team": t, "avg_label": team_avg_rating_label(view, game)})
    return render(request, "admin/teams.html", user=user, game=game, rows=rows, q=q)


@router.get("/{game}/players")
def admin_players(
    request: Request, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx), q: str = "",
):
    query = db.query(GameProfile).filter(GameProfile.game == game.key)
    if q:
        query = query.filter(GameProfile.nickname.ilike(f"%{q}%"))
    profiles = query.order_by(GameProfile.updated_at.desc()).all()
    return render(request, "admin/players.html", user=user, game=game, profiles=profiles, q=q)


@router.post("/{game}/players/{profile_id}/reverify")
def admin_reverify(
    request: Request, profile_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx),
):
    profile = db.get(GameProfile, profile_id)
    if not profile or profile.game != game.key:
        raise HTTPException(status_code=404)
    if not game.has_auto_verify or game.verify_fn is None or not profile.account_id:
        flash(request, "Для этого профиля автопроверка недоступна", "info")
        return RedirectResponse(url=f"/admin/{game.key}/players", status_code=303)

    result = game.verify_fn(profile.account_id, profile.rating_raw)
    profile.verify_status = result.status
    profile.verified_raw = result.verified_raw
    profile.verified_label = result.verified_label
    profile.verified_at = datetime.utcnow()
    db.commit()
    flash(request, "Проверено", "success")
    return RedirectResponse(url=f"/admin/{game.key}/players", status_code=303)


@router.post("/{game}/players/{profile_id}/ban")
def admin_ban_player(
    request: Request, profile_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx),
):
    profile = db.get(GameProfile, profile_id)
    if not profile or profile.game != game.key:
        raise HTTPException(status_code=404)
    target = db.get(User, profile.user_id)
    target.banned_at = None if target.banned_at else datetime.utcnow()
    db.commit()
    flash(request, "Заблокирован" if target.banned_at else "Разблокирован", "success")
    return RedirectResponse(url=f"/admin/{game.key}/players", status_code=303)


@router.get("/{game}/export.csv")
def admin_export_csv(
    db: Session = Depends(get_db), user: User = Depends(require_admin), game: GameConfig = Depends(get_game_ctx),
):
    teams = (
        db.query(Team)
        .filter(Team.game == game.key, Team.status != "disbanded")
        .order_by(Team.name)
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Команда", "Тег", "Роль", "Ник", "Рейтинг", "Игрок email", "Контакт"])
    for t in teams:
        view = build_roster_view(db, t, game)
        for v in view:
            if v["slot"].state != "filled":
                continue
            writer.writerow([
                t.name, t.tag or "", v["role"].label if v["role"] else v["slot"].role,
                v["profile"].nickname if v["profile"] else "", v["profile"].rating_raw if v["profile"] else "",
                v["user"].email if v["user"] else "", (v["user"].contact_note if v["user"] else "") or "",
            ])
    return Response(
        content=buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={game.key}_rosters.csv"},
    )
