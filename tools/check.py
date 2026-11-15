"""Run every local regression check. This is the one command to run before pushing.

    python tools/check.py                 # everything
    python tools/check.py --fast          # skip the production-snapshot tests
    python tools/check.py -k recruit      # only tests matching a keyword
    python tools/check.py --update-golden # accept intended Elo changes, then review the diff

Steps:
  1. Compile every module (catches syntax errors in files no test imports).
  2. Import the bot entrypoint and build every cog's slash commands (catches bad
     command signatures that only fail when Discord syncs them at startup).
  3. Run the test suite: unit tests, end-to-end flow tests on a fake Discord
     (tests/harness.py), the golden Elo season, and — when data/prod_inhouse.db
     exists — checks against a copy of the real database.

Exit code is 0 only if every step passes. Nothing here talks to Discord, Riot,
Anthropic or Fly, and nothing writes to data/.
"""
from __future__ import annotations

import argparse
import compileall
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def step(name: str):
    print(f"\n=== {name} ===", flush=True)
    return time.time()


def compile_all() -> bool:
    ok = compileall.compile_dir(str(ROOT / "bot"), quiet=1, force=True)
    ok = compileall.compile_dir(str(ROOT / "tests"), quiet=1, force=True) and ok
    print("ok" if ok else "FAILED: syntax errors above")
    return bool(ok)


def import_smoke() -> bool:
    """Import everything and register all slash commands on a command tree."""
    code = r"""
import types
import discord
from discord import app_commands
import bot.main  # noqa: F401  (import-time errors surface here)
from bot.cogs.admin import AdminCog
from bot.cogs.linking import LinkingCog
from bot.cogs.recruitment import RecruitmentCog
from bot.cogs.stats import StatsCog

tree = app_commands.CommandTree(discord.Client(intents=discord.Intents.default()))
names = []
for cog_cls in (AdminCog, LinkingCog, RecruitmentCog, StatsCog):
    for cmd in cog_cls.__cog_app_commands__:
        tree.add_command(cmd)          # rejects duplicate names
        payload = cmd.to_dict(tree)    # the exact JSON Discord would be sent
        assert 1 <= len(payload["description"]) <= 100, cmd.name
        for opt in payload.get("options", []):
            assert 1 <= len(opt["description"]) <= 100, (cmd.name, opt["name"])
        names.append(cmd.name)
assert len(names) <= 100, "Discord allows at most 100 slash commands per guild"
print(f"ok: {len(names)} slash commands across 4 cogs")
"""
    env = dict(os.environ, DISCORD_TOKEN="x", DISCORD_GUILD_ID="1", OWNER_DISCORD_ID="1",
               RIOT_API_KEY="x", DATABASE_URL="sqlite+aiosqlite:///:memory:")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env).returncode == 0


def run_tests(args) -> bool:
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    if args.fast:
        cmd += ["--deselect", "tests/test_prod_snapshot.py"]
    if args.k:
        cmd += ["-k", args.k]
    cmd += args.pytest_args
    env = dict(os.environ)
    if args.update_golden:
        env["UPDATE_GOLDEN"] = "1"
    return subprocess.run(cmd, cwd=ROOT, env=env).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fast", action="store_true", help="skip tests that need data/prod_inhouse.db")
    ap.add_argument("--update-golden", action="store_true", help="rewrite tests/golden/*.json from current behaviour")
    ap.add_argument("-k", help="only run tests matching this keyword expression")
    ap.add_argument("pytest_args", nargs="*", help="extra arguments passed to pytest")
    args = ap.parse_args()

    results = {}
    t = step("1/3 compile")
    results["compile"] = compile_all()
    t = step("2/3 import + slash-command smoke test")
    results["import"] = import_smoke()
    t = step("3/3 test suite")
    results["tests"] = run_tests(args)

    snapshot = ROOT / "data" / "prod_inhouse.db"
    print("\n=== summary ===")
    for name, ok in results.items():
        print(f"  {name:8s} {'PASS' if ok else 'FAIL'}")
    if args.fast or not snapshot.exists():
        print("  note: production-snapshot tests were skipped"
              + ("" if args.fast else " (no data/prod_inhouse.db — see CLAUDE.md to pull one)"))
    if args.update_golden:
        print("  golden files were rewritten — review `git diff tests/golden` before committing")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
