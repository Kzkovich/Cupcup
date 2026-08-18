from datetime import datetime

from sqlalchemy.orm import Session

from app.games.base import GameConfig
from app.models import Application, Invite, Team, TeamSlot, User
from app.notify import notify


def join_team(db: Session, game: GameConfig, team: Team, slot: TeamSlot, user: User) -> None:
    """Сажает игрока в слот и подчищает все его прочие висящие заявки/приглашения в этой дисциплине."""
    slot.state, slot.user_id, slot.note = "filled", user.id, None

    other_apps = (
        db.query(Application)
        .join(Team, Team.id == Application.team_id)
        .filter(
            Application.user_id == user.id, Application.status == "pending",
            Team.game == game.key, Application.team_id != team.id,
        )
        .all()
    )
    for a in other_apps:
        a.status, a.decided_at = "auto_withdrawn", datetime.utcnow()
        other_team = db.get(Team, a.team_id)
        notify(db, other_team.captain_id, "application_auto_withdrawn", team_id=other_team.id, team_name=other_team.name, game=game.key)

    other_invites = (
        db.query(Invite)
        .join(Team, Team.id == Invite.team_id)
        .filter(
            Invite.user_id == user.id, Invite.status == "pending",
            Team.game == game.key, Invite.team_id != team.id,
        )
        .all()
    )
    for inv in other_invites:
        inv.status, inv.decided_at = "expired", datetime.utcnow()
        other_team = db.get(Team, inv.team_id)
        notify(db, other_team.captain_id, "invite_expired", team_id=other_team.id, team_name=other_team.name, game=game.key)

    if team.status in ("recruiting", "full"):
        team.status = "full" if team.filled_count >= len(team.slots) else "recruiting"
