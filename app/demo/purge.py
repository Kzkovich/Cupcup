from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    Application, AuditLog, GameProfile, Invite, Notification, ResetRequest,
    ResetToken, Team, TeamSlot, User,
)


def demo_counts(db: Session) -> dict:
    """Сколько демо-записей сейчас в базе (по тегам)."""
    users = dict(
        db.query(User.demo_tag, func.count(User.id))
        .filter(User.demo_tag.isnot(None)).group_by(User.demo_tag).all()
    )
    teams = dict(
        db.query(Team.demo_tag, func.count(Team.id))
        .filter(Team.demo_tag.isnot(None)).group_by(Team.demo_tag).all()
    )
    return {"users": users, "teams": teams,
            "users_total": sum(users.values()), "teams_total": sum(teams.values())}


def purge_demo(db: Session, tag: str | None = None) -> dict:
    """Удаляет демо-данные. tag=None — всё, что помечено demo_tag; иначе только этот тег.

    Настоящие записи не трогает. Если настоящий человек успел податься в демо-команду
    или попасть в неё — заявка/слот уходят вместе с командой, сам аккаунт остаётся.
    """
    uq = db.query(User.id)
    tq = db.query(Team.id)
    if tag is None:
        uq = uq.filter(User.demo_tag.isnot(None))
        tq = tq.filter(Team.demo_tag.isnot(None))
    else:
        uq = uq.filter(User.demo_tag == tag)
        tq = tq.filter(Team.demo_tag == tag)
    user_ids = {i for (i,) in uq.all()}
    team_ids = {i for (i,) in tq.all()}

    if not user_ids and not team_ids:
        return {"users": 0, "teams": 0, "applications": 0, "invites": 0, "notifications": 0}

    team_ids_all_demo = set(team_ids)

    # 1. Демо-игроки, севшие в слоты настоящих команд, — освобождаем слот.
    touched_real_teams: set[int] = set()
    if user_ids:
        slots = (
            db.query(TeamSlot)
            .filter(TeamSlot.user_id.in_(user_ids), ~TeamSlot.team_id.in_(team_ids or {-1}))
            .all()
        )
        for s in slots:
            s.state, s.user_id, s.note = "open", None, None
            touched_real_teams.add(s.team_id)
        db.flush()

    # 2. Настоящая команда с демо-капитаном: передаём капитанство настоящему участнику,
    #    а если такого нет — команда удаляется вместе с демо-данными.
    if user_ids:
        orphaned = (
            db.query(Team)
            .filter(Team.captain_id.in_(user_ids), ~Team.id.in_(team_ids_all_demo or {-1}))
            .all()
        )
        for t in orphaned:
            heir = (
                db.query(TeamSlot.user_id)
                .filter(TeamSlot.team_id == t.id, TeamSlot.state == "filled",
                        TeamSlot.user_id.isnot(None), ~TeamSlot.user_id.in_(user_ids))
                .first()
            )
            if heir:
                t.captain_id = heir[0]
            else:
                team_ids.add(t.id)
        db.flush()

    # 3. Заявки и приглашения, связанные с демо-пользователями или демо-командами.
    app_q = db.query(Application).filter(
        Application.team_id.in_(team_ids or {-1}) | Application.user_id.in_(user_ids or {-1})
    )
    n_apps = app_q.count()
    app_q.delete(synchronize_session=False)

    inv_q = db.query(Invite).filter(
        Invite.team_id.in_(team_ids or {-1}) | Invite.user_id.in_(user_ids or {-1})
    )
    n_invites = inv_q.count()
    inv_q.delete(synchronize_session=False)

    # 4. Ссылки на демо-пользователей из служебных таблиц — обнуляем.
    if user_ids:
        db.query(Application).filter(Application.decided_by.in_(user_ids)).update(
            {"decided_by": None}, synchronize_session=False)
        db.query(ResetRequest).filter(ResetRequest.resolved_by.in_(user_ids)).update(
            {"resolved_by": None}, synchronize_session=False)
        db.query(ResetToken).filter(ResetToken.created_by.in_(user_ids)).update(
            {"created_by": None}, synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.actor_user_id.in_(user_ids)).update(
            {"actor_user_id": None}, synchronize_session=False)

    # 5. Уведомления: самих демо-пользователей и ссылающиеся на удаляемые команды.
    n_notifs = 0
    if user_ids:
        n_notifs += db.query(Notification).filter(Notification.user_id.in_(user_ids)).count()
        db.query(Notification).filter(Notification.user_id.in_(user_ids)).delete(synchronize_session=False)
    if team_ids:
        stale = [
            n.id for n in db.query(Notification).all()
            if (n.payload or {}).get("team_id") in team_ids
        ]
        if stale:
            n_notifs += len(stale)
            db.query(Notification).filter(Notification.id.in_(stale)).delete(synchronize_session=False)

    # 6. Команды (слоты вместе с ними), затем профили/токены/пользователи.
    n_teams = len(team_ids)
    if team_ids:
        db.query(TeamSlot).filter(TeamSlot.team_id.in_(team_ids)).delete(synchronize_session=False)
        db.query(Team).filter(Team.id.in_(team_ids)).delete(synchronize_session=False)
    if user_ids:
        db.query(ResetToken).filter(ResetToken.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(GameProfile).filter(GameProfile.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    db.flush()

    # 7. У настоящих команд, где освободили слоты, пересчитываем статус набора.
    for tid in touched_real_teams - team_ids:
        t = db.get(Team, tid)
        if t and t.status in ("recruiting", "full"):
            filled = db.query(TeamSlot).filter(TeamSlot.team_id == tid, TeamSlot.state == "filled").count()
            t.status = "full" if filled >= 5 else "recruiting"

    db.commit()
    return {"users": len(user_ids), "teams": n_teams, "applications": n_apps,
            "invites": n_invites, "notifications": n_notifs}
