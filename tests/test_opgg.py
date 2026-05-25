"""Tests for OP.GG past-season parsing, region mapping, and decay counting.

The sample text is a real (trimmed) response from OP.GG's lol_get_summoner_profile
for tawaim#NA1 — the token-compressed "class-repr" format the MCP server returns.
"""
from datetime import datetime, timedelta, timezone

from bot.services.opgg_client import (
    SEASON_LENGTH_DAYS,
    _seasons_since,
    opgg_region,
    parse_past_season,
)

# Most recent Solo/Duo season (previous_seasons[0]) is season 31 = Emerald III.
# Note season 27 in previous_season_tiers has its SOLORANKED slot mislabeled with
# flex data (OP.GG's null-collapse bug) — parsing must NOT be fooled by it.
SAMPLE = (
    'LolGetSummonerProfile(Data(Summoner('
    '[PreviousSeason(31,TierInfo("EMERALD",3)),PreviousSeason(25,TierInfo("EMERALD",2)),'
    'PreviousSeason(23,TierInfo("PLATINUM",1))],'
    '[PreviousSeasonTier(31,[RankEntrie("SOLORANKED",RankInfo("EMERALD",3,"2026-01-08T18:44:41+09:00")),'
    'RankEntrie("FLEXRANKED",RankInfo("EMERALD",2,"2026-01-08T18:33:43+09:00"))]),'
    'PreviousSeasonTier(27,[RankEntrie("SOLORANKED",RankInfo("EMERALD",2,"2024-09-25T17:54:55+09:00")),'
    'RankEntrie1("FLEXRANKED")])])))'
)


def test_parse_picks_most_recent_solo_season():
    r = parse_past_season(SAMPLE)
    assert r is not None
    assert r.tier == "EMERALD"
    assert r.division == "III"   # division int 3 -> roman III
    assert r.season_id == 31


def test_parse_seasons_elapsed_is_nonnegative_int():
    r = parse_past_season(SAMPLE)
    # The placement date is in the past, so at least 0 (exact value is time-dependent).
    assert isinstance(r.seasons_elapsed, int)
    assert r.seasons_elapsed >= 0


def test_parse_no_history_returns_none():
    assert parse_past_season("LolGetSummonerProfile(Data(Summoner([],[])))") is None
    assert parse_past_season("garbage") is None


def test_parse_apex_tier():
    text = 'Summoner([PreviousSeason(33,TierInfo("CHALLENGER",1))],[])'
    r = parse_past_season(text)
    assert r.tier == "CHALLENGER"
    assert r.division == "I"


def test_seasons_since_counts_whole_seasons():
    now = datetime.now(timezone.utc)
    # Just under one season elapsed -> 0
    recent = (now - timedelta(days=SEASON_LENGTH_DAYS - 5)).isoformat()
    assert _seasons_since(recent) == 0
    # Two-and-a-bit seasons elapsed -> 2
    older = (now - timedelta(days=2 * SEASON_LENGTH_DAYS + 10)).isoformat()
    assert _seasons_since(older) == 2


def test_seasons_since_future_or_bad_input_is_zero():
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert _seasons_since(future) == 0
    assert _seasons_since("not-a-date") == 0


def test_region_mapping():
    assert opgg_region("na1") == "na"
    assert opgg_region("euw1") == "euw"
    assert opgg_region("eun1") == "eune"
    assert opgg_region("kr") == "kr"
    # Unknown platform: strip trailing digits as a best-effort fallback.
    assert opgg_region("zz9") == "zz"
