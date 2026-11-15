# Claude Notes — Inhouse Bot

## Production database

The bot runs on Fly.io (app `inhouse-league-bot`, region `atl`). State lives in a SQLite file on a Fly volume at `/data/inhouse.db`. The local `data/inhouse.db` is empty dev data — not production.

### Pulling the prod DB locally

Run this via the **PowerShell tool** (not Bash — Git Bash mangles the POSIX remote path into `C:/Program Files/Git/data/...` and the transfer fails):

```powershell
~/.fly/bin/fly ssh sftp get /data/inhouse.db ./data/prod_inhouse.db -a inhouse-league-bot
```

If the file already exists locally, remove it first:

```powershell
Remove-Item ./data/prod_inhouse.db -Force
~/.fly/bin/fly ssh sftp get /data/inhouse.db ./data/prod_inhouse.db -a inhouse-league-bot
```

The pulled file lands at `./data/prod_inhouse.db` (gitignored).

### Backups

The bot writes a copy of the database to `/data/backups/` every time it boots (before migrations run) and before `/clear-matches` and `/reseed-all`, keeping the newest 14. To restore one, stop relying on memory and list them first:

```powershell
~/.fly/bin/fly ssh console -a inhouse-league-bot -C "ls -la /data/backups"
```

Replacing `/data/inhouse.db` by hand: upload the new file under another name, `mv` it over `inhouse.db`, then restart the machine straight away (`fly machine restart`). The running bot keeps the old file open until it restarts. The database deliberately stays in SQLite's default single-file journal mode (not WAL) so that pulling or replacing `inhouse.db` alone is always complete.

### Schema notes

- Players are identified by `discord_id`. Display names are **not** stored — only `riot_game_name`, `riot_tag_line`, and `discord_id`.
- Ratings are in the `ratings` table, keyed by `discord_id` + `role`. Roles are `TOP`, `JUNGLE`, `MID`, `BOT`, `SUPPORT`, and `INHOUSE`.
- Mapping a Discord nickname (e.g. "Carter") to a player requires asking the user — there is no display-name column.

### Useful queries

```python
import sqlite3
conn = sqlite3.connect('./data/prod_inhouse.db')
cur = conn.cursor()

# All players
cur.execute('SELECT riot_game_name, discord_id FROM players')

# Elo for a player at a specific role
cur.execute('''
    SELECT r.elo, r.games_played
    FROM players p JOIN ratings r ON r.discord_id = p.discord_id
    WHERE p.riot_game_name = ? AND r.role = ?
''', ('Zack Fox', 'MID'))
```

## Testing

Run `./check.ps1` (or `python tools/check.py`) before every push; pushing to `main` deploys to production. It compiles everything, builds all slash commands, and runs the test suite, including end-to-end flow tests on a fake Discord (`tests/harness.py`), a golden Elo season, and checks against `data/prod_inhouse.db` when that snapshot exists. A pre-push hook in `.githooks/` runs the same check.

- Every bug fix gets a flow test that fails without the fix.
- If Elo output changes on purpose, run `./check.ps1 --update-golden` and review the diff in `tests/golden/`.
- Pull a fresh prod snapshot before changing anything that touches the schema, migrations or rating writes.
