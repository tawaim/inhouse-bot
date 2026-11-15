"""Rank seeding: re-derive players' base_seed from their current Riot solo rank
(or, when unranked, their OP.GG past-season rank). The network half (fetch) and
the database half (apply) are separate so no database lock is held across a
network call. Used by /sync-ranks, /reseed-all, /refresh-seed, the recruitment
close and the weekly scheduler job."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import discord
from sqlalchemy import select

from bot.config import ROLES
from bot.db.models import Player, Rating
from bot.db.session import get_session
from bot.services.elo import INHOUSE_ROLE, seed_from_past_season, seed_from_rank
from bot.services.riot_client import RiotAuthError, RiotUnavailable

log = logging.getLogger(__name__)


def format_sync_report(result: dict, headline: str) -> list[str]:
    """Render a refresh_all_base_seeds result as Discord messages: the counts,
    then who changed (old seed → new seed, and why), who was skipped for a
    manual seed, and who errored. Each message stays under Discord's
    2000-character limit; at most 4 are produced (the rest is summarised).
    """
    esc = discord.utils.escape_markdown
    lines = [headline]
    notable = [c for c in result.get("changes", []) if c["notable"]]
    if notable:
        lines.append("**Changes**")
        for c in notable:
            if c["new"] is not None and c["new"] != c["old"]:
                old = c["old"] if c["old"] is not None else "none"
                line = f"• {esc(c['name'])}: {old} → {c['new']} — {c['reason']}"
            else:
                line = f"• {esc(c['name'])}: seed kept at {c['old']} — {c['reason']}"
            if c.get("renamed_from"):
                line += f" · Riot ID was {esc(c['renamed_from'])}"
            lines.append(line)
    else:
        lines.append("No seed changes.")
    for title, key in (("Skipped (manual seed)", "skipped_names"),
                       ("Errors (seed left unchanged)", "error_names")):
        names = result.get(key) or []
        if names:
            lines.append(f"**{title}**")
            lines.extend(f"• {esc(n)}" for n in names)

    limit, max_messages = 1900, 4
    chunks: list[str] = []
    current = ""
    for i, line in enumerate(lines):
        line = line if len(line) <= limit else line[: limit - 1] + "…"
        if current and len(current) + 1 + len(line) > limit:
            if len(chunks) == max_messages - 1:
                current += f"\n…and {len(lines) - i} more line(s) — see the bot log."
                break
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    chunks.append(current)
    return chunks


async def fetch_seed_data(
    riot, opgg, puuid: str, game_name: Optional[str], tag_line: Optional[str], region: str
) -> dict:
    """Network half of a seed refresh: current Riot ID, current solo rank and
    (only if unranked) OP.GG past-season rank. Touches no database session.

    Raises RiotAuthError / RiotUnavailable; a None rank therefore really
    means "unranked", never "Riot didn't answer".
    """
    # Riot IDs can be renamed; the PUUID can't. Refresh the name first so
    # OP.GG is asked about the right account (and not a stranger who took
    # the old name).
    account = await riot.get_account_by_puuid(puuid)
    if account is not None:
        game_name, tag_line = account.game_name, account.tag_line
    rank = await riot.get_solo_rank(puuid)
    past = None
    if rank is None:
        past = await opgg.get_past_season_rank(game_name, tag_line, region)
    return {"game_name": game_name, "tag_line": tag_line, "rank": rank, "past": past}


async def apply_seed_data(db, player: Player, data: dict) -> dict:
    """Database half of a seed refresh: write fetched rank data onto the
    player and their rating rows. No network. The caller commits.

    Returns a change record: name, discord_id, old (seed before), new (seed
    written, or None when the existing seed was kept), reason, rows (rating
    rows written), renamed_from, notable (worth showing an admin).
    """
    rank, past = data["rank"], data["past"]
    renamed_from = None
    if data.get("game_name") and data.get("tag_line") and (
        (data["game_name"], data["tag_line"]) != (player.riot_game_name, player.riot_tag_line)
    ):
        renamed_from = f"{player.riot_game_name}#{player.riot_tag_line}"
        player.riot_game_name = data["game_name"]
        player.riot_tag_line = data["tag_line"]

    old_rank = f"{player.solo_tier} {player.solo_rank}" if player.solo_tier else None
    ratings = {
        role: await db.get(Rating, (player.discord_id, role))
        for role in [*ROLES, INHOUSE_ROLE]
    }
    existing = [r for r in ratings.values() if r is not None]
    if ratings[INHOUSE_ROLE] is not None:
        old_seed = ratings[INHOUSE_ROLE].base_seed
    else:
        old_seed = existing[0].base_seed if existing else None

    if rank:
        player.solo_tier = rank.tier
        player.solo_rank = rank.rank
        player.solo_lp = rank.league_points
        new_seed = seed_from_rank(rank.tier, rank.rank)
        reason = f"rank change: {old_rank or 'unranked'} → {rank.tier} {rank.rank}"
    else:
        # Really unranked (an outage would have raised): drop the stale rank.
        player.solo_tier = None
        player.solo_rank = None
        player.solo_lp = None
        prefix = "became unranked" if old_rank else "unranked"
        if past and getattr(past, "date_known", True):
            new_seed = seed_from_past_season(past.tier, past.division, past.seasons_elapsed)
            full = seed_from_rank(past.tier, past.division)
            ago = (f"{past.seasons_elapsed} season(s) ago" if past.seasons_elapsed
                   else "this past season")
            if full != new_seed:
                detail = (f"past-season decay: last ranked {past.tier} {past.division} "
                          f"{ago}, {full} − {full - new_seed}")
            else:
                detail = f"past-season rank: last ranked {past.tier} {past.division} {ago}"
            reason = f"became unranked; {detail}" if old_rank else detail
        elif past:
            # A past rank with no placement date: we can't tell how stale it
            # is, so don't re-seed as if it were zero seasons old.
            new_seed = None
            reason = (f"{prefix}; OP.GG returned {past.tier} {past.division} with no "
                      f"placement date, so decay is unknown")
        else:
            new_seed = None
            reason = f"{prefix}; no past-season rank found"

    player.riot_last_synced = datetime.utcnow()
    changed = new_seed is not None and (
        len(existing) < len(ratings) or any(r.base_seed != new_seed for r in existing)
    )
    rows = 0
    if new_seed is not None:
        player.last_synced_seed_elo = new_seed
        for role, r in ratings.items():
            if r is None:
                db.add(Rating(
                    discord_id=player.discord_id,
                    role=role,
                    elo=new_seed,
                    base_seed=new_seed,
                    inhouse_modifier=0,
                    games_played=0,
                ))
            else:
                r.base_seed = new_seed
                r.elo = r.base_seed + r.inhouse_modifier
            rows += 1
    return {
        "name": player.riot_game_name or f"<@{player.discord_id}>",
        "discord_id": player.discord_id,
        "old": old_seed,
        "new": new_seed,
        "reason": reason,
        "rows": rows,
        "renamed_from": renamed_from,
        "notable": bool(changed or renamed_from or (old_rank and not rank)
                        or (past and new_seed is None)),
    }


async def refresh_base_seed_for_player(riot, opgg, db, player: Player) -> int:
    """Re-derive one player's base_seed from current Riot rank (or OP.GG past
    season), updating all their rating rows in place. The caller commits.

    Returns the number of rating rows written; 0 means the existing
    base_seed was kept (no current rank, and no usable past-season rank).
    Sets riot_last_synced on success. Raises RiotAuthError / RiotUnavailable
    without changing anything. Does NOT consult manual_seed — callers decide
    whether a player should be refreshed.
    """
    data = await fetch_seed_data(
        riot, opgg,
        player.riot_puuid, player.riot_game_name, player.riot_tag_line, player.region
    )
    return (await apply_seed_data(db, player, data))["rows"]


async def refresh_all_base_seeds(riot, opgg) -> dict:
    """Refresh base_seed for every linked player from current Riot rank.
    Used by /sync-ranks, /reseed-all and the Monday close job. Players with
    a manual seed (set via /set-seed) are skipped so an admin override isn't
    clobbered.

    All Riot/OP.GG calls happen first with NO database session open; the
    results are then written in one short transaction, so the SQLite write
    lock is never held across a network call. A player whose lookup fails
    (Riot outage, rate limit) keeps their seed and is reported as an error.

    Returns counts (updated, rows_updated, errors, skipped) plus per-player
    detail: changes (see apply_seed_data), skipped_names, error_names.
    RiotAuthError propagates with nothing written.
    """
    def label(game_name, discord_id) -> str:
        return game_name or f"<@{discord_id}>"

    async with get_session() as db:
        players = (await db.execute(
            select(Player).where(
                Player.riot_puuid.is_not(None),
                Player.link_status == "approved",
            )
        )).scalars().all()
        skipped_names = [label(p.riot_game_name, p.discord_id) for p in players if p.manual_seed]
        todo = [
            (p.discord_id, p.riot_puuid, p.riot_game_name, p.riot_tag_line, p.region)
            for p in players if not p.manual_seed
        ]

    fetched: list[tuple[int, str, dict]] = []
    error_names: list[str] = []
    for discord_id, puuid, game_name, tag_line, region in todo:
        try:
            fetched.append((discord_id, puuid,
                            await fetch_seed_data(riot, opgg, puuid, game_name, tag_line, region)))
        except RiotAuthError:
            raise
        except RiotUnavailable as e:
            log.warning("Rank sync: Riot unavailable for %s (%s); seed kept", discord_id, e)
            error_names.append(f"{label(game_name, discord_id)} (Riot unavailable)")
        except Exception:
            log.exception("Failed to sync %s", discord_id)
            error_names.append(f"{label(game_name, discord_id)} (unexpected error)")

    changes: list[dict] = []
    async with get_session() as db:
        for discord_id, puuid, data in fetched:
            player = await db.get(Player, discord_id)
            if player is None or player.riot_puuid != puuid or player.link_status != "approved":
                continue  # unlinked / re-linked while we were fetching
            if player.manual_seed:
                skipped_names.append(label(player.riot_game_name, discord_id))
                continue  # /set-seed ran while we were fetching
            changes.append(await apply_seed_data(db, player, data))
        await db.commit()

    for c in changes:
        if c["notable"]:
            log.info("Rank sync: %s seed %s -> %s (%s)", c["name"], c["old"], c["new"], c["reason"])
    return {
        "updated": len(changes),
        "rows_updated": sum(c["rows"] for c in changes),
        "errors": len(error_names),
        "skipped": len(skipped_names),
        "changes": changes,
        "skipped_names": skipped_names,
        "error_names": error_names,
    }
