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
4. Owner clicks Approve → bot pulls your solo queue rank and recent match history, seeds your inhouse elo accordingly, and DMs you the confirmation
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

**Without `role`:** shows top 15 players by their best per-role conservative skill (mu - 3σ).
**With `role`:** shows top 15 players for just that role.

Players with 0 inhouse games played are excluded.

---

## Recruitment commands

### `/recruit-now <game_date>` (admin)

Manually post a recruitment for a specific Thursday. Useful for testing or filling in if the scheduler missed a Friday.

**Argument:**
- `game_date` — The Thursday in YYYY-MM-DD format (must be a Thursday)

**Behavior:**
- Posts the recruitment embed in the channel where you ran the command (not the configured `recruit` channel)
- Adds the three RSVP buttons: 🎮 Playing, ❌ Not Playing, 📺 Commentator

### `/match-preview` (admin)

Shows the top-3 matchmaker proposals for the currently active recruiting session. Ephemeral — only you see the result. Doesn't commit anything.

Useful for sanity-checking what the matchmaker will produce before signups close.

Requires at least 10 `playing` signups.

---

## Admin commands

### `/set-channel <purpose> <channel>` (admin)

Configures which channel the bot uses for various purposes.

**Arguments:**
- `purpose` — `recruit` or `results`
- `channel` — The channel to use

**`recruit`** — Where the **scheduled Friday 9 AM** job posts its recruitment message. (Manual `/recruit-now` always posts in its own caller's channel.)

**`results`** — Currently scaffolded but not actively used. Match outcomes are posted in the same channel as the recruitment.

If you don't configure `recruit`, the Friday job falls back to the first channel where the bot has send permission.

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

### `/report-manual <match_id> <winner>` (admin)

Same as `/report` but skips OCR. Use when you don't have a screenshot or OCR is failing.

**Arguments:**
- `match_id` — The match number
- `winner` — `team1` or `team2`

Updates ratings the same way as `/report`. Per-player performance rows are still created but with KDA fields blank.

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
