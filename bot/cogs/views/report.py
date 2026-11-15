"""The /report confirm flow: the embed showing what was read from the
screenshots, and the view that lets the admin set winners, fix subs, edit
champions/KDA and commit."""
from __future__ import annotations

import logging
import re
from typing import Optional

import discord

from bot.db.models import Player
from bot.db.session import get_session
from bot.services.champions import resolve_champion
from bot.services.report_analysis import ReportState
from bot.services.rosters import TEAM_NAMES

log = logging.getLogger(__name__)



ALL_UNREADABLE_MSG = (
    "🚫 **None of the screenshots could be read.** The lineups below are just the match "
    "roster, not what was played. Pick each game's winner and enter any subs by hand — "
    "or cancel and run `/report` again."
)


def _commit_status_message(status: str, match_id: int) -> str:
    """What to tell the admin when _commit_games/_commit_result did NOT commit."""
    if status == "not_found":
        return f"❌ Match {match_id} not found — nothing was recorded."
    if status == "already_reported":
        return (f"⚠️ Match {match_id} was already reported (by someone else, or a moment ago) — "
                f"nothing was changed. `/unreport {match_id}` first to correct it.")
    if status == "empty":
        return f"❌ No games to record for match {match_id} — nothing was recorded."
    return f"❌ Match {match_id} was not recorded ({status})."


def _report_notes(state: ReportState) -> str:
    """The embed description: everything the read flagged, per game. These are the
    only sign that a screenshot failed or a guess was withheld, so they must show."""
    lines: list[str] = []
    if state.all_unreadable:
        lines.append(ALL_UNREADABLE_MSG)
    multi = len(state.games) > 1
    for gi, g in enumerate(state.games, start=1):
        for note in g.notes:
            prefix = f"**Game {gi}:** " if multi else ""
            lines.append(f"⚠️ {prefix}{note[:300]}")
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3490] + "…"


def _report_embed(state: ReportState, labels: dict[int, str]) -> discord.Embed:
    """Render the current proposed report for the confirm message."""
    e = discord.Embed(title=f"Confirm report — Match {state.match_id}",
                      color=discord.Color.orange())
    notes = _report_notes(state)
    if notes:
        e.description = notes
    for gi, (g, winner) in enumerate(zip(state.games, state.winners), start=1):
        lines = []
        if winner not in (1, 2):
            lines.append("❓ **No winner set** — press the game's button below.")
        for team in (1, 2):
            tag = "🔵" if team == 1 else "🔴"
            crown = " 🏆" if winner == team else ""
            lines.append(f"{tag} **{TEAM_NAMES[team]}**{crown}")
            for s in [x for x in g.slots if x.team == team]:
                who = labels.get(s.discord_id, f"<@{s.discord_id}>") if s.discord_id \
                    else f"⚠️ {s.name_guess or '?'}"
                mark = " 🔁" if s.is_sub else ""  # subbed-in player (not the rostered one)
                champ = s.champion or "—"
                kda = f"{s.kills}/{s.deaths}/{s.assists}" if s.kills is not None else "—"
                lines.append(f"`{s.role:7}` {who}{mark} · {champ} · {kda}")
        e.add_field(name=f"Game {gi}", value="\n".join(lines)[:1024], inline=False)
    ok, problems = state.ready()
    e.set_footer(text=(("⚠️ " + " · ".join(problems[:3]))[:2000]) if problems
                 else "Check winners, players and champions, then ✅ Commit.")
    return e


def _winner_label(gi: int, winner: Optional[int]) -> str:
    return f"G{gi+1}: {TEAM_NAMES[winner]} won" if winner in (1, 2) else f"G{gi+1}: winner?"


class _WinnerButton(discord.ui.Button):
    """Cycles one game's winner. Red 'winner?' until the admin (or a trustworthy
    hint from the screenshot) has set it — a winner is never assumed."""

    def __init__(self, gi: int, state: ReportState):
        w = state.winners[gi]
        super().__init__(label=_winner_label(gi, w), row=0,
                         style=discord.ButtonStyle.secondary if w in (1, 2)
                         else discord.ButtonStyle.danger)
        self.gi = gi

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v.state.toggle_winner(self.gi)
        self.label = _winner_label(self.gi, v.state.winners[self.gi])
        self.style = discord.ButtonStyle.secondary
        await v.refresh(interaction)


class _GameSelect(discord.ui.Select):
    """Fix mode, step 1: which game. Each game has 10 slots and a select holds at
    most 25 options, so a 3-game series can't list all 30 slots in one menu —
    pick the game here, then the slot below."""

    def __init__(self, state: ReportState, current: int):
        options = []
        for gi, g in enumerate(state.games):
            subs = sum(1 for s in g.slots if s.is_sub)
            desc = f"{subs} sub(s)" if subs else "no subs"
            if g.orientation_uncertain:
                desc += " · teams uncertain"
            options.append(discord.SelectOption(
                label=f"Game {gi+1}", value=str(gi), description=desc, default=(gi == current)))
        super().__init__(placeholder="Which game?", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        v = self.view
        v._game = int(self.values[0])
        v._sel = None
        v.show_fix()
        await v.refresh(interaction)


class _SlotSelect(discord.ui.Select):
    """Fix mode, step 2: which of the chosen game's 10 slots to change. Slots are
    pre-filled from the match roster, so this is mainly for confirming/correcting
    subs (listed first) — or fixing the rare slot the read got wrong."""

    def __init__(self, state: ReportState, gi: int = 0):
        slots = list(state.games[gi].slots)
        slots.sort(key=lambda s: (not s.is_sub, s.team))  # subs first (stable: role order kept)
        options = []
        for s in slots[:25]:  # Discord caps a select at 25 options; a game has 10
            read = s.name_guess or "—"
            options.append(discord.SelectOption(
                label=f"G{gi+1} · {TEAM_NAMES[s.team]} · {s.role}"[:100],
                value=f"{gi}:{s.team}:{s.role}",
                description=(("SUB → " if s.is_sub else "read: ") + read)[:100],
            ))
        super().__init__(placeholder=f"Change a Game {gi+1} slot (subs listed first)…",
                         options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        gi, team, role = self.values[0].split(":")
        self.view._game = int(gi)
        self.view._sel = (int(gi), int(team), role)
        self.view.show_fix()
        await self.view.refresh(interaction)


class _SlotPlayerSelect(discord.ui.UserSelect):
    """Pick the player for the slot chosen in _SlotSelect. Only a linked, approved
    player is accepted. If the name read for that slot didn't resolve to anyone,
    it is saved as an alias for the picked player so it auto-resolves next time —
    and the admin is told exactly what was learned."""

    def __init__(self, sel):
        gi, _team, role = sel
        super().__init__(placeholder=f"Set G{gi+1} {role} to…",
                         min_values=1, max_values=1, row=2)
        self.sel = sel

    async def callback(self, interaction: discord.Interaction):
        gi, team, role = self.sel
        member = self.values[0]
        v = self.view
        none = discord.AllowedMentions.none()

        # The picker lists every guild member, including bots and people who never
        # linked. Only a linked + approved player has ratings to update.
        async with get_session() as db:
            player = await db.get(Player, member.id)
        if (getattr(member, "bot", False) or player is None or not player.riot_puuid
                or player.link_status != "approved"):
            what = "a bot" if getattr(member, "bot", False) else "not a linked, approved player"
            await interaction.response.send_message(
                f"❌ {member.mention} is {what}, so they can't be put in a game. "
                f"They need to `/link` (and be approved) first. The slot was not changed.",
                ephemeral=True, allowed_mentions=none)
            return

        slot = v.state._slot(gi, team, role)
        v.state.set_player(gi, team, role, member.id)
        v.labels[member.id] = member.display_name
        note = None
        # Learn an alias ONLY for a name nobody matched. If the read name already
        # resolved to a player, re-pointing it here would bind one player's name to
        # another (e.g. when picking a sub for the slot the rostered name was read on).
        if slot and slot.name_guess and not slot.read_resolved:
            status, other = await v.cog._learn_alias(slot.name_guess, member.id)
            shown = f"“{slot.name_guess}”"
            if status == "learned":
                slot.read_resolved = True
                note = (f"🧠 Learned alias {shown} → {member.mention}. "
                        f"Undo with `/remove-alias` if that's wrong.")
            elif status == "conflict":
                note = (f"⚠️ {shown} is already an alias for <@{other}> — left unchanged. "
                        f"Use `/set-alias` if it should point to {member.mention}.")
        v._sel = None
        v.show_fix()
        await v.refresh(interaction)
        if note:
            await interaction.followup.send(note, ephemeral=True, allowed_mentions=none)


_KDA_LINE_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$")


class _ChampionModal(discord.ui.Modal, title="Champions & KDA — one player per line"):
    """Edit champion + KDA per player. One paragraph box per game, 10 lines in the
    shown order (T1 top→sup, then T2 top→sup); each line is 'Champion k/d/a'."""

    def __init__(self, view: "ReportConfirmView"):
        super().__init__()
        self.view_ref = view
        self.boxes = []
        for gi, g in enumerate(view.state.games):
            lines = []
            for s in g.slots:
                kda = f"{s.kills}/{s.deaths}/{s.assists}" if s.kills is not None else ""
                lines.append(f"{s.champion or ''} {kda}".strip())
            box = discord.ui.TextInput(
                label=f"Game {gi+1} — 'Champion k/d/a' per line",
                style=discord.TextStyle.paragraph, required=False,
                default="\n".join(lines), max_length=600,
            )
            self.boxes.append(box)
            self.add_item(box)

    async def on_submit(self, interaction: discord.Interaction):
        for gi, box in enumerate(self.boxes):
            slots = self.view_ref.state.games[gi].slots
            for i, line in enumerate(box.value.splitlines()):
                if i >= len(slots) or not line.strip():
                    continue
                m = _KDA_LINE_RE.search(line)
                if m:
                    slots[i].kills = int(m.group(1))
                    slots[i].deaths = int(m.group(2))
                    slots[i].assists = int(m.group(3))
                    line = line[: m.start()].strip()
                if line.strip():
                    slots[i].champion = resolve_champion(line) or line.strip()
        await self.view_ref.refresh(interaction)


class ReportConfirmView(discord.ui.View):
    """Ephemeral confirm/correct UI for /report. Drives a pure ReportState; the
    Discord components are a thin shell over its tested transitions."""

    # Comfortably under Discord's 15-minute interaction-token lifetime (the /report
    # token is already a minute or two old when the confirm message appears), so
    # on_timeout can still edit the message.
    TIMEOUT_SECONDS = 780

    def __init__(self, cog, state: ReportState, labels: dict[int, str],
                 screenshot_url: Optional[str], author_id: int):
        super().__init__(timeout=self.TIMEOUT_SECONDS)
        self.cog = cog
        self.state = state
        self.labels = labels
        self.screenshot_url = screenshot_url
        self.author_id = author_id
        self._game = 0    # game being edited in fix mode
        self._sel = None  # (game_idx, team, role) currently being edited in fix mode
        self._committing = False
        self.message = None   # the confirm message (set by /report), for on_timeout
        self._last_ix = None  # most recent interaction on this view, for on_timeout
        self.show_main()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your report.", ephemeral=True)
            return False
        self._last_ix = interaction
        return True

    async def on_timeout(self) -> None:
        """The report sat untouched until the view expired. Nothing was saved (the
        proposal lives only in memory) — say so instead of leaving dead buttons."""
        if self._committing:
            return
        text = (f"⌛ This report for match {self.state.match_id} expired after "
                f"{self.TIMEOUT_SECONDS // 60} minutes without being committed — "
                f"**nothing was saved**. Run `/report` again with the same screenshots.")
        try:
            if self._last_ix is not None:
                await self._last_ix.edit_original_response(content=text, embed=None, view=None)
            elif self.message is not None:
                await self.message.edit(content=text, embed=None, view=None)
        except Exception:
            log.warning("report: couldn't mark the expired confirm message", exc_info=True)

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=_report_embed(self.state, self.labels), view=self)

    def show_main(self):
        self.clear_items()
        for gi in range(len(self.state.games)):
            self.add_item(_WinnerButton(gi, self.state))
        fix = discord.ui.Button(label="Subs / fix", emoji="🔁",
                                style=discord.ButtonStyle.primary, row=1)
        fix.callback = self._on_fix
        champs = discord.ui.Button(label="Champs / KDA", emoji="⚔️",
                                   style=discord.ButtonStyle.primary, row=1)
        champs.callback = self._on_champions
        commit = discord.ui.Button(label="Commit", emoji="✅",
                                   style=discord.ButtonStyle.success, row=2)
        commit.callback = self._on_commit
        cancel = discord.ui.Button(label="Cancel", emoji="❌",
                                   style=discord.ButtonStyle.danger, row=2)
        cancel.callback = self._on_cancel
        for b in (fix, champs, commit, cancel):
            self.add_item(b)

    def show_fix(self):
        """Fix mode: game → slot → player, plus 'swap teams' for the chosen game.
        Picking the game first keeps every slot reachable (a select holds 25
        options; three games have 30 slots)."""
        self.clear_items()
        n = len(self.state.games)
        self._game = min(max(self._game, 0), n - 1)
        if n > 1:
            self.add_item(_GameSelect(self.state, self._game))
        self.add_item(_SlotSelect(self.state, self._game))
        if self._sel is not None:
            self.add_item(_SlotPlayerSelect(self._sel))
        swap = discord.ui.Button(label=f"Swap teams (Game {self._game + 1})", emoji="🔀",
                                 style=discord.ButtonStyle.secondary, row=3)
        swap.callback = self._on_swap
        self.add_item(swap)
        done = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary, row=4)
        done.callback = self._on_done
        self.add_item(done)

    async def _on_fix(self, interaction: discord.Interaction):
        self._sel = None
        self.show_fix()
        await self.refresh(interaction)

    async def _on_swap(self, interaction: discord.Interaction):
        """The screenshot's two teams were mapped onto the match's teams the wrong
        way round — flip them for this game (and with them the hinted winner)."""
        gi = self._game
        if not self.state.swap_teams(gi):
            await interaction.response.send_message(
                "This game wasn't built from a screenshot, so there's nothing to swap.",
                ephemeral=True)
            return
        self.state.games[gi].notes.append(
            "Teams swapped by you. Any player/champion edits made to this game before "
            "the swap were reset — re-check it.")
        self._sel = None
        self.show_fix()
        await self.refresh(interaction)

    async def _on_done(self, interaction: discord.Interaction):
        self.show_main()
        await self.refresh(interaction)

    async def _on_champions(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_ChampionModal(self))

    async def _on_cancel(self, interaction: discord.Interaction):
        for c in self.children:
            c.disabled = True
        self.stop()
        await interaction.response.edit_message(content="❌ Report cancelled.", embed=None, view=None)

    async def _on_commit(self, interaction: discord.Interaction):
        if self._committing:
            await interaction.response.send_message(
                "Already committing this report — hang on.", ephemeral=True)
            return
        ok, problems = self.state.ready()
        if not ok:
            await interaction.response.send_message(
                "Can't commit yet:\n• " + "\n• ".join(problems), ephemeral=True)
            return
        # Lock the view BEFORE the first await, so a double-click (two interactions
        # dispatched back to back) can't start a second commit.
        self._committing = True
        for c in self.children:
            c.disabled = True
        match_id = self.state.match_id
        embed = _report_embed(self.state, self.labels)
        try:
            await interaction.response.edit_message(
                content=f"⏳ Recording match {match_id}…", embed=embed, view=self)
            games = self.state.to_game_results()
            status = await self.cog._commit_games(
                match_id, games, self.screenshot_url, self.author_id)
        except Exception as e:
            # Without this the admin sees nothing at all: the interaction was already
            # acknowledged, so discord.py only logs the error.
            log.exception("report: commit failed for match %s", match_id)
            self._committing = False
            self.show_main()
            detail = f"{type(e).__name__}: {e}"[:300]
            try:
                await interaction.edit_original_response(
                    content=(f"❌ **Commit failed — match {match_id} was NOT recorded** and no "
                             f"elo changed.\n`{detail}`\nYou can press ✅ Commit to try again."),
                    embed=embed, view=self)
            except Exception:
                log.exception("report: couldn't show the commit failure")
            return
        self.stop()
        if status == "ok":
            content = f"✅ Match {match_id} recorded — {len(games)} game(s), elo updated."
        else:
            content = _commit_status_message(status, match_id)
        await interaction.edit_original_response(content=content, embed=embed, view=None)
