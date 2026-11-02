import datetime as dt
import json
import plistlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fhockey import launchd, tools
from fhockey.notify import Notifier
from fhockey.players import Projections, model_value, norm, stat

from .helpers import FIX, NOW, TEST_S2, Env, fixture


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_mac_banner_passes_text_as_arguments(self):
        calls = []
        self.env.cfg.data["mac_notifications"] = True
        n = Notifier(self.env.cfg, runner=lambda cmd, **kw: calls.append(cmd))
        msg = 'he said "hi" & do shell script "rm -rf ~"'
        with mock.patch("sys.platform", "darwin"):
            n.send("chat", msg)
        (cmd,) = calls
        self.assertEqual(cmd[0], "osascript")
        self.assertEqual(cmd[-2:], [msg, "Fantasy hockey"])
        self.assertNotIn(msg, " ".join(cmd[1:-2]))

    def test_ntfy_off_without_topic(self):
        opened = []
        Notifier(self.env.cfg, opener=lambda *a, **k: opened.append(a)).send("drop", "x")
        self.assertEqual(opened, [])

    def test_ntfy_push(self):
        sent = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def opener(req, timeout=None):
            sent.append(req)
            return Resp()

        self.env.cfg.data["ntfy_topic"] = "fh-long-random-topic"
        Notifier(self.env.cfg, opener=opener).send("trade", f"offer from Team Two {TEST_S2}", priority="high")
        (req,) = sent
        self.assertEqual(req.full_url, "https://ntfy.sh/")
        body = json.loads(req.data)
        self.assertEqual((body["topic"], body["priority"], body["tags"]), ("fh-long-random-topic", 4, ["trade"]))
        self.assertNotIn(TEST_S2, body["message"])
        log = self.env.cfg.events_log.read_text()
        self.assertIn("[trade] offer from Team Two ***", log)


class LaunchdTest(unittest.TestCase):
    def test_plist(self):
        env = Env()
        try:
            p = launchd.build_plist(env.cfg, python="/usr/bin/python3")
            self.assertEqual(p["Label"], "com.ethanzhoucool.fantasy-hockey")
            self.assertEqual(p["StartInterval"], 120)
            self.assertEqual(p["ProgramArguments"], ["/usr/bin/python3", "-m", "fhockey", "watch", "--once"])
            self.assertTrue(p["StandardOutPath"].endswith("logs/watch.log"))
            self.assertIn(".local/bin", p["EnvironmentVariables"]["PATH"])
            target = env.root / "LaunchAgents" / "x.plist"
            calls = []
            with mock.patch.object(launchd, "plist_path", lambda cfg: target):
                ok, _ = launchd.install(env.cfg, runner=lambda cmd, **kw: calls.append(cmd) or mock.Mock(returncode=0))
                self.assertTrue(ok)
                self.assertEqual(plistlib.loads(target.read_bytes())["StartInterval"], 120)
                self.assertEqual([c[1] for c in calls], ["bootout", "bootstrap"])
                existed, _ = launchd.uninstall(env.cfg, runner=lambda cmd, **kw: None)
                self.assertTrue(existed)
                self.assertFalse(target.exists())
        finally:
            env.close()


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_snapshot_format(self):
        text, sp = tools.build_snapshot(self.env.client(), now=dt.datetime(2026, 11, 2, 20, 0, tzinfo=dt.timezone.utc))
        lines = text.splitlines()
        self.assertEqual(lines[0], "# pulled 2026-11-02T20:00:00Z period 34 (ESPN mRoster+kona_player_info; "
                                   "FA = top 60 FA/waivers by %owned)")
        self.assertEqual(lines[1], "# team|name|pos|inj|espnProjAvg|seasonAvg|gp|pctOwned|lastNews|playerId|status")
        self.assertIn("4|Ari Alpha|F||2.50|2.80|12|90|2026-11-01|1001|ONTEAM", lines)
        self.assertIn("4|Leo Lima|F|DTD|2.10|2.20|9|79|2026-11-01|1012|ONTEAM", lines)
        self.assertIn("4|Vic Victor|F|IR|2.50|2.60|3|72|2026-11-01|1301|ONTEAM", lines)
        self.assertIn("FA|Xavi Xray|F||1.90|2.10|12|60|2026-11-01|1402|WAIVERS", lines)
        self.assertTrue(all(len(l.split("|")) == 11 for l in lines[2:]))
        # trade_scan.py's freshness check parses the header like this
        pulled = re.search(r"pulled (\S+)", lines[0])[1]
        self.assertEqual(dt.datetime.fromisoformat(pulled.replace("Z", "+00:00")).hour, 20)

    def test_trade_input_for_offer(self):
        text = tools.trade_input(self.env.client(), 4, NOW, offer_id="tx-offer-in")
        lines = text.splitlines()
        self.assertIn("MINE|Vic Victor|F|IR", lines)
        self.assertIn("MINE|Leo Lima|F|DTD", lines)
        self.assertIn("THEIRS|Will Wolf|F|", lines)
        self.assertIn("GIVE|Max Mike", lines)
        self.assertIn("GET|Will Wolf", lines)
        fa = [l for l in lines if l.startswith("FA|")]
        self.assertEqual(fa[0], "FA|Zed Zulu|F")
        self.assertNotIn("FA|Cy Goalie|G", fa)  # filterSlotIds F/D only
        self.assertNotIn("FA|Xavi Xray|F", fa)  # FREEAGENT only

    def test_offers_and_chat_reports(self):
        text, offers = tools.offers_report(self.env.client(), 4, NOW)
        self.assertEqual(len(offers), 2)
        self.assertIn("tx-offer-in: OFFER TO ME from Team Two (team 2) | I give Max Mike | I get Will Wolf", text)
        self.assertIn("tx-offer-mine: MY OFFER to Team Five (team 5) | I give Ian India | I get Zane Bear", text)
        view = self.env.view()
        chat = tools.chat_report(self.env.client(), view, NOW - 3600_000, "TEST-SWID-0004")
        self.assertRegex(chat, r"## DM Blake Example \| last .* by Blake Example \| NEEDS REPLY")
        self.assertIn("## LEAGUE CHAT", chat)
        self.assertNotIn("oops", chat)


class PlayerTest(unittest.TestCase):
    def test_norm_matches_project_py(self):
        self.assertEqual(norm("Tim Stützle"), "tim stutzle")
        self.assertEqual(norm("J.T. Miller"), "jt miller")
        self.assertEqual(norm("Pierre-Luc Dubois"), "pierre luc dubois")

    def test_projections_from_js_and_json(self):
        with tempfile.TemporaryDirectory() as d:
            js = Path(d) / "proj.js"
            js.write_text("window.__PROJ = {\"a b\":2.1,\"c d\":1.5}; window.__PROJ_SD = 0.266; window.__PROJ_DATE = '2026-11-02';\n")
            p = Projections.load(Path(d) / "proj.json")  # falls back to proj.js
            self.assertEqual((p.get("A B"), p.sd, p.date), (2.1, 0.266, "2026-11-02"))
        p = Projections.load(FIX / "proj.json")
        self.assertEqual(p.get("Xavi Xray"), 2.0)
        self.assertEqual(len(Projections.load("/nonexistent/proj.json")), 0)

    def test_fallback_value_for_goalies(self):
        sam = next(e for e in fixture("league_team4")["teams"][3]["roster"]["entries"] if e["playerId"] == 1201)
        p = sam["playerPoolEntry"]["player"]
        # bot.js blend: w = gp/(gp+25) on this season, last season (>= 20 GP) as the base
        self.assertAlmostEqual(model_value(p, Projections(), 2027), 10 / 35 * 5.0 + 25 / 35 * 4.5, places=6)
        self.assertEqual(stat(p, 2027, 1)["appliedAverage"], 4.6)


if __name__ == "__main__":
    unittest.main()
