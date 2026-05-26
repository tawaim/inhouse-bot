"""ORM models. Matches the schema laid out in the design doc."""
from __future__ import annotations

from datetime import date, datetime
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
    last_synced_seed_elo: Mapped[Optional[int]] = mapped_column(Integer)
    # The seed elo computed from the player's rank at last sync. Used to apply
    # a delta to inhouse elo when the player ranks up/down in solo queue
    # between syncs.
    previous_riot_puuid: Mapped[Optional[str]] = mapped_column(String(128))
    # Tracks the last-known PUUID across unlinks. Used to detect when a re-link
    # is a different account (-> reseed elo) vs the same one (-> keep elo).
    link_status: Mapped[str] = mapped_column(String(16), default="approved")
    # "pending" | "approved" — set by /link approval flow
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    ratings: Mapped[list["Rating"]] = relationship(back_populates="player", cascade="all, delete-orphan")


class Rating(Base):
    """One row per (player, role). Chess-style Elo split into two pieces.

    - base_seed:        derived from solo queue rank, refreshed weekly (Monday
                        close job) and on-demand via /sync-ranks. Updates
                        whenever the player's Riot rank changes. Reset by
                        /reseed-all. This is the "what does the matchmaker
                        think you're worth based on your solo queue?" number.
    - inhouse_modifier: cumulative +/- from inhouse W/L since the player
                        linked. Starts at 0, updates by chess-elo delta after
                        each match. Persists across rank changes and across
                        re-links. NEVER reset by /reseed-all (only by an
                        explicit hard wipe).

    Displayed elo is base_seed + inhouse_modifier and is recomputed whenever
    either piece changes.

    Roles include the 5 standard plus "INHOUSE" — a sixth row tracking the
    player's cumulative elo across all roles played.
    """
    __tablename__ = "ratings"

    discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.discord_id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(8), primary_key=True)
    elo: Mapped[int] = mapped_column(Integer, default=1200)
    base_seed: Mapped[int] = mapped_column(Integer, default=1200)
    inhouse_modifier: Mapped[int] = mapped_column(Integer, default=0)
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
    """A 5v5 best-of-3 series.

    session_id is nullable so pickup games (not tied to a recruitment) can exist.
    Series score is stored as team1_wins/team2_wins (e.g., 2-0, 2-1).
    The legacy `winner` column is still set (1 or 2) for compatibility with
    queries that check has-it-been-reported, but the elo math uses the score.
    """
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("sessions.id"), nullable=True)
    # The day the series was actually played. Sessions have their own game_date;
    # pickups don't, so this lets us record/display the real date. Display falls
    # back to reported_at when unset.
    game_date: Mapped[Optional[date]] = mapped_column(Date)
    team1_json: Mapped[str] = mapped_column(Text)  # {"TOP": discord_id, ...}
    team2_json: Mapped[str] = mapped_column(Text)
    predicted_balance: Mapped[Optional[float]] = mapped_column(Float)
    winner: Mapped[Optional[int]] = mapped_column(Integer)  # 1, 2, or NULL
    team1_wins: Mapped[int] = mapped_column(Integer, default=0)
    team2_wins: Mapped[int] = mapped_column(Integer, default=0)
    reported_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    reported_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    screenshot_url: Mapped[Optional[str]] = mapped_column(Text)

    session: Mapped[Optional[Session]] = relationship(back_populates="matches")
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
    # Elo deltas this match applied to the player's role rating and INHOUSE rating.
    # Stored so /unreport can reverse a series exactly (elo updates aren't trivially
    # invertible since K-factor depends on games_played and averages are pre-match).
    role_elo_delta: Mapped[int] = mapped_column(Integer, default=0)
    inhouse_elo_delta: Mapped[int] = mapped_column(Integer, default=0)

    match: Mapped[Match] = relationship(back_populates="performances")


class GameStat(Base):
    """Per-game, per-player stats within a series (Match).

    A Match is a best-of-3, so a player can have up to 3 GameStat rows for one
    Match — one per game, each with its own champion and KDA (players swap champs
    between games). Populated manually from screenshots (no server-side OCR), and
    used by /player-stats for champion pools and KDA. Independent of
    MatchPerformance, which stays one-row-per-series for elo bookkeeping.
    """
    __tablename__ = "game_stats"

    match_id: Mapped[int] = mapped_column(Integer, ForeignKey("matches.id"), primary_key=True)
    game_no: Mapped[int] = mapped_column(Integer, primary_key=True)  # 1, 2, 3 within the series
    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[Optional[str]] = mapped_column(String(8))
    champion: Mapped[Optional[str]] = mapped_column(String(32))
    kills: Mapped[Optional[int]] = mapped_column(Integer)
    deaths: Mapped[Optional[int]] = mapped_column(Integer)
    assists: Mapped[Optional[int]] = mapped_column(Integer)
    won: Mapped[bool] = mapped_column(Boolean)


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
    # Category under which /match-channels creates the per-team private channels.
    match_category_id: Mapped[Optional[int]] = mapped_column(BigInteger)
