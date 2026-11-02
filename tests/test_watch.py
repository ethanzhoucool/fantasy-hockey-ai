import contextlib
import io
import json
import subprocess
import unittest

from fhockey import claude_hook
from fhockey.notify import Notifier, get_logger
from fhockey.state import FileLock, load_state
from fhockey.watch import Watcher, diff_offers, find_drops, find_messages, find_offers, update_watched

from .helpers import FIX, HOUR, MIN, NOW, Env, fixture

WPD = NOW + 12 * HOUR  # Xavi Xray's waiverProcessDate in the fixture (Nov 3 08:00Z)


def run_watch(env, now, spawn=None, runner=None):
    n = Notifier(env.cfg)
    spawned = []
    w = Watcher(env.cfg, env.client(writes=bool(env.cfg["auto_execute"])), n, get_logger(env.cfg), now=now,
                spawn=spawn or (lambda *a, **k: spawned.append((a, k))), runner=runner or subprocess.run)
    with contextlib.redirect_stdout(io.StringIO()):
        code = w.run()
    return code, n.sent, spawned


def kinds(sent):
    return [k for k, _ in sent]


def text(sent, kind):
    return "\n".join(m for k, m in sent if k == kind)


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.txs = fixture("transactions")["34"]["transactions"] + fixture("transactions")["33"]["transactions"]

    def test_find_drops(self):
        d = find_drops(self.txs, 4, {}, NOW - 24 * HOUR)
        self.assertEqual([(x["player_id"], x["team_id"], x["tx_type"]) for x in d], [(1402, 2, "FREEAGENT")])
        # own drops (9999), drops older than the lookback (3002) and non-drop items are ignored
        self.assertEqual(find_drops(self.txs, 4, {d[0]["key"]: NOW}, NOW - 24 * HOUR), [])

    def test_drops_from_any_transaction_type_but_only_executed(self):
        txs = [{"id": t, "type": t, "status": "EXECUTED", "teamId": 3, "processDate": NOW,
                "items": [{"playerId": i, "type": "DROP", "fromTeamId": 3}]}
               for i, t in enumerate(("FREEAGENT", "ROSTER", "WAIVER", "TRADE_ACCEPT"))]
        txs.append({"id": "p", "type": "WAIVER", "status": "PENDING", "teamId": 3, "processDate": NOW,
                    "items": [{"playerId": 9, "type": "DROP", "fromTeamId": 3}]})
        self.assertEqual([x["player_id"] for x in find_drops(txs, 4, {}, NOW - HOUR)], [0, 1, 2, 3])

    def test_offers(self):
        cur = find_offers(self.txs, 4)
        self.assertEqual(set(cur), {"tx-offer-in", "tx-offer-mine"})
        self.assertEqual((cur["tx-offer-in"]["partner"], cur["tx-offer-in"]["give"], cur["tx-offer-in"]["get"]),
                         (2, [1101], [2001]))
        received, updates = diff_offers({}, cur, NOW)
        self.assertEqual([oid for oid, _ in received], ["tx-offer-in"])  # my own new offer is not "received"
        changed = dict(cur, **{"tx-offer-mine": dict(cur["tx-offer-mine"], status="DECLINED")})
        _, updates = diff_offers(cur, changed, NOW)
        self.assertEqual([(u[0], u[1], u[2]["status"]) for u in updates], [("tx-offer-mine", "PENDING", "DECLINED")])
        expired = {"x": dict(cur["tx-offer-in"], expires=NOW - 1)}
        self.assertEqual(diff_offers({}, expired, NOW)[0], [])

    def test_messages(self):
        topics = fixture("communication")["topics"]
        m = find_messages(topics, "TEST-SWID-0004", NOW - 2 * HOUR)
        self.assertEqual([x["content"] for x in m], ["max for wolf?", "gg everyone"])  # not ESPN, own, deleted, old

    def test_update_watched(self):
        w = {"1": {"n": "a", "status": "WAIVERS", "since": NOW}, "2": {"n": "b", "status": "WAIVERS", "since": NOW},
             "3": {"n": "c", "status": "WAIVERS", "since": NOW - 5 * 24 * HOUR}}
        clears = update_watched(w, [{"id": 1, "status": "FREEAGENT"}, {"id": 2, "status": "ONTEAM"}], NOW)
        self.assertEqual(clears, [1])
        self.assertEqual(set(w), {"1"})  # 2 was claimed, 3 expired


class WatchRunTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_first_run_events(self):
        code, sent, _ = run_watch(self.env, NOW)
        self.assertEqual(code, 0)
        self.assertEqual(kinds(sent), ["start", "trade", "chat", "pickup"])
        self.assertIn("trade offer from Team Two: you give Max Mike, you get Will Wolf", text(sent, "trade"))
        self.assertEqual(text(sent, "chat"), "DM Blake Example: max for wolf?; league Casey Example: gg everyone")
        p = text(sent, "pickup")
        self.assertIn("dropped: Xavi Xray (F, Team Two) -> WAIVERS", p)
        self.assertIn("would claim +Xavi Xray (waivers) -Kai Kilo", p)
        self.assertIn("would add +Walt Whiskey -Jon Juliet", p)
        st = load_state(self.env.cfg)
        self.assertEqual(st["watched"]["1402"]["status"], "WAIVERS")
        self.assertEqual(st["watched"]["1402"]["wpd"], WPD)
        inbox = [json.loads(f.read_text()) for f in claude_hook.pending(self.env.cfg)]
        self.assertEqual(sorted(e["type"] for e in inbox), ["chat", "trade_offer"])
        self.assertEqual(self.env.espn.posts, [])
        # the morning stream is skipped: the pickup already uses today's free-agent add
        self.assertNotIn("stream", kinds(sent))

    def test_second_run_is_quiet_and_cheap(self):
        run_watch(self.env, NOW)
        before = len(self.env.espn.calls)
        code, sent, _ = run_watch(self.env, NOW + 2 * MIN)
        self.assertEqual((code, sent), (0, []))
        views = [c["views"] for c in self.env.espn.calls[before:]]
        self.assertEqual(len(views), 4, views)  # roster+status, 2x transactions, chat

    def test_new_drop_detected_once(self):
        run_watch(self.env, NOW)
        self.env.espn.tx(34).append({"id": "tx-new", "type": "ROSTER", "status": "EXECUTED", "teamId": 3,
                                     "processDate": NOW + MIN, "items": [{"playerId": 1408, "type": "DROP", "fromTeamId": 3}]})
        self.env.espn.pool_player(1408)["status"] = "WAIVERS"
        _, sent, _ = run_watch(self.env, NOW + 2 * MIN)
        self.assertEqual(kinds(sent), ["drop"])
        self.assertIn("Del Early (F, Team Three) -> WAIVERS", sent[0][1])
        _, sent, _ = run_watch(self.env, NOW + 4 * MIN)
        self.assertEqual(sent, [])

    def test_waiver_clear_triggers_free_agent_pickup(self):
        run_watch(self.env, NOW)
        x = self.env.espn.pool_player(1402)
        x["status"] = "FREEAGENT"
        x.pop("waiverProcessDate")
        _, sent, _ = run_watch(self.env, WPD + 5 * MIN)
        self.assertEqual(text(sent, "waivers"), "cleared to free agency: Xavi Xray")
        self.assertIn("would add +Xavi Xray -Kai Kilo", text(sent, "pickup"))
        self.assertEqual(load_state(self.env.cfg)["watched"]["1402"]["status"], "FREEAGENT")

    def test_watched_player_claimed_elsewhere_is_dropped_from_watch(self):
        run_watch(self.env, NOW)
        self.env.espn.d["players_pool"]["players"] = [x for x in self.env.espn.d["players_pool"]["players"]
                                                      if x["id"] != 1402]
        self.env.espn.d["league_all"]["teams"][2]["roster"]["entries"].append(
            {"playerId": 1402, "lineupSlotId": 7, "playerPoolEntry": {"id": 1402, "player": {"id": 1402, "fullName": "Xavi Xray"}}})
        _, sent, _ = run_watch(self.env, WPD + 5 * MIN)
        self.assertNotIn("waivers", kinds(sent))
        self.assertNotIn("1402", load_state(self.env.cfg)["watched"])

    def test_own_offer_status_change(self):
        run_watch(self.env, NOW)
        next(t for t in self.env.espn.tx(33) if t["id"] == "tx-offer-mine")["status"] = "ACCEPTED"
        _, sent, _ = run_watch(self.env, NOW + 2 * MIN)
        self.assertEqual(text(sent, "trade"), "your offer to Team Five (Ian India for Zane Bear): PENDING -> ACCEPTED")
        self.assertTrue(load_state(self.env.cfg)["lineup_dirty"])

    def test_lineup_timing_and_injury_rerun(self):
        _, sent, _ = run_watch(self.env, NOW)  # 20:00, first game 23:00
        self.assertNotIn("lineup", kinds(sent))
        _, sent, _ = run_watch(self.env, NOW + 2 * HOUR + 31 * MIN)  # 22:31
        self.assertIn("lineup", kinds(sent))
        self.assertIn("DRY RUN: 8 moves not saved", text(sent, "lineup"))
        _, sent, _ = run_watch(self.env, NOW + 2 * HOUR + 33 * MIN)
        self.assertNotIn("lineup", kinds(sent))
        self.env.espn.roster_player(1001)["playerPoolEntry"]["player"]["injuryStatus"] = "OUT"
        _, sent, _ = run_watch(self.env, NOW + 2 * HOUR + 35 * MIN)
        self.assertEqual(text(sent, "injury"), "Ari Alpha: ACTIVE -> OUT")
        self.assertIn("lineup", kinds(sent))

    def test_stream_runs_each_morning_when_add_is_free(self):
        self.env.cfg.data["rules"]["min_edge"] = 9  # no pickup passes, so the stream gets today's add
        _, sent, _ = run_watch(self.env, NOW)
        self.assertEqual(text(sent, "stream"), "stream (dry run): +Yan Yankee (DET, plays today) -Kai Kilo, adds this week 3/7")
        _, sent, _ = run_watch(self.env, NOW + 2 * HOUR + 1 * MIN)  # 22:01 = 1 h before first game: no repeat
        self.assertNotIn("stream", kinds(sent))

    def test_daily_projection_refresh(self):
        self.env.cfg.data["refresh_projections"] = True
        data = self.env.root / "modeldata"
        data.mkdir()
        (data / "model.json").write_text("{}")
        self.env.cfg.data["model_data_dir"] = str(data)
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout="wrote 700 projections\n", stderr="")

        run_watch(self.env, NOW, runner=runner)
        run_watch(self.env, NOW + 2 * MIN, runner=runner)  # once per day
        (cmd, kw), = calls
        self.assertTrue(cmd[-1].endswith("model/project.py"))
        self.assertEqual(kw["env"]["FHOCKEY_MODEL_DATA"], str(data))
        self.assertEqual(kw["env"]["FHOCKEY_PROJ_DIR"], str(FIX))

    def test_auth_error_notifies_once(self):
        self.env.espn.fail = (401, b'{"messages":["not authorized"]}')
        code, sent, _ = run_watch(self.env, NOW)
        self.assertEqual(code, 2)
        self.assertEqual(kinds(sent), ["auth"])
        self.assertIn("python3 -m fhockey setup", sent[0][1])
        code, sent, _ = run_watch(self.env, NOW + 2 * MIN)
        self.assertEqual((code, sent), (2, []))

    def test_outage_alerts_after_fifteen_failures(self):
        self.env.espn.fail = (503, b"")
        for i in range(14):
            code, sent, _ = run_watch(self.env, NOW + i * 2 * MIN)
            self.assertEqual((code, sent), (1, []))
        _, sent, _ = run_watch(self.env, NOW + 30 * MIN)
        self.assertEqual(kinds(sent), ["error"])

    def test_concurrent_run_skips(self):
        with FileLock(self.env.cfg.state_dir / "watch.lock") as lk:
            self.assertTrue(lk.acquired)
            code, sent, _ = run_watch(self.env, NOW)
        self.assertEqual((code, sent), (0, []))
        self.assertEqual(self.env.espn.calls, [])


class ClaudeHookTest(unittest.TestCase):
    def test_disabled_by_default(self):
        env = Env()
        try:
            _, _, spawned = run_watch(env, NOW)
            self.assertEqual(spawned, [])
            self.assertEqual(len(claude_hook.pending(env.cfg)), 2)  # events still land in the inbox
        finally:
            env.close()

    def test_rate_limit_and_lock(self):
        env = Env(claude_hook=True, claude_bin="/usr/bin/true", claude_max_per_day=2, claude_min_gap_minutes=30)
        try:
            _, _, spawned = run_watch(env, NOW)
            self.assertEqual(len(spawned), 1)
            args, kw = spawned[0]
            self.assertEqual(args[0][1:], ["-m", "fhockey", "claude-run"])
            self.assertTrue(kw["start_new_session"])
            st = load_state(env.cfg)
            self.assertEqual(claude_hook.maybe_dispatch(env.cfg, st, NOW + 10 * MIN), "rate limited (too soon)")
            with FileLock(claude_hook.lock_path(env.cfg)):
                self.assertEqual(claude_hook.maybe_dispatch(env.cfg, st, NOW + 40 * MIN), "busy (another claude run is active)")
            self.assertEqual(claude_hook.maybe_dispatch(env.cfg, st, NOW + 41 * MIN, spawn=lambda *a, **k: None), "started")
            self.assertEqual(claude_hook.maybe_dispatch(env.cfg, st, NOW + 90 * MIN), "rate limited (daily cap)")
            self.assertEqual(claude_hook.maybe_dispatch(env.cfg, st, NOW + 25 * HOUR, spawn=lambda *a, **k: None), "started")
        finally:
            env.close()

    def test_run_moves_events_and_pushes_answer(self):
        env = Env(claude_hook=True, claude_bin="/opt/claude")
        try:
            run_watch(env, NOW)
            calls = []

            def runner(cmd, **kw):
                calls.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, 0, stdout="decline the offer, counter with Oli Oscar", stderr="")

            n = Notifier(env.cfg)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(claude_hook.run(env.cfg, n, runner=runner), 0)
            (cmd, kw), = calls
            self.assertEqual(cmd[:2], ["/opt/claude", "-p"])
            self.assertIn("docs/TRADE_PLAYBOOK.md", cmd[2])
            self.assertIn("trade_offer", cmd[2])
            self.assertIn("--allowedTools", cmd)
            self.assertEqual(kw["timeout"], 20 * 60)
            self.assertEqual(claude_hook.pending(env.cfg), [])
            self.assertEqual(len(list((env.cfg.inbox_dir / "done").glob("*.json"))), 2)
            self.assertEqual(n.sent, [("claude", "decline the offer, counter with Oli Oscar")])
            # nothing left: a second run does not call claude
            with contextlib.redirect_stdout(io.StringIO()):
                claude_hook.run(env.cfg, n, runner=runner)
            self.assertEqual(len(calls), 1)
        finally:
            env.close()

    def test_run_refuses_when_another_is_active(self):
        env = Env(claude_hook=True, claude_bin="/opt/claude")
        try:
            run_watch(env, NOW)
            with FileLock(claude_hook.lock_path(env.cfg)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(claude_hook.run(env.cfg, Notifier(env.cfg), runner=lambda *a, **k: 1 / 0), 0)
            self.assertEqual(len(claude_hook.pending(env.cfg)), 2)
        finally:
            env.close()


if __name__ == "__main__":
    unittest.main()
