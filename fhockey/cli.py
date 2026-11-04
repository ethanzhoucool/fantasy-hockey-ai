"""python3 -m fhockey <command>. Run with no arguments for the list."""
import argparse
import builtins
import datetime as dt
import getpass as _getpass
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import actions, claude_hook, launchd, tools
from .config import Config, ConfigError
from .espn import AuthError, EspnClient, EspnError, load_auth, save_auth, scrub
from .espn import urlopen as espn_urlopen
from .league import LeagueView
from .notify import Notifier, get_logger
from .players import INJ_SHORT, SLOT_NAME
from .state import load_state, save_state
from .watch import Watcher, now_ms

COOKIE_HELP = """Copy two cookies from Chrome while signed in to ESPN Fantasy:
  DevTools (Cmd+Opt+I) > Application > Storage > Cookies > https://fantasy.espn.com
  SWID     looks like {XXXXXXXX-XXXX-...}
  espn_s2  a long string
Input is hidden. The values are saved to {path} (mode 600) and never printed."""


class Deps:
    """Injection points for tests; defaults are the real thing."""

    def __init__(self, transport=None, getpass=None, input_fn=None, now=None, runner=None, spawn=None, opener=None,
                 cfg=None):
        self.transport = transport
        self.getpass = getpass or _getpass.getpass
        self.input = input_fn or builtins.input
        self.now = now
        self.runner = runner or subprocess.run
        self.spawn = spawn or subprocess.Popen
        self.opener = opener or espn_urlopen
        self.cfg = cfg


def _client(cfg, deps, writes=False):
    return EspnClient(cfg, load_auth(cfg.auth_file), transport=deps.transport, writes_enabled=writes)


def _now(deps):
    return deps.now if deps.now is not None else now_ms()


def _mode(execute):
    return "LIVE" if execute else "dry run"


# ---------------------------------------------------------------- commands


def cmd_setup(cfg, args, deps):
    for key, flag, label in (("league_id", args.league, "ESPN league id"), ("team_id", args.team, "your team id"),
                             ("season", args.season, "season (e.g. 2027 for 2026-27)")):
        if flag is not None:
            cfg.set(key, int(flag))
        elif not cfg.user_data.get(key) and not (key == "season" and cfg["season"]):
            cfg.set(key, int(deps.input(f"{label}: ").strip()))
    cfg.save()
    print(COOKIE_HELP.replace("{path}", str(cfg.auth_file)))
    swid = deps.getpass("SWID: ")
    s2 = deps.getpass("espn_s2: ")
    save_auth(cfg.auth_file, swid, s2)
    try:
        r = _client(cfg, deps).teams()
        ok = any(t.get("id") == cfg.team_id for t in r.get("teams") or [])
        if not ok:
            get_logger(cfg).warning("setup: team %s not found in league %s", cfg.team_id, cfg["league_id"])
    except EspnError as e:
        get_logger(cfg).warning("setup validation failed: %s", e)
        ok = False
    print("ok" if ok else "failed")
    return 0 if ok else 1


def cmd_status(cfg, args, deps):
    client = _client(cfg, deps)
    now = _now(deps)
    st = load_state(cfg)
    view = LeagueView(client, cfg, now, st).load()
    me = view.my_team
    budget = view.budget()
    print(f"{view.team_name(cfg.team_id)} | league {cfg['league_id']} team {cfg.team_id} | period {view.cur}, "
          f"matchup {view.mp} | mode: {_mode(cfg['auto_execute'])}")
    try:
        m = client.matchup()
        g = next(x for x in m.get("schedule") or [] if x.get("matchupPeriodId") == view.mp
                 and cfg.team_id in ((x.get("home") or {}).get("teamId"), (x.get("away") or {}).get("teamId")))
        mine, opp = (g["home"], g["away"]) if g["home"]["teamId"] == cfg.team_id else (g["away"], g["home"])
        pts = lambda s: s.get("totalPointsLive", s.get("totalPoints", 0)) or 0
        print(f"matchup: me {pts(mine):.1f} vs {view.team_name(opp['teamId'])} {pts(opp):.1f}")
    except (StopIteration, KeyError, EspnError):
        print("matchup: n/a")
    print(f"adds this week {view.weekly_used()} + {len(budget.pending_claims)} pending claim(s) "
          f"/ {cfg['rules']['weekly_add_limit']} | adds today: {budget.fa_adds_this_period}"
          f" | days left in matchup week: {view.days_left_in_week()}")
    first = view.schedule.first_game(view.cur)
    if first:
        print(f"first game today: {dt.datetime.fromtimestamp(first / 1000, cfg.tz()):%H:%M}")
    print(f"projections: {len(view.proj)} players, dated {view.proj.date or '?'}")
    for p in sorted(view.roster(), key=lambda p: (list(SLOT_NAME).index(p["slot"]) if p["slot"] in SLOT_NAME else 9, -p["proj"])):
        game = dt.datetime.fromtimestamp(p["gt"] / 1000, cfg.tz()).strftime("%H:%M") if p["gt"] else "-"
        print(f"  {SLOT_NAME.get(p['slot'], p['slot']):4} {p['pos']} {p['n']:<26} {p['proj']:.2f} "
              f"{INJ_SHORT.get(p['inj'], ''):4} game {game}")
    trades = view.pending_trades()
    if trades:
        print(f"pending trades involving you: {len(trades)} (python3 -m fhockey offers)")
    return 0


def _dry_hint(cfg, args):
    if not cfg["auto_execute"] and not args.dry_run:
        print(f"(auto_execute is off in {cfg.config_file}: nothing was written to ESPN)")


def cmd_lineup(cfg, args, deps):
    execute = bool(cfg["auto_execute"]) and not args.dry_run
    view = LeagueView(_client(cfg, deps, execute), cfg, _now(deps), load_state(cfg)).load()
    r = actions.run_lineup(view, execute, args.sp)
    print("\n".join(r["lines"]))
    _dry_hint(cfg, args)
    return 0 if r["ok"] else 1


def cmd_pickups(cfg, args, deps):
    execute = bool(cfg["auto_execute"]) and not args.dry_run
    st = load_state(cfg)
    view = LeagueView(_client(cfg, deps, execute), cfg, _now(deps), st).load()
    extra = [int(k) for k in (st.get("watched") or {})]
    r = actions.run_pickups(view, execute, extra, st)
    if args.verbose:
        for c in r["candidates"][:10]:
            print(f"  candidate {c['st']:9} +{c['add']['n']} -{c['drop']['n']} edge {c['edge']:+.2f} P {c['p']:.2f}")
    print("\n".join(r["lines"]))
    _dry_hint(cfg, args)
    if r["executed"]:
        save_state(cfg, st)
        _after_roster_change(cfg, deps, st, execute)
    return 0


def cmd_stream(cfg, args, deps):
    execute = bool(cfg["auto_execute"]) and not args.dry_run
    st = load_state(cfg)
    view = LeagueView(_client(cfg, deps, execute), cfg, _now(deps), st).load()
    r = actions.run_stream(view, execute, st)
    print(r["line"])
    _dry_hint(cfg, args)
    if r["executed"]:
        save_state(cfg, st)
        _after_roster_change(cfg, deps, st, execute)
    return 0


def _after_roster_change(cfg, deps, st, execute):
    view = LeagueView(_client(cfg, deps, execute), cfg, _now(deps), st).load()
    r = actions.run_lineup(view, execute)
    print("lineup after the add: " + r["lines"][0])


def cmd_watch(cfg, args, deps):
    log = get_logger(cfg)
    notifier = Notifier(cfg, runner=deps.runner, opener=deps.opener)
    while True:
        try:
            client = _client(cfg, deps, bool(cfg["auto_execute"]))
        except AuthError as e:
            st = load_state(cfg)
            if _now(deps) - st.get("auth_alert", 0) > 6 * 3600 * 1000:
                st["auth_alert"] = _now(deps)
                save_state(cfg, st)
                notifier.send("auth", str(e), priority="high")
            log.warning("%s", e)
            code = 2
        else:
            code = Watcher(cfg, client, notifier, log, now=deps.now, spawn=deps.spawn, runner=deps.runner).run()
        if args.once:
            return code
        time.sleep(int(cfg["poll_interval_seconds"]))


def cmd_install(cfg, args, deps):
    ok, msg = launchd.install(cfg, runner=deps.runner, load=not args.no_load)
    print(("installed: " if ok else "") + msg)
    if ok:
        print(f"polls every {cfg['poll_interval_seconds']}s in {_mode(cfg['auto_execute'])} mode; logs in {cfg.log_dir}")
    return 0 if ok else 1


def cmd_uninstall(cfg, args, deps):
    existed, path = launchd.uninstall(cfg, runner=deps.runner)
    print(("removed " if existed else "not installed: ") + path)
    return 0


def cmd_snapshot(cfg, args, deps):
    client = _client(cfg, deps)
    text, sp = tools.build_snapshot(client, fa_limit=args.fa)
    out = Path(args.out) if args.out else cfg.trades_dir / f"league_{dt.date.today().isoformat()}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"wrote {out} ({text.count(chr(10)) - 2} players, period {sp})")
    return 0


def cmd_offers(cfg, args, deps):
    text, _ = tools.offers_report(_client(cfg, deps), cfg.team_id, _now(deps))
    print(text)
    return 0


def cmd_trade_input(cfg, args, deps):
    split = lambda s: [x.strip() for x in (s or "").split(",") if x.strip()]
    text = tools.trade_input(_client(cfg, deps), cfg.team_id, _now(deps), offer_id=args.offer, partner=args.partner,
                             give=split(args.give), get=split(args.get), drops=split(args.drop))
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}; next: python3 -I model/trade_eval.py {args.out}")
    else:
        print(text, end="")
    return 0


def cmd_chat(cfg, args, deps):
    client = _client(cfg, deps)
    now = _now(deps)
    view = LeagueView(client, cfg, now).load()
    print(tools.chat_report(client, view, now - int(args.hours * 3600 * 1000), client.auth.bare_id))
    return 0


def cmd_inbox(cfg, args, deps):
    files = claude_hook.pending(cfg)
    print(f"{len(files)} event(s) in {cfg.inbox_dir}")
    for f in files:
        try:
            e = json.loads(f.read_text())
            print(f"  {f.name}: {e.get('summary') or e.get('type')}")
        except ValueError:
            print(f"  {f.name}: unreadable")
    return 0


def cmd_claude_run(cfg, args, deps):
    return claude_hook.run(cfg, Notifier(cfg, runner=deps.runner, opener=deps.opener), runner=deps.runner)


def build_parser():
    ap = argparse.ArgumentParser(prog="python3 -m fhockey", description="ESPN fantasy hockey manager")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("setup", help="save league ids and ESPN cookies, check them with one read")
    s.add_argument("--league", type=int)
    s.add_argument("--team", type=int)
    s.add_argument("--season", type=int)
    sub.add_parser("status", help="period, matchup score, adds used, roster")
    for name, h in (("lineup", "set today's optimal lineup"), ("pickups", "apply the pickup rules"),
                    ("stream", "fill an empty slot with a free agent who plays today")):
        p = sub.add_parser(name, help=h)
        p.add_argument("--dry-run", action="store_true", help="never write, even if auto_execute is on")
        if name == "lineup":
            p.add_argument("--sp", type=int, help="scoring period (future periods use FUTURE_ROSTER)")
        if name == "pickups":
            p.add_argument("-v", "--verbose", action="store_true")
    w = sub.add_parser("watch", help="poll ESPN once (or forever without --once)")
    w.add_argument("--once", action="store_true")
    i = sub.add_parser("install", help="install the launchd agent (every 2 min)")
    i.add_argument("--no-load", action="store_true", help="write the plist but do not load it")
    sub.add_parser("uninstall", help="remove the launchd agent")
    sn = sub.add_parser("snapshot", help="write the league file trades/trade_scan.py reads")
    sn.add_argument("--out")
    sn.add_argument("--fa", type=int, default=60)
    sub.add_parser("offers", help="pending trade offers with player names")
    t = sub.add_parser("trade-input", help="build model/trade_eval.py input")
    t.add_argument("--offer")
    t.add_argument("--partner", type=int)
    t.add_argument("--give", help="comma-separated names you send")
    t.add_argument("--get", help="comma-separated names you receive")
    t.add_argument("--drop", help="comma-separated names you drop to make roster room (uneven trades)")
    t.add_argument("--out")
    c = sub.add_parser("chat", help="recent chat threads")
    c.add_argument("--hours", type=float, default=24)
    sub.add_parser("inbox", help="events waiting for judgment")
    sub.add_parser("claude-run", help="run the Claude hook once over the inbox (watch starts this)")
    return ap


COMMANDS = {
    "setup": cmd_setup, "status": cmd_status, "lineup": cmd_lineup, "pickups": cmd_pickups, "stream": cmd_stream,
    "watch": cmd_watch, "install": cmd_install, "uninstall": cmd_uninstall, "snapshot": cmd_snapshot,
    "offers": cmd_offers, "trade-input": cmd_trade_input, "chat": cmd_chat, "inbox": cmd_inbox,
    "claude-run": cmd_claude_run,
}


def main(argv=None, deps=None):
    deps = deps or Deps()
    ap = build_parser()
    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 0
    try:
        cfg = deps.cfg or Config.load()
        if args.cmd not in ("setup", "install", "uninstall", "inbox"):
            cfg.require_league()
        return COMMANDS[args.cmd](cfg, args, deps)
    except AuthError as e:
        print(str(e), file=sys.stderr)
        if args.cmd not in ("setup", "watch"):
            try:
                Notifier(cfg, runner=deps.runner, opener=deps.opener).send("auth", str(e), priority="high")
            except Exception:
                pass
        return 2
    except (ConfigError, EspnError, ValueError) as e:
        print(scrub(f"error: {e}"), file=sys.stderr)
        return 1
