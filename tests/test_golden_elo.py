"""Golden-file regression test for Elo.

Plays a fixed, synthetic season (uneven seeds, a substitute, a 2-0 and several
2-1s) through the real commit path and compares every resulting rating with
tests/golden/elo_season.json. Any change to the Elo maths, the per-game commit
logic, or games-played bookkeeping shows up here as a diff.

If a change is INTENDED, regenerate the file and review the diff before committing:

    python tools/check.py --update-golden
"""
import json
import os
from pathlib import Path

from bot.cogs.admin import AdminCog, GameResult
from bot.db.models import Match
from bot.db.session import get_session
from tests.harness import assert_ratings_consistent, flow

GOLDEN = Path(__file__).parent / "golden" / "elo_season.json"
R = ["TOP", "JUNGLE", "MID", "BOT", "SUPPORT"]

# 12 players, ids 101..112, seeds spread over ~900 Elo.
SEEDS = [2300, 2100, 2050, 1900, 1850, 1800, 1750, 1650, 1600, 1500, 1400, 1250]

# Each series: (team1 ids in role order, team2 ids in role order, [game winners]).
# A game may override a slot with a substitute: {"t1": {role: id}} / {"t2": {...}}.
SEASON = [
    ([101, 104, 105, 108, 109], [102, 103, 106, 107, 110], [1, 1]),
    ([102, 104, 106, 108, 110], [101, 103, 105, 107, 109], [2, 1, 2]),
    ([101, 102, 109, 110, 111], [103, 104, 105, 106, 112], [2, 2]),
    ([103, 105, 107, 109, 111], [101, 104, 106, 108, 110], [1, 2, 1]),
    # Player 112 subs in for 107 (MID, team 1) in game 2 only.
    ([101, 106, 107, 110, 103], [102, 104, 105, 108, 109], [1, {"w": 2, "t1": {"MID": 112}}, 2]),
    ([112, 111, 110, 109, 108], [101, 102, 103, 104, 105], [2, 1, 1]),
]


async def _play_season() -> dict:
    for t1_ids, t2_ids, results in SEASON:
        t1, t2 = dict(zip(R, t1_ids)), dict(zip(R, t2_ids))
        async with get_session() as db:
            m = Match(session_id=None, team1_json=json.dumps(t1), team2_json=json.dumps(t2))
            db.add(m)
            await db.commit()
            await db.refresh(m)
        games = []
        for res in results:
            if isinstance(res, dict):
                games.append(GameResult(winner=res["w"], team1={**t1, **res.get("t1", {})},
                                        team2={**t2, **res.get("t2", {})}))
            else:
                games.append(GameResult(winner=res, team1=t1, team2=t2))
        await AdminCog._commit_games(None, m.id, games, None, 1)


@flow
async def test_season_matches_golden_file(world):
    await world.add_players(12, elo=SEEDS)
    await _play_season()
    ratings = await world.ratings()
    assert_ratings_consistent(ratings)
    # Only rows that moved, as "id:ROLE" -> [elo, inhouse_modifier, games_played].
    actual = {f"{pid}:{role}": [v[0], v[2], v[3]]
              for (pid, role), v in sorted(ratings.items()) if v[3] > 0}

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=1) + "\n", encoding="utf-8")
    assert GOLDEN.exists(), "golden file missing — run: python tools/check.py --update-golden"
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    diff = {k: (expected.get(k), actual.get(k)) for k in sorted(set(expected) | set(actual))
            if expected.get(k) != actual.get(k)}
    assert not diff, (
        "Elo results changed (key: (expected, actual)). If intended, run "
        f"`python tools/check.py --update-golden` and review the diff.\n{json.dumps(diff, indent=1)}"
    )


@flow
async def test_season_unreport_in_reverse_restores_seeds(world):
    """Un-reporting every series, newest first, returns every rating to its seed."""
    await world.add_players(12, elo=SEEDS)
    before = await world.ratings()
    await _play_season()
    for m in reversed(await world.matches()):
        status, _, _ = await AdminCog._revert_result(None, m.id)
        assert status == "ok"
    assert await world.ratings() == before
