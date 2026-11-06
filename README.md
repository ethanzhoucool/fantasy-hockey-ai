# fantasy-hockey-ai

A small service that manages one team in an ESPN Fantasy Hockey league without a browser. Every 2 minutes it
polls ESPN's API, applies fixed rules (lineup, pickups, streaming), sends you a notification when something
happens, and only hands a decision to Claude Code when it needs judgment (a trade offer or a chat message)
and you have turned that on.

Agents (Claude Code and others): start with [AGENTS.md](AGENTS.md), the project's long-term memory and rules.

Python 3.9+ standard library only. No pip installs. macOS for the launchd agent and banners; the rest runs anywhere.

> **ESPN's API is unofficial and undocumented.** Endpoints, field names and write payloads can change without
> notice. This project polls the same endpoints the ESPN website uses; it has no webhooks or push to rely on.

## What it does

| When | What |
|------|------|
| Another team drops a player | Within ~2 minutes: runs the pickup rules. A waiver player gets a `WAIVER` claim, a free agent a `FREEAGENT` add/drop. |
| A watched player clears waivers (daily run ~08:00 UTC) | Runs the pickup rules again, now as a free agent. |
| Each morning, and ~60 min before the first game | Streaming: if a starting skater slot would be empty today, adds a free agent who plays today and drops a replacement-level player. |
| ~30 min before the first puck drop, and after any roster or injury change | Sets the optimal lineup. |
| Injury status changes on your roster | Notification, lineup re-run. |
| A trade offer arrives, or your own offer changes status | Notification + an inbox event (and optionally Claude). |
| A new chat message (DM or league chat) | Notification + an inbox event (and optionally Claude). |
| Once a day | Refreshes the projection model (`model/project.py`) if the model data is present. |

Everything that writes to ESPN is **off by default** (`auto_execute: false`): the service logs and notifies
what it *would* do. Turn writes on only after a few days of dry runs look right.

With `gm.enabled` on, Claude Opus makes the adds and drops instead of the pickup and streaming rows above. See
[Opus GM](#opus-gm-opt-in-off-by-default).

## Architecture

```
            launchd (every 120 s)
                    |
                    v
   python3 -m fhockey watch --once -----------------------------+
     |  flock: one run at a time                                |
     |                                                          |
     |  fhockey/espn.py   GET lm-api-reads.fantasy.espn.com     |   ~/.config/fantasy-hockey/
     |  (Cookie: SWID + espn_s2, retries, 401 -> "run setup")   |<-- auth.json  (mode 600)
     |     mStatus+mRoster+mTeam, mTransactions2 (today,        |    config.json
     |     yesterday), kona_league_communication, and when      |
     |     needed kona_player_info, proTeamSchedules_wl,        |   ~/.local/state/fantasy-hockey/
     |     mSettings (cached daily)                             |<-> state.json, schedule.json
     v                                                          |
   fhockey/watch.py  detect: drops, waiver clears, offers,      |
     |               chat, injuries, game times                 |
     |                                                          |
     +--> pickups.py  bot.js candidates + the 7 pickup rules    |
     +--> stream.py   empty-slot streaming rules                |
     +--> gm.py       (opt-in, replaces the two above) starts   |
     |                `gm` in the background: Opus plans, hard  |
     |                limits re-check every move, then it runs  |
     +--> lineup.py   9F/5D/1UTIL/2G optimizer (bot.js port)    |
     |      |                                                   |
     |      +--> espn.py POST lm-api-writes .../transactions/   |  only when auto_execute is true
     |                                                          |
     +--> notify.py   events.log, macOS banner, ntfy.sh push    |   ~/Library/Logs/fantasy-hockey/
     |                                                          |
     +--> claude_hook.py  inbox/*.json for trade offers / chat  |
              |   (opt-in) background `claude -p`, rate-limited,|
              |   one at a time, reads docs/TRADE_PLAYBOOK.md   |
              v                                                 |
         Claude's answer --> notify.py ------------------------+

   model/project.py  (daily) NHL stats -> ridge projections -> model/proj.json
```

## Setup

1. **Clone and check the tests.**
   ```
   git clone https://github.com/ethanzhoucool/fantasy-hockey-ai && cd fantasy-hockey-ai
   python3 -m unittest
   ```

2. **Copy your two ESPN cookies.** Private leagues need them. In Chrome, signed in to ESPN Fantasy:
   DevTools (Cmd+Opt+I) > **Application** > Storage > **Cookies** > `https://fantasy.espn.com`.
   Copy the values of `SWID` (looks like `{XXXXXXXX-XXXX-...}`) and `espn_s2` (a long string).

3. **Run setup.** League and team ids are in your team page URL (`leagueId=...&teamId=...`).
   ```
   python3 -m fhockey setup --league <leagueId> --team <teamId> --season 2027
   ```
   It asks for SWID and espn_s2 with hidden input, saves them to `~/.config/fantasy-hockey/auth.json`
   (mode 600), makes one read call and prints only `ok` or `failed` (details, without secrets, go to
   `~/Library/Logs/fantasy-hockey/service.log`). The cookies last for months; when ESPN rejects them the
   service notifies you to run `setup` again.

4. **Build the projections** (optional but recommended; without them skaters are valued with the fallback blend).
   See [Rebuilding the model data](#rebuilding-the-model-data), then `python3 -I model/project.py`.

5. **Try it by hand, in dry-run mode.**
   ```
   python3 -m fhockey status            # period, matchup score, adds used, roster with projections
   python3 -m fhockey lineup --dry-run
   python3 -m fhockey pickups --dry-run -v
   python3 -m fhockey stream --dry-run
   python3 -m fhockey watch --once      # one poll, exactly what launchd runs
   ```

6. **Install the agent** (polls every 2 minutes, survives reboots):
   ```
   python3 -m fhockey install      # ~/Library/LaunchAgents/com.ethanzhoucool.fantasy-hockey.plist
   python3 -m fhockey uninstall
   ```
   Logs: `~/Library/Logs/fantasy-hockey/` (`events.log` = every notification, `service.log` = details,
   `watch.log` = launchd stdout). Re-run `install` if you change which `python3` you use.

7. **Go live** when the dry runs look right: set `"auto_execute": true` in `~/.config/fantasy-hockey/config.json`.

### Phone notifications (optional)

Set `ntfy_topic` in config.json and subscribe to the same topic in the [ntfy](https://ntfy.sh) app. Anyone who
knows a topic name can read it, so use a long random one:
`python3 -c "import secrets; print('fh-' + secrets.token_urlsafe(18))"`. Pushes are off while `ntfy_topic` is empty.

## Commands

| Command | |
|---|---|
| `setup [--league N --team N --season N]` | Save ids and cookies, validate with one read |
| `status` | Period, matchup, adds used this week, roster with projections and game times |
| `lineup [--dry-run] [--sp N]` | Optimal lineup for today (or a future scoring period, sent as `FUTURE_ROSTER`) |
| `pickups [--dry-run] [-v]` | Apply the pickup rules once |
| `stream [--dry-run]` | Apply the streaming rules once |
| `watch [--once]` | One poll (or loop forever) |
| `install` / `uninstall` | launchd agent |
| `snapshot [--out F]` | League file for `trades/trade_scan.py` / `robust.py` / `streaming.py` |
| `offers` | Pending trade offers with player names |
| `trade-input --offer ID` or `--partner N --give "A,B" --get "C"` `[--out F]` | Input for `model/trade_eval.py` |
| `chat [--hours 24]` | Recent chat threads, flags threads that need a reply |
| `inbox` | Events waiting for judgment |
| `gm [--dry-run] [--reason X]` | Ask Opus for today's adds and drops, check them against the limits, execute them |
| `gm-report [--entries N]` | Is the GM on, how its last run went, and its latest decisions and reasons |

`--dry-run` never writes. Without it, commands still write only when `auto_execute` is true.

## The rules

**Lineup** (`fhockey/lineup.py`, a port of the original browser `bot.js`). Starts the highest-value players
whose NHL team plays today in 9 F, 5 D, 1 UTIL (any skater), 2 G. `DAY_TO_DAY` players count at half value;
`OUT`, `INJURY_RESERVE` and `SUSPENSION` players don't start. Players whose game has started and the IR slot
are never moved. Moves are minimal: an idle starter keeps his slot unless a playing player needs it.

**Player value.** Skaters: the ridge model's rest-of-season fantasy pts/g (`model/proj.json`). Goalies and
anyone missing from the model: this season's ESPN average blended with last season's (or ESPN's projection),
weight `gp / (gp + 25)` on this season.

**Pickups** (`fhockey/pickups.py`). Candidates are the best 3 available players at each position against your
2 weakest healthy players there (gap >= 0.2). Going down the list by edge, the first candidate that passes
*every* rule is taken; at most one free-agent add and one waiver claim per run:

- edge >= 0.25 pts/g and P(add outscores drop) >= 0.75, where P = Phi(edge / (SD * sqrt 2)) and SD is the
  model's backtested residual SD
- the add is `ACTIVE` with >= 5 games played this season
- the drop is not in a pending trade, not OUT/IR, and projects below 1.85 (core players are never dropped)
- neither player is already in one of your pending waiver claims
- weekly limit: `transactionCounter.matchupAcquisitionTotals[currentMatchupPeriod]` + pending waiver claims
  + this move <= 7
- no daily limit: ESPN doesn't enforce one, so a real upgrade doesn't wait for tomorrow
- waiver players get a claim of type `WAIVER`

**Streaming** (`fhockey/stream.py`). Each morning and ~60 minutes before the first game:

- today has an empty F, D or UTIL slot after the optimal lineup (started players count as filled)
- adds this week (counter + pending claims) < 7, and 1 add stays in reserve until the last 2 days of the
  matchup week (from `mSettings` matchup periods); no add yet today (`stream.adds_per_day`, the service's own pacing)
- add: a `FREEAGENT` (not on waivers), `ACTIVE`, >= 5 GP, whose game starts more than 20 minutes from now,
  highest value at a position with an empty slot (UTIL takes F or D)
- drop: the lowest-value skater who is not OUT/IR, projects <= 1.80 (never >= 1.85), is not in a pending trade
  or claim, wasn't streamed in today, hasn't started playing, and projects no more than 0.10 above the add.
  Players without a game today are dropped first.
- then the lineup is re-run so the streamer starts

**Planned moves** (`planned_moves` in config.json). An add/drop you approved ahead of time, e.g. a player
who is about to clear waivers: `{"add": <playerId>, "drop": <playerId>, "from_sp": <period>}`.
It runs once, before pickups and streaming. It waits while the add is still on
waivers, the week is full or the drop is in a pending trade, and is cancelled if the
add joins another team or the drop leaves your roster.

All thresholds live under `rules` and `stream` in config.json (see `config.example.json`).

## Opus GM (opt-in, off by default)

With `"gm": {"enabled": true}` the pickup and streaming rules stop and Claude Opus makes every add and drop. Python
keeps polling, the lineup math, the ESPN writes and the hard limits. Planned moves and lineups don't change. Trades
and messages still need you; Opus can only leave a note.

**When it runs** (at most `max_runs_per_day`, 4, and one per `min_gap_minutes`, 30):

- each morning (`morning_hour`), once
- before the first game, only as a retry if the morning run got no answer
- when a starter goes OUT, or when a dropped or cleared player beats your weakest by `trigger_margin` (0.2) on his
  best valuation. An event that hits the rate limit waits for the rest of that scoring day.
- not at all when the week's adds are used up

**What Opus sees.** Over stdin: `docs/GM_PLAYBOOK.md`, `memory/LEAGUE.local.md`, the end of `memory/GM_LOG.local.md`
(Python appends every decision, so later runs remember) and a brief of live data: matchup score, adds left, open
slots per day for two weeks, your roster and the top available players with three valuations, games, ownership and
ESPN news, pending trades, and your recent moves with the points scored since. **Chat, DMs and team names are never
in it**, because other managers write those and this run can cause moves. The league notes are written by sessions
with you and can describe what managers said, so anything in quotation marks is removed before Opus sees them.

**What it can do.** `claude -p` with WebSearch only, none of your Claude settings or connectors, from an empty
directory. It answers with a JSON plan. It has no way to touch ESPN or a file.

**Hard limits**, re-checked against a fresh ESPN read right before each write, whatever Opus says:

- the add is in the brief, is still a free agent or on waivers, and isn't OUT, on IR or suspended
- the drop is in the brief and on your roster, not in the IR slot, with no game started, not in a pending trade or
  claim, and not in `protected_players`. A field ESPN left out blocks the move instead of reading as fine.
- the median of the drop's three valuations is under `core_floor` (2.05; goalies `core_floor_goalie`, 3.5), so stars
  can't be cut. Goalies score on a higher scale, so protect your real starters by name.
- ids and names match
- at least 9 F, 5 D, 2 G remain outside the IR slot, even if a pending claim loses in waiver order
- 7 adds a week including pending claims, `max_moves_per_run` (2), `max_moves_per_day` (3)
- the plan is under `max_plan_age_minutes` (60) old
- `FREEAGENT` players get an add, `WAIVERS` players a claim. Writes are never retried, and a failed claim blocks claims
  for a week, since they have never been confirmed live.

**If anything fails** (timeout, usage limit, malformed output, a plan not written by Opus), there are no moves and no
fallback to the old rules. Lineups continue, and the failure is reported.

**Where to see it.** `python3 -m fhockey gm-report`, or `events.log` (kind `gm`). It runs on your Claude plan and only
while your Mac is awake. Try it first with `python3 -m fhockey gm --dry-run`, which asks Opus and checks the plan but
writes nothing to ESPN, `state.json` or the decision log (it keeps its answer in `gm_dry_run.json`).

## Claude hook (opt-in, off by default)

Trade offers and chat messages need judgment, so the service never acts on them by itself. Each one is written
to `~/.local/state/fantasy-hockey/inbox/*.json` and you get a notification.

With `"claude_hook": true` the watcher also starts `claude -p` in the background, with the repo as its working
directory and a prompt that says to read `docs/TRADE_PLAYBOOK.md`, inspect the events with the read-only
commands above, run `model/trade_eval.py` / `trades/trade_scan.py`, and finish with a short recommendation,
which goes to `events.log` (and to Mac or phone alerts if you have them on). Limits:

- at most `claude_max_per_day` runs (default 4) per 24 hours and one per `claude_min_gap_minutes` (30)
- never two at once (file lock), killed after `claude_timeout_minutes` (20)
- the run reads other managers' chat, so it is locked down: `claude_hook.LOCKDOWN` drops your Claude settings
  (their allow list, auto mode, hooks) and all MCP connectors, and denies anything not allowed. `claude_args` then
  allows Read/Grep/Glob inside the repo and only the read-only commands (`offers`, `chat`, `trade-input`,
  `snapshot`, `status`, the trade scripts). `FHOCKEY_READ_ONLY=1` makes every ESPN write fail in that run, and
  `--out` can only write inside `trades/`.
- event files move to `inbox/done/` after a run, so a failing run is not retried forever
- this service cannot accept, propose or decline trades, or send chat messages

## Safety

- **Dry run by default.** Writes need `auto_execute: true`; the ESPN client itself refuses to POST otherwise, and
  refuses whenever `FHOCKEY_READ_ONLY` is set.
- **Limits.** The pickup and streaming rules above, 1 streaming add per day (the service's own pacing), 7 adds per week including pending
  claims. If ESPN's response is missing the add counter, the service assumes the limit is reached. The Opus GM has
  its own hard limits, checked in Python.
- **Writes are never retried.** A timed-out POST may have gone through, so a failed write is reported, not repeated.
- **Never prints secrets.** Cookies are read from `auth.json` (mode 600, directory 700). They never appear in
  output, logs, notifications, state or inbox files: every value is registered with a scrubber that all
  output passes through, and a test runs every command and checks.
- **Polling volume.** About 4 small requests per 2-minute poll (~3,000 a day), plus a few more when something
  happens. Raise `poll_interval_seconds` if you want fewer.
- The ESPN API is unofficial. If a payload changes, writes fail loudly and nothing is retried.

## What is tested, and what is not

`python3 -m unittest` runs against hand-built fixtures shaped like ESPN responses (`tests/fixtures/`):
lineup optimization, every pickup rule rejecting, weekly-limit math, streaming budget/reserve, drop eligibility
and game-time cutoff, drop detection and dedupe, waiver-clear detection, trade/chat/injury detection, lineup
timing, the Claude hook's rate limit, lock and read-only arguments, the Opus GM's brief, output parsing, every hard
limit, run flow and watcher triggers (with a fake Claude), notifications, the launchd plist, the snapshot format,
and that no command leaks a cookie.

Verified against a live league (Nov 2 2026): every read the service makes, a `FUTURE_ROSTER` lineup save, and
pending-trade parsing (including the named `DROP` item on uneven trades). ESPN never changes a trade proposal's own status: an accept, decline or cancel is a
new record whose `relatedTransactionId` points at the proposal, and the service reads the status from those. In ESPN hockey,
`scheduleSettings.matchupPeriods` maps matchups to week numbers, not scoring days, so the service uses a
Monday-Sunday week. Free-agent adds from Python were confirmed Nov 3-4 2026. Still not confirmed live: **`WAIVER`
claims**, which were never confirmed to succeed anywhere.

python.org builds of Python ship without CA certificates; the client falls back to `/etc/ssl/cert.pem` on macOS.

Run `status`, `lineup --dry-run`, `pickups --dry-run` and `watch --once` against your league before turning on writes.

## Rebuilding the model data

`model/data/` (~23 MB of public NHL stats) is not in the repo. To rebuild it:

```
python3 -I model/bt_fetch.py model/data      # NHL skater/goalie stats, 2021-22 to now (api.nhle.com)
python3 -I model/fetch_sched.py              # every team's schedule -> model/data/sched/
python3 -I model/bt_model.py model/data      # backtest + fit the ridge model -> model/data/model.json
python3 -I model/project.py                  # today's projections -> model/proj.json (the service runs this daily)
```

`FHOCKEY_MODEL_DATA` / config `model_data_dir` point elsewhere if you keep the data outside the repo.

## Trade tools

`docs/TRADE_PLAYBOOK.md` holds the trade rules. `python3 -m fhockey snapshot` writes `trades/league_<date>.txt`;
then `python3 -I trades/trade_scan.py trades/league_<date>.txt <partnerTeamId>`, `trades/robust.py` and
`trades/streaming.py` work as before. Set `FHOCKEY_TEAM_ID` if your team id isn't 4. League snapshots and scans
contain other managers' rosters, so `.gitignore` keeps them out of the repo.

## Files

| Path | |
|---|---|
| `~/.config/fantasy-hockey/config.json` | settings (no secrets) |
| `~/.config/fantasy-hockey/auth.json` | SWID + espn_s2, mode 600 |
| `~/.local/state/fantasy-hockey/` | `state.json`, schedule/settings caches, `inbox/` |
| `~/.local/state/fantasy-hockey/gm_last.json` | the Opus GM's last brief, plan and result |
| `~/Library/Logs/fantasy-hockey/` | `events.log`, `service.log`, `watch.log`, `claude.log`, `gm.log` |
| `memory/GM_LOG.local.md` | every GM decision (gitignored), read back by later runs |

Override the directories with `FHOCKEY_CONFIG_DIR`, `FHOCKEY_STATE_DIR`, `FHOCKEY_LOG_DIR`.
