# Inhouse League Discord Bot

A Discord bot for managing weekly 5v5 League of Legends inhouse leagues. Automates recruitment scheduling, balanced matchmaking using chess-style Elo, private role preferences via buttons, and per-game stat tracking. Designed for small private friend groups.

## What it does

- **Recruitment posts** — An admin posts a recruitment for a Thursday with `/recruit-now`. Signups close automatically at 9:30 PM ET on the Monday, Tuesday or Wednesday before the game (your choice), unless posted open-ended. With fewer than 10 signups they stay open and teams are made as soon as there are 10.
- **Button-based signup** — Players click 🎮 Playing, ❌ Not Playing, or 📺 Commentator. Clicking Playing opens a private (ephemeral) role picker where they toggle TOP / JUNGLE / MID / BOT / SUPPORT / FILL. Role choices stay private; only the public counts are visible.
- **Top-10 balanced matchmaking** — On Monday at 9:30 PM ET, signups close. The matchmaker enumerates every legal team split with role assignments, scores each by skill balance and role-preference penalty, and DMs the bot owner the top 10 most-balanced options that differ by at least 2 player swaps. The owner picks one with a button click; the chosen teams get posted publicly.
- **Chess-style Elo** — Each player has 6 ratings: one per role plus an "Inhouse" rating that updates every game. New players are seeded from solo queue rank (Iron 800 → Challenger 2500). K-factor 60 for the first 10 games, 50 thereafter. Beating someone higher-rated gains more; losing to someone lower-rated costs more.
- **Riot account linking with admin approval** — Players run `/link Faker#KR1`. The bot validates the Riot ID, then DMs the bot owner with Approve/Reject buttons. Once approved, the player's solo queue rank is fetched and their Elo is seeded. Unranked players are seeded from their most recent past-season rank (sourced from OP.GG, since Riot's API has no rank history), decayed ~100 elo per season elapsed.
- **Manual result reporting** — Custom games don't appear in Riot's match history, so admins report outcomes via `/report-manual` (winner-only) or `/report` (with screenshot OCR for KDA enrichment, admin-confirmed before commit).
- **Manual team override** — `/manual-match` lets the admin paste in a custom roster and bypass the matchmaker entirely.

## Tech stack

- Python 3.11
- discord.py 2.4 (slash commands, buttons, modals, persistent views)
- APScheduler (cron-style job for the Monday close)
- SQLAlchemy 2.0 + SQLite (async, via aiosqlite)
- httpx (async Riot API client with rate-limit handling)
- pytesseract + Pillow (best-effort screenshot OCR for KDA extraction)
- Deployed on Fly.io with a 1GB persistent volume for the SQLite database

## Architecture

```
bot/
├── main.py              # entrypoint, wires up cogs + scheduler
├── config.py            # env var loading, role/emoji constants
├── db/
│   ├── models.py        # SQLAlchemy ORM (players, ratings, sessions, signups, matches, etc.)
│   └── session.py       # async session factory
├── services/            # no Discord UI in here; services never import from cogs
│   ├── riot_client.py   # async wrapper for account-v1, league-v4, match-v5
│   ├── opgg_client.py   # past-season ranks from OP.GG (for seeding unranked players)
│   ├── elo.py           # chess Elo math (expected score, update, K-factor, rank seeding)
│   ├── matchmaking.py   # top-N team optimizer with diversity enforcement
│   ├── ocr.py           # Screenshot reader (Claude vision) + winner rules
│   ├── champions.py     # champion name resolution (aliases, fuzzy match)
│   ├── champion_vision.py  # champion-icon recognition by perceptual hash
│   ├── report_analysis.py  # scoreboard read -> proposed per-game lineups (ReportState, GameResult)
│   ├── rosters.py       # team names; parse/print the TEAM 1 / ROLE: @user roster block
│   ├── names.py         # name/alias resolution: informal or scoreboard name -> linked player
│   ├── results.py       # rating writes: commit a series game by game, revert it, rescore it
│   ├── ratings_lock.py  # the one lock every rating write runs under
│   ├── seeding.py       # rank-seed sync: fetch Riot/OP.GG rank, apply base_seed, report
│   ├── sessions.py      # signup close time, "current session" lookup, who gets matched
│   └── scheduler.py     # APScheduler jobs (signup close poll, weekly rank sync)
└── cogs/
    ├── linking.py       # /link, /link-user, /unlink, /profile, /admin-list-players + approval DMs
    ├── recruitment.py   # /recruit-now, /signups, /match-preview, /close-signups, ... + posting a
    │                    #   recruitment and close-and-match (used by the scheduler)
    ├── admin.py         # every other admin command: /set-channel, aliases, /match-channels, /report,
    │                    #   /report-manual, /unreport, /sync-ranks, /set-seed, /manual-match, /test-*
    ├── stats.py         # /leaderboard, /elo-history, /player-stats, /recent-games
    └── views/           # discord.ui views and modals, one file per flow
        ├── rsvp.py          # recruitment message: RSVP buttons, role picker, signup embed
        ├── proposals.py     # owner's A/B/C team-option DM and committing the pick
        ├── manual_match.py  # roster modals: /manual-match, /pickup-series, /resolve-names
        ├── report.py        # /report confirm view (winners, subs, champions/KDA, commit)
        └── match_channels.py  # /match-channels screenshot confirm
```

## Setup

1. Copy `.env.example` to `.env` and fill in your tokens (see env var table below).
2. `pip install -r requirements.txt`
3. `python -m bot.main`

The bot creates `data/inhouse.db` on first run.

## Environment variables

| Name | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token from the Discord developer portal |
| `DISCORD_GUILD_ID` | yes | Your Discord server ID (for fast slash command sync) |
| `OWNER_DISCORD_ID` | yes | Your personal Discord user ID — receives link approval DMs and team-choice DMs |
| `RIOT_API_KEY` | yes | Personal or production Riot API key (development keys expire every 24 hours and won't work) |
| `ANTHROPIC_API_KEY` | no | Lets `/report` read scoreboard screenshots. Without it the lineup is entered by hand |
| `RIOT_REGION` | no | Default `na1` |
| `RIOT_REGIONAL_ROUTE` | no | Default `americas` (for match-v5 and account-v1) |
| `ADMIN_ROLE_NAME` | no | Default `League Admin` |
| `DATABASE_URL` | no | Default `sqlite+aiosqlite:///./data/inhouse.db` |
| `TZ` | no | Default `America/New_York` |

## Commands

A full reference lives in [`COMMANDS.md`](./COMMANDS.md). Quick summary:

**Public**
- `/link <riot_id>` — submit a link request (admin-approved)
- `/unlink` — remove your Riot link
- `/profile [user]` — show ranks, Inhouse Elo, win/loss stats
- `/leaderboard [role] [sort]` — ranked by series wins (or net games / Elo)

**Admin**
- `/recruit-now <yyyy-mm-dd>` — manually post a recruitment in the current channel
- `/match-preview` — peek at what teams the matchmaker would generate
- `/manual-match` — opens a modal for pasting in a custom roster
- `/report <match_id> <winner> <screenshot>` — report results with OCR
- `/report-manual <match_id> <winner>` — report results without OCR
- `/sync-ranks` — refresh Riot rank data for all linked players. Rewrites `base_seed` on every rating row (all roles, played or not) and recomputes Elo as `base_seed + inhouse_modifier`; inhouse results are untouched. Players with a manual seed (`/set-seed`) are skipped.
- `/admin-list-players [filter]` — list all linked players with their Elo and games played
- `/link-user @member <riot_id>` — admin-link someone, no approval flow
- `/set-channel <recruit|results> <channel>` — set the default channel `/recruit-now` posts to

**Owner-only DM interactions** (you, set by `OWNER_DISCORD_ID`)
- Approve/Reject buttons on incoming `/link` requests
- Pick A to Pick J buttons when the matchmaker DMs you its 10 options on Monday night

## Deployment (Fly.io)

The repo is set up for Fly.io with a Dockerfile (plain Python — no Tesseract; screenshot reading goes through the Anthropic API) and `fly.toml` configured for Atlanta region with a persistent volume for SQLite.

```bash
fly launch --no-deploy
fly volumes create inhouse_data --size 1 --region atl
fly secrets set DISCORD_TOKEN=... RIOT_API_KEY=... DISCORD_GUILD_ID=... OWNER_DISCORD_ID=...
fly deploy
```

`fly.toml` deliberately has **no** `[http_service]` block — this is a worker app (outbound Discord WebSocket only, no inbound HTTP). Including an http_service causes Fly's proxy to auto-stop the machine when there's no inbound traffic, killing the bot.

## Limitations and design notes

- **Custom games are invisible to Riot's API.** Riot's match-v5 endpoint doesn't expose custom-game results, so inhouse outcomes have to be reported manually via `/report` or `/report-manual`. Riot's API is used only for solo queue rank seeding and inferring primary role from recent ranked games.
- **Past-season ranks come from OP.GG, not Riot.** Riot's API has no rank history (LEAGUE-V4 is current-split only; MATCH-V5 carries no tier field), so currently-unranked players are seeded from their most recent past-season Solo/Duo rank via OP.GG's hosted MCP server (`lol_get_summoner_profile`). The response is a token-compressed text format that `bot/services/opgg_client.py` parses with regexes; it's an unofficial endpoint, so the lookup fails soft (falls back to the 1200 default) if OP.GG changes it.
- **Screenshot reading is best-effort.** Screenshots are read by a vision model and always shown to the admin for confirmation before anything is committed. A game with no clear winner is left blank for the admin to set, and champions are recorded only from the lobby screen that prints their names.
- **Designed for small leagues.** The architecture (single SQLite file, single bot process) suits 10-30 active players comfortably. Scaling beyond that would require Postgres, sharded hosting, etc.
- **Per-role and Inhouse Elo update independently.** Each match updates one role rating (the role you played) and your Inhouse rating. The other 4 role ratings sit untouched until you play those roles.

## Tests

One command runs every local regression check. Nothing in it talks to Discord, Riot, Anthropic or Fly.

```powershell
pip install -r requirements.txt -r requirements-dev.txt   # once
./check.ps1                 # or: python tools/check.py
./check.ps1 --fast          # skip the production-snapshot tests
./check.ps1 -k recruit      # only tests matching a keyword
```

It does three things:

1. **Compiles** every module.
2. **Builds every slash command** exactly as Discord would receive it, so a bad command signature fails here instead of at startup.
3. **Runs the test suite** in `tests/`:
   - Unit tests for Elo, matchmaking, parsing and name resolution.
   - **End-to-end flow tests** (`tests/test_flow_*.py`). `tests/harness.py` provides a fake Discord (guild, members, channels, DMs, button clicks) wired to the real cogs and a throwaway SQLite database, so a test can post a recruitment, click RSVP buttons, close signups, pick teams from the owner DM, report a series and undo it.
   - **Golden Elo season** (`tests/test_golden_elo.py`). A fixed synthetic season is replayed and every rating is compared with `tests/golden/elo_season.json`. If you change the Elo maths on purpose, run `./check.ps1 --update-golden` and review `git diff tests/golden`.
   - **Production snapshot** (`tests/test_prod_snapshot.py`). When `data/prod_inhouse.db` exists (see `CLAUDE.md` for how to pull it), the tests copy it to a temp folder and check that the current code boots on the real data without losing anything, that ratings are consistent, and that un-reporting and re-reporting the latest series is lossless. Skipped when there is no snapshot.

The same check runs in two more places:

- **Before every push** via `.githooks/pre-push`. Enable it once per clone with `git config core.hooksPath .githooks`. Pushing to `main` deploys, so a failing check blocks the push (`git push --no-verify` skips it).
- **In GitHub Actions**, as a job the deploy depends on.

### Writing a flow test

```python
from datetime import date
from tests.harness import flow

@flow
async def test_under_ten_signups_stays_open(world):
    players = await world.add_players(9)
    session = await world.recruit(date(2030, 5, 16))
    for p in players:
        await world.signup(session.id, p)
    await world.close(session.id)
    assert (await world.session(session.id)).status == "recruiting"
```

When you fix a bug, add a flow test that fails without the fix.
