# Inhouse League Discord Bot

A Discord bot for managing a weekly 5v5 League of Legends inhouse league. Handles recruitment, balanced matchmaking, and elo tracking.

## What it does

- **Recruitment scheduling** — Every Friday at 9:00 AM ET, posts a recruitment message for the Thursday-after-next (13 days out). Players react with role emojis to sign up.
- **Auto-matchmaking** — Every Monday at 9:30 PM ET, signups close and the bot generates two balanced 5-player teams using TrueSkill ratings and respecting role preferences.
- **Riot API integration** — Players link their Riot ID with `/link`. The bot pulls solo queue rank, recent match performance, and primary role to seed inhouse elo.
- **Screenshot-based result reporting** — Custom games don't appear in Riot match history, so admins report outcomes via `/report` with an end-of-game screenshot. OCR extracts winner and KDAs; admin confirms before elo updates.
- **Elo tracking** — Per-role TrueSkill ratings update after each game. `/profile` and `/leaderboard` commands show standings.

## Tech stack

- Python 3.11
- discord.py (Discord client)
- APScheduler (cron-style jobs)
- SQLAlchemy + SQLite (persistence)
- httpx (async Riot API client)
- trueskill (rating algorithm)
- pytesseract + Pillow (screenshot OCR)
- Deployed on Fly.io

## Architecture

```
bot/
├── main.py              # entrypoint, sets up client + scheduler
├── config.py            # env var loading
├── db/
│   ├── models.py        # SQLAlchemy ORM models
│   └── session.py       # DB session factory
├── services/
│   ├── riot_client.py   # async Riot API wrapper
│   ├── elo.py           # TrueSkill engine, rank → mu seeding
│   ├── matchmaking.py   # team balancing optimizer
│   ├── ocr.py           # screenshot parsing
│   └── scheduler.py     # APScheduler jobs
└── cogs/
    ├── linking.py       # /link, /unlink, /profile
    ├── recruitment.py   # reaction handling, recruitment messages
    ├── admin.py         # /report, /set-channel, /sync-ranks
    └── stats.py         # /leaderboard
```

## Setup

1. Copy `.env.example` to `.env` and fill in your tokens.
2. `pip install -r requirements.txt`
3. `python -m bot.main`

## Environment variables

| Name | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | yes | Bot token from Discord developer portal |
| `DISCORD_GUILD_ID` | yes | Your Discord server ID (for fast slash command sync) |
| `OWNER_DISCORD_ID` | yes | Your personal Discord user ID — receives link approval DMs |
| `RIOT_API_KEY` | yes | Personal or production Riot API key |
| `RIOT_REGION` | no | Default `na1` |
| `RIOT_REGIONAL_ROUTE` | no | Default `americas` (for match-v5 and account-v1) |
| `ADMIN_ROLE_NAME` | no | Default `League Admin` |
| `DATABASE_URL` | no | Default `sqlite:///./data/inhouse.db` |
| `TZ` | no | Default `America/New_York` |

## Deployment (Fly.io)

See `fly.toml`. Single small VM (256MB) with a 1GB persistent volume mounted at `/data` for SQLite.

```bash
fly launch --no-deploy
fly volumes create inhouse_data --size 1
fly secrets set DISCORD_TOKEN=... RIOT_API_KEY=... DISCORD_GUILD_ID=... OWNER_DISCORD_ID=...
fly deploy
```
