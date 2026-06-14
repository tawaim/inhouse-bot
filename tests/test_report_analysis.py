"""Tests for OCR scoreboard parsing + per-game proposal building.

Uses anonymized synthetic OCR text shaped like the real match-history dumps
(garbled separators, portrait-marker noise) so the committed suite never carries
real player names.
"""
from bot.services.ocr import parse_scoreboard_text
from bot.services.report_analysis import ReportState, build_game_proposal

# Match-history style: "<level> <marker> Name <items...> k / d / a <cs> <gold>".
SAMPLE = """\
@ DEFEAT
Summoner's Rift - Custom - 28:46
TEAM 1 19/26/36 53,677
17 @ Alpha Reeune 3 / 4 / 7 240 11,828
13 @ Bravo Me S= 1/8/11 121 8,762
15 @ Charlie SGA 7/7/4 175 12,007
14 @ Delta BRAM 3/4/7 206 11,706
13 @ Echo PASS 5/3/7 53 9,374
TEAM 2 26/19/46 57,978
17 @ Foxtrot CA 5 / 4 / 7 233 12,208
16 @ Golf IPE 6/3/8 191 12,407
15 @ Hotel OAM 6/5/7 218 11,056
14 @ India BUT 7/5/6 198 13,251
12 @ Juliet PRB 2/2/18 25 9,056
"""

FIXED_T1 = {"TOP": 1, "JUNGLE": 2, "MID": 3, "BOT": 4, "SUPPORT": 5}
FIXED_T2 = {"TOP": 6, "JUNGLE": 7, "MID": 8, "BOT": 9, "SUPPORT": 10}
NAME_TO_ID = {
    "alpha": 1, "bravo": 2, "charlie": 3, "delta": 4, "echo": 5,
    "foxtrot": 6, "golf": 7, "hotel": 8, "india": 9, "juliet": 10,
}


def _resolve(name):
    return NAME_TO_ID.get(name.strip().lower())


def test_parse_finds_both_teams_and_banner():
    sb = parse_scoreboard_text(SAMPLE)
    assert sb.banner == "DEFEAT"
    assert len(sb.team1) == 5 and len(sb.team2) == 5
    # The raw parser may keep trailing OCR garbage; prefix-resolution (tested via
    # build_game_proposal) strips it. Here just confirm the name leads each row.
    assert sb.team1[0].name_guess.startswith("Alpha")
    assert sb.team2[4].name_guess.startswith("Juliet")


def test_parse_extracts_kda_even_when_spaced():
    sb = parse_scoreboard_text(SAMPLE)
    assert (sb.team1[0].kills, sb.team1[0].deaths, sb.team1[0].assists) == (3, 4, 7)
    assert (sb.team2[0].kills, sb.team2[0].deaths, sb.team2[0].assists) == (5, 4, 7)


def test_parse_missing_teams_notes():
    sb = parse_scoreboard_text("no teams here\njust noise")
    assert not sb.team1 and not sb.team2
    assert any("TEAM 1" in n for n in sb.notes)


def test_proposal_assigns_roles_and_resolves():
    sb = parse_scoreboard_text(SAMPLE)
    prop = build_game_proposal(sb, _resolve, FIXED_T1, FIXED_T2)
    assert len(prop.slots) == 10
    assert not prop.unresolved()  # all names resolve cleanly
    by = {(s.team, s.role): s.discord_id for s in prop.slots}
    assert by[(1, "TOP")] == 1 and by[(1, "SUPPORT")] == 5
    assert by[(2, "TOP")] == 6 and by[(2, "SUPPORT")] == 10
    # Banner DEFEAT + screenshot-team1 == match-team1 -> match team 2 won.
    assert prop.winner_hint == 2


def test_proposal_aligns_swapped_screenshot_teams():
    """If the screenshot lists the match's team2 first, alignment must swap so
    roles/ids land on the correct match team."""
    sb = parse_scoreboard_text(SAMPLE)
    # Swap the fixed rosters so Alpha..Echo are actually match team 2.
    prop = build_game_proposal(sb, _resolve, FIXED_T2, FIXED_T1)
    by = {(s.team, s.role): s.discord_id for s in prop.slots}
    assert by[(1, "TOP")] == 6   # match team1 is Foxtrot's group now
    assert by[(2, "TOP")] == 1
    assert prop.winner_hint == 1  # DEFEAT, screenshot-team1 is match-team2


def test_proposal_flags_unresolved_for_picker():
    sb = parse_scoreboard_text(SAMPLE)

    def partial(name):
        # Bravo can't be matched (e.g. a sub not in the alias index).
        return None if name.strip().lower() == "bravo" else NAME_TO_ID.get(name.strip().lower())

    prop = build_game_proposal(sb, partial, FIXED_T1, FIXED_T2)
    un = prop.unresolved()
    assert len(un) == 1 and un[0].role == "JUNGLE" and un[0].team == 1
    assert un[0].name_guess == "Bravo"


# --- ReportState: the working state the confirm View drives -------------------

def _state(banner_resolver=_resolve):
    sb = parse_scoreboard_text(SAMPLE)
    prop = build_game_proposal(sb, banner_resolver, FIXED_T1, FIXED_T2)
    return ReportState.from_proposals(match_id=99, games=[prop])


def test_state_winner_defaults_to_hint_and_toggles():
    st = _state()
    assert st.winners == [2]   # SAMPLE banner DEFEAT -> match team 2 won
    st.toggle_winner(0)
    assert st.winners == [1]
    st.set_winner(0, 2)
    assert st.winners == [2]


def test_state_ready_only_after_all_resolved():
    sb = parse_scoreboard_text(SAMPLE)

    def partial(name):
        return None if name.strip().lower() == "bravo" else _resolve(name)

    st = ReportState.from_proposals(99, [build_game_proposal(sb, partial, FIXED_T1, FIXED_T2)])
    ok, problems = st.ready()
    assert not ok and problems
    gi, slot = st.unresolved_slots()[0]
    st.set_player(gi, slot.team, slot.role, 2)  # resolve the sub
    ok, problems = st.ready()
    assert ok and not problems


def test_state_to_game_results():
    st = _state()
    st.set_winner(0, 1)
    st.set_champion(0, 1, "MID", "leblanc")  # picker would pass a resolved name
    results = st.to_game_results()
    assert len(results) == 1
    g = results[0]
    assert g.winner == 1
    assert g.team1 == {"TOP": 1, "JUNGLE": 2, "MID": 3, "BOT": 4, "SUPPORT": 5}
    assert g.team2 == {"TOP": 6, "JUNGLE": 7, "MID": 8, "BOT": 9, "SUPPORT": 10}
    assert g.champions[3] == "leblanc"
    assert g.kdas[1] == (3, 4, 7)  # Alpha's parsed KDA carried through
