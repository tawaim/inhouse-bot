"""Admin commands: result reporting, channel config, rank syncing."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.config import Config, ROLES
from bot.db.models import GuildConfig, Match, MatchPerformance, Player, ProposalSet, Rating, Session as InhouseSession, Signup
from bot.db.session import get_session
from bot.services.elo import ENV, rating_from_db, update_team_ratings
from bot.services.ocr import parse_screenshot
from bot.services.riot_client import RiotAuthError, RiotClient

log = logging.getLogger(__name__)


# =============================================================================
# Manual match parsing
# =============================================================================

# Discord mention format: <@123456789> or <@!123456789> (the ! is for nicknames)
_MENTION_RE = re.compile(r"<@!?(\d+)>")
_ROLE_LINE_RE = re.compile(r"^\s*([A-Za-z]+)\s*:\s*(.+?)\s*$")


class ManualMatchParseError(ValueError):
    """Raised when the manual match input is malformed. Message is shown to admin."""


def parse_manual_match(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Parse a strict line-based team roster.

    Expected format (case-insensitive headers, exact role names):
        TEAM 1
        TOP: <@123>
        JUNGLE: <@456>
        MID: <@789>
        BOT: <@111>
        SUPPORT: <@222>
        TEAM 2
        TOP: <@333>
        ...

    Returns (team1_dict, team2_dict) where each dict maps role -> discord_id.

    Raises ManualMatchParseError with a human-readable message on any issue.
    """
    if not text or not text.strip():
        raise ManualMatchParseError("Input is empty.")

    current_team: Optional[int] = None
    teams: dict[int, dict[str, int]] = {1: {}, 2: {}}

    for line_num, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue

        # Team header?
        upper = line.upper()
        if upper in ("TEAM 1", "TEAM1", "T1"):
            current_team = 1
            continue
        if upper in ("TEAM 2", "TEAM2", "T2"):
            current_team = 2
            continue

        # Role line
        if current_team is None:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected a TEAM header first."
            )
        m = _ROLE_LINE_RE.match(line)
        if not m:
            raise ManualMatchParseError(
                f"Line {line_num}: `{line}` — expected `ROLE: @user`."
            )

        role = m.group(1).upper()
        # Accept some shorthand
        role_aliases = {
            "TOP": "TOP",
            "JUNGLE": "JUNGLE", "JG": "JUNGLE", "JUNG": "JUNGLE",
            "MID": "MID", "MIDDLE": "MID",
            "BOT": "BOT", "BOTTOM": "BOT", "ADC": "BOT",
            "SUPPORT": "SUPPORT", "SUP": "SUPPORT", "SUPP": "SUPPORT",
        }
        if role not in role_aliases:
            raise ManualMatchParseError(
                f"Line {line_num}: unknown role `{role}`. "
                f"Use TOP / JUNGLE / MID / BOT / SUPPORT."
            )
        canonical = role_aliases[role]

        if canonical in teams[current_team]:
            raise ManualMatchParseError(
                f"Line {line_num}: {canonical} already assigned on team {current_team}."
            )

        mention_match = _MENTION_RE.search(m.group(2))
        if not mention_match:
            raise ManualMatchParseError(
                f"Line {line_num}: no Discord mention found in `{m.group(2)}`. "
                f"Use @-mentions, e.g. `TOP: @alice`."
            )
        teams[current_team][canonical] = int(mention_match.group(1))

    # Validate completeness
    for team_num in (1, 2):
        missing = [r for r in ROLES if r not in teams[team_num]]
        if missing:
            raise ManualMatchParseError(
                f"Team {team_num} is missing: {', '.join(missing)}."
            )

    # No duplicate players across teams
    t1_ids = set(teams[1].values())
    t2_ids = set(teams[2].values())
    overlap = t1_ids & t2_ids
    if overlap:
        raise ManualMatchParseError(
            f"Player(s) on both teams: {', '.join(f'<@{i}>' for i in overlap)}."
        )
    if len(t1_ids) != 5 or len(t2_ids) != 5:
        raise ManualMatchParseError(
            "Each team must have 5 distinct players (a player was listed twice)."
        )

    return teams[1], teams[2]


# =============================================================================
# Modal for /manual-match input
# =============================================================================

class ManualMatchModal(discord.ui.Modal, title="Manual Match Entry"):
    roster = discord.ui.TextInput(
        label="Team roster",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000,
        placeholder=(
            "TEAM 1\n"
            "TOP: @user1\n"
            "JUNGLE: @user2\n"
            "MID: @user3\n"
            "BOT: @user4\n"
            "SUPPORT: @user5\n"
            "TEAM 2\n"
            "TOP: @user6\n..."
        ),
    )

    def __init__(self, session_id: int):
        super().__init__()
        self.session_id = session_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            team1, team2 = parse_manual_match(self.roster.value)
        except ManualMatchParseError as e:
            await interaction.followup.send(f"❌ Parse error: {e}", ephemeral=True)
            return

        all_ids = list(team1.values()) + list(team2.values())

        # Verify every mentioned user is linked
        async with get_session() as db:
            unlinked = []
            for pid in all_ids:
                player = await db.get(Player, pid)
                if player is None or not player.riot_puuid or player.link_status != "approved":
                    unlinked.append(pid)
            if unlinked:
                mentions = " ".join(f"<@{i}>" for i in unlinked)
                await interaction.followup.send(
                    f"❌ Not linked / not approved: {mentions}\n"
                    f"They must run `/link` (and be approved) before being used in a manual match.",
                    ephemeral=True,
                )
                return

            # Create the Match row
            match = Match(
                session_id=self.session_id,
                team1_json=json.dumps(team1),
                team2_json=json.dumps(team2),
                predicted_balance=None,  # n/a for manual
            )
            db.add(match)
            session = await db.get(InhouseSession, self.session_id)
            if session and session.status == "recruiting":
                session.status = "matched"
            await db.commit()
            await db.refresh(match)

        # Post to recruit channel
        from bot.config import ROLE_EMOJIS
        embed = discord.Embed(
            title=f"🏆 Manual Teams for Thursday {session.game_date.strftime('%b %d')}",
            color=discord.Color.green(),
        )
        t1 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team1[r]}>" for r in ROLES)
        t2 = "\n".join(f"{ROLE_EMOJIS[r]} **{r}**: <@{team2[r]}>" for r in ROLES)
        embed.add_field(name="🔵 Team 1 (Blue)", value=t1, inline=True)
        embed.add_field(name="🔴 Team 2 (Red)", value=t2, inline=True)
        embed.set_footer(text=f"Match {match.id} · Manual entry by {interaction.user.display_name}")

        if session and session.recruit_channel_id:
            channel = interaction.client.get_channel(session.recruit_channel_id)
            if channel:
                await channel.send(content="🔒 Teams (manually set):", embed=embed)

        await interaction.followup.send(
            f"✅ Match {match.id} created with manual roster. Posted to recruit channel.\n"
            f"When games are played, report the result with `/report` or `/report-manual`.",
            ephemeral=True,
        )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: Config, riot: RiotClient):
        self.bot = bot
        self.config = config
        self.riot = riot

    async def _is_admin(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        return any(r.name == self.config.admin_role_name for r in interaction.user.roles)

    @app_commands.command(name="set-channel", description="(admin) Configure which channel the bot posts to.")
    @app_commands.describe(
        purpose="Which type of channel to configure",
        channel="The channel to use",
    )
    async def set_channel(
        self,
        interaction: discord.Interaction,
        purpose: Literal["recruit", "results"],
        channel: discord.TextChannel,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        async with get_session() as db:
            cfg = await db.get(GuildConfig, interaction.guild_id)
            if cfg is None:
                cfg = GuildConfig(guild_id=interaction.guild_id)
                db.add(cfg)
            if purpose == "recruit":
                cfg.recruit_channel_id = channel.id
            elif purpose == "results":
                cfg.results_channel_id = channel.id
            await db.commit()
        await interaction.response.send_message(
            f"✅ {purpose} channel set to {channel.mention}", ephemeral=True
        )

    @app_commands.command(name="report", description="(admin) Report game outcome with screenshot.")
    @app_commands.describe(
        match_id="The match ID from the teams post",
        winner="Which team won",
        screenshot="End-of-game screenshot",
    )
    async def report(
        self,
        interaction: discord.Interaction,
        match_id: int,
        winner: Literal["team1", "team2"],
        screenshot: discord.Attachment,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer()

        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            if match.winner is not None:
                await interaction.followup.send(
                    f"Match {match_id} already reported (winner: team{match.winner}). "
                    f"Use /unreport first if this is a correction.",
                    ephemeral=True,
                )
                return

        # OCR for KDA enrichment (winner is admin-specified, not OCR-determined)
        image_bytes = await screenshot.read()
        parsed = parse_screenshot(image_bytes)

        # Show admin a confirmation embed before committing
        embed = discord.Embed(
            title=f"Confirm Match {match_id} Report",
            color=discord.Color.orange(),
            description=f"**Winner:** {winner}\n**Screenshot:** {screenshot.filename}",
        )
        if parsed.notes:
            embed.add_field(name="OCR Notes", value="\n".join(parsed.notes), inline=False)
        if parsed.players:
            lines = [
                f"`{p.kills}/{p.deaths}/{p.assists}` {p.riot_id or '?'}"
                for p in parsed.players[:12]
            ]
            embed.add_field(name="Detected rows", value="\n".join(lines) or "none", inline=False)
        embed.set_footer(text=f"OCR confidence: {parsed.confidence:.0%}. Click ✅ to commit, ❌ to cancel.")

        confirm_msg = await interaction.followup.send(embed=embed)
        await confirm_msg.add_reaction("✅")
        await confirm_msg.add_reaction("❌")

        def check(reaction: discord.Reaction, user: discord.User):
            return (
                user.id == interaction.user.id
                and reaction.message.id == confirm_msg.id
                and str(reaction.emoji) in ("✅", "❌")
            )

        try:
            reaction, _ = await self.bot.wait_for("reaction_add", check=check, timeout=120.0)
        except Exception:
            await confirm_msg.edit(content="⏱️ Confirmation timed out.", embed=None)
            return

        if str(reaction.emoji) == "❌":
            await confirm_msg.edit(content="❌ Cancelled.", embed=None)
            return

        # Commit: update match, write performances, run elo update
        winner_int = 1 if winner == "team1" else 2
        await self._commit_result(match_id, winner_int, screenshot.url, interaction.user.id, parsed)
        await confirm_msg.edit(
            content=f"✅ Match {match_id} recorded. Team {winner_int} wins. Elo updated.",
            embed=None,
        )

    @app_commands.command(name="report-manual", description="(admin) Report game outcome without a screenshot.")
    @app_commands.describe(
        match_id="The match ID from the teams post",
        winner="Which team won",
    )
    async def report_manual(
        self,
        interaction: discord.Interaction,
        match_id: int,
        winner: Literal["team1", "team2"],
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                await interaction.followup.send(f"Match {match_id} not found.", ephemeral=True)
                return
            if match.winner is not None:
                await interaction.followup.send(f"Match {match_id} already reported.", ephemeral=True)
                return
        winner_int = 1 if winner == "team1" else 2
        await self._commit_result(match_id, winner_int, None, interaction.user.id, None)
        await interaction.followup.send(
            f"✅ Match {match_id} recorded. Team {winner_int} wins. Elo updated.",
            ephemeral=True,
        )

    @app_commands.command(name="sync-ranks", description="(admin) Refresh Riot API rank data for all linked players.")
    async def sync_ranks(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        async with get_session() as db:
            players = (await db.execute(
                select(Player).where(
                    Player.riot_puuid.is_not(None),
                    Player.link_status == "approved",
                )
            )).scalars().all()
            updated = 0
            errors = 0
            for player in players:
                try:
                    rank = await self.riot.get_solo_rank(player.riot_puuid)
                    if rank:
                        player.solo_tier = rank.tier
                        player.solo_rank = rank.rank
                        player.solo_lp = rank.league_points
                    player.riot_last_synced = datetime.utcnow()
                    updated += 1
                except RiotAuthError:
                    await interaction.followup.send("❌ Riot API key rejected. Stop and check key.", ephemeral=True)
                    return
                except Exception:
                    log.exception("Failed to sync %s", player.discord_id)
                    errors += 1
            await db.commit()
        await interaction.followup.send(
            f"✅ Synced {updated} players ({errors} errors).", ephemeral=True
        )

    # ---------- internal: commit a match result + run elo update ----------

    async def _commit_result(
        self,
        match_id: int,
        winner: int,  # 1 or 2
        screenshot_url: str | None,
        admin_id: int,
        parsed,
    ) -> None:
        async with get_session() as db:
            match = await db.get(Match, match_id)
            if match is None:
                return
            team1: dict[str, int] = {k: int(v) for k, v in json.loads(match.team1_json).items()}
            team2: dict[str, int] = {k: int(v) for k, v in json.loads(match.team2_json).items()}

            # Pull current ratings for each player at the role they played
            t1_ratings = []
            t2_ratings = []
            t1_keys = []   # parallel list of (discord_id, role) for write-back
            t2_keys = []
            for role, pid in team1.items():
                r = await db.get(Rating, (pid, role))
                if r is None:
                    r = Rating(discord_id=pid, role=role, mu=ENV.mu, sigma=ENV.sigma, games_played=0)
                    db.add(r)
                t1_ratings.append(rating_from_db(r.mu, r.sigma))
                t1_keys.append((pid, role, r))
            for role, pid in team2.items():
                r = await db.get(Rating, (pid, role))
                if r is None:
                    r = Rating(discord_id=pid, role=role, mu=ENV.mu, sigma=ENV.sigma, games_played=0)
                    db.add(r)
                t2_ratings.append(rating_from_db(r.mu, r.sigma))
                t2_keys.append((pid, role, r))

            new_t1, new_t2 = update_team_ratings(t1_ratings, t2_ratings, team1_won=(winner == 1))
            for (pid, role, r), nr in zip(t1_keys, new_t1):
                r.mu = nr.mu
                r.sigma = nr.sigma
                r.games_played += 1
            for (pid, role, r), nr in zip(t2_keys, new_t2):
                r.mu = nr.mu
                r.sigma = nr.sigma
                r.games_played += 1

            # Write per-player performance rows. KDA from OCR if available.
            ocr_by_riot_id = {}
            if parsed:
                ocr_by_riot_id = {p.riot_id: p for p in parsed.players if p.riot_id}

            async def get_kda(pid: int):
                if not parsed:
                    return None, None, None
                player = await db.get(Player, pid)
                if not player or not player.riot_game_name:
                    return None, None, None
                key = f"{player.riot_game_name}#{player.riot_tag_line}"
                row = ocr_by_riot_id.get(key)
                if not row:
                    return None, None, None
                return row.kills, row.deaths, row.assists

            for role, pid in team1.items():
                k, d, a = await get_kda(pid)
                db.add(MatchPerformance(
                    match_id=match.id, discord_id=pid, role=role,
                    kills=k, deaths=d, assists=a, won=(winner == 1),
                ))
            for role, pid in team2.items():
                k, d, a = await get_kda(pid)
                db.add(MatchPerformance(
                    match_id=match.id, discord_id=pid, role=role,
                    kills=k, deaths=d, assists=a, won=(winner == 2),
                ))

            match.winner = winner
            match.reported_by = admin_id
            match.reported_at = datetime.utcnow()
            match.screenshot_url = screenshot_url

            # Mark session completed if this was its only match
            session = await db.get(InhouseSession, match.session_id)
            if session and session.status == "matched":
                session.status = "completed"

            await db.commit()

    # ========== Test/dev helpers ==========

    @app_commands.command(
        name="test-fake-signups",
        description="(admin) Fill the active session with fake players for testing matchmaking.",
    )
    @app_commands.describe(
        count="How many fake players to add (default 10)",
        clear_first="If true, wipe existing signups from this session before adding fakes",
    )
    async def test_fake_signups(
        self,
        interaction: discord.Interaction,
        count: int = 10,
        clear_first: bool = True,
    ):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        if count < 1 or count > 30:
            await interaction.response.send_message("Count must be 1-30.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        import random
        # Stable per-fake skills so /match-preview shows the same proposals on re-runs
        rng = random.Random(42)

        # Find the most recent recruiting session
        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send(
                    "No active recruiting session. Run `/recruit-now` first.",
                    ephemeral=True,
                )
                return

            if clear_first:
                # Delete all existing signups for this session
                existing = (await db.execute(
                    select(Signup).where(Signup.session_id == session.id)
                )).scalars().all()
                for s in existing:
                    await db.delete(s)
                # Also delete the fake players we created previously, identified by
                # discord_id range below 1000 (real Discord IDs are ~18 digits)
                fake_players = (await db.execute(
                    select(Player).where(Player.discord_id < 10_000)
                )).scalars().all()
                for p in fake_players:
                    await db.delete(p)
                await db.commit()

            # Generate roles with realistic distribution: a few specialists per role,
            # plus some FILLs. We pick from this template and shuffle.
            role_templates = [
                ["TOP"],
                ["TOP"],
                ["JUNGLE"],
                ["JUNGLE"],
                ["MID"],
                ["MID"],
                ["BOT"],
                ["BOT"],
                ["SUPPORT"],
                ["SUPPORT"],
                ["FILL"],
                ["FILL"],
                ["TOP", "MID"],     # multi-role
                ["JUNGLE", "SUPPORT"],
                ["BOT"],
            ]
            rng.shuffle(role_templates)

            # Realistic skill spread: mostly mid-range with a few outliers
            tiers_pool = [
                ("SILVER", "II"), ("GOLD", "IV"), ("GOLD", "II"),
                ("PLATINUM", "IV"), ("PLATINUM", "II"), ("EMERALD", "III"),
                ("DIAMOND", "IV"), ("BRONZE", "I"), ("GOLD", "I"),
                ("PLATINUM", "I"),
            ]

            from bot.services.elo import seed_from_rank
            from bot.config import ROLES

            created = []
            for i in range(count):
                fake_id = 1 + i  # use 1, 2, 3, ... — won't collide with real Discord IDs
                tier, division = tiers_pool[i % len(tiers_pool)]
                # Add a little noise so two players with same tier differ slightly
                seed_mu, seed_sigma = seed_from_rank(tier, division)
                jitter = rng.uniform(-1.0, 1.0)

                # Insert/update fake Player
                player = await db.get(Player, fake_id)
                if player is None:
                    player = Player(
                        discord_id=fake_id,
                        riot_game_name=f"FakePlayer{i+1}",
                        riot_tag_line="TEST",
                        solo_tier=tier,
                        solo_rank=division,
                        link_status="approved",
                    )
                    db.add(player)

                # Per-role ratings
                for role in ROLES:
                    rating = await db.get(Rating, (fake_id, role))
                    if rating is None:
                        db.add(Rating(
                            discord_id=fake_id,
                            role=role,
                            mu=seed_mu + jitter,
                            sigma=seed_sigma,
                            games_played=0,
                        ))

                # Signup with a random role template
                roles = role_templates[i % len(role_templates)]
                db.add(Signup(
                    session_id=session.id,
                    discord_id=fake_id,
                    status="playing",
                    roles=",".join(roles),
                    signed_up_at=datetime.utcnow(),
                ))
                created.append((fake_id, tier, division, roles))

            await db.commit()

        summary = "\n".join(
            f"`{pid:>3}` **FakePlayer{pid}** · {tier} {div} · {','.join(roles)}"
            for pid, tier, div, roles in created
        )
        await interaction.followup.send(
            f"✅ Created {count} fake playing signups for session {session.id}.\n\n"
            f"{summary}\n\n"
            f"Next: run `/match-preview` to see the top-3 proposals, or wait for "
            f"the Monday 9:30 PM scheduler to fire.",
            ephemeral=True,
        )

    @app_commands.command(
        name="test-trigger-close",
        description="(admin) Manually trigger the Monday close-and-DM-options job for testing.",
    )
    async def test_trigger_close(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        from bot.cogs.recruitment import RecruitmentCog
        cog = self.bot.get_cog("RecruitmentCog")
        if not isinstance(cog, RecruitmentCog):
            await interaction.followup.send("Recruitment cog not loaded.", ephemeral=True)
            return

        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession).where(InhouseSession.status == "recruiting")
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.followup.send("No active recruiting session.", ephemeral=True)
                return

        try:
            await cog.close_signups_and_match(session.id)
            await interaction.followup.send(
                f"✅ Triggered close on session {session.id}. Check your DMs for the 3 options.",
                ephemeral=True,
            )
        except Exception as e:
            log.exception("test-trigger-close failed")
            await interaction.followup.send(f"❌ Failed: {e}", ephemeral=True)

    @app_commands.command(
        name="manual-match",
        description="(admin) Override the matchmaker — paste in a roster you made yourself.",
    )
    async def manual_match(self, interaction: discord.Interaction):
        if not await self._is_admin(interaction):
            await interaction.response.send_message("League Admin only.", ephemeral=True)
            return

        # Find the active session — either still recruiting, or already matched
        # (admin can override even after auto-matchmaker ran)
        async with get_session() as db:
            session = (await db.execute(
                select(InhouseSession)
                .where(InhouseSession.status.in_(["recruiting", "matched"]))
                .order_by(InhouseSession.game_date.asc())
            )).scalars().first()
            if session is None:
                await interaction.response.send_message(
                    "No active session. Use `/recruit-now` first.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(ManualMatchModal(session.id))
