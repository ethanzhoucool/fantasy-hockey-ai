import unittest

from fhockey import actions

from .helpers import NOW, Env
from .test_watch import run_watch

PLAN = {"add": 1403, "drop": 1010, "from_sp": 34}  # Walt Whiskey (free agent) for Jon Juliet (bench)


class PlannedMoveTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.espn.d["transactions"]["34"]["transactions"] = [
            t for t in self.env.espn.tx(34) if t.get("type") not in ("FREEAGENT", "WAIVER")]

    def run_plan(self, plans=(PLAN,), execute=True, state=None):
        state = {} if state is None else state
        return actions.run_planned(self.env.view(writes=execute), execute, list(plans), state), state

    def test_executes_once(self):
        res, st = self.run_plan()
        self.assertEqual(len(res["executed"]), 1)
        body = self.env.espn.posts[0]["body"]
        self.assertEqual(body["type"], "FREEAGENT")
        self.assertEqual([(i["playerId"], i["type"]) for i in body["items"]], [(1403, "ADD"), (1010, "DROP")])
        self.assertIn("1403:1010", st["planned_done"])
        res, _ = self.run_plan(state=st)
        self.assertEqual(res["lines"], [])
        self.assertEqual(len(self.env.espn.posts), 1)

    def test_waits_for_start_period(self):
        res, st = self.run_plan([dict(PLAN, from_sp=35)])
        self.assertEqual((res["lines"], st["planned_done"], self.env.espn.posts), ([], {}, []))

    def test_dry_run_writes_nothing(self):
        res, st = self.run_plan(execute=False)
        self.assertIn("would add", res["lines"][0])
        self.assertEqual((self.env.espn.posts, st["planned_done"]), ([], {}))

    def test_waits_while_on_waivers(self):
        res, st = self.run_plan([dict(PLAN, add=1402)])
        self.assertIn("still on waivers", res["lines"][0])
        self.assertEqual((self.env.espn.posts, st["planned_done"]), ([], {}))

    def test_runs_after_todays_add(self):
        self.env.espn.tx(34).append({"id": "fa1", "type": "FREEAGENT", "teamId": 4, "status": "EXECUTED",
                                     "items": [{"playerId": 1405, "type": "ADD", "toTeamId": 4}]})
        res, st = self.run_plan()
        self.assertIn("ADDED", res["lines"][0])
        self.assertEqual(len(self.env.espn.posts), 1)

    def test_retired_when_drop_gone(self):
        res, st = self.run_plan([dict(PLAN, drop=999999)])
        self.assertIn("cancelled", res["lines"][0])
        self.assertIn("1403:999999", st["planned_done"])
        self.assertEqual(self.env.espn.posts, [])

    def test_skips_drop_in_pending_trade(self):
        view = self.env.view(writes=True)
        traded = sorted(view.budget().pending_trade_ids & {p["id"] for p in view.roster()})
        self.assertTrue(traded, "fixture should have a pending trade with a team-4 player")
        res, st = self.run_plan([dict(PLAN, drop=traded[0])])
        self.assertIn("pending trade", res["lines"][0])
        self.assertEqual(self.env.espn.posts, [])

    def test_watcher_runs_it_before_streaming(self):
        env = Env(auto_execute=True, planned_moves=[PLAN])
        self.addCleanup(env.close)
        env.espn.d["transactions"]["34"]["transactions"] = [
            t for t in env.espn.tx(34) if t.get("type") not in ("FREEAGENT", "WAIVER")]
        code, sent, _ = run_watch(env, NOW)
        self.assertEqual(code, 0)
        adds = [c["body"] for c in env.espn.posts if c["body"]["type"] == "FREEAGENT"]
        self.assertEqual([i["playerId"] for i in adds[0]["items"]], [1403, 1010])
        self.assertNotIn("stream", [k for k, _ in sent])  # a pickup may follow, but today's stream stays out
        self.assertTrue(any(k == "pickup" and "planned move: ADDED" in m for k, m in sent))


if __name__ == "__main__":
    unittest.main()
