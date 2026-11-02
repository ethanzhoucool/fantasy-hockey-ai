# fantasy-hockey-ai

A small service that manages one team in an ESPN Fantasy Hockey league without a browser. Every 2 minutes it
polls ESPN's API, applies fixed rules (lineup, pickups, streaming), sends you a notification when something
happens, and only hands a decision to Claude Code when it needs judgment (a trade offer or a chat message)
and you have turned that on.

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
- free agents: no free-agent add yet this scoring period (the league allows 1 per scoring day)
- waiver players get a claim of type `WAIVER`

**Streaming** (`fhockey/stream.py`). Each morning and ~60 minutes before the first game:

- today has an empty F, D or UTIL slot after the optimal lineup (started players count as filled)
- adds this week (counter + pending claims) < 7, and 1 add stays in reserve until the last 2 days of the
  matchup week (from `mSettings` matchup periods); no free-agent add yet today
- add: a `FREEAGENT` (not on waivers), `ACTIVE`, >= 5 GP, whose game starts more than 20 minutes from now,
  highest value at a position with an empty slot (UTIL takes F or D)
- drop: the lowest-value skater who is not OUT/IR, projects <= 1.80 (never >= 1.85), is not in a pending trade
  or claim, wasn't streamed in today, hasn't started playing, and projects no more than 0.10 above the add.
  Players without a game today are dropped first.
- then the lineup is re-run so the streamer starts

**Planned moves** (`planned_moves` in config.json). An add/drop you approved ahead of time, e.g. a player
who clears waivers after today's add is used: `{"add": <playerId>, "drop": <playerId>, "from_sp": <period>}`.
It runs once, before pickups and streaming, as that day's free-agent add. It waits while the add is still on
waivers, the day's add is used, the week is full or the drop is in a pending trade, and is cancelled if the
add joins another team or the drop leaves your roster.

All thresholds live under `rules` and `stream` in config.json (see `config.example.json`).

## Claude hook (opt-in, off by default)

Trade offers and chat messages need judgment, so the service never acts on them by itself. Each one is written
to `~/.local/state/fantasy-hockey/inbox/*.json` and you get a notification.

With `"claude_hook": true` the watcher also starts `claude -p` in the background, with the repo as its working
directory and a prompt that says to read `docs/TRADE_PLAYBOOK.md`, inspect the events with the read-only
commands above, run `model/trade_eval.py` / `trades/trade_scan.py`, and finish with a short recommendation,
which is pushed to you. Limits:

- at most `claude_max_per_day` runs (default 4) per 24 hours and one per `claude_min_gap_minutes` (30)
- never two at once (file lock), killed after `claude_timeout_minutes` (20)
- tools are limited by `claude_args` (default: Read/Grep/Glob and the repo's own Python commands)
- event files move to `inbox/done/` after a run, so a failing run is not retried forever
- this service cannot accept, propose or decline trades, or send chat messages. Anything Claude does beyond a
  recommendation depends on what you allow in `claude_args` and `claude_extra_prompt`.

## Safety

- **Dry run by default.** Writes need `auto_execute: true`; the ESPN client itself refuses to POST otherwise.
- **Limits.** The pickup and streaming rules above, 1 free-agent add per day, 7 adds per week including pending
  claims. If ESPN's response is missing the add counter, the service assumes the limit is reached.
- **Writes are never retried.** A timed-out POST may have gone through, so a failed write is reported, not repeated.
- **Never prints secrets.** Cookies are read from `auth.json` (mode 600, directory 700). They never appear in
  output, logs, notifications, state or inbox files: every value is registered with a scrubber that all
  output passes through, and a test runs every command and checks.
- **Polling volume.** About 4 small requests per 2-minute poll (~3,000 a day), plus a few more when something
  happens. Raise `poll_interval_seconds` if you want fewer.
- The ESPN API is unofficial. If a payload changes, writes fail loudly and nothing is retried.

## What is tested, and what is not

`python3 -m unittest` (100 tests) runs against hand-built fixtures shaped like ESPN responses (`tests/fixtures/`):
lineup optimization, every pickup rule rejecting, weekly-limit math, streaming budget/reserve, drop eligibility
and game-time cutoff, drop detection and dedupe, waiver-clear detection, trade/chat/injury detection, lineup
timing, the Claude hook's rate limit and lock, notifications, the launchd plist, the snapshot format, and that
no command leaks a cookie.

Verified against a live league (Nov 2 2026): every read the service makes, a `FUTURE_ROSTER` lineup save, and
pending-trade parsing (including the named `DROP` item on uneven trades). In ESPN hockey,
`scheduleSettings.matchupPeriods` maps matchups to week numbers, not scoring days, so the service uses a
Monday-Sunday week. Still not confirmed live: the free-agent add payload from Python (the same JSON already
worked from the browser) and **`WAIVER` claims**, which were never confirmed to succeed anywhere.

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
| `~/Library/Logs/fantasy-hockey/` | `events.log`, `service.log`, `watch.log`, `claude.log` |

Override the directories with `FHOCKEY_CONFIG_DIR`, `FHOCKEY_STATE_DIR`, `FHOCKEY_LOG_DIR`.
