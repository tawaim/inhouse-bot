"""Pure tests for reading a screenshot into a report proposal: winner evidence,
row-order-is-not-role alignment, team orientation, series validation, name
resolution, and the pre-flight image checks. No Discord, no DB, no network.

Facts these encode (from real screenshots of this server):
  - the match-history scoreboard lists the VIEWING player first on their team,
    so a row's position is not its role;
  - the VICTORY/DEFEAT banner is relative to whoever took the screenshot;
  - Riot names change, so a name failing to resolve is normal.
"""
from bot.services.ocr import (
    MAX_IMAGE_BYTES, ParsedScoreboard, board_to_scoreboard, decide_winner, image_problem,
    parse_scoreboard_image, parse_scoreboard_text,
)
from bot.services.report_analysis import ReportState, _resolve_name, build_game_proposal
from tests.harness import PNG_BYTES, failed_board, make_board

T1 = {"TOP": 1, "JUNGLE": 2, "MID": 3, "BOT": 4, "SUPPORT": 5}
T2 = {"TOP": 6, "JUNGLE": 7, "MID": 8, "BOT": 9, "SUPPORT": 10}
NAMES = {f"p{i}": i for i in range(1, 13)}  # p11, p12 are linked players on neither roster


def resolve(name):
    return NAMES.get(name.strip().lower())


def by_slot(prop):
    return {(s.team, s.role): s for s in prop.slots}


def full_board(**fields):
    """Both teams in plain role order, every name resolvable."""
    return make_board([f"p{i}" for i in range(1, 6)], [f"p{i}" for i in range(6, 11)], **fields)


# --------------------------------------------------------------------- item 2: no silent winner

def test_unreadable_screenshot_has_no_winner_and_cannot_commit():
    prop = build_game_proposal(failed_board("Vision read failed (boom)."), resolve, T1, T2)
    assert prop.read_failed and prop.winner_hint is None
    assert "Vision read failed (boom)." in prop.notes
    st = ReportState.from_proposals(7, [prop])
    assert st.winners == [None]          # NOT defaulted to team 1
    assert st.all_unreadable
    ok, problems = st.ready()
    assert not ok and any("pick the winner" in p for p in problems)
    st.toggle_winner(0)                  # the admin decides
    assert st.winners == [1]
    assert st.ready() == (True, [])


def test_readable_screenshot_without_outcome_still_needs_a_winner():
    st = ReportState.from_proposals(7, [build_game_proposal(full_board(), resolve, T1, T2)])
    assert st.winners == [None] and not st.all_unreadable
    assert not st.ready()[0]


def test_all_unreadable_is_false_when_one_game_read():
    games = [build_game_proposal(failed_board(), resolve, T1, T2),
             build_game_proposal(full_board(winner=1), resolve, T1, T2)]
    assert not ReportState.from_proposals(7, games).all_unreadable


# --------------------------------------------------------------------- item 3: row is not role

def test_match_history_order_keeps_rostered_roles():
    """Viewer (p4, the BOT laner) is listed FIRST on team 1, as on the match-history
    screen. Everyone must stay in their rostered role with their own champion/KDA."""
    board = make_board(
        [("p4", "Jinx", (9, 1, 5)), ("p1", "Garen", (1, 2, 3)), ("p2", "Vi", (4, 4, 4)),
         ("p3", "Ahri", (5, 0, 7)), ("p5", "Leona", (0, 3, 12))],
        [f"p{i}" for i in range(6, 11)], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    s = by_slot(prop)
    assert [s[(1, r)].discord_id for r in T1] == [1, 2, 3, 4, 5]
    assert not any(x.is_sub for x in prop.slots)
    assert (s[(1, "BOT")].champion, s[(1, "BOT")].kills) == ("Jinx", 9)
    assert (s[(1, "TOP")].champion, s[(1, "TOP")].kills) == ("Garen", 1)
    assert s[(1, "SUPPORT")].champion == "Leona"
    assert prop.winner_hint == 1
    assert ReportState.from_proposals(1, [prop]).ready() == (True, [])


def test_sub_takes_the_slot_of_the_missing_rostered_player():
    """p11 (on neither roster) is the viewer, so listed first; rostered MID p3 is
    absent. The sub goes to MID — not to TOP, the row they appeared in."""
    board = make_board([("p11", "Zed", (7, 2, 1)), "p1", "p2", "p4", "p5"],
                       [f"p{i}" for i in range(6, 11)], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    s = by_slot(prop)
    assert s[(1, "MID")].discord_id == 11 and s[(1, "MID")].is_sub
    assert s[(1, "MID")].champion == "Zed"
    assert s[(1, "TOP")].discord_id == 1 and not s[(1, "TOP")].is_sub
    assert [x for x in prop.slots if x.is_sub] == [s[(1, "MID")]]


def test_unrecognised_name_keeps_the_rostered_player():
    """A renamed account ("NewName") doesn't resolve. The roster slot it must belong
    to keeps its rostered player; only the stats attach, and the read name is
    remembered as unresolved (so it may later be learned as an alias)."""
    board = make_board(["p1", "p2", ("NewName Here", "Ahri", (3, 3, 3)), "p4", "p5"],
                       [f"p{i}" for i in range(6, 11)], winner=2)
    slot = by_slot(build_game_proposal(board, resolve, T1, T2))[(1, "MID")]
    assert slot.discord_id == 3 and not slot.is_sub
    assert slot.name_guess == "NewName Here" and not slot.read_resolved
    assert slot.champion == "Ahri"


def test_two_names_resolving_to_one_player_is_flagged_not_double_credited():
    board = make_board(["p1", "p1", "p3", "p4", "p5"], [f"p{i}" for i in range(6, 11)], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    ids = [x.discord_id for x in prop.slots if x.team == 1]
    assert ids == [1, 2, 3, 4, 5]
    assert any("already in this game" in n for n in prop.notes)


def test_ready_rejects_same_player_twice_in_one_team():
    st = ReportState.from_proposals(1, [build_game_proposal(full_board(winner=1), resolve, T1, T2)])
    assert st.ready()[0]
    st.set_player(0, 1, "TOP", 2)  # p2 is now TOP and JUNGLE on team 1
    ok, problems = st.ready()
    assert not ok and any("two slots on one team" in p for p in problems)


# --------------------------------------------------------------------- item 4: orientation

def test_orientation_not_guessed_when_too_few_names_resolve():
    """Only 3 of 10 names are known — not enough to say which screenshot team is
    which match team. No winner hint, a visible note, and the admin can swap."""
    board = make_board(["p6", "p7", "x1", "x2", "x3"], ["p1", "y1", "y2", "y3", "y4"], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    assert prop.orientation_uncertain and prop.winner_hint is None
    assert any("can't tell which screenshot team is which" in n for n in prop.notes)
    st = ReportState.from_proposals(1, [prop])
    assert st.winners == [None] and not st.ready()[0]


def test_orientation_tie_is_not_guessed():
    # Two names say "identity", two say "swapped".
    board = make_board(["p1", "p2", "p6", "p7", "x"], ["y1", "y2", "y3", "y4", "y5"], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    assert prop.orientation_uncertain and prop.winner_hint is None


def test_swap_teams_flips_orientation_and_winner():
    """No rostered name is recognised, so the top screenshot team is shown as match
    team 1 without any evidence (and no winner). It is really match team 2: after
    the admin swaps, the sub read on it lands on team 2 and the winner maps over."""
    board = make_board(["x0", "x1", "x2", "x3", ("p11", "Zed")],     # top team: unknowns + a sub
                       ["y1", "y2", "y3", "y4", "y5"], winner=1)
    st = ReportState.from_proposals(1, [build_game_proposal(board, resolve, T1, T2)])
    assert st.games[0].orientation_uncertain and st.winners == [None]

    def sub_slot():
        return next(s for s in st.games[0].slots if s.discord_id == 11)

    assert (sub_slot().team, sub_slot().role) == (1, "SUPPORT")   # displayed on team 1

    assert st.swap_teams(0)
    after = st.games[0]
    assert not after.orientation_uncertain and not after.sb1_is_match_team1
    assert (sub_slot().team, sub_slot().champion) == (2, "Zed")   # now on team 2
    assert by_slot(after)[(1, "SUPPORT")].discord_id == 5         # team 1 back to its roster
    assert st.winners == [2]                             # screenshot team 1 == match team 2
    assert st.swap_teams(0) and st.winners == [1]        # and back


def test_confident_orientation_swapped_screenshot():
    board = make_board([f"p{i}" for i in range(6, 11)], [f"p{i}" for i in range(1, 6)], winner=1)
    prop = build_game_proposal(board, resolve, T1, T2)
    assert not prop.orientation_uncertain and not prop.sb1_is_match_team1
    assert prop.winner_hint == 2
    assert [s.discord_id for s in prop.slots] == list(range(1, 11))


# --------------------------------------------------------------------- item 4: winner evidence

def test_decide_winner_uses_banner_relative_to_viewer():
    # VICTORY on a screenshot taken by a bottom-team player: the BOTTOM team won.
    assert decide_winner(None, None, "VICTORY", 2) == (2, None)
    assert decide_winner(None, None, "DEFEAT", 2) == (1, None)
    # A banner with no known viewer says nothing.
    assert decide_winner(None, None, "VICTORY", None) == (None, None)
    # Kills agreeing with the banner: no note needed.
    assert decide_winner(12, 30, "VICTORY", 2) == (2, None)


def test_decide_winner_banner_beats_a_small_kill_deficit():
    """Real case (Jul 30 game 1): the viewer's team won 21 kills to 28."""
    winner, note = decide_winner(21, 28, "VICTORY", 1)
    assert winner == 1 and "double-check" in note


def test_decide_winner_refuses_when_kills_strongly_contradict_banner():
    winner, note = decide_winner(30, 12, "VICTORY", 2)   # banner says 2, team 1 is +18
    assert winner is None and "set the winner yourself" in note


def test_decide_winner_from_kills_alone_is_flagged_or_refused():
    winner, note = decide_winner(30, 12, None, None)
    assert winner == 1 and "guess" in note
    # Real case (Oct 29 game 3): no banner in the crop, 26 kills to 27.
    winner, note = decide_winner(26, 27, None, 1)
    assert winner is None and "close" in note
    # The model's own read disagreeing with the kills: no hint.
    winner, note = decide_winner(30, 12, None, None, model_vote=2)
    assert winner is None and note


def _player(name, viewer=False):
    return {"name": name, "champion": "Ahri", "kills": 1, "deaths": 2, "assists": 3,
            "is_viewer": viewer}


def _vision_json(**over):
    board = {
        "team1": [_player(f"p{i}") for i in range(1, 6)],
        "team2": [_player(f"p{i}", viewer=(i == 8)) for i in range(6, 11)],
        "team1_name": None, "team2_name": None,
        "team1_kills": 14, "team2_kills": 33, "banner": "VICTORY",
        "team1_won": False, "team2_won": True,
    }
    board.update(over)
    return board


def test_vision_output_victory_for_bottom_team_viewer():
    sb = board_to_scoreboard(_vision_json())
    assert sb.viewer_team == 2 and sb.banner == "VICTORY"
    assert (sb.team1_kills, sb.team2_kills) == (14, 33)
    assert sb.winner == 2            # NOT "VICTORY means the top team won"
    assert len(sb.team1) == 5 and sb.team1[0].champion == "Ahri" and not sb.notes


def test_vision_output_conflicting_evidence_gives_no_winner_and_a_note():
    sb = board_to_scoreboard(_vision_json(team1_kills=40, team2_kills=10))
    assert sb.winner is None
    assert any("set the winner yourself" in n for n in sb.notes)


def test_vision_output_roster_table_has_no_outcome():
    sb = board_to_scoreboard(_vision_json(
        team1_kills=None, team2_kills=None, banner=None, team1_won=None, team2_won=None))
    assert sb.winner is None and not sb.notes


def test_text_parser_reads_header_kills_for_the_winner():
    text = "VICTORY\nTEAM 1 30/12/40 60,000\n1 @ Aa 1/1/1\nTEAM 2 12/30/20 45,000\n1 @ Bb 1/1/1\n"
    sb = parse_scoreboard_text(text)
    assert (sb.team1_kills, sb.team2_kills, sb.winner) == (30, 12, 1)


# --------------------------------------------------------------------- item 9: series validation

def _state(winners):
    games = [build_game_proposal(full_board(winner=w), resolve, T1, T2) for w in winners]
    return ReportState.from_proposals(1, games)


def test_ready_accepts_only_real_series_results():
    for winners in ([1, 1], [2, 2], [1, 2, 1], [2, 1, 2], [1, 2, 2], [1], [2]):
        assert _state(winners).ready() == (True, []), winners
    for winners in ([1, 2], [2, 1], [1, 1, 1], [2, 2, 2]):
        ok, problems = _state(winners).ready()
        assert not ok and any("isn't a best-of-3 result" in p for p in problems), winners


# --------------------------------------------------------------------- item 5: name resolution

def test_resolve_name_never_matches_a_single_leading_word():
    known = {"zack": 1, "zack fox": 2, "solo": 3}
    r = lambda n: known.get(n.lower())
    assert _resolve_name("Zack Fox", r) == (2, "Zack Fox")           # full name
    assert _resolve_name("Zack Fox 12 300", r) == (2, "Zack Fox")    # 2-word prefix: fine
    assert _resolve_name("Zack Smith", r) == (None, "Zack Smith")    # NOT matched to "zack"
    assert _resolve_name("Solo", r) == (3, "Solo")                   # a one-word name still resolves
    assert _resolve_name("Solo Queue", r) == (None, "Solo Queue")
    assert _resolve_name("  ", r) == (None, "")


# --------------------------------------------------------------------- item 13: image pre-flight

def test_image_problem_checks_size_and_type():
    assert image_problem(PNG_BYTES) is None
    assert image_problem(b"\xff\xd8\xff" + b"\x00" * 10) is None
    assert "Empty" in image_problem(b"")
    assert "isn't a PNG" in image_problem(b"%PDF-1.7 not an image")
    assert "too large" in image_problem(b"\x89PNG" + b"\x00" * MAX_IMAGE_BYTES)


def test_parse_scoreboard_image_never_raises_and_explains(monkeypatch):
    """Bad uploads come back as an empty board with a clear note — before any API
    call (no key is set here, and none is needed to get these answers)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    for data, expect in ((b"", "Empty"), (b"GARBAGE", "isn't a PNG"),
                         (b"\x89PNG" + b"\x00" * MAX_IMAGE_BYTES, "too large"),
                         (PNG_BYTES, "ANTHROPIC_API_KEY not set")):
        sb = parse_scoreboard_image(data)
        assert isinstance(sb, ParsedScoreboard) and sb.unreadable
        assert expect in sb.notes[0], sb.notes


def test_vision_client_is_given_a_timeout(monkeypatch):
    """The Anthropic client must be built with a ~60s timeout, and a timeout must
    come back as a note, not an exception. No network: the SDK class is faked."""
    import anthropic

    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            self.messages = self

        def create(self, **kwargs):
            seen["model"] = kwargs["model"]
            raise anthropic.APITimeoutError(request=None)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    sb = parse_scoreboard_image(PNG_BYTES)
    assert seen["timeout"] == 60.0
    assert sb.unreadable and "timed out" in sb.notes[0]


def test_champions_kept_on_lobby_screen_dropped_on_match_history_screen():
    """Champion names are printed on the lobby screen (which also prints an 'x.x KDA'
    line); on the match-history screen they are only portraits, and a guessed
    champion must not reach player stats."""
    def board(ratio_line):
        return {"kda_ratio_line": ratio_line,
                "team1": [_player(f"a{i}") for i in range(5)],
                "team2": [_player(f"b{i}") for i in range(5)], "team1_name": None,
                "team2_name": None, "team1_kills": 20, "team2_kills": 9, "banner": None,
                "team1_won": None, "team2_won": None}

    assert all(r.champion == "Ahri" for r in board_to_scoreboard(board("4.0 KDA")).team1)
    assert all(r.champion is None for r in board_to_scoreboard(board("")).team1)
    assert all(r.kills == 1 for r in board_to_scoreboard(board("")).team2)
