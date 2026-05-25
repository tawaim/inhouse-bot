# Inhouse League Discord Bot

A Discord bot for managing weekly 5v5 League of Legends inhouse leagues. Automates recruitment scheduling, balanced matchmaking using chess-style Elo, private role preferences via buttons, and per-game stat tracking. Designed for small private friend groups.

## What it does

- **Scheduled recruitment** — Every Friday at 9:00 AM ET, posts a recruitment message for the Thursday 13 days out. Two recruitments stay live at any time (this week's and next week's).
- **Button-based signup** — Players click 🎮 Playing, ❌ Not Playing, or 📺 Commentator. Clicking Playing opens a private (ephemeral) role picker where they toggle TOP / JUNGLE / MID / BOT / SUPPORT / FILL. Role choices stay private; only the public counts are visible.
- **Top-3 balanced matchmaking** — On Monday at 9:30 PM ET, signups close. The matchmaker enumerates every legal team split with role assignments, scores each by skill balance and role-preference penalty, and DMs the bot owner the top 3 most-balanced options that differ by at least 2 player swaps. The owner picks one with a button click; the chosen teams get posted publicly.
- **Chess-style Elo** — Each player has 6 ratings: one per role plus an "Inhouse" rating that updates every game. New players are seeded from solo queue rank (Iron 800 → Challenger 2500). K-factor 40 for the first 10 games, 20 thereafter. Beating someone higher-rated gains more; losing to someone lower-rated costs more.
- **Riot account linking with admin approval** — Players run `/link Faker#KR1`. The bot validates the Riot ID, then DMs the bot owner with Approve/Reject buttons. Once approved, the player's solo queue rank is fetched and their Elo is seeded. Unranked players are seeded from their most recent past-season rank (sourced from OP.GG, since Riot's API has no rank history), decayed ~100 elo per season elapsed.
- **Manual result reporting** — Custom games don't appear in Riot's match history, so admins report outcomes via `/report-manual` (winner-only) or `/report` (with screenshot OCR for KDA enrichment, admin-confirmed before commit).
- **Manual team override** — `/manual-match` lets the admin paste in a custom roster and bypass the matchmaker entirely.

## Tech stack

- Python 3.11
- discord.py 2.4 (slash commands, buttons, modals, persistent views)
- APScheduler (cron-style jobs for the Friday post and Monday close)
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
├── services/
│   ├── riot_client.py   # async wrapper for account-v1, league-v4, match-v5
│   ├── elo.py           # chess Elo math (expected score, update, K-factor, rank seeding)
│   ├── matchmaking.py   # top-N team optimizer with diversity enforcement
│   ├── ocr.py           # Tesseract-based screenshot parser
│   └── scheduler.py     # APScheduler jobs (Friday recruit, Monday close)
└── cogs/
    ├── linking.py       # /link, /link-user, /unlink, /profile, /admin-list-players + approval DMs
    ├── recruitment.py   # button views (RSVP, role picker, proposal choice), session lifecycle
    ├── admin.py         # /set-channel, /report, /report-manual, /sync-ranks, /manual-match, /test-* helpers
    └── stats.py         # /leaderboard
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
- `/leaderboard [role]` — top players by Elo

**Admin**
- `/recruit-now <yyyy-mm-dd>` — manually post a recruitment in the current channel
- `/match-preview` — peek at what teams the matchmaker would generate
- `/manual-match` — opens a modal for pasting in a custom roster
- `/report <match_id> <winner> <screenshot>` — report results with OCR
- `/report-manual <match_id> <winner>` — report results without OCR
- `/sync-ranks` — refresh Riot rank data for all linked players (re-seeds Elo for unplayed roles only)
- `/admin-list-players [filter]` — list all linked players with their Elo and games played
- `/link-user @member <riot_id>` — admin-link someone, no approval flow
- `/set-channel <recruit|results> <channel>` — set the channel the scheduled Friday job posts to

**Owner-only DM interactions** (you, set by `OWNER_DISCORD_ID`)
- Approve/Reject buttons on incoming `/link` requests
- Pick A / B / C buttons when the matchmaker DMs you 3 options on Monday night

## Deployment (Fly.io)

The repo is set up for Fly.io with a Dockerfile (includes Tesseract for OCR) and `fly.toml` configured for Atlanta region with a persistent volume for SQLite.

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
- **OCR is best-effort.** Tesseract on League scoreboards has high variance — different resolutions, overlays, and font rendering all affect accuracy. The screenshot parser always presents results to the admin for confirmation before committing; KDAs that don't extract cleanly are simply left blank rather than guessed.
- **Designed for small leagues.** The architecture (single SQLite file, single bot process) suits 10-30 active players comfortably. Scaling beyond that would require Postgres, sharded hosting, etc.
- **Per-role and Inhouse Elo update independently.** Each match updates one role rating (the role you played) and your Inhouse rating. The other 4 role ratings sit untouched until you play those roles.

## Tests

```bash
pip install pytest
python -m pytest tests/
```

36 tests covering chess Elo math (expected score, K-factor transitions, upset gains), matchmaking (balance, role assignment, diversity enforcement, edge cases), and the manual-match parser (mention extraction, role aliases, validation errors).
