"""One-shot migration to add base_seed and inhouse_modifier to the ratings table.

Run this from the bot environment after deploying new code:
    python -m bot.scripts.migrate_split_elo

Idempotent — safe to run multiple times. Assumes no inhouse games have been
played yet (so inhouse_modifier = 0 for everyone, base_seed = elo).
"""
import asyncio
import sqlite3
from pathlib import Path

from bot.config import Config


def migrate(db_path: str) -> None:
    if db_path.startswith("sqlite+aiosqlite:////"):
        # absolute path
        actual_path = db_path.replace("sqlite+aiosqlite:////", "/")
    elif db_path.startswith("sqlite+aiosqlite:///"):
        # relative path
        actual_path = db_path.replace("sqlite+aiosqlite:///", "")
    else:
        raise ValueError(f"Unexpected DATABASE_URL format: {db_path}")

    print(f"Migrating {actual_path}")
    if not Path(actual_path).exists():
        print(f"  Database file doesn't exist yet — skip (will be created fresh).")
        return

    conn = sqlite3.connect(actual_path)
    cur = conn.cursor()

    # Find ratings columns
    existing = {row[1] for row in cur.execute("PRAGMA table_info(ratings)").fetchall()}
    print(f"  Current columns: {existing}")

    if "base_seed" not in existing:
        cur.execute("ALTER TABLE ratings ADD COLUMN base_seed INTEGER DEFAULT 1200")
        # Initialize base_seed = elo for existing rows (since no games have been
        # played yet, elo is purely the seed value)
        cur.execute("UPDATE ratings SET base_seed = elo")
        print("  Added base_seed (initialized = elo)")
    else:
        print("  base_seed already present, skip")

    if "inhouse_modifier" not in existing:
        cur.execute("ALTER TABLE ratings ADD COLUMN inhouse_modifier INTEGER DEFAULT 0")
        # All zero by default — assumes no games played yet
        print("  Added inhouse_modifier (initialized = 0)")
    else:
        print("  inhouse_modifier already present, skip")

    conn.commit()
    conn.close()
    print("✅ Migration complete")


if __name__ == "__main__":
    cfg = Config.load()
    migrate(cfg.database_url)
