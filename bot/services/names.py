"""Name resolution: turn informal names (a pasted signup list, names read off a
scoreboard screenshot) into linked Discord players, via stored aliases, exact
identity matches and a fuzzy fallback. Used by /resolve-names, /set-alias,
/report and /match-channels."""
from __future__ import annotations

import difflib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import discord
from sqlalchemy import select

from bot.db.models import Alias, Player
from bot.db.session import get_session
from bot.services.rosters import _ROLE_LINE_RE


# A line that's just a team header ("TEAM 1", "T2") — passed through untouched.
_TEAM_HEADER_RE = re.compile(r"^(team\s*\d+|t\d+)$", re.IGNORECASE)
# Below this difflib ratio we don't even suggest a fuzzy match.
_FUZZY_CUTOFF = 0.72


def _norm_name(s: str) -> str:
    """The match key: lowercased, trimmed, whitespace-collapsed, leading @ dropped."""
    s = s.strip().lstrip("@").strip()
    s = re.sub(r"\s+", " ", s)
    return s.lower()


def _name_variants(s: str) -> set[str]:
    """Normalized forms accepted as an exact match: the full name and the name
    with any '(...)' suffix stripped — so 'kaari (inhouse arena champ)' still
    matches a pasted 'kaari'."""
    out: set[str] = set()
    full = _norm_name(s)
    if full:
        out.add(full)
    stripped = _norm_name(re.sub(r"\(.*?\)", "", s))
    if stripped:
        out.add(stripped)
    return out


async def _build_candidate_index(
    guild: discord.Guild, db
) -> tuple[dict[str, set[int]], dict[int, str]]:
    """Build {normalized identity string -> {discord_ids}} over all linked+approved
    players (matching on Riot game name + Discord display name / username / nick),
    plus a {discord_id -> display label} map. Only linked players are candidates,
    since they're the only ones valid in /manual-match."""
    rows = (
        await db.execute(
            select(Player).where(
                Player.link_status == "approved", Player.riot_puuid.isnot(None)
            )
        )
    ).scalars().all()

    index: dict[str, set[int]] = defaultdict(set)
    labels: dict[int, str] = {}
    for p in rows:
        member = guild.get_member(p.discord_id)
        strings = [p.riot_game_name]
        if member is not None:
            strings += [member.display_name, member.name, member.global_name, member.nick]
        for s in strings:
            if not s:
                continue
            for v in _name_variants(s):
                index[v].add(p.discord_id)
        labels[p.discord_id] = (
            member.display_name if member is not None else (p.riot_game_name or str(p.discord_id))
        )
    return index, labels


async def resolve_name_block(guild: discord.Guild, raw: str) -> tuple[str, list[str], int]:
    """Rewrite a pasted block of plain names into <@id> mentions.

    Lines shaped like 'ROLE: name' keep the 'ROLE:' prefix; team headers pass
    through; any other line is treated as a bare name. Confident, unambiguous
    exact matches are saved as aliases (so they're instant next time). Returns
    (rewritten_block, notes, learned_alias_count)."""
    async with get_session() as db:
        alias_map = {
            a.alias_norm: a.discord_id
            for a in (await db.execute(select(Alias))).scalars().all()
        }
        index, labels = await _build_candidate_index(guild, db)
        keys = list(index.keys())

        out_lines: list[str] = []
        notes: list[str] = []
        learned = 0

        def label(i: int) -> str:
            return f"{labels.get(i, i)} (<@{i}>)"

        for raw_line in raw.splitlines():
            line = raw_line.strip()
            if not line:
                out_lines.append("")
                continue
            if _TEAM_HEADER_RE.match(line):
                out_lines.append(line)
                continue
            m = _ROLE_LINE_RE.match(line)
            if m:
                prefix, namepart = f"{m.group(1)}: ", m.group(2)
            else:
                prefix, namepart = "", line
            name = namepart.strip()
            n = _norm_name(name)

            # 1. Known alias — exact, trusted.
            if n in alias_map:
                out_lines.append(f"{prefix}<@{alias_map[n]}>")
                continue
            # 2. Exact match on a live identity string.
            if n in index:
                ids = index[n]
                if len(ids) == 1:
                    did = next(iter(ids))
                    out_lines.append(f"{prefix}<@{did}>")
                    db.add(Alias(alias_norm=n, discord_id=did, alias=name))
                    alias_map[n] = did
                    learned += 1
                    continue
                out_lines.append(f"{prefix}⚠️ {name}")
                notes.append(
                    f"❓ **{name}** is ambiguous — matches {', '.join(label(i) for i in ids)}. "
                    f"Pick one with `/set-alias`."
                )
                continue
            # 3. Fuzzy fallback — suggested, never auto-saved.
            cand_ids: list[int] = []
            for k in difflib.get_close_matches(n, keys, n=5, cutoff=_FUZZY_CUTOFF):
                for i in index[k]:
                    if i not in cand_ids:
                        cand_ids.append(i)
            if len(cand_ids) == 1:
                did = cand_ids[0]
                out_lines.append(f"{prefix}<@{did}>  ← guess, verify")
                notes.append(
                    f"🤔 Guessed **{name}** → {label(did)} (not saved). Fix with `/set-alias` if wrong."
                )
                continue
            if len(cand_ids) > 1:
                out_lines.append(f"{prefix}⚠️ {name}")
                notes.append(
                    f"🤔 **{name}** is close to {', '.join(label(i) for i in cand_ids[:5])}. "
                    f"Disambiguate with `/set-alias`."
                )
                continue
            # 4. Nothing.
            out_lines.append(f"{prefix}❌ {name}")
            notes.append(
                f"❌ No match for **{name}** — are they `/link`'d? "
                f"Map them with `/set-alias member:@them alias:{name}`."
            )

        if learned:
            await db.commit()

    return "\n".join(out_lines), notes, learned


# --- Single-name resolution, reused for screenshot player identification ------

@dataclass
class NameMatch:
    """Result of resolving one in-game/scoreboard name to a Discord player.

    `discord_id` is set only for a CONFIDENT match (stored alias or a unique
    exact identity hit). Ambiguous, fuzzy, and no-match cases leave it None and
    surface `candidates` so the caller can offer an ephemeral picker; the chosen
    member is then saved as an alias so the name auto-resolves next time.
    """
    name: str
    discord_id: Optional[int]
    confidence: str  # "alias" | "exact" | "ambiguous" | "fuzzy" | "none"
    candidates: list[int] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        return self.discord_id is not None


def _resolve_single_name(
    name: str,
    alias_map: dict[str, int],
    index: dict[str, set[int]],
    keys: list[str],
) -> NameMatch:
    """Resolve one name against the alias map + candidate index. Same tiers as
    resolve_name_block (alias → exact → fuzzy), but returns structured data
    instead of a rewritten line."""
    n = _norm_name(name)
    if not n:
        return NameMatch(name, None, "none", [])
    if n in alias_map:
        did = alias_map[n]
        return NameMatch(name, did, "alias", [did])
    if n in index:
        ids = list(index[n])
        if len(ids) == 1:
            return NameMatch(name, ids[0], "exact", ids)
        return NameMatch(name, None, "ambiguous", ids)
    cand_ids: list[int] = []
    for k in difflib.get_close_matches(n, keys, n=5, cutoff=_FUZZY_CUTOFF):
        for i in index[k]:
            if i not in cand_ids:
                cand_ids.append(i)
    if len(cand_ids) == 1:
        return NameMatch(name, None, "fuzzy", cand_ids)
    if len(cand_ids) > 1:
        return NameMatch(name, None, "ambiguous", cand_ids)
    return NameMatch(name, None, "none", [])


async def match_player_names(
    guild: discord.Guild, names: list[str]
) -> tuple[list[NameMatch], dict[int, str]]:
    """Resolve a list of scoreboard names to Discord players. Returns the matches
    (in input order) plus a {discord_id -> display label} map for rendering."""
    async with get_session() as db:
        alias_map = {
            a.alias_norm: a.discord_id
            for a in (await db.execute(select(Alias))).scalars().all()
        }
        index, labels = await _build_candidate_index(guild, db)
        keys = list(index.keys())
        matches = [_resolve_single_name(nm, alias_map, index, keys) for nm in names]
    return matches, labels


async def build_resolver(guild: discord.Guild):
    """Build a name->discord_id resolver over the alias index + linked players,
    plus a {discord_id -> display label} map. Only CONFIDENT matches resolve
    (alias or unique exact); fuzzy/ambiguous return None so the slot goes to the
    ephemeral picker."""
    async with get_session() as db:
        alias_map = {a.alias_norm: a.discord_id
                     for a in (await db.execute(select(Alias))).scalars().all()}
        index, labels = await _build_candidate_index(guild, db)
        keys = list(index.keys())

    def resolve(name: str) -> Optional[int]:
        return _resolve_single_name(name, alias_map, index, keys).discord_id

    return resolve, labels


async def learn_alias(name: str, discord_id: int) -> tuple[str, Optional[int]]:
    """Persist a confirmed scoreboard-name -> player mapping so it auto-resolves
    next time. The FULL name as read is stored. An alias that already points at
    someone else is never overwritten here — that is what /set-alias is for.

    Returns (status, other_id): 'learned' | 'exists' (already this player) |
    'conflict' (belongs to other_id; unchanged) | 'skipped' (nothing to store).
    """
    n = _norm_name(name)
    if not n or len(n) > 64:
        return ("skipped", None)
    async with get_session() as db:
        existing = await db.get(Alias, n)
        if existing is not None:
            if existing.discord_id == discord_id:
                return ("exists", None)
            return ("conflict", existing.discord_id)
        db.add(Alias(alias_norm=n, discord_id=discord_id, alias=name.strip()))
        await db.commit()
    return ("learned", None)
