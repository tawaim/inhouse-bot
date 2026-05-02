"""ORM models. Matches the schema laid out in the design doc."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    riot_game_name: Mapped[Optional[str]] = mapped_column(String(64))
    riot_tag_line: Mapped[Optional[str]] = mapped_column(String(16))
    riot_puuid: Mapped[Optional[str]] = mapped_column(String(128), unique=True, index=True)
    region: Mapped[str] = mapped_column(String(8), default="na1")
    solo_tier: Mapped[Optional[str]] = mapped_column(String(16))
    solo_rank: Mapped[Optional[str]] = mapped_column(String(4))
    solo_lp: Mapped[Optional[int]] = mapped_column(Integer)
    primary_role: Mapped[Optional[str]] = mapped_column(String(8))
    riot_last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime)
    link_status: Mapped[str] = mapped_column(String(16), default="approved")
    # "pending" | "approved" — set by /link approval flow
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ratings: Mapped[list["Rating"]] = relationship(back_populates="player", cascade="all, delete-orphan")


class Rating(Base):
    """One row per (player, role). TrueSkill mu/sigma."""
    __tablename__ = "ratings"

    discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.discord_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(8), primary_key=True)
    mu: Mapped[float] = mapped_column(Float, default=25.0)
    sigma: Mapped[float] = mapped_column(Float, default=8.333)
    games_played: Mapped[int] = mapped_column(Integer, default=0)

    player: Mapped[Player] = relationship(back_populates="ratings")


class Session(Base):
    """One per Thursday inhouse night."""
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_date: Mapped[datetime] = mapped_column(Date, index=True, unique=True)
    recruit_posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    signups_close_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    recruit_msg_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    recruit_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="recruiting")

    signups: Mapped[list["Signup"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    matches: Mapped[list["Match"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Signup(Base):
    __tablename__ = "signups"

    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"), primary_key=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="playing")
    # "playing" | "not_playing" | "commentator"
    roles: Mapped[Optional[str]] = mapped_column(String(64))
    # CSV: "TOP,MID" or "FILL". None for non-playing statuses.
    signed_up_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped[Session] = relationship(back_populates="signups")

    @property
    def role_list(self) -> list[str]:
        if not self.roles:
            return []
        return [r.strip() for r in self.roles.split(",") if r.strip()]


class Match(Base):
    """A single 5v5 game generated from a session's signups."""
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"))
    team1_json: Mapped[str] = mapped_column(Text)  # {"TOP": discord_id, ...}
    team2_json: Mapped[str] = mapped_column(Text)
    predicted_balance: Mapped[Optional[float]] = mapped_column(Float)
    winner: Mapped[Optional[int]] = mapped_column(Integer)  # 1, 2, or NULL
    reported_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    screenshot_url: Mapped[Optional[str]] = mapped_column(Text)

    session: Mapped[Session] = relationship(back_populates="matches")
    performances: Mapped[list["MatchPerformance"]] = relationship(
        back_populates="match", cascade="all, delete-orphan"
    )


class MatchPerformance(Base):
    """Per-player stats from a single inhouse game (from OCR or manual)."""
    __tablename__ = "match_performances"

    match_id: Mapped[int] = mapped_column(Integer, ForeignKey("matches.id"), primary_key=True)
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(String(8))
    champion: Mapped[Optional[str]] = mapped_column(String(32))
    kills: Mapped[Optional[int]] = mapped_column(Integer)
    deaths: Mapped[Optional[int]] = mapped_column(Integer)
    assists: Mapped[Optional[int]] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column(Boolean)

    match: Mapped[Match] = relationship(back_populates="performances")


class ProposalSet(Base):
    """Holds the top-3 match proposals while waiting for the owner to pick one.
    Once the owner clicks an option, that proposal is committed as a Match
    row and this row is marked resolved.
    """
    __tablename__ = "proposal_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.id"), index=True)
    proposals_json: Mapped[str] = mapped_column(Text)
    # JSON list of {team1: {role: discord_id}, team2: {...}, balance_diff, role_penalty}
    dm_message_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    chosen_index: Mapped[Optional[int]] = mapped_column(Integer)  # 0, 1, or 2
    resolved_match_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("matches.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class GuildConfig(Base):
    """Per-guild channel configuration set via /set-channel."""
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    recruit_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    results_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    admin_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger)
