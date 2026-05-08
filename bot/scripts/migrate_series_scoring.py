"""Migration: add team1_wins and team2_wins columns to matches table for series scoring.

Run after deploying:
    python -m bot.scripts.migrate_series_scoring

Idempotent. SQLite doesn't support altering nullability of an existing column,
but since session_id was already NOT NULL and we want to make it nullable, we
can rely on application-layer behavior — SQLite stores all values in nullable
storage anyway. The schema constraint is what actually changes; the column
already accepts NULL at the storage layer.
"""
import sqlite3
from pathlib import Path

from bot.config import Config


def migrate(db_path: str) -> None:
    if db_path.startswith("sqlite+aiosqlite:////"):
        actual_path = db_path.replace("sqlite+aiosqlite:////", "/")
    elif db_path.startswith("sqlite+aiosqlite:///"):
        actual_path = db_path.replace("sqlite+aiosqlite:///", "")
    else:
        raise ValueError(f"Unexpected DATABASE_URL format: {db_path}")

    print(f"Migrating {actual_path}")
    if not Path(actual_path).exists():
        print(f"  Database file doesn't exist yet — skip (will be created fresh).")
        return

    conn = sqlite3.connect(actual_path)
    cur = conn.cursor()

    existing = {row[1] for row in cur.execute("PRAGMA table_info(matches)").fetchall()}
    print(f"  Current matches columns: {existing}")

    if "team1_wins" not in existing:
        cur.execute("ALTER TABLE matches ADD COLUMN team1_wins INTEGER DEFAULT 0")
        # Backfill: any previously-reported matches got team1_wins=1 if team1 won, 0 otherwise.
        # We assume reported matches were single-game (winner=1 or 2) and treat as 1-0 or 0-1.
        cur.execute("UPDATE matches SET team1_wins = 1 WHERE winner = 1")
        cur.execute("UPDATE matches SET team1_wins = 0 WHERE winner = 2")
        print("  Added team1_wins (backfilled from winner)")
    else:
        print("  team1_wins already present, skip")

    if "team2_wins" not in existing:
        cur.execute("ALTER TABLE matches ADD COLUMN team2_wins INTEGER DEFAULT 0")
        cur.execute("UPDATE matches SET team2_wins = 1 WHERE winner = 2")
        cur.execute("UPDATE matches SET team2_wins = 0 WHERE winner = 1")
        print("  Added team2_wins (backfilled from winner)")
    else:
        print("  team2_wins already present, skip")

    conn.commit()
    conn.close()
    print("✅ Migration complete")


if __name__ == "__main__":
    cfg = Config.load()
    migrate(cfg.database_url)
