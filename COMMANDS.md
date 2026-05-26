# Bot Commands Reference

All commands are Discord slash commands — type `/` in any channel and they'll autocomplete.

Permissions:
- **Public** — any server member can use
- **Admin** — requires the `League Admin` Discord role
- **Owner** — only the user whose ID matches `OWNER_DISCORD_ID` (you)

---

## Player commands (public)

### `/link <riot_id>`

Submit a request to link your Discord account to a League of Legends account.

**Argument:**
- `riot_id` — Your Riot ID in the form `GameName#TAG` (e.g., `Faker#KR1`)

**What happens:**
1. Bot validates the Riot ID against Riot's API
2. Creates a pending link request
3. **DMs the bot owner** with Approve / Reject buttons
4. Owner clicks Approve → bot pulls your solo queue rank and recent match history, seeds your inhouse elo accordingly, and DMs you the confirmation. If you're unranked this split, it falls back to your most recent past-season Solo/Duo rank (via OP.GG), decayed by how long ago that was; with no ranked history at all you start at 1200.
5. Owner clicks Reject → opens a reason prompt, then DMs you with the reason

You can't use other commands that depend on linking until your request is approved.

**Restrictions:**
- You can have only one approved Riot account linked at a time
- Re-linking is blocked while a previous request is pending or approved — run `/unlink` first

### `/unlink`

Removes your Riot account link. Works whether your link is pending or approved.

Your inhouse elo is **preserved** — it persists across unlink/relink cycles. Only the Riot ID association is removed.

### `/profile [user]`

Shows the inhouse profile for yourself or another user.

**Optional argument:**
- `user` — A Discord member to look up. Defaults to yourself.

**Shows:**
- Their linked Riot ID (or "Pending admin approval" if not yet approved)
- Solo queue rank and LP
- Inferred main role from recent ranked games
- Per-role inhouse elo (μ, σ, conservative skill, games played)

### `/leaderboard [role]`

Shows the inhouse leaderboard.

**Optional argument:**
- `role` — Filter to a specific role: `TOP`, `JUNGLE`, `MID`, `BOT`, or `SUPPORT`

**Without `role`:** shows top 15 players by their best per-role elo.
**With `role`:** shows top 15 players for just that role.

Players with 0 inhouse games played are excluded.

### `/elo-history [user]`

Shows a player's match-by-match inhouse elo history (defaults to you). Each line is a game: date, win/loss, series score, role, the `±` change to their INHOUSE rating, and the running cumulative modifier. The header shows current INHOUSE elo broken into rank-base + inhouse modifier + games played. Shows the last 15 games (with a count of any earlier ones).

---

## Recruitment commands

### `/recruit-now <game_date> [channel] [open_ended]` (admin)

Manually post a recruitment for a specific Thursday. Useful for testing or filling in if the scheduler missed a Friday.

**Arguments:**
- `game_date` — The Thursday in YYYY-MM-DD format (must be a Thursday)
- `channel` *(optional)* — where to post
- `open_ended` *(optional, default false)* — if true, signups stay open **indefinitely** until you run `/close-signups`; the Monday auto-close skips it. Use this while you're gathering elo data and closing/drafting by hand.

**Behavior:**
- Posts to the `channel` you pass, else the configured `recruit` channel (`/set-channel recruit`), else the channel you ran the command in.
- Adds the three RSVP buttons: 🎮 Playing, ❌ Not Playing, 📺 Commentator
- The recruitment embed footer shows `Session #N` — use that id with `/signups`, `/close-signups`, `/manual-match`, `/report-manual`, etc.

### `/close-signups [session_id]` (admin)

Closes signups for **one specific** recruitment — locks the RSVP buttons (status → `closed`) and posts a notice with the playing count. Then draft teams with `/manual-match session_id:<N>`.

- Pass `session_id` to close that exact recruitment.
- With no id: closes the open one if there's exactly one; if several are open, it **refuses and lists them** so you pick the right one (it never closes more than the one you mean).
- Mainly for `open_ended` recruitments, but also works to close a scheduled one early.

### `/match-preview` (admin)

Shows the top-3 matchmaker proposals for the currently active recruiting session. Ephemeral — only you see the result. Doesn't commit anything.

Useful for sanity-checking what the matchmaker will produce before signups close.

Requires at least 10 `playing` signups.

### `/signups [session_id]` (admin)

Lists everyone who signed up for a session and **the role(s) each playing player picked** — info that's hidden from the public recruitment message (role choices are private). Output looks like:

```
🎮 Playing (7)
 • @Alice — TOP, MID
 • @Bob — FILL
❌ Not Playing (2): @Dave, @Eve
📺 Commentators (1): @Finn
```

Ephemeral, so it doesn't leak role picks publicly. Defaults to the soonest active recruiting session; pass `session_id` (shown on the recruitment post footer as `Session #N`) to view a specific one.

> **Reference IDs:** every night's messages carry a `Session #N` (recruitment post, signups-closed notice, teams post); match/result messages carry `Match #M`. Use the **session id** for session-scoped commands (`/signups`, `/manual-match`, `/report-manual`) and the **match id** for match-scoped ones (`/report`, `/match-roster`, `/unreport`). You never have to type a date.

### `/cancel-session [session_id]` (admin)

Cancels a recruiting or matched session (marks it `cancelled` and posts a notice in the recruit channel). Defaults to the soonest active session; pass `session_id` to target a specific one.

### `/remove-signup <user> [session_id]` (admin)

Removes a player's signup from a session (e.g. a no-show or someone who left the server) and refreshes the public counts. Defaults to the soonest recruiting session. Before teams are made this re-orders who's in the first 10; after teams are set, edit the roster with `/match-roster` instead.

> **Note on signups:** if someone clicks 🎮 Playing without an approved Riot link, the bot warns them to `/link` (they'd otherwise be seeded at the default 1200), and `/signups` flags them with ⚠️ not linked.

---

## Admin commands

### `/set-channel <purpose> <channel>` (admin)

Configures which channel the bot uses for various purposes.

**Arguments:**
- `purpose` — `recruit` or `results`
- `channel` — The channel to use

**`recruit`** — Where the **scheduled Friday 9 AM** job posts its recruitment message, and where `/recruit-now` posts by default (unless you pass it an explicit `channel`). **Required for auto-posting:** if no recruit channel is set, the bot skips auto-posting (with a log warning) rather than posting to an arbitrary channel — so set this before relying on the weekly cron.

> **Auto-post is now self-healing.** On startup the bot ensures any upcoming Thursday whose recruitment window has opened (within ~13 days) has a recruitment posted, so a missed Friday cron run (e.g. the host restarted at 9 AM) is recovered on the next boot instead of being skipped. The Friday cron and startup both run the same reconciliation.

**`results`** — Currently scaffolded but not actively used. Match outcomes are posted in the same channel as the recruitment.

If you don't configure `recruit`, the Friday job falls back to the first channel where the bot has send permission.

### `/set-match-category <category>` (admin)

Sets the Discord **category** under which `/match-channels` creates the per-team private channels.

**Arguments:**
- `category` — the category channel to nest the team channels under

The bot warns immediately if it's missing **Manage Channels** / **Manage Roles** (both are required for `/match-channels`), and its own role must sit **above** the team roles to assign them.

### `/match-channels <match_id>` (admin)

Creates the two per-team private comms for a match:
- A Discord **role** per team — `lo-gang` (team 1) and `team-10` (team 2) — reused if it already exists.
- Assigns that role to each of the team's 5 players (members who've left the server are skipped and reported).
- A **private text channel** per team under the configured category (`/set-match-category`), visible only to that team's role, the bot, and the League Admin role. Posts an intro message tagging the team.

**Arguments:**
- `match_id` — the match whose rosters to build channels from (see `/matches`)

Requires a category set via `/set-match-category` first. Idempotent: re-running reuses existing roles/channels and refreshes their permissions.

### `/clear-match-channels` (admin)

Deletes the `lo-gang` and `team-10` private channels (looked up under the configured category, falling back to a guild-wide name match) and **strips those roles from every member** — the roles themselves are kept so they're reused on the next `/match-channels`. Run this between matches to tear down the previous game's comms.

### `/link-user <member> <riot_id>` (admin)

Link a Discord member directly to a Riot ID, bypassing the approval flow.

**Arguments:**
- `member` — The Discord member to link
- `riot_id` — Their Riot ID in the form `GameName#TAG`

Useful when:
- Onboarding new members and you want to skip the back-and-forth
- A user can't run `/link` themselves for some reason
- You're transferring a known account

The same uniqueness checks apply (one Riot account can't be linked to two Discord users).

### `/manual-match` (admin)

Override the matchmaker by submitting your own roster. Opens a popup with a multi-line text box.

**Format:**
```
TEAM 1
TOP: @alice
JUNGLE: @bob
MID: @charlie
BOT: @dave
SUPPORT: @eve
TEAM 2
TOP: @frank
JUNGLE: @grace
MID: @henry
BOT: @iris
SUPPORT: @jack
```

**Tolerated variations:**
- Headers: `TEAM 1`, `TEAM1`, or `T1` (case-insensitive)
- Role aliases: `JG`/`JUNG` for `JUNGLE`, `MIDDLE` for `MID`, `ADC`/`BOTTOM` for `BOT`, `SUP`/`SUPP` for `SUPPORT`

**Validation:**
- All 10 players must already be `/link`'d and approved
- Both teams must be complete (5 distinct roles each)
- No player can be on both teams
- All entries must be Discord @-mentions (won't accept plain names like "alice")

When validated, the match is created and posted publicly to the recruit channel. Works even after the auto-matchmaker has already run.

Targets the soonest active session by default; pass `session_id` (from the recruitment post footer) or `game_date` to target a specific night.

### `/match-roster [match_id]` (admin)

Prints an existing match's roster in the exact format `/manual-match` and `/pickup-series` accept, inside an ephemeral code block — so you don't have to retype @-mentions when editing teams. Defaults to the most recent match if `match_id` is omitted.

Two ways to edit from here:
- **Edit teams** button — if the match isn't reported yet, click it to open the roster modal **pre-filled** with the current teams. Submitting **updates the existing match in place** (no duplicate match created).
- **Copy/paste** — copy the code block, change whatever lines you need, then paste into `/manual-match` (sets teams) or `/pickup-series` (sets teams + a score).

The output also lists each player's Riot name so you can tell who's who. The message is ephemeral (only you see it). A reported match can't be edited — `/unreport` it first.

### `/matches [session_id]` (admin)

Lists recent matches with their `Match #` IDs and report status, so you can find the id to use with `/report`, `/match-roster`, or `/unreport`. Each line shows the match id, its session/date (or `pickup`), and status (`⏳ unreported` or the final score + winner). Defaults to the last 20 matches; pass `session_id` to filter to one night. Ephemeral.

### `/roster-template` (admin)

Builds an editable roster from the **current signups** before any teams exist. Takes the first 10 playing signups, runs the matchmaker for a balanced starting point, and prints it as a copy/paste block (falling back to arbitrary seating if role coverage makes a balance impossible). Copy it, tweak the roles/players, and paste into `/manual-match`. Ephemeral.

### `/sync-ranks` (admin)

Refreshes Riot API rank data for every linked-and-approved player. Useful to call before a game night so seeding reflects current ranks.

Pending players are skipped (we don't want to waste API calls on links that might be rejected).

### `/report <match_id> <winner> <screenshot>` (admin)

Report the outcome of a match using an end-of-game screenshot.

**Arguments:**
- `match_id` — The match number from the teams post (shown in the embed footer)
- `winner` — `team1` or `team2`
- `screenshot` — End-of-game scoreboard image

**Flow:**
1. Bot OCRs the screenshot to extract per-player KDAs
2. Shows you a confirmation embed with detected stats and OCR confidence
3. You react ✅ to commit or ❌ to cancel
4. On commit: TrueSkill ratings update for all 10 players based on the result; per-player KDA rows are saved

OCR is best-effort — confidence varies with screenshot resolution and overlay state. The admin always confirms before committing.

### `/report-manual <series_score> [match_id | session_id | game_date]` (admin)

Same as `/report` but skips OCR/screenshot. Use when you don't have a screenshot or OCR is failing.

**Arguments:**
- `series_score` — from team1's perspective: `2-0`, `2-1`, `1-2`, or `0-2`
- Exactly **one** of: `match_id` (the match number), `session_id` (reports that session's latest unreported match), or `game_date` (`YYYY-MM-DD`, must be a Thursday)

Updates ratings the same way as `/report`. Per-player performance rows are still created but with KDA fields blank.

### `/unreport <match_id>` (admin)

Undo a reported series. Reverses the elo deltas that the report applied (on both the role and INHOUSE ratings), decrements each player's `games_played`, deletes the performance rows, and clears the result so the match can be re-reported with the correct score.

Use this to fix a mis-entered series score — e.g. you reported `2-0` but it was `0-2`. Run `/unreport <match_id>`, then `/report-manual` again with the right score.

Note: reversal is exact for matches reported after this feature shipped (deltas are stored per match). Matches reported earlier have no stored deltas, so `/unreport` will clear the result and decrement games but cannot restore the old modifier precisely.

---

## Test/dev commands (admin)

These exist for testing the matchmaking pipeline without needing 10 humans.

### `/test-fake-signups [count] [clear_first]`

Fills the active recruiting session with fake players.

**Optional arguments:**
- `count` — How many fake players to add (default 10, max 30)
- `clear_first` — If true (default), wipes existing signups and old fake players before adding

The fakes get realistic-looking ranks (Bronze through Diamond) and a mix of role preferences (specialists + Fills) so the matchmaker can produce valid teams.

**Important:** these are not real Discord users — their `discord_id` values are 1, 2, 3, ..., which won't collide with real IDs. They show up in `<@1>` mentions as "Unknown User" since Discord can't resolve them. That's fine for testing the matchmaker math; you just won't see nice names.

### `/test-trigger-close`

Manually fires the Monday 9:30 PM "close signups + send 3 options to owner" job on the active session. Lets you skip waiting for the cron when testing end-to-end.

---

## Owner-only DM interactions

These aren't slash commands — they're button clicks on DMs the bot sends you.

### Link approval (DM)

When someone runs `/link`, you get a DM with the request and two buttons:
- **✅ Approve** — Pulls their rank, seeds their elo, notifies them
- **❌ Reject** — Opens a popup for a rejection reason, then notifies them

### Match option choice (DM)

When the Monday 9:30 PM scheduler fires (or `/test-trigger-close` is run), you get a DM with the top-3 proposals:
- **Pick Option A** (green)
- **Pick Option B** (blue)
- **Pick Option C** (gray)

Click one → bot creates the Match record and posts the chosen teams publicly to the recruit channel. The match is then ready for `/report` once games finish.

If you don't pick, the proposal sits there forever — no auto-pick.

---

## Scheduled jobs (no command)

These run automatically based on cron triggers:

### Friday 9:00 AM ET — Post recruitment

Posts a new recruitment message in the configured `recruit` channel for the Thursday **13 days out** (the Thursday-after-next).

If no `recruit` channel is configured, falls back to the first channel where the bot has send permission.

### Monday 9:30 PM ET — Close signups, send proposals

For the session whose Thursday is 3 days away (this week's Thursday):
1. Counts `playing` signups (ignores Not Playing and Commentator)
2. If less than 10 — cancels the session with a "not enough" message
3. If 10 or more — runs the matchmaker, generates top-3 diverse proposals, DMs them to you, posts a "teams being finalized" placeholder publicly

You then click an option in your DM to commit it.

---

## Reading the elo numbers

When you see something like `**MID**: 24.3 (μ=27.1, σ=0.93) · 14 games`:

- **μ (mu)** — The mean of the player's skill estimate. Higher = better.
- **σ (sigma)** — The uncertainty of that estimate. Lower = more confident. Drops as the player plays more games.
- **Conservative skill** — `μ - 3σ`. The "we're 99.7% sure they're at least this good" number. Used for matchmaking and leaderboard ranking. New players have a wide σ and so a low conservative skill, even if their mu is high — they have to play a few games to "prove it."
- **Games played** — Inhouse games specifically. Riot ranked games don't count here.

A fresh-from-link Plat IV player starts roughly μ=28, σ=6.0 → conservative skill 10. After ~10 games, σ drops to ~3.0 → conservative skill ~19. The matchmaker will weight them more heavily once σ is small.
