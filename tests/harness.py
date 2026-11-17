"""Test harness: run the bot's real cogs end-to-end with no Discord and no network.

A `World` is a throwaway SQLite database plus fake Discord objects (guild, members,
channels, DMs, interactions) wired to the real RecruitmentCog / AdminCog / LinkingCog
/ StatsCog. Tests drive the same code paths production does — slash-command
callbacks, button handlers, the close job — and assert on what the bot "sent" and
on the database afterwards.

    from tests.harness import flow

    @flow
    async def test_something(world):
        players = await world.add_players(10)
        session = await world.recruit(date(2026, 11, 19))
        ...

Fakes subclass the discord.py classes the bot isinstance-checks (Member,
TextChannel) but never call their constructors, so nothing here opens a socket.
"""
from __future__ import annotations

import asyncio
import functools
import itertools
import types
from datetime import date, datetime
from typing import Any, Callable, Optional

import discord
from sqlalchemy import select

import json

from bot.config import ROLES, Config
from bot.db.models import (
    Alias, GameStat, GuildConfig, Match, Player, ProposalSet, Rating,
    Session as InhouseSession, Signup,
)
from bot.db.session import close_db, get_session, init_db
from bot.services.elo import INHOUSE_ROLE
from bot.services.ocr import ParsedScoreboard, ScoreRow

ADMIN_ROLE = "League Admin"
GUILD_ID = 1000
OWNER_ID = 1
_ids = itertools.count(5000)


def make_config(db_path: str, timezone: str = "America/New_York") -> Config:
    return Config(
        discord_token="x", discord_guild_id=GUILD_ID, owner_discord_id=OWNER_ID,
        riot_api_key="x", riot_region="na1", riot_regional_route="americas",
        admin_role_name=ADMIN_ROLE, database_url=f"sqlite+aiosqlite:///{db_path}",
        timezone=timezone,
    )


def http_error(status: int = 500) -> discord.HTTPException:
    """A Discord API failure, for testing error paths."""
    cls = discord.Forbidden if status == 403 else discord.HTTPException
    return cls(types.SimpleNamespace(status=status, reason="simulated"), "simulated failure")


# --------------------------------------------------------------------------- messages

class FakeMessage:
    def __init__(self, channel, content=None, **kwargs):
        self.id = next(_ids)
        self.channel = channel
        self.content = content
        self.embed = kwargs.get("embed")
        self.embeds = kwargs.get("embeds") or ([self.embed] if self.embed else [])
        self.view = kwargs.get("view")
        self.kwargs = kwargs
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "embed" in kwargs:
            self.embed = kwargs["embed"]
            self.embeds = [self.embed] if self.embed else []
        if "view" in kwargs:
            self.view = kwargs["view"]
        return self

    @property
    def text(self) -> str:
        """Everything a human would read in this message, flattened."""
        parts = [self.content or ""]
        for e in self.embeds:
            parts += [e.title or "", e.description or ""]
            parts += [f"{f.name}\n{f.value}" for f in e.fields]
            if e.footer and e.footer.text:
                parts.append(e.footer.text)
        return "\n".join(p for p in parts if p)


class _Sendable:
    """Shared send/fetch behaviour for channels and DMs."""

    def _init_sendable(self):
        self.sent: list[FakeMessage] = []
        self.fail_sends = 0  # set to N to make the next N sends raise HTTPException

    async def send(self, content=None, **kwargs):
        if self.fail_sends:
            self.fail_sends -= 1
            raise http_error()
        msg = FakeMessage(self, content, **kwargs)
        self.sent.append(msg)
        return msg

    async def fetch_message(self, message_id: int):
        for m in self.sent:
            if m.id == message_id:
                return m
        raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "unknown message")

    def get_partial_message(self, message_id: int):
        for m in self.sent:
            if m.id == message_id:
                return m
        return FakeMessage(self)

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


class FakeChannel(_Sendable, discord.TextChannel):
    """A guild text channel. Passes isinstance(x, discord.TextChannel)."""

    def __init__(self, name: str, guild: "FakeGuild"):  # noqa: super().__init__ deliberately skipped
        self.id = next(_ids)
        self.name = name
        self.guild = guild
        self._init_sendable()

    def permissions_for(self, obj):  # the bot can do everything unless a test says otherwise
        return discord.Permissions.all()

    async def edit(self, **kwargs):
        return self

    async def delete(self, **kwargs):
        self.guild.channels.pop(self.id, None)


class FakeDM(_Sendable):
    def __init__(self, user):
        self.id = next(_ids)
        self.recipient = user
        self.guild = None
        self._init_sendable()


# --------------------------------------------------------------------------- people

class FakeMember(discord.Member):
    """A guild member. Passes isinstance(x, discord.Member)."""

    def __init__(self, id: int, name: str, guild: "FakeGuild", roles: tuple[str, ...] = (), bot: bool = False):
        self.__dict__["_f"] = dict(id=id, name=name, roles=roles, bot=bot)
        self.guild = guild
        self.dm = FakeDM(self)
        self.dms_closed = False

    id = property(lambda self: self._f["id"])
    name = property(lambda self: self._f["name"])
    display_name = property(lambda self: self._f["name"])
    global_name = property(lambda self: self._f["name"])
    nick = property(lambda self: None)
    bot = property(lambda self: self._f["bot"])
    mention = property(lambda self: f"<@{self._f['id']}>")
    roles = property(lambda self: [types.SimpleNamespace(name=r, id=hash(r) & 0xFFFF) for r in self._f["roles"]])
    guild_permissions = property(lambda self: discord.Permissions.all())

    def __eq__(self, other):
        return getattr(other, "id", None) == self.id

    def __hash__(self):
        return hash(self.id)

    async def send(self, content=None, **kwargs):
        if self.dms_closed:
            raise http_error(403)
        return await self.dm.send(content, **kwargs)

    async def add_roles(self, *roles, **kwargs):
        return None

    async def remove_roles(self, *roles, **kwargs):
        return None


class FakeGuild:
    def __init__(self):
        self.id = GUILD_ID
        self.chunked = True  # member cache is "complete"; the bot skips guild.chunk()
        self.members: dict[int, FakeMember] = {}
        self.channels: dict[int, FakeChannel] = {}
        self.roles: list = []
        self.default_role = types.SimpleNamespace(name="@everyone", id=GUILD_ID)
        self.me = FakeMember(999, "bot", self, bot=True)
        self.chunked = True  # the member cache is always complete here

    def get_member(self, uid):
        return self.members.get(int(uid))

    async def fetch_member(self, uid):
        m = self.members.get(int(uid))
        if m is None:
            raise discord.NotFound(types.SimpleNamespace(status=404, reason="nf"), "unknown member")
        return m

    async def chunk(self, **kwargs):
        return list(self.members.values())

    def get_channel(self, cid):
        return self.channels.get(cid)

    @property
    def text_channels(self):
        return list(self.channels.values())


class FakeBot:
    """Stands in for commands.Bot: lookups only, no gateway."""

    def __init__(self, guild: FakeGuild):
        self.guild = guild
        self.cogs: dict[str, Any] = {}
        self.user = guild.me
        self.views: list = []

    def get_guild(self, gid):
        return self.guild if gid == self.guild.id else None

    def get_channel(self, cid):
        return self.guild.channels.get(cid)

    def get_user(self, uid):
        return self.guild.members.get(int(uid))

    async def fetch_user(self, uid):
        return await self.guild.fetch_member(uid)

    def get_cog(self, name):
        return self.cogs.get(name)

    def add_view(self, view, **kwargs):
        self.views.append(view)


# --------------------------------------------------------------------------- interactions

class _Response:
    def __init__(self, ix: "FakeInteraction"):
        self._ix = ix
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self._ix.replies.append(FakeMessage(self._ix.channel, content, **kwargs))

    async def defer(self, **kwargs):
        self._done = True

    async def edit_message(self, **kwargs):
        self._done = True
        if self._ix.message is not None:
            await self._ix.message.edit(**kwargs)
        self._ix.replies.append(FakeMessage(self._ix.channel, kwargs.get("content"), **{
            k: v for k, v in kwargs.items() if k != "content"}))

    async def send_modal(self, modal):
        self._done = True
        self._ix.modal = modal


class _Followup:
    def __init__(self, ix: "FakeInteraction"):
        self._ix = ix

    async def send(self, content=None, **kwargs):
        msg = FakeMessage(self._ix.channel, content, **kwargs)
        self._ix.replies.append(msg)
        return msg


class FakeInteraction:
    """One slash-command invocation or button click. `guild=None` models a DM."""

    def __init__(self, bot: FakeBot, user: FakeMember, channel=None, message=None, in_dm: bool = False):
        self.client = bot
        self.user = user
        self.guild = None if in_dm else bot.guild
        self.guild_id = None if in_dm else bot.guild.id
        self.channel = channel
        self.message = message
        self.replies: list[FakeMessage] = []
        self.modal = None
        self.response = _Response(self)
        self.followup = _Followup(self)

    async def edit_original_response(self, **kwargs):
        self.replies.append(FakeMessage(self.channel, kwargs.get("content"), **{
            k: v for k, v in kwargs.items() if k != "content"}))

    @property
    def text(self) -> str:
        """Everything the invoking user was shown, flattened."""
        return "\n".join(m.text for m in self.replies)


# --------------------------------------------------------------------------- /report inputs

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # passes the bot's image sniffing; never decoded


class FakeAttachment:
    """A file uploaded with a slash command (e.g. /report's game screenshots)."""

    def __init__(self, filename: str = "game.png", data: bytes = PNG_BYTES,
                 content_type: Optional[str] = "image/png", size: Optional[int] = None):
        self.id = next(_ids)
        self.filename = filename
        self.content_type = content_type
        self.size = len(data) if size is None else size
        self.url = f"https://cdn.invalid/{self.id}/{filename}"
        self._data = data
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self._data


def make_board(team1: list, team2: list, winner: Optional[int] = None, **fields) -> ParsedScoreboard:
    """What the vision model would return for one game screenshot.

    `team1`/`team2` are the rows of the TOP and BOTTOM screenshot teams in DISPLAY
    order. Each row is a name, or (name, champion), or (name, champion, (k, d, a)).
    `winner` is the screenshot team (1/2) that won, if the read could tell.
    Extra ParsedScoreboard fields (notes=..., banner=...) pass through.
    """
    def row(r) -> ScoreRow:
        r = (r,) if isinstance(r, str) else tuple(r)
        name = r[0]
        champ = r[1] if len(r) > 1 else None
        k, d, a = r[2] if len(r) > 2 else (None, None, None)
        return ScoreRow(raw=name, name_guess=name, champion=champ, kills=k, deaths=d, assists=a)

    return ParsedScoreboard(team1=[row(r) for r in team1], team2=[row(r) for r in team2],
                            winner=winner, **fields)


def failed_board(note: str = "Vision read failed (simulated). Enter the lineup manually.") -> ParsedScoreboard:
    """What parse_scoreboard_image returns when the read fails: no rows, one note."""
    return ParsedScoreboard(notes=[note])


def find_item(view, label: Optional[str] = None, cls: Optional[type] = None):
    """A component on a view, by (part of) its label and/or its class."""
    for item in view.children:
        if cls is not None and not isinstance(item, cls):
            continue
        if label is not None and label.lower() not in (getattr(item, "label", None) or "").lower():
            continue
        return item
    raise AssertionError(
        f"no component matching label={label!r} cls={cls} among "
        f"{[(type(c).__name__, getattr(c, 'label', None)) for c in view.children]}")


# --------------------------------------------------------------------------- external APIs

class FakeRiot:
    """Riot API stand-in. `ranks[puuid] = (tier, division, lp)`; missing = unranked.

    Failure injection: `fail` raises on every call; `fail_puuids[puuid]` raises on
    calls about that one account; `before_call` is an async callable awaited once
    at the start of the next call (to simulate something happening concurrently
    while the bot is waiting on Riot).
    """

    def __init__(self):
        self.ranks: dict[str, tuple[str, str, int]] = {}
        self.accounts: dict[str, str] = {}  # "name#tag" (lowercased) -> puuid
        self.names: dict[str, tuple[str, str]] = {}  # puuid -> current (game_name, tag_line); set to simulate a rename
        self.fail: Optional[Exception] = None
        self.fail_puuids: dict[str, Exception] = {}
        self.before_call: Optional[Callable] = None
        self.calls = 0

    async def _maybe_fail(self, puuid=None):
        self.calls += 1
        hook, self.before_call = self.before_call, None
        if hook is not None:
            await hook()
        if self.fail is not None:
            raise self.fail
        if puuid in self.fail_puuids:
            raise self.fail_puuids[puuid]

    async def get_account_by_riot_id(self, game_name, tag_line):
        await self._maybe_fail()
        from bot.services.riot_client import RiotAccount
        puuid = self.accounts.get(f"{game_name}#{tag_line}".lower())
        if puuid is None:
            return None
        return RiotAccount(puuid=puuid, game_name=game_name, tag_line=tag_line)

    async def get_account_by_puuid(self, puuid):
        await self._maybe_fail(puuid)
        from bot.services.riot_client import RiotAccount
        name = self.names.get(puuid)
        if name is None:
            return None
        return RiotAccount(puuid=puuid, game_name=name[0], tag_line=name[1])

    async def get_solo_rank(self, puuid):
        await self._maybe_fail(puuid)
        r = self.ranks.get(puuid)
        if r is None:
            return None
        return types.SimpleNamespace(queue_type="RANKED_SOLO_5x5", tier=r[0], rank=r[1], league_points=r[2])

    async def infer_primary_role(self, puuid, sample: int = 20):
        await self._maybe_fail(puuid)
        return None

    async def close(self):
        return None


class FakeOpgg:
    def __init__(self):
        self.past: dict[str, Any] = {}  # "name#tag" (lowercased) -> PastSeasonRank-like
        self.lookups: list[str] = []    # every "name#tag" asked about, in order

    async def get_past_season_rank(self, game_name, tag_line, region):
        self.lookups.append(f"{game_name}#{tag_line}")
        return self.past.get(f"{game_name}#{tag_line}".lower())

    async def close(self):
        return None


# --------------------------------------------------------------------------- the world

class World:
    def __init__(self, tmp_path, timezone: str = "America/New_York"):
        self.config = make_config(str(tmp_path / "world.db"), timezone)
        self.guild = FakeGuild()
        self.bot = FakeBot(self.guild)
        self.riot = FakeRiot()
        self.opgg = FakeOpgg()
        self.recruit_channel = self.add_channel("wrangling")
        self.other_channel = self.add_channel("general")
        self.owner = self.add_member(OWNER_ID, "owner", roles=(ADMIN_ROLE,))
        self.admin = self.add_member(2, "admin", roles=(ADMIN_ROLE,))
        self.rando = self.add_member(3, "rando")

    # ---- lifecycle
    async def start(self):
        from bot.cogs.admin import AdminCog
        from bot.cogs.linking import LinkingCog
        from bot.cogs.recruitment import RecruitmentCog
        from bot.cogs.stats import StatsCog

        await init_db(self.config)
        self.recruitment = RecruitmentCog(self.bot, self.config)
        self.admin_cog = AdminCog(self.bot, self.config, self.riot, self.opgg)
        self.linking = LinkingCog(self.bot, self.config, self.riot, self.opgg)
        self.stats = StatsCog(self.bot)
        for cog in (self.recruitment, self.admin_cog, self.linking, self.stats):
            self.bot.cogs[type(cog).__name__] = cog
        async with get_session() as db:
            db.add(GuildConfig(guild_id=GUILD_ID, recruit_channel_id=self.recruit_channel.id))
            await db.commit()
        return self

    async def stop(self):
        await close_db()

    # ---- building blocks
    def add_channel(self, name: str) -> FakeChannel:
        ch = FakeChannel(name, self.guild)
        self.guild.channels[ch.id] = ch
        return ch

    def add_member(self, id: int, name: str, roles: tuple[str, ...] = ()) -> FakeMember:
        m = FakeMember(id, name, self.guild, roles)
        self.guild.members[id] = m
        return m

    async def add_players(self, n: int, elo: int | list[int] = 1500, start_id: int = 101,
                          games: int = 0) -> list[FakeMember]:
        """Create n linked, approved players (and guild members) with seeded ratings."""
        elos = elo if isinstance(elo, list) else [elo] * n
        members = []
        async with get_session() as db:
            for i in range(n):
                pid = start_id + i
                members.append(self.add_member(pid, f"P{pid}"))
                db.add(Player(discord_id=pid, riot_game_name=f"P{pid}", riot_tag_line="NA1",
                              riot_puuid=f"puuid{pid}", link_status="approved"))
                for role in [*ROLES, INHOUSE_ROLE]:
                    db.add(Rating(discord_id=pid, role=role, elo=elos[i], base_seed=elos[i],
                                  inhouse_modifier=0, games_played=games))
            await db.commit()
        return members

    def ix(self, user: Optional[FakeMember] = None, channel=None, message=None, in_dm: bool = False):
        return FakeInteraction(self.bot, user or self.admin,
                               channel if channel is not None else (None if in_dm else self.recruit_channel),
                               message, in_dm)

    async def run(self, command, user: Optional[FakeMember] = None, channel=None, **kwargs) -> FakeInteraction:
        """Invoke a slash command (an app_commands.Command on a cog) as `user`."""
        ix = self.ix(user, channel)
        await command.callback(command.binding, ix, **kwargs)
        return ix

    # ---- the weekly flow, via the real handlers
    async def recruit(self, game_date: date, **kwargs) -> InhouseSession:
        ix = await self.run(self.recruitment.recruit_now, game_date=game_date.isoformat(), **kwargs)
        async with get_session() as db:
            s = (await db.execute(
                select(InhouseSession).where(InhouseSession.game_date == game_date)
            )).scalar_one_or_none()
        assert s is not None, f"recruit-now did not create a session: {ix.text}"
        return s

    async def recruit_message(self, session_id: int) -> FakeMessage:
        async with get_session() as db:
            s = await db.get(InhouseSession, session_id)
        return await self.bot.get_channel(s.recruit_channel_id).fetch_message(s.recruit_msg_id)

    async def signup(self, session_id: int, member: FakeMember, roles=("FILL",)) -> FakeInteraction:
        """Click 🎮 Playing, toggle roles, click Done — the real button path."""
        from bot.cogs.recruitment import DoneButton, RoleToggleButton, RsvpView

        msg = await self.recruit_message(session_id)
        ix = self.ix(member, message=msg)
        await RsvpView().playing.callback(ix)
        picker = ix.replies[-1].view
        if picker is None:  # signups closed / not active: caller inspects ix.text
            return ix
        for role in roles:
            if role not in picker.selected:
                btn = next(c for c in picker.children if isinstance(c, RoleToggleButton) and c.role == role)
                await btn.callback(self.ix(member, message=ix.replies[-1]))
        done = next(c for c in picker.children if isinstance(c, DoneButton))
        done_ix = self.ix(member, message=ix.replies[-1])
        await done.callback(done_ix)
        return done_ix

    async def rsvp(self, session_id: int, member: FakeMember, button: str) -> FakeInteraction:
        """Click ❌ Not Playing ('not_playing') or 📺 Commentator ('commentator')."""
        from bot.cogs.recruitment import RsvpView

        ix = self.ix(member, message=await self.recruit_message(session_id))
        await getattr(RsvpView(), button).callback(ix)
        return ix

    async def close(self, session_id: int):
        return await self.recruitment.close_signups_and_match(session_id)

    def owner_options_message(self) -> Optional[FakeMessage]:
        """The latest DM to the owner that carries the pick-an-option buttons."""
        for m in reversed(self.owner.dm.sent):
            if m.view is not None:
                return m
        return None

    # ---- account linking, via the real handlers
    def link_request_message(self, member: Optional[FakeMember] = None) -> Optional[FakeMessage]:
        """The latest link-approval DM to the owner (optionally: about `member`)."""
        for m in reversed(self.owner.dm.sent):
            if m.embeds and "Link Request" in (m.embeds[0].title or ""):
                if member is None or f"Discord ID: {member.id} " in m.text:
                    return m
        return None

    async def approve_link(self, message: Optional[FakeMessage] = None,
                           user: Optional[FakeMember] = None) -> FakeInteraction:
        """Owner clicks Approve on a link-request DM."""
        msg = message or self.link_request_message()
        assert msg is not None, "no link request DM was sent to the owner"
        ix = self.ix(user or self.owner, channel=self.owner.dm, message=msg, in_dm=True)
        await msg.view.approve.callback(ix)
        return ix

    async def reject_link(self, message: Optional[FakeMessage] = None, reason: str = "no",
                          user: Optional[FakeMember] = None) -> FakeInteraction:
        """Owner clicks Reject on a link-request DM and submits the reason modal."""
        msg = message or self.link_request_message()
        assert msg is not None, "no link request DM was sent to the owner"
        ix = self.ix(user or self.owner, channel=self.owner.dm, message=msg, in_dm=True)
        await msg.view.reject.callback(ix)
        if ix.modal is None:  # refused before the modal (e.g. not the owner)
            return ix
        ix.modal.reason._value = reason
        submit = self.ix(user or self.owner, channel=self.owner.dm, message=msg, in_dm=True)
        await ix.modal.on_submit(submit)
        return submit

    async def player(self, discord_id: int) -> Optional[Player]:
        async with get_session() as db:
            return await db.get(Player, discord_id)

    async def pick(self, index: int, message: Optional[FakeMessage] = None) -> FakeInteraction:
        """Owner clicks a Pick button (0 = A) in their DM (guild is None there)."""
        from bot.cogs.recruitment import _handle_proposal_pick

        msg = message or self.owner_options_message()
        assert msg is not None, "no options DM was sent to the owner"
        ix = self.ix(self.owner, channel=self.owner.dm, message=msg, in_dm=True)
        await _handle_proposal_pick(ix, index)
        return ix

    # ---- reporting results, via the real /report command and its confirm view
    async def add_match(self, team1, team2, session_id: Optional[int] = None,
                        predicted_balance: Optional[float] = None) -> int:
        """Create an unreported match. Teams are {role: id} dicts, or 5 ids/members
        in role order (TOP, JUNGLE, MID, BOT, SUPPORT)."""
        def roster(t) -> dict[str, int]:
            if isinstance(t, dict):
                return {r: int(getattr(v, "id", v)) for r, v in t.items()}
            return {r: int(getattr(v, "id", v)) for r, v in zip(ROLES, t)}

        async with get_session() as db:
            m = Match(session_id=session_id, team1_json=json.dumps(roster(team1)),
                      team2_json=json.dumps(roster(team2)), predicted_balance=predicted_balance)
            db.add(m)
            await db.commit()
            await db.refresh(m)
            return m.id

    async def report(self, match_id: int, boards: list, user: Optional[FakeMember] = None,
                     attachments: Optional[list] = None) -> FakeInteraction:
        """Run /report with one screenshot per entry of `boards`. No vision call is
        made: each screenshot "reads" as the matching ParsedScoreboard (see
        make_board / failed_board). The confirm view is `world.report_view(ix)`."""
        import bot.cogs.admin as admin_module

        # Distinct bytes per screenshot: the reads run concurrently, so each board is
        # looked up by the image it belongs to rather than by call order. (Pass
        # `attachments` with distinct data too if you supply your own.)
        atts = attachments or [FakeAttachment(f"game{i + 1}.png", PNG_BYTES + bytes([i]))
                               for i in range(len(boards))]
        by_image = {a._data: b for a, b in zip(atts, boards)}
        real = admin_module.parse_scoreboard_image
        admin_module.parse_scoreboard_image = lambda image_bytes: by_image[image_bytes]
        try:
            kwargs = {f"game{i + 1}": a for i, a in enumerate(atts)}
            return await self.run(self.admin_cog.report, user=user, match_id=match_id, **kwargs)
        finally:
            admin_module.parse_scoreboard_image = real

    @staticmethod
    def report_view(ix: FakeInteraction):
        """The ReportConfirmView attached to /report's confirm message."""
        view = ix.replies[-1].view
        assert view is not None, f"/report sent no confirm view: {ix.text}"
        return view

    async def click(self, item, user: Optional[FakeMember] = None, values: Optional[list] = None,
                    message: Optional[FakeMessage] = None) -> FakeInteraction:
        """Click a button, or choose `values` in a select, on a live view. Runs the
        view's interaction_check first, like Discord does."""
        view = item.view
        ix = self.ix(user, message=message if message is not None else getattr(view, "message", None))
        if values is not None:
            item._values = list(values)
        if await view.interaction_check(ix):
            await item.callback(ix)
        return ix

    async def set_winners(self, view, winners: list[int], user: Optional[FakeMember] = None) -> None:
        """Press each game's winner button until the view shows `winners` (1/2 per game)."""
        from bot.cogs.admin import _WinnerButton

        for gi, want in enumerate(winners):
            btn = next(c for c in view.children if isinstance(c, _WinnerButton) and c.gi == gi)
            for _ in range(2):
                if view.state.winners[gi] == want:
                    break
                await self.click(btn, user)
            assert view.state.winners[gi] == want

    async def game_stats(self, match_id: Optional[int] = None) -> list[GameStat]:
        async with get_session() as db:
            q = select(GameStat).order_by(GameStat.match_id, GameStat.game_no, GameStat.discord_id)
            if match_id is not None:
                q = q.where(GameStat.match_id == match_id)
            return list((await db.execute(q)).scalars().all())

    async def aliases(self) -> dict[str, int]:
        """alias_norm -> discord_id for every stored alias."""
        async with get_session() as db:
            return {a.alias_norm: a.discord_id for a in (await db.execute(select(Alias))).scalars().all()}

    async def match(self, match_id: int) -> Match:
        async with get_session() as db:
            return await db.get(Match, match_id)

    # ---- reading state
    async def session(self, session_id: int) -> InhouseSession:
        async with get_session() as db:
            return await db.get(InhouseSession, session_id)

    async def matches(self, session_id: Optional[int] = None) -> list[Match]:
        async with get_session() as db:
            q = select(Match).order_by(Match.id)
            if session_id is not None:
                q = q.where(Match.session_id == session_id)
            return list((await db.execute(q)).scalars().all())

    async def signups(self, session_id: int) -> list[Signup]:
        async with get_session() as db:
            return list((await db.execute(
                select(Signup).where(Signup.session_id == session_id).order_by(Signup.signed_up_at)
            )).scalars().all())

    async def proposal_sets(self, session_id: int) -> list[ProposalSet]:
        async with get_session() as db:
            return list((await db.execute(
                select(ProposalSet).where(ProposalSet.session_id == session_id).order_by(ProposalSet.id)
            )).scalars().all())

    async def ratings(self) -> dict[tuple[int, str], tuple[int, int, int, int]]:
        """(discord_id, role) -> (elo, base_seed, inhouse_modifier, games_played)."""
        async with get_session() as db:
            rows = (await db.execute(select(Rating))).scalars().all()
            return {(r.discord_id, r.role): (r.elo, r.base_seed, r.inhouse_modifier, r.games_played)
                    for r in rows}

    async def set_session(self, session_id: int, **fields):
        """Directly edit a session row (e.g. move its close time into the past)."""
        async with get_session() as db:
            s = await db.get(InhouseSession, session_id)
            for k, v in fields.items():
                setattr(s, k, v)
            await db.commit()


def assert_ratings_consistent(ratings: dict) -> None:
    """The invariant every rating write must keep: elo == base_seed + inhouse_modifier."""
    bad = {k: v for k, v in ratings.items() if v[0] != v[1] + v[2] or v[3] < 0}
    assert not bad, f"inconsistent ratings: {bad}"


def flow(fn: Callable) -> Callable:
    """Decorator: write `async def test_x(world)`; pytest sees `test_x(tmp_path)`."""

    def wrapper(tmp_path):
        async def body():
            world = await World(tmp_path).start()
            try:
                await fn(world)
            finally:
                await world.stop()
        asyncio.run(body())

    functools.update_wrapper(wrapper, fn, assigned=("__module__", "__name__", "__qualname__", "__doc__"), updated=())
    del wrapper.__wrapped__  # otherwise pytest reads fn's signature and looks for a `world` fixture
    return wrapper
