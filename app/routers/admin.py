import csv
import io
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_game_ctx, get_last_game, require_admin
from app.flash import flash
from app.demo.purge import demo_counts, purge_demo
from app.demo.scenario_runner import run_isolated
from app.demo.seed import AlreadySeeded, seed_demo
from app.games import GAMES
from app.games.base import GameConfig
from app.models import (
    Application, GameProfile, Invite, ResetRequest, ResetToken, Setting, Team, TeamSlot, User,
)
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
        request, "admin/reset_requests.html", user=user, game=get_last_game(request),
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


@router.get("/admins")
def admin_admins(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin), q: str = ""):
    admins = db.query(User).filter(User.is_admin == True).order_by(User.email).all()  # noqa: E712
    found_users = []
    if q:
        found_users = (
            db.query(User)
            .filter(User.email.ilike(f"%{q}%") | User.display_name.ilike(f"%{q}%"))
            .filter(User.is_admin == False)  # noqa: E712
            .limit(20)
            .all()
        )
    return render(
        request, "admin/admins.html", user=user, game=get_last_game(request),
        admins=admins, q=q, found_users=found_users,
    )


@router.post("/users/{target_user_id}/toggle-admin")
def admin_toggle_admin(
    request: Request, target_user_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin),
):
    target = db.get(User, target_user_id)
    if not target:
        raise HTTPException(status_code=404)
    if target.id == user.id:
        flash(request, "Нельзя снять права администратора с самого себя", "error")
        return RedirectResponse(url="/admin/admins", status_code=303)

    target.is_admin = not target.is_admin
    db.commit()
    flash(request, f"{target.email}: {'выдали права администратора' if target.is_admin else 'сняли права администратора'}", "success")
    return RedirectResponse(url="/admin/admins", status_code=303)


# --- демо-режим: страница не прописана в навигации, открывается по прямому адресу /admin/demo ---

def _personas(db: Session) -> list[dict]:
    """Несколько готовых демо-аккаунтов, под которыми админ может пройти клиентский путь."""
    out: list[dict] = []

    def nick(user_id: int, game: str) -> str:
        p = db.query(GameProfile).filter(GameProfile.user_id == user_id, GameProfile.game == game).first()
        return p.nickname if p else f"#{user_id}"

    for gk, g in GAMES.items():
        busy = {uid for (uid,) in db.query(TeamSlot.user_id).join(Team, Team.id == TeamSlot.team_id)
                .filter(TeamSlot.state == "filled", Team.game == gk, Team.status != "disbanded").all()}

        row = (
            db.query(Team, func.count(Application.id))
            .join(Application, Application.team_id == Team.id)
            .filter(Team.demo_tag.isnot(None), Team.game == gk, Application.status == "pending")
            .group_by(Team.id).order_by(func.count(Application.id).desc()).first()
        )
        if row:
            out.append({"game": gk, "game_label": g.label, "label": f"Капитан «{row[0].name}» — {row[1]} заявк. ждут решения",
                        "user_id": row[0].captain_id, "nick": nick(row[0].captain_id, gk)})

        inv = (
            db.query(Invite).join(Team, Team.id == Invite.team_id)
            .filter(Invite.status == "pending", Team.game == gk, Team.demo_tag.isnot(None)).first()
        )
        if inv:
            out.append({"game": gk, "game_label": g.label, "label": f"Игрок, которого пригласили в «{inv.team.name}»",
                        "user_id": inv.user_id, "nick": nick(inv.user_id, gk)})

        agent = next(
            (p for p in db.query(GameProfile).join(User, User.id == GameProfile.user_id)
             .filter(GameProfile.game == gk, GameProfile.is_looking.is_(True), User.demo_tag.isnot(None))
             .order_by(GameProfile.rating_ordinal.desc()).all() if p.user_id not in busy), None)
        if agent:
            out.append({"game": gk, "game_label": g.label, "label": "Свободный игрок без команды (ищет, подаёт заявки)",
                        "user_id": agent.user_id, "nick": agent.nickname})
    return out


def _last_run(db: Session) -> dict | None:
    row = db.get(Setting, "demo_last_run")
    if not row:
        return None
    try:
        return json.loads(row.value)
    except ValueError:
        return None


@router.get("/demo")
def admin_demo(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return render(
        request, "admin/demo.html", user=user, game=get_last_game(request),
        counts=demo_counts(db), personas=_personas(db), last_run=_last_run(db),
    )


@router.post("/demo/seed")
def admin_demo_seed(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    try:
        res = seed_demo(db)
        flash(request, f"Демо-данные созданы: {res['users']} игроков, {res['teams']} команд, "
                       f"{res['applications']} заявок, {res['invites']} приглашений", "success")
    except AlreadySeeded as e:
        flash(request, str(e), "info")
    return RedirectResponse(url="/admin/demo", status_code=303)


@router.post("/demo/purge")
def admin_demo_purge(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    res = purge_demo(db)
    flash(request, f"Удалено: {res['users']} игроков, {res['teams']} команд, {res['applications']} заявок, "
                   f"{res['invites']} приглашений, {res['notifications']} уведомлений. Настоящие данные не тронуты.",
          "success")
    return RedirectResponse(url="/admin/demo", status_code=303)


@router.post("/demo/run")
def admin_demo_run(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    db.rollback()  # не держим транзакцию, пока сценарий работает в отдельном процессе
    report = run_isolated()
    report["at"] = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    row = db.get(Setting, "demo_last_run")
    payload = json.dumps(report, ensure_ascii=False)
    if row:
        row.value = payload
    else:
        db.add(Setting(key="demo_last_run", value=payload))
    db.commit()
    flash(request, f"Сценарий прогнан: {report['passed']} из {report['total']} шагов прошли",
          "success" if report["failed"] == 0 else "error")
    return RedirectResponse(url="/admin/demo", status_code=303)


@router.post("/demo/impersonate/{target_id}")
def admin_demo_impersonate(
    request: Request, target_id: int, db: Session = Depends(get_db),
    user: User = Depends(require_admin), game: str = Form("dota2"),
):
    target = db.get(User, target_id)
    if not target or target.demo_tag is None:
        # заходить можно только под демо-аккаунтами, под настоящими людьми — никогда
        raise HTTPException(status_code=404)
    request.session["impersonator_id"] = user.id
    request.session["user_id"] = target.id
    return RedirectResponse(url=f"/{game if game in GAMES else 'dota2'}/me", status_code=303)


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
