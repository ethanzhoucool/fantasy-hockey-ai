"""One cheap poll per run. launchd starts `python3 -m fhockey watch --once` every 2 minutes.

Each run reads: status + Ethan's roster (1 request), transactions for today and yesterday (2), chat (1), and only
when needed: the dropped/watched players (1), the NHL schedule (once a day) and league settings (once a day).
It detects:
  - players dropped by other teams        -> pickup rules run right away (FREEAGENT add or WAIVER claim)
  - watched players clearing WAIVERS -> FREEAGENT (daily waiver run ~08:00 UTC) -> pickup rules again
  - new trade offers to Ethan, status changes of his own offers   -> inbox (+ optional Claude hook)
  - new chat messages                                              -> inbox (+ optional Claude hook)
  - injury-status and roster changes on Ethan's team               -> lineup re-run
  - first puck drop of the day: lineup 30 min before; streaming in the morning and 60 min before
With gm.enabled on, Opus (fhockey/gm.py) makes the adds and drops instead of the pickup and streaming rules, and the
drop, waiver-clear and injury detectors above decide when it is worth waking. Planned moves and lineups don't change.
State lives in <state_dir>/state.json.
"""
import datetime as dt
import os
import subprocess
import sys
import time
import traceback

from . import actions
from . import claude_hook
from . import gm
from .config import REPO_ROOT
from .espn import AuthError, EspnError, scrub
from .league import TERMINAL_TRADE, LeagueView, trade_outcomes, trade_status
from .players import OUT, INJ_SHORT, STARTING, Projections, position
from .state import FileLock, load_state, save_state

MIN = 60_000
HOUR = 60 * MIN
DAY = 24 * HOUR
DROP_LOOKBACK = 24 * HOUR
WATCH_TTL = 4 * DAY
WATCH_RECHECK = 30 * MIN
CHAT_FIRST_LOOKBACK = 2 * HOUR
ALERT_EVERY = 6 * HOUR


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- pure detectors (unit tested)


def find_drops(transactions, team_id, seen, since_ms):
    """New DROP items by other teams in EXECUTED transactions (FREEAGENT, WAIVER, ROSTER, TRADE_ACCEPT, ...)."""
    out, keys = [], set()
    for t in transactions:
        if t.get("status") != "EXECUTED":
            continue
        for it in t.get("items") or []:
            if it.get("type") != "DROP":
                continue
            frm = it.get("fromTeamId", t.get("teamId"))
            if frm == team_id:
                continue
            when = t.get("processDate") or t.get("proposedDate") or 0
            key = f"{t.get('id')}:{it.get('playerId')}"
            if when < since_ms or key in seen or key in keys:
                continue
            keys.add(key)
            out.append({"key": key, "player_id": it["playerId"], "team_id": frm, "when": when, "tx_type": t.get("type")})
    return out


def find_offers(transactions, team_id):
    """TRADE_PROPOSALs involving team_id, keyed by transaction id (as str), with the status their answers give them."""
    out, outcomes = {}, trade_outcomes(transactions)
    for t in transactions:
        if t.get("type") != "TRADE_PROPOSAL" or t.get("relatedTransactionId"):
            continue
        it = t.get("items") or []
        if not any(i.get("fromTeamId") == team_id or i.get("toTeamId") == team_id for i in it):
            continue
        partner = next((x for x in (i.get("toTeamId") if i.get("fromTeamId") == team_id else i.get("fromTeamId")
                                    for i in it if i.get("type") != "DROP") if x and x != team_id), None)
        out[str(t.get("id"))] = {
            "status": trade_status(t, outcomes), "mine": t.get("teamId") == team_id, "partner": partner,
            "expires": t.get("expirationDate"), "sp": t.get("scoringPeriodId") or t.get("_sp"),
            "give": [i["playerId"] for i in it if i.get("fromTeamId") == team_id and i.get("type") != "DROP"],
            "get": [i["playerId"] for i in it if i.get("toTeamId") == team_id],
            # uneven trades name a drop (type DROP, toTeamId 0) for each side that ends up over the roster limit
            "drop": [i["playerId"] for i in it if i.get("fromTeamId") == team_id and i.get("type") == "DROP"],
        }
    return out


def diff_offers(known, current, now_ms):
    """-> (received, updates). received: new PENDING offers to Ethan. updates: (id, old, new) status changes."""
    received, updates = [], []
    for oid, o in current.items():
        k = known.get(oid)
        if k is None:
            if not o["mine"] and o["status"] == "PENDING" and not (o["expires"] and o["expires"] < now_ms):
                received.append((oid, o))
        elif k.get("status") != o["status"]:
            updates.append((oid, k.get("status"), o))
    return received, updates


def find_messages(topics, me_bare, since_ms):
    """Chat messages newer than since_ms that are not Ethan's and not ESPN system posts."""
    out = []
    for tp in topics or []:
        if not str(tp.get("type", "")).startswith("CHAT"):
            continue
        for m in tp.get("messages") or []:
            if not m.get("content") or m.get("isDeleted") or (m.get("date") or 0) <= since_ms:
                continue
            a = m.get("author") or ""
            if a == "LM" or a.strip("{}").upper() == me_bare:
                continue
            out.append({"thread": "league" if tp.get("type") == "CHAT_ALL_MEMBERS" else "dm",
                        "topic": tp.get("id"), "author": a, "date": m["date"], "content": m["content"]})
    return sorted(out, key=lambda m: m["date"])


def latest_message(topics):
    return max((m.get("date") or 0 for tp in topics or [] for m in tp.get("messages") or []), default=0)


def roster_map(entries):
    out = {}
    for e in entries:
        p = e["playerPoolEntry"]["player"]
        out[str(p["id"])] = {"n": p.get("fullName", ""), "inj": p.get("injuryStatus") or "ACTIVE",
                             "slot": e.get("lineupSlotId"), "pos": position(p)}
    return out


def diff_roster(prev, cur):
    added = [cur[k]["n"] for k in cur if k not in prev]
    removed = [prev[k]["n"] for k in prev if k not in cur]
    inj = [(cur[k]["n"], prev[k]["inj"], cur[k]["inj"]) for k in cur if k in prev and prev[k]["inj"] != cur[k]["inj"]]
    return added, removed, inj


def update_watched(watched, players, now_ms):
    """Apply fresh kona_player_info entries to the watch list. Returns ids that went WAIVERS -> FREEAGENT."""
    clears = []
    for x in players:
        pid = str(x.get("id"))
        w = watched.get(pid)
        if w is None:
            continue
        new = x.get("status")
        if x.get("player", {}).get("fullName"):
            w["n"] = x["player"]["fullName"]
        if w.get("status") == "WAIVERS" and new == "FREEAGENT":
            clears.append(int(pid))
        if new in ("FREEAGENT", "WAIVERS") or w.get("manual"):
            w["status"] = new
            w["wpd"] = x.get("waiverProcessDate")
        else:
            del watched[pid]  # claimed / back on a roster
    for pid in [k for k, w in watched.items() if not w.get("manual") and now_ms - w.get("since", now_ms) > WATCH_TTL]:
        del watched[pid]
    return clears


def watch_poll_due(watched, last_poll, now_ms):
    if not watched:
        return False
    if any(w.get("status") == "WAIVERS" and w.get("wpd") and w["wpd"] <= now_ms for w in watched.values()):
        return True
    return now_ms - (last_poll or 0) >= WATCH_RECHECK


def lineup_due(first_game, lead_min, daily, dirty, now_ms):
    if not first_game or now_ms < first_game - lead_min * MIN:
        return False
    return not daily.get("lineup") or dirty


# ---------------------------------------------------------------- the run


def _fmt_time(ms, tz):
    return dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%a %H:%M") if ms else "?"


def refresh_projections(cfg, runner=subprocess.run):
    """Run model/project.py (pulls this season's NHL stats, rewrites proj.json). Needs the model data dir."""
    script = REPO_ROOT / "model" / "project.py"
    if not (cfg.model_data_dir / "model.json").exists():
        return False, f"{cfg.model_data_dir}/model.json missing (see README: rebuilding the model data)"
    env = dict(os.environ, FHOCKEY_MODEL_DATA=str(cfg.model_data_dir), FHOCKEY_PROJ_DIR=str(cfg.proj_path.parent))
    try:
        r = runner([sys.executable, "-I", str(script)], cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=900,
                   env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, type(e).__name__
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()[-1:] or [""]
    return r.returncode == 0, tail[0][:200]


class Watcher:
    def __init__(self, cfg, client, notifier, log, now=None, spawn=subprocess.Popen, runner=subprocess.run):
        self.cfg = cfg
        self.client = client
        self.notifier = notifier
        self.log = log
        self.now = now if now is not None else now_ms()
        self.spawn = spawn
        self.runner = runner
        self.tz = cfg.tz()
        self.summary = []

    def note(self, kind, msg, priority="default", push=True):
        self.summary.append(f"[{kind}] {msg}".replace("\n", " / "))
        self.log.info("[%s] %s", kind, msg.replace("\n", " / "))
        if push:
            self.notifier.send(kind, msg, priority=priority)

    def run(self):
        with FileLock(self.cfg.state_dir / "watch.lock") as lk:
            if not lk.acquired:
                return 0
            st = load_state(self.cfg)
            try:
                code = self._run(st)
                st["fail_count"] = 0
                return code
            except AuthError as e:
                if self.now - st.get("auth_alert", 0) > ALERT_EVERY:
                    st["auth_alert"] = self.now
                    self.note("auth", str(e), priority="high")
                else:
                    self.log.warning("%s", e)
                return 2
            except EspnError as e:
                st["fail_count"] = st.get("fail_count", 0) + 1
                self.log.warning("poll failed (%d in a row): %s", st["fail_count"], e)
                if st["fail_count"] == 15:
                    self.note("error", f"ESPN unreachable for ~30 min: {e}", priority="high")
                return 1
            except Exception as e:  # keep the agent alive; report once per 6 h
                self.log.error("watch crashed: %s", scrub(traceback.format_exc()))
                if self.now - st.get("crash_alert", 0) > ALERT_EVERY:
                    st["crash_alert"] = self.now
                    self.note("error", f"watcher error: {type(e).__name__}: {scrub(str(e))[:150]}", priority="high")
                return 1
            finally:
                save_state(self.cfg, st)

    def _run(self, st):
        cfg, now = self.cfg, self.now
        team = cfg.team_id
        execute = bool(cfg["auto_execute"])
        first = not st.get("initialized")
        view = LeagueView(self.client, cfg, now, st).load()
        sp = view.cur
        daily = st.get("daily") or {}
        if daily.get("sp") != sp:
            daily = {"sp": sp}
        st["daily"] = daily
        local = dt.datetime.fromtimestamp(now / 1000, self.tz)
        if first:
            self.note("start", f"fantasy watcher started ({'LIVE: will write to ESPN' if execute else 'dry run'})")

        # 1. Ethan's roster: injuries and roster changes
        gm_on = bool(cfg["gm"]["enabled"])
        cur_roster = roster_map(view.roster_entries)
        prev = st.get("roster")
        if prev is not None:
            if gm_on:
                for k, r in cur_roster.items():
                    if k in prev and r["inj"] in OUT and prev[k]["inj"] not in OUT and r["slot"] in STARTING:
                        gm.note_event(st, sp, f"{r['n']} is out")
            added, removed, inj = diff_roster(prev, cur_roster)
            if inj:
                self.note("injury", "; ".join(f"{n}: {INJ_SHORT.get(a, a)} -> {INJ_SHORT.get(b, b)}" for n, a, b in inj),
                          priority="high" if any(b in OUT for _, _, b in inj) else "default")
            if added or removed:
                self.note("roster", " ".join([f"+{n}" for n in added] + [f"-{n}" for n in removed]), priority="low")
            if inj or added or removed:
                st["lineup_dirty"] = True
        st["roster"] = cur_roster

        # 2. transactions: today + yesterday, plus the periods of offers still being tracked
        offers_known = st.setdefault("offers", {})
        periods = {sp, sp - 1} | {int(o["sp"]) for o in offers_known.values()
                                  if o.get("sp") and o.get("status") not in TERMINAL_TRADE}
        if first:
            periods |= set(range(sp - 3, sp + 1))
        txs = [t for p in sorted(x for x in periods if x >= 1) for t in view.tx(p)]

        # 2a. drops by other teams
        seen = st.setdefault("seen_drops", {})
        drops = find_drops(txs, team, seen, now - DROP_LOOKBACK)
        for d in drops:
            seen[d["key"]] = now
        for k in [k for k, ts in seen.items() if now - ts > 3 * DAY]:
            del seen[k]
        watched = st.setdefault("watched", {})
        for pid in cfg["watch_player_ids"]:
            watched.setdefault(str(pid), {"n": str(pid), "status": "UNKNOWN", "since": now, "manual": True})
        trigger, drop_lines = [], []
        if drops:
            info = {x["id"]: x for x in self.client.players_by_id([d["player_id"] for d in drops], sp).get("players") or []}
            for d in drops:
                x = info.get(d["player_id"], {})
                p = x.get("player") or {}
                status = x.get("status") or "?"
                name = p.get("fullName") or str(d["player_id"])
                when = f" until {_fmt_time(x.get('waiverProcessDate'), self.tz)}" if status == "WAIVERS" else ""
                drop_lines.append(f"{name} ({position(p) if p else '?'}, {view.team_name(d['team_id'])}) -> {status}{when}")
                if status in ("FREEAGENT", "WAIVERS"):
                    trigger.append(d["player_id"])
                    watched[str(d["player_id"])] = {"n": name, "status": status, "wpd": x.get("waiverProcessDate"),
                                                    "since": now}
                    st.setdefault("watch_poll", now)

        # 2b. watched players clearing waivers
        new_ids = {str(i) for i in trigger}
        to_poll = {k: w for k, w in watched.items() if k not in new_ids}
        clears = []
        if watch_poll_due(to_poll, st.get("watch_poll"), now):
            resp = self.client.players_by_id([int(k) for k in to_poll], sp).get("players") or []
            clears = update_watched(watched, resp, now)
            st["watch_poll"] = now
        else:
            update_watched(watched, [], now)  # expire old entries
        if clears:
            names = ", ".join(watched.get(str(c), {}).get("n", str(c)) for c in clears)
            self.note("waivers", f"cleared to free agency: {names}")
            trigger += clears
        if gm_on and trigger:
            gm.note_available(st, trigger)

        # 2c. trade offers
        current = find_offers(txs, team)
        received, updates = diff_offers(offers_known, current, now)
        if received or updates:
            ids = {pid for _, o in received for pid in o["give"] + o["get"]} | \
                  {pid for _, _, o in updates for pid in o["give"] + o["get"] + o.get("drop", [])}
            names = {x["id"]: (x.get("player") or {}).get("fullName", str(x["id"]))
                     for x in self.client.players_by_id(sorted(ids), sp).get("players") or []}
            nm = lambda ids_: " + ".join(names.get(i, str(i)) for i in ids_) or "nothing"
            for oid, o in received:
                msg = (f"trade offer from {view.team_name(o['partner'])}: you give {nm(o['give'])}, you get {nm(o['get'])}"
                       f" (expires {_fmt_time(o['expires'], self.tz)})")
                self.note("trade", msg, priority="high")
                claude_hook.write_event(cfg, {"type": "trade_offer", "id": oid, "summary": msg, "offer": o,
                                              "partner_team_id": o["partner"], "give_names": [names.get(i) for i in o["give"]],
                                              "get_names": [names.get(i) for i in o["get"]], "detected": now})
            for oid, old, o in updates:
                who = "your offer to" if o["mine"] else "offer from"
                dropped = f", you drop {nm(o['drop'])}" if o.get("drop") else ""
                self.note("trade", f"{who} {view.team_name(o['partner'])} ({nm(o['give'])} for {nm(o['get'])}{dropped}): "
                                   f"{old} -> {o['status']}")
                if o["status"] in ("EXECUTED", "ACCEPTED"):
                    st["lineup_dirty"] = True
        for oid, o in current.items():
            offers_known[oid] = dict(o, seen=offers_known.get(oid, {}).get("seen", now))
        for oid in [k for k, o in offers_known.items()
                    if o.get("status") in TERMINAL_TRADE and now - o.get("seen", now) > 7 * DAY]:
            del offers_known[oid]

        # 3. chat
        comm = self.client.communication(dm_limit=10, league_limit=3)
        topics = comm.get("topics") or []
        since = st.get("chat_last_ts") or (now - CHAT_FIRST_LOOKBACK)
        msgs = find_messages(topics, self.client.auth.bare_id, since)
        st["chat_last_ts"] = max(since, latest_message(topics))
        if msgs:
            def who(a):
                return view.member_name(a) or f"team {view.member_team(a) or '?'}"
            text = "; ".join(f"{'DM' if m['thread'] == 'dm' else 'league'} {who(m['author'])}: {m['content'][:120]}"
                             for m in msgs[-3:])
            self.note("chat", text + (f" (+{len(msgs) - 3} more)" if len(msgs) > 3 else ""))
            claude_hook.write_event(cfg, {"type": "chat", "id": str(msgs[-1]["date"]), "detected": now,
                                          "messages": [dict(m, author_name=who(m["author"]),
                                                            author_team=view.member_team(m["author"])) for m in msgs]})

        # 4. daily projection refresh (before any decision that uses them)
        if cfg["refresh_projections"] and st.get("proj_refreshed") != local.date().isoformat() \
                and local.hour >= max(0, cfg["morning_hour"] - 1):
            st["proj_refreshed"] = local.date().isoformat()
            ok, msg = refresh_projections(cfg, self.runner)
            self.log.info("projections refresh: %s %s", "ok" if ok else "FAILED", msg)
            if not ok:
                self.note("model", f"projection refresh failed, using old values: {msg}", priority="low")
            view.proj = Projections.load(cfg.proj_path)

        # 5a. moves approved ahead of time (config "planned_moves")
        roster_changed = False
        if cfg["planned_moves"]:
            res = actions.run_planned(view, execute, cfg["planned_moves"], st)
            sent = daily.setdefault("planned_msgs", [])
            for line in res["lines"]:
                if line not in sent:
                    sent.append(line)
                    self.note("pickup", line, priority="high" if res["executed"] or "FAILED" in line else "low",
                              push="waiting" not in line)
            if res["executed"]:
                roster_changed = True
                daily["fa_planned"] = True
                view = LeagueView(self.client, cfg, now, st, proj=view.proj, schedule=view.schedule).load()

        # 5b. pickups: right after a drop / waiver clear, and once each morning. With the GM on, Opus decides (step 8).
        morning_due = local.hour >= cfg["morning_hour"] and not daily.get("morning")
        if gm_on:
            if drop_lines and cfg["notify_all_drops"]:
                self.note("drop", "dropped: " + "; ".join(drop_lines))
            elif drop_lines:
                self.log.info("dropped: %s", "; ".join(drop_lines))
        elif trigger or morning_due:
            extra = sorted(set(trigger) | {int(k) for k in watched})
            res = actions.run_pickups(view, execute, extra, st)
            sent = daily.setdefault("pickup_msgs", [])
            move_lines = [l for l in res["lines"] if l.startswith("pickup")]
            fresh = [l for l in move_lines if l not in sent]
            sent.extend(fresh)
            if drop_lines and cfg["notify_all_drops"] or fresh:
                body = ("dropped: " + "; ".join(drop_lines) + "\n" if drop_lines else "") + \
                       "\n".join(fresh or [l for l in res["lines"] if l.startswith("no move")])
                self.note("pickup" if fresh else "drop", body.strip(), priority="high" if res["executed"] else "default")
            else:
                self.log.info("pickups: %s", " | ".join(res["lines"]))
            if any(m["kind"] == "FREEAGENT" for m in res["executed"]):
                roster_changed = True
            if any(m["kind"] == "FREEAGENT" for m in res["moves"]):
                daily["fa_planned"] = True  # a pickup replaces today's stream (also in dry run)
        elif drop_lines:
            self.log.info("dropped: %s", "; ".join(drop_lines))

        if roster_changed:
            st["lineup_dirty"] = True
            view = LeagueView(self.client, cfg, now, st, proj=view.proj, schedule=view.schedule).load()

        # 6. streaming: each morning and ~60 min before the first game
        first_game = view.schedule.first_game(sp)
        scfg = cfg["stream"]
        pregame_due = bool(first_game) and now >= first_game - scfg["pregame_minutes"] * MIN and not daily.get("pregame")
        force_lineup = roster_changed
        if gm_on:
            pass  # step 8 decides, and marks morning and pregame only once a run has really started
        elif scfg["enabled"] and (morning_due or pregame_due) and daily.get("fa_planned"):
            self.log.info("stream: no move (today's free-agent add already used by a pickup)")
        elif scfg["enabled"] and (morning_due or pregame_due):
            r = actions.run_stream(view, execute, st)
            if r["ok"]:
                daily["fa_planned"] = True
                if r["line"] not in daily.get("stream_msgs", []):
                    daily.setdefault("stream_msgs", []).append(r["line"])
                    self.note("stream", r["line"], priority="high" if r["executed"] else "default")
            elif "FAILED" in r["line"]:
                self.note("stream", r["line"], priority="high")
            else:
                self.log.info("%s", r["line"])
            if r["executed"]:
                st["lineup_dirty"] = True
                force_lineup = True
                view = LeagueView(self.client, cfg, now, st, proj=view.proj, schedule=view.schedule).load()
        if morning_due and not gm_on:
            daily["morning"] = now
        if pregame_due and not gm_on:
            daily["pregame"] = now

        # 7. lineup: 30 min before the first game, after roster changes, and right after a stream add
        if force_lineup or lineup_due(first_game, cfg["lineup_lead_minutes"], daily, st.get("lineup_dirty"), now):
            r = actions.run_lineup(view, execute)
            if first_game and now >= first_game - cfg["lineup_lead_minutes"] * MIN:
                daily["lineup"] = now
            st["lineup_dirty"] = False
            if r["moves"] or not r["ok"]:
                self.note("lineup", " | ".join(r["lines"][:1] + r["lines"][-1:]) if r["moves"] else r["lines"][0],
                          priority="high" if not r["ok"] else "low")
            else:
                self.log.info("lineup: %s", r["lines"][0])

        # 8. Opus GM (opt-in): morning, a retry before the first game if that failed, and events worth waking it for
        if gm_on:
            result = gm.watch_step(cfg, st, view, daily, now, morning_due, pregame_due, self.spawn, self.log)
            if result != "nothing due":
                self.log.info("gm: %s", result)
            if result == "claude binary not found" and not daily.get("gm_no_claude"):
                daily["gm_no_claude"] = True
                self.note("gm", "the Opus GM can't run: claude binary not found. No adds or drops until that is fixed; "
                                "lineups continue.", priority="high")

        # 9. Claude hook (opt-in)
        reason = claude_hook.maybe_dispatch(cfg, st, now, self.spawn, self.log)
        if reason not in ("disabled", "inbox empty"):
            self.log.info("claude hook: %s", reason)

        st["initialized"] = True
        st["last_ok"] = now
        st["requests_last_run"] = self.client.request_count
        if self.summary:
            print(scrub(f"{dt.datetime.fromtimestamp(now / 1000, self.tz):%Y-%m-%d %H:%M} " + " | ".join(self.summary)))
        return 0
