"""End-to-end tests for the stats commands: /leaderboard, /elo-history, /recent-games."""
import re
from datetime import date, datetime

from bot.db.models import GameStat, Match, MatchPerformance
from bot.db.session import get_session
from tests.harness import flow


def _line(text: str, name: str) -> str:
    """The leaderboard line for one player."""
    for ln in text.splitlines():
        if f"**{name}**" in ln:
            return ln
    raise AssertionError(f"{name} not on the leaderboard:\n{text}")


async def _series_with_a_sub(world):
    """Match 1, team 1 wins 2-1. `starter` plays all three games (TOP, TOP, then
    JUNGLE in game 3); `sub` plays only game 2 (MID), which team 1 lost."""
    starter, sub, loser = await world.add_players(3)
    async with get_session() as db:
        db.add(Match(id=1, team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=1,
                     game_date=date(2026, 6, 4), reported_at=datetime(2026, 6, 4, 23)))
        # Series rows: role = the LAST role played.
        db.add(MatchPerformance(match_id=1, discord_id=starter.id, role="JUNGLE", won=True, inhouse_elo_delta=15))
        db.add(MatchPerformance(match_id=1, discord_id=sub.id, role="MID", won=True, inhouse_elo_delta=-14))
        db.add(MatchPerformance(match_id=1, discord_id=loser.id, role="TOP", won=False, inhouse_elo_delta=-15))
        for game, role, won in ((1, "TOP", True), (2, "TOP", False), (3, "JUNGLE", True)):
            db.add(GameStat(match_id=1, game_no=game, discord_id=starter.id, role=role, won=won))
            db.add(GameStat(match_id=1, game_no=game, discord_id=loser.id, role="TOP", won=not won))
        db.add(GameStat(match_id=1, game_no=2, discord_id=sub.id, role="MID", won=False))
        await db.commit()
    return starter, sub, loser


@flow
async def test_leaderboard_counts_only_games_actually_played(world):
    starter, sub, loser = await _series_with_a_sub(world)
    text = (await world.run(world.stats.leaderboard)).text
    assert "(2-1 games)" in _line(text, starter.display_name)
    assert "(1-2 games)" in _line(text, loser.display_name)
    # The sub played one game and lost it: 0-1, not the series' 2-1.
    sub_line = _line(text, sub.display_name)
    assert "-1 (0-1 games)" in sub_line and "**1-0** series" in sub_line


@flow
async def test_leaderboard_role_filter_uses_the_role_played_each_game(world):
    starter, sub, loser = await _series_with_a_sub(world)

    top = (await world.run(world.stats.leaderboard, role="TOP")).text
    # Two of the starter's three games were TOP (one win, one loss), even though
    # their series row says JUNGLE (the last role they played).
    assert "(1-1 games)" in _line(top, starter.display_name)
    assert "(1-2 games)" in _line(top, loser.display_name)
    assert sub.display_name not in top

    jungle = (await world.run(world.stats.leaderboard, role="JUNGLE")).text
    assert "(1-0 games)" in _line(jungle, starter.display_name)
    assert loser.display_name not in jungle and sub.display_name not in jungle

    mid = (await world.run(world.stats.leaderboard, role="MID")).text
    assert "(0-1 games)" in _line(mid, sub.display_name)
    assert starter.display_name not in mid


@flow
async def test_leaderboard_falls_back_to_series_score_without_game_rows(world):
    winner, loser = await world.add_players(2)
    async with get_session() as db:
        db.add(Match(id=1, team1_json="{}", team2_json="{}", winner=2, team1_wins=1, team2_wins=2))
        db.add(MatchPerformance(match_id=1, discord_id=winner.id, role="BOT", won=True))
        db.add(MatchPerformance(match_id=1, discord_id=loser.id, role="BOT", won=False))
        await db.commit()
    text = (await world.run(world.stats.leaderboard)).text
    assert "(2-1 games)" in _line(text, winner.display_name)
    assert "(1-2 games)" in _line(text, loser.display_name)
    assert winner.display_name not in (await world.run(world.stats.leaderboard, role="TOP")).text


@flow
async def test_elo_history_shows_the_score_from_the_players_side(world):
    t1_loser, t2_winner = await world.add_players(2)
    async with get_session() as db:
        db.add(Match(id=1, team1_json="{}", team2_json="{}", winner=2, team1_wins=0, team2_wins=2,
                     game_date=date(2026, 6, 4), reported_at=datetime(2026, 6, 4, 23)))
        db.add(MatchPerformance(match_id=1, discord_id=t1_loser.id, role="TOP", won=False, inhouse_elo_delta=-30))
        db.add(MatchPerformance(match_id=1, discord_id=t2_winner.id, role="TOP", won=True, inhouse_elo_delta=30))
        await db.commit()
    won = (await world.run(world.stats.elo_history, user=t2_winner)).text
    assert "W 2-0" in won and "W 0-2" not in won
    lost = (await world.run(world.stats.elo_history, user=t1_loser)).text
    assert "L 0-2" in lost


@flow
async def test_recent_games_orders_by_date_played_not_match_id(world):
    (p,) = await world.add_players(1)
    async with get_session() as db:
        # id 1: played June 10. id 2: an OLDER series (June 3) entered afterwards.
        # id 3: no game_date, reported June 20 -> falls back to reported_at.
        db.add(Match(id=1, team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=0,
                     game_date=date(2026, 6, 10), reported_at=datetime(2026, 6, 10, 23)))
        db.add(Match(id=2, team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=0,
                     game_date=date(2026, 6, 3), reported_at=datetime(2026, 6, 25, 12)))
        db.add(Match(id=3, team1_json="{}", team2_json="{}", winner=1, team1_wins=2, team2_wins=0,
                     game_date=None, reported_at=datetime(2026, 6, 20, 22)))
        for mid in (1, 2, 3):
            for game in (1, 2):
                db.add(GameStat(match_id=mid, game_no=game, discord_id=p.id, role="MID",
                                champion="Ahri", kills=1, deaths=1, assists=1, won=True))
        await db.commit()
    text = (await world.run(world.stats.recent_games, user=p)).text
    order = re.findall(r"`(s\d g\d)`", text)
    assert order == ["s3 g2", "s3 g1", "s1 g2", "s1 g1", "s2 g2", "s2 g1"], text

    # The limit keeps the most recently PLAYED games.
    text = (await world.run(world.stats.recent_games, user=p, count=2)).text
    assert re.findall(r"`(s\d g\d)`", text) == ["s3 g2", "s3 g1"]


async def _series(world, t1_ids, t2_ids, winners):
    """Record one series between two five-player teams (ids in role order)."""
    import json

    from bot.cogs.admin import AdminCog, GameResult
    from bot.db.models import Match
    from bot.db.session import get_session

    roles = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]
    t1, t2 = dict(zip(roles, t1_ids)), dict(zip(roles, t2_ids))
    async with get_session() as db:
        m = Match(session_id=None, team1_json=json.dumps(t1), team2_json=json.dumps(t2))
        db.add(m)
        await db.commit()
        await db.refresh(m)
    await AdminCog._commit_games(None, m.id, [GameResult(winner=w, team1=t1, team2=t2) for w in winners], None, 1)


def _order(text):
    """Player names in the order the leaderboard lists them."""
    return [ln.split("**")[1] for ln in text.splitlines() if ln.startswith("`")]


@flow
async def test_leaderboard_default_ranks_by_series_wins_ahead_of_game_record(world):
    """A player with more series wins ranks above one with a better game differential.

    A = ids 101-105, B = 106-110, C = 111-115.
      A beats B 2-1, A beats C 2-1, A loses to B 0-2  -> A: 2-1 series, 4-4 games (net  0)
      C beats B 2-0                                   -> C: 1-1 series, 3-2 games (net +1)
                                                         B: 1-3 series, 3-6 games (net -3)
    Series-first puts A above C; the old net-games order put C above A.
    """
    players = await world.add_players(15)
    a, b, c = [p.id for p in players[:5]], [p.id for p in players[5:10]], [p.id for p in players[10:]]
    await _series(world, a, b, [1, 2, 1])
    await _series(world, a, c, [1, 2, 1])
    await _series(world, b, a, [1, 1])
    await _series(world, c, b, [1, 1])

    default = _order((await world.run(world.stats.leaderboard)).text)
    assert set(default[:5]) == {f"P{i}" for i in a}          # 2 series wins
    assert set(default[5:10]) == {f"P{i}" for i in c}        # 1 series win, 1 loss
    assert set(default[10:]) == {f"P{i}" for i in b}         # 1 series win, 3 losses
    line = (await world.run(world.stats.leaderboard)).text.splitlines()
    assert any("**2-1** series" in ln for ln in line)        # series record leads the row

    by_net = _order((await world.run(world.stats.leaderboard, sort="net")).text)
    assert set(by_net[:5]) == {f"P{i}" for i in c}           # net +1 beats net 0
