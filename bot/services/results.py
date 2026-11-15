"""Rating writes for match results: commit a reported series game by game,
reverse it (/unreport), and re-apply it from its saved per-game rows
(/rescore-match). Plain async functions over a database session — no Discord.

`tz` is the bot's timezone: it decides which calendar day "today" is when a
match's game_date has to be filled in.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

import pytz
from sqlalchemy import select

from bot.config import ROLES
from bot.db.models import GameStat, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession
from bot.db.session import get_session
from bot.services.champions import resolve_champion
from bot.services.elo import DEFAULT_ELO, INHOUSE_ROLE, average_elo, update_elo_team_game
from bot.services.ratings_lock import ratings_lock
from bot.services.report_analysis import GameResult

log = logging.getLogger(__name__)


def _local_today(tz) -> date:
    """Today's date in the bot's timezone."""
    return datetime.now(tz).date()


def _local_date_of(tz, utc_naive: datetime) -> date:
    """The bot-timezone calendar date of a naive-UTC timestamp (how reported_at is stored)."""
    return pytz.utc.localize(utc_naive).astimezone(tz).date()


async def commit_result(
    match_id: int,
    team1_wins: int,
    team2_wins: int,
    screenshot_url: str | None,
    admin_id: int,
    parsed,
    db=None,
    tz=pytz.utc,
) -> str:
    """Series-score reporting (no subs): expand the series into per-game
    results over the match's fixed roster and delegate to commit_games.

    This keeps /report-manual and /pickup-series working unchanged while elo
    is applied per game. The multi-screenshot flow calls commit_games
    directly with real per-game lineups (subs).

    Returns commit_games' status: 'ok' | 'not_found' | 'already_reported' |
    'empty'. `db` works as in commit_games.
    """
    if db is None:
        async with get_session() as own:
            found = await series_as_games(own, match_id, team1_wins, team2_wins, parsed)
    else:
        found = await series_as_games(db, match_id, team1_wins, team2_wins, parsed)
    if isinstance(found, str):
        return found
    return await commit_games(match_id, found, screenshot_url, admin_id, db=db, tz=tz)


async def series_as_games(db, match_id: int, team1_wins: int, team2_wins: int, parsed):
    """The GameResult list for a series score over the match's fixed roster, or
    a status string ('not_found' | 'already_reported') if it can't be reported."""
    match = await db.get(Match, match_id)
    if match is None:
        return "not_found"
    if match.winner is not None:
        return "already_reported"  # idempotency guard (see commit_games)
    team1 = {k: int(v) for k, v in json.loads(match.team1_json).items()}
    team2 = {k: int(v) for k, v in json.loads(match.team2_json).items()}

    # Series-level OCR gives one scoreboard, not per-game KDA. Attach it to
    # the LAST game only (assume the end-of-series scoreboard) so per-game
    # KDA totals aren't multiplied across games.
    kdas: dict[int, tuple] = {}
    if parsed:
        ocr_by_riot_id = {p.riot_id: p for p in parsed.players if p.riot_id}
        for pid in list(team1.values()) + list(team2.values()):
            player = await db.get(Player, pid)
            if player and player.riot_game_name:
                row = ocr_by_riot_id.get(f"{player.riot_game_name}#{player.riot_tag_line}")
                if row:
                    kdas[pid] = (row.kills, row.deaths, row.assists)

    # team1 wins first, then team2 — order has only a minor effect on elo
    # (ratings evolve game to game) and is deterministic.
    games = [GameResult(winner=1, team1=team1, team2=team2) for _ in range(team1_wins)]
    games += [GameResult(winner=2, team1=team1, team2=team2) for _ in range(team2_wins)]
    if games and kdas:
        games[-1].kdas = kdas
    return games


async def commit_games(
    match_id: int,
    games: list["GameResult"],
    screenshot_url: str | None,
    admin_id: int,
    db=None,
    tz=pytz.utc,
) -> str:
    """Apply a match GAME BY GAME. Each game is its own elo event of ~±15 (see
    update_elo_team_game), so a 2-0 ≈ +30 and a 2-1 nets ≈ +15, and a sub earns
    elo only for the games they actually played. Writes per-game GameStat rows
    (each carrying that game's deltas, which is what /unreport reverses) and
    one aggregate MatchPerformance per player. games_played counts ACTUAL games.

    Returns what happened, so callers can tell the admin the truth:
      'ok'               committed
      'not_found'        no such match
      'already_reported' the match already has a result (idempotency guard)
      'empty'            no games were given
    Nothing is written unless the status is 'ok'.

    With no `db`, runs in its own session + transaction under the rating lock.
    Pass an open session to make it part of a larger transaction (rescore,
    pickup-series): the caller must already hold ratings_lock() and must commit.
    """
    if not games:
        return "empty"
    if db is not None:
        return await commit_games_in(db, match_id, games, screenshot_url, admin_id, tz)
    async with ratings_lock():
        async with get_session() as own:
            status = await commit_games_in(
                own, match_id, games, screenshot_url, admin_id, tz)
            if status == "ok":
                await own.commit()
            return status


async def commit_games_in(
    db,
    match_id: int,
    games: list["GameResult"],
    screenshot_url: str | None,
    admin_id: int,
    tz=pytz.utc,
) -> str:
    """commit_games' work inside the caller's session. Never commits."""
    match = await db.get(Match, match_id)
    if match is None:
        return "not_found"
    if match.winner is not None:
        return "already_reported"  # idempotency guard

    cache: dict[tuple, Rating] = {}

    async def rget(pid: int, role: str) -> Rating:
        key = (pid, role)
        r = cache.get(key)
        if r is None:
            r = await db.get(Rating, key)
            if r is None:
                r = Rating(discord_id=pid, role=role, elo=DEFAULT_ELO,
                           base_seed=DEFAULT_ELO, inhouse_modifier=0, games_played=0)
                db.add(r)
            cache[key] = r
        return r

    deltas: dict[int, dict[str, int]] = defaultdict(lambda: {"role": 0, "inhouse": 0})
    played: dict[int, int] = defaultdict(int)  # discord_id -> games actually played
    first_balance: Optional[float] = None
    t1_wins = t2_wins = 0

    for gi, g in enumerate(games, start=1):
        if g.winner == 1:
            t1_wins += 1
        else:
            t2_wins += 1
        t1, t2 = g.team1, g.team2
        t1won = g.winner == 1

        t1_role = {role: await rget(pid, role) for role, pid in t1.items()}
        t2_role = {role: await rget(pid, role) for role, pid in t2.items()}
        t1_over = {pid: await rget(pid, INHOUSE_ROLE) for pid in t1.values()}
        t2_over = {pid: await rget(pid, INHOUSE_ROLE) for pid in t2.values()}

        # Pre-game snapshots so the two team loops stay symmetric.
        t1_ravg = average_elo([r.elo for r in t1_role.values()])
        t2_ravg = average_elo([r.elo for r in t2_role.values()])
        t1_oavg = average_elo([r.elo for r in t1_over.values()])
        t2_oavg = average_elo([r.elo for r in t2_over.values()])
        t1_rpre = {role: r.elo for role, r in t1_role.items()}
        t2_rpre = {role: r.elo for role, r in t2_role.items()}
        t1_opre = {pid: r.elo for pid, r in t1_over.items()}
        t2_opre = {pid: r.elo for pid, r in t2_over.items()}

        if first_balance is None:
            first_balance = abs(sum(t1_rpre.values()) - sum(t2_rpre.values()))

        # discord_id -> (role delta, inhouse delta) for THIS game. Stored on
        # the GameStat row so /unreport can undo it against this game's role.
        game_deltas: dict[int, tuple[int, int]] = {}

        for role, pid in t1.items():
            rr = t1_role[role]
            _, rd = update_elo_team_game(
                t1_ravg, t2_ravg, t1_rpre[role], t2_rpre.get(role, t2_ravg), won=t1won)
            rr.inhouse_modifier += rd
            rr.elo = rr.base_seed + rr.inhouse_modifier
            rr.games_played += 1
            ov = t1_over[pid]
            _, od = update_elo_team_game(
                t1_oavg, t2_oavg, t1_opre[pid], t2_opre.get(t2.get(role), t2_oavg), won=t1won)
            ov.inhouse_modifier += od
            ov.elo = ov.base_seed + ov.inhouse_modifier
            ov.games_played += 1
            deltas[pid]["role"] += rd
            deltas[pid]["inhouse"] += od
            played[pid] += 1
            game_deltas[pid] = (rd, od)

        for role, pid in t2.items():
            rr = t2_role[role]
            _, rd = update_elo_team_game(
                t2_ravg, t1_ravg, t2_rpre[role], t1_rpre.get(role, t1_ravg), won=not t1won)
            rr.inhouse_modifier += rd
            rr.elo = rr.base_seed + rr.inhouse_modifier
            rr.games_played += 1
            ov = t2_over[pid]
            _, od = update_elo_team_game(
                t2_oavg, t1_oavg, t2_opre[pid], t1_opre.get(t1.get(role), t1_oavg), won=not t1won)
            ov.inhouse_modifier += od
            ov.elo = ov.base_seed + ov.inhouse_modifier
            ov.games_played += 1
            deltas[pid]["role"] += rd
            deltas[pid]["inhouse"] += od
            played[pid] += 1
            game_deltas[pid] = (rd, od)

        # Per-game stat rows (champions normalized to canonical names).
        for roster, won in ((t1, t1won), (t2, not t1won)):
            for role, pid in roster.items():
                k = g.kdas.get(pid, (None, None, None))
                champ = g.champions.get(pid)
                db.add(GameStat(
                    match_id=match.id, game_no=gi, discord_id=pid, role=role,
                    champion=(resolve_champion(champ) or champ) if champ else None,
                    kills=k[0], deaths=k[1], assists=k[2], won=won,
                    role_elo_delta=game_deltas[pid][0],
                    inhouse_elo_delta=game_deltas[pid][1],
                ))

    # NULL means "not computed yet" (manual / pickup matches): fill it from
    # the pre-game ratings. revert_in clears it again on /unreport. A
    # matchmaker match already carries its own value, which is kept.
    if match.predicted_balance is None:
        match.predicted_balance = float(first_balance or 0.0)
    balance = match.predicted_balance or 0.0
    series_winner = 1 if t1_wins > t2_wins else 2

    role_of: dict[int, str] = {}
    team_of: dict[int, int] = {}
    for g in games:
        for role, pid in g.team1.items():
            role_of[pid], team_of[pid] = role, 1
        for role, pid in g.team2.items():
            role_of[pid], team_of[pid] = role, 2

    for pid, cnt in played.items():
        ov = cache[(pid, INHOUSE_ROLE)]
        ov.total_balance_diff += balance * cnt
        db.add(MatchPerformance(
            match_id=match.id, discord_id=pid, role=role_of[pid],
            kills=None, deaths=None, assists=None,
            won=(team_of[pid] == series_winner),
            role_elo_delta=deltas[pid]["role"], inhouse_elo_delta=deltas[pid]["inhouse"],
        ))

    match.winner = series_winner
    match.team1_wins = t1_wins
    match.team2_wins = t2_wins
    match.reported_by = admin_id
    match.reported_at = datetime.utcnow()
    match.screenshot_url = screenshot_url
    session = None
    if match.session_id is not None:
        session = await db.get(InhouseSession, match.session_id)
        if session and session.status == "matched":
            session.status = "completed"
    # The day the series was played: the session's night if there is one,
    # else today (bot timezone) — results are reported right after playing.
    # Only filled when unset, so a later re-report keeps the original day.
    if match.game_date is None:
        match.game_date = (
            session.game_date if session is not None and session.game_date
            else _local_today(tz)
        )

    return "ok"


async def revert_result(match_id: int, db=None, tz=pytz.utc) -> tuple[str, int, str]:
    """Reverse a reported series (mirror of commit_games). Returns
    (status, players_reversed, prev_score) where status is
    'ok' | 'not_found' | 'not_reported'.

    With no `db`, runs in its own session + transaction under the rating lock.
    Pass an open session to make it part of a larger transaction (see
    rescore): the caller must already hold ratings_lock() and must commit.
    """
    if db is not None:
        return await revert_in(db, match_id, tz)
    async with ratings_lock():
        async with get_session() as own:
            result = await revert_in(own, match_id, tz)
            if result[0] == "ok":
                await own.commit()
            return result


async def revert_in(db, match_id: int, tz=pytz.utc) -> tuple[str, int, str]:
    """revert_result's work inside the caller's session. Flushes, never commits."""
    match = await db.get(Match, match_id)
    if match is None:
        return ("not_found", 0, "")
    if match.winner is None:
        return ("not_reported", 0, "")

    perfs = (await db.execute(
        select(MatchPerformance).where(MatchPerformance.match_id == match_id)
    )).scalars().all()
    gstats = (await db.execute(
        select(GameStat).where(GameStat.match_id == match_id)
    )).scalars().all()
    balance = match.predicted_balance or 0.0

    # Matches committed since per-game deltas were stored: undo each game row on
    # the role that row was actually played in. This is exact even when a
    # player played two different roles in the series.
    per_game = bool(gstats) and all(
        gs.role and gs.role_elo_delta is not None and gs.inhouse_elo_delta is not None
        for gs in gstats
    )
    if per_game:
        games_of: dict[int, int] = defaultdict(int)
        for gs in gstats:
            games_of[gs.discord_id] += 1
            role_rating = await db.get(Rating, (gs.discord_id, gs.role))
            if role_rating is not None:
                role_rating.inhouse_modifier -= gs.role_elo_delta
                role_rating.games_played = max(0, role_rating.games_played - 1)
                role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
            overall = await db.get(Rating, (gs.discord_id, INHOUSE_ROLE))
            if overall is not None:
                overall.inhouse_modifier -= gs.inhouse_elo_delta
                overall.games_played = max(0, overall.games_played - 1)
                overall.elo = overall.base_seed + overall.inhouse_modifier
        # Same arithmetic the commit used (balance x games, applied once).
        for pid, cnt in games_of.items():
            overall = await db.get(Rating, (pid, INHOUSE_ROLE))
            if overall is not None:
                overall.total_balance_diff -= balance * cnt
    else:
        # Older matches (GameStat deltas are NULL, or there are no GameStat rows
        # at all): fall back to the one-row-per-player MatchPerformance, which
        # holds the SUMMED deltas against the last role played. Unchanged from
        # how these matches have always been un-reported.
        #
        # How many games each player actually played, from GameStat. Pre-per-game
        # ("legacy") matches have no GameStat rows — fall back to 1 per player,
        # matching how they were committed (one rating event per series).
        games_by_pid: dict[int, int] = defaultdict(int)
        for gs in gstats:
            games_by_pid[gs.discord_id] += 1
        legacy = len(gstats) == 0
        for perf in perfs:
            cnt = 1 if legacy else games_by_pid.get(perf.discord_id, 0)
            role_rating = await db.get(Rating, (perf.discord_id, perf.role))
            if role_rating is not None:
                role_rating.inhouse_modifier -= perf.role_elo_delta or 0
                role_rating.games_played = max(0, role_rating.games_played - cnt)
                role_rating.elo = role_rating.base_seed + role_rating.inhouse_modifier
            overall = await db.get(Rating, (perf.discord_id, INHOUSE_ROLE))
            if overall is not None:
                overall.inhouse_modifier -= perf.inhouse_elo_delta or 0
                overall.games_played = max(0, overall.games_played - cnt)
                overall.elo = overall.base_seed + overall.inhouse_modifier
                overall.total_balance_diff -= balance * cnt
    for perf in perfs:
        await db.delete(perf)
    for gs in gstats:
        await db.delete(gs)

    # predicted_balance: the matchmaker's own number (the match came from a
    # ProposalSet) is real and stays. Anything else was filled in by the commit
    # being undone, from the ratings at that moment — clear it so the next
    # report recomputes it instead of reusing a stale figure.
    from_matchmaker = (await db.execute(
        select(ProposalSet.id).where(ProposalSet.resolved_match_id == match_id).limit(1)
    )).first() is not None
    if not from_matchmaker:
        match.predicted_balance = None

    # Matches reported before game_date was recorded: keep the day they were
    # first reported as the day played, rather than losing it with reported_at.
    # (Session matches take their session's date at the next commit instead.)
    if match.game_date is None and match.session_id is None and match.reported_at is not None:
        match.game_date = _local_date_of(tz, match.reported_at)

    prev_score = f"{match.team1_wins}-{match.team2_wins}"
    match.winner = None
    match.team1_wins = 0
    match.team2_wins = 0
    match.reported_by = None
    match.reported_at = None
    match.screenshot_url = None

    # Roll the session back from completed -> matched so it reads as un-played.
    if match.session_id is not None:
        session = await db.get(InhouseSession, match.session_id)
        if session and session.status == "completed":
            session.status = "matched"

    # Push the deletes out now, so a re-commit in this same transaction
    # (rescore) inserts fresh rows instead of colliding with pending deletes.
    await db.flush()
    return ("ok", len(perfs), prev_score)


def _gamestat_problems(rows) -> list[str]:
    """Why a match's saved GameStat rows can't be turned back into lineups for
    /rescore-match. Each game must have exactly 5 winners + 5 losers, each side
    covering the five roles once, with no missing role. Empty list = fine."""
    by_game: dict[int, list] = defaultdict(list)
    for r in rows:
        by_game[r.game_no].append(r)
    problems: list[str] = []
    for gno in sorted(by_game):
        for label, side in (("winning", [r for r in by_game[gno] if r.won]),
                            ("losing", [r for r in by_game[gno] if not r.won])):
            roles = [r.role for r in side]
            if len(side) != 5:
                problems.append(f"Game {gno}: {len(side)} players on the {label} side (need 5)")
            elif any(not role for role in roles):
                problems.append(f"Game {gno}: a {label}-side player has no role")
            elif sorted(roles) != sorted(ROLES):
                problems.append(f"Game {gno}: {label}-side roles are {', '.join(roles)}")
    return problems


async def games_from_gamestats(db, match) -> list:
    """Rebuild the per-game lineups (GameResult list) from a match's saved
    GameStat rows, so it can be rescored without re-uploading screenshots.
    GameStat stores role + won + champion + KDA per player per game; the team
    split comes from the won flag, oriented to the match's teams by roster
    overlap."""
    ros1 = set(int(v) for v in json.loads(match.team1_json).values())
    ros2 = set(int(v) for v in json.loads(match.team2_json).values())
    rows = (await db.execute(
        select(GameStat).where(GameStat.match_id == match.id).order_by(GameStat.game_no)
    )).scalars().all()
    by_game: dict[int, list] = defaultdict(list)
    for r in rows:
        by_game[r.game_no].append(r)
    games = []
    for gno in sorted(by_game):
        g = by_game[gno]
        win_ids = {r.discord_id for r in g if r.won}
        winners_are_t1 = len(win_ids & ros1) >= len(win_ids & ros2)
        games.append(GameResult(
            winner=1 if winners_are_t1 else 2,
            team1={r.role: r.discord_id for r in g if r.won == winners_are_t1},
            team2={r.role: r.discord_id for r in g if r.won != winners_are_t1},
            champions={r.discord_id: r.champion for r in g if r.champion},
            kdas={r.discord_id: (r.kills, r.deaths, r.assists)
                  for r in g if r.kills is not None},
        ))
    return games


async def rescore(match_id: int, admin_id: int, tz=pytz.utc) -> str:
    """Reverse a reported match and re-apply its saved lineups, ATOMICALLY: one
    session, one transaction, under the rating lock. If anything goes wrong —
    including a crash between the two halves — nothing is written, so the match
    can never be left reverted-but-not-recommitted with its lineups (which live
    only in the GameStat rows the revert deletes) gone. Returns the message
    for the admin."""
    async with ratings_lock():
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return f"Match {match_id} not found."
            if match.winner is None:
                return f"Match {match_id} isn't reported — nothing to rescore."
            rows = (await db.execute(
                select(GameStat).where(GameStat.match_id == match_id)
            )).scalars().all()
            if not rows:
                return (f"Match {match_id} has no per-game data (reported before per-game "
                        f"tracking). `/unreport` it and re-report it with `/report` to rescore.")
            problems = _gamestat_problems(rows)
            if problems:
                return (f"❌ Can't rescore match {match_id}: its saved per-game data is "
                        f"incomplete, so the lineups can't be rebuilt reliably — nothing was "
                        f"changed.\n• " + "\n• ".join(problems[:6]) +
                        f"\n`/unreport {match_id}` and re-report it instead.")
            games = await games_from_gamestats(db, match)
            screenshot_url = match.screenshot_url
            try:
                # Reverse the old elo, then re-apply the same lineups with the
                # current tuning — both inside this one transaction.
                rstatus, _, _ = await revert_result(match_id, db=db, tz=tz)
                cstatus = await commit_games(
                    match_id, games, screenshot_url, admin_id, db=db, tz=tz
                ) if rstatus == "ok" else rstatus
                if cstatus != "ok":
                    await db.rollback()
                    return (f"❌ Rescore of match {match_id} failed ({cstatus}) — "
                            f"nothing was changed.")
                await db.commit()
            except Exception as e:
                await db.rollback()
                log.exception("rescore-match %s failed; rolled back", match_id)
                return (f"❌ Rescore of match {match_id} failed "
                        f"({type(e).__name__}: {str(e)[:200]}) — nothing was changed.")
    return (f"✅ Rescored Match {match_id} with current elo settings ({len(games)} game(s)). "
            f"Player counts and champions/KDA preserved.")
