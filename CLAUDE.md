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
