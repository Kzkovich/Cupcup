from datetime import datetime

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    recovery_code_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    consent_152fz_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contact_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    tg_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    banned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    game_profiles: Mapped[list["GameProfile"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def has_contact_info(self) -> bool:
        return bool(self.consent_152fz_at and (self.display_name or self.phone))


class GameProfile(Base):
    __tablename__ = "game_profiles"
    __table_args__ = (UniqueConstraint("user_id", "game", name="uq_profile_user_game"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    game: Mapped[str] = mapped_column(String(20), index=True)

    nickname: Mapped[str] = mapped_column(String(60))
    account_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(20), nullable=True)
    profile_url: Mapped[str | None] = mapped_column(String(300), nullable=True)

    rating_raw: Mapped[str] = mapped_column(String(30))
    rating_ordinal: Mapped[int] = mapped_column(Integer, index=True)

    verify_status: Mapped[str] = mapped_column(String(20), default="none")  # none|ok|mismatch|private|failed
    verified_raw: Mapped[str | None] = mapped_column(String(30), nullable=True)
    verified_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    preferred_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    is_looking: Mapped[bool] = mapped_column(Boolean, default=True)
    about: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="game_profiles")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    game: Mapped[str] = mapped_column(String(20), index=True)
    name: Mapped[str] = mapped_column(String(80))
    tag: Mapped[str | None] = mapped_column(String(10), nullable=True)
    captain_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="recruiting")  # recruiting|full|locked|disbanded
    rating_min_wanted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_max_wanted: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bumped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    captain: Mapped["User"] = relationship()
    slots: Mapped[list["TeamSlot"]] = relationship(back_populates="team", cascade="all, delete-orphan", order_by="TeamSlot.id")

    def avg_rating_ordinal(self) -> float | None:
        filled = [s for s in self.slots if s.state == "filled" and s.profile_rating_ordinal is not None]
        if not filled:
            return None
        return sum(s.profile_rating_ordinal for s in filled) / len(filled)

    @property
    def filled_count(self) -> int:
        return sum(1 for s in self.slots if s.state == "filled")

    @property
    def open_roles(self) -> list[str]:
        return [s.role for s in self.slots if s.state == "open"]


class TeamSlot(Base):
    __tablename__ = "team_slots"
    __table_args__ = (UniqueConstraint("team_id", "role", name="uq_slot_team_role"),)
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20), default="open")  # open|reserved|filled
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    team: Mapped["Team"] = relationship(back_populates="slots")
    member: Mapped["User | None"] = relationship()

    # заполняется в вызывающем коде для avg_rating_ordinal(), не хранится в БД
    profile_rating_ordinal: int | None = None


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending|accepted|rejected|withdrawn|auto_withdrawn

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    team: Mapped["Team"] = relationship()
    applicant: Mapped["User"] = relationship(foreign_keys=[user_id])


class Invite(Base):
    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending|accepted|declined|expired|cancelled

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    team: Mapped["Team"] = relationship()
    invitee: Mapped["User"] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tg_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tg_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ResetRequest(Base):
    __tablename__ = "reset_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(255))  # почта или ник, как ввёл человек
    contact_note: Mapped[str] = mapped_column(String(255))  # как с ним связаться
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|resolved|dismissed
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ResetToken(Base):
    __tablename__ = "reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str | None] = mapped_column(String(40), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
