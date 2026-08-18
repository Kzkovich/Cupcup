from sqlalchemy.orm import Session

from app.games.base import GameConfig
from app.models import GameProfile, Team, TeamSlot, User


def build_roster_view(db: Session, team: Team, game: GameConfig) -> list[dict]:
    member_ids = [s.user_id for s in team.slots if s.user_id]
    profiles: dict[int, GameProfile] = {}
    if member_ids:
        rows = (
            db.query(GameProfile)
            .filter(GameProfile.user_id.in_(member_ids), GameProfile.game == game.key)
            .all()
        )
        profiles = {p.user_id: p for p in rows}

    order = {r.key: i for i, r in enumerate(game.roles)}
    view = []
    for slot in team.slots:
        view.append({
            "slot": slot,
            "role": game.role_by_key(slot.role),
            "profile": profiles.get(slot.user_id) if slot.user_id else None,
            "user": slot.member,
        })
    view.sort(key=lambda v: order.get(v["slot"].role, 99))
    return view


def team_avg_rating_label(view: list[dict], game: GameConfig) -> str:
    ords = [v["profile"].rating_ordinal for v in view if v["profile"]]
    if not ords:
        return "—"
    avg = sum(ords) / len(ords)
    if game.rating_kind == "numeric":
        return f"~{int(avg)} MMR"
    nearest = min(game.rating_choices, key=lambda c: abs(c.ordinal - avg))
    return f"~{nearest.label}"


def team_avg_rating_ordinal(view: list[dict]) -> float | None:
    ords = [v["profile"].rating_ordinal for v in view if v["profile"]]
    if not ords:
        return None
    return sum(ords) / len(ords)


def get_active_slot(db: Session, user: User, game: GameConfig) -> TeamSlot | None:
    return (
        db.query(TeamSlot)
        .join(Team, Team.id == TeamSlot.team_id)
        .filter(
            TeamSlot.user_id == user.id,
            TeamSlot.state == "filled",
            Team.game == game.key,
            Team.status != "disbanded",
        )
        .first()
    )


def get_active_team(db: Session, user: User, game: GameConfig) -> Team | None:
    slot = get_active_slot(db, user, game)
    return slot.team if slot else None
