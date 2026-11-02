import unittest

from fhockey import actions
from fhockey.espn import WritesDisabled
from fhockey.lineup import optimize
from fhockey.players import SLOT

from .helpers import NOW, TEST_SWID, Env, prepped

F, D, U, G, BE, IR = SLOT["F"], SLOT["D"], SLOT["UTIL"], SLOT["G"], SLOT["BE"], SLOT["IR"]
LATER = NOW + 3_600_000


def moves(res):
    return {i["playerId"]: (i["fromLineupSlotId"], i["toLineupSlotId"]) for i in res["items"]}


class OptimizerTest(unittest.TestCase):
    def test_fixture_lineup(self):
        env = Env()
        try:
            r = actions.run_lineup(env.view(), execute=False)
        finally:
            env.close()
        m = moves(r["result"])
        self.assertEqual(m, {1005: (F, BE), 1006: (F, BE), 1010: (BE, F), 1012: (BE, F), 1104: (D, BE),
                             1106: (BE, D), 1202: (G, BE), 1203: (BE, G)})
        self.assertEqual(len(r["result"]["starters"]), 16)
        self.assertEqual(r["result"]["empty"], {F: 0, D: 0, U: 1, G: 0})
        self.assertIn("DRY RUN: 8 moves not saved", r["lines"][0])
        self.assertNotIn(1301, m)  # IR slot never touched

    def test_caps_and_util(self):
        ros = [prepped(i, f"f{i}", "F", BE, 3.0 - i * 0.1, gt=LATER) for i in range(12)]
        ros += [prepped(100 + i, f"d{i}", "D", BE, 2.0 - i * 0.1, gt=LATER) for i in range(5)]
        ros += [prepped(200 + i, f"g{i}", "G", BE, 4.0 - i, gt=LATER) for i in range(3)]
        res = optimize(ros)
        f = res["final"]
        self.assertEqual(sum(1 for p in ros if f[p["id"]] == F), 9)
        self.assertEqual(sum(1 for p in ros if f[p["id"]] == D), 5)
        self.assertEqual(sum(1 for p in ros if f[p["id"]] == G), 2)
        self.assertEqual(f[9], U)  # 10th best forward takes UTIL
        self.assertEqual(f[10], BE)
        self.assertEqual(f[11], BE)
        self.assertEqual(f[202], BE)  # third goalie sits; goalies never take UTIL
        self.assertEqual(res["empty"], {F: 0, D: 0, U: 0, G: 0})

    def test_day_to_day_counts_half(self):
        # healthy 1.3 > healthy 1.2 > DAY_TO_DAY 2.2 (worth 1.1 today)
        ros = [prepped(i, f"f{i}", "F", F, 2.5, gt=LATER) for i in range(8)]
        ros += [prepped(50, "dtd", "F", BE, 2.2, gt=LATER, inj="DAY_TO_DAY"),
                prepped(51, "healthy", "F", BE, 1.3, gt=LATER),
                prepped(52, "util", "F", BE, 1.2, gt=LATER)]
        res = optimize(ros)
        self.assertEqual((res["final"][51], res["final"][52], res["final"][50]), (F, U, BE))

    def test_locked_and_ir_never_move(self):
        ros = [prepped(i, f"f{i}", "F", F, 2.0, gt=LATER) for i in range(9)]
        ros += [prepped(60, "started, benched", "F", BE, 3.0, gt=NOW - 60_000, locked=True),
                prepped(61, "started, starting", "F", F, 0.5, gt=NOW - 60_000, locked=True),
                prepped(62, "IR but healthy", "F", IR, 3.0, gt=LATER)]
        res = optimize(ros)
        m = moves(res)
        self.assertNotIn(60, m)
        self.assertNotIn(61, m)
        self.assertNotIn(62, m)
        # the locked starter uses one F slot, so only 8 of the nine 2.0 forwards fit in F; one goes to UTIL
        self.assertEqual(sum(1 for i in range(9) if res["final"][i] == F), 8)
        self.assertEqual(sum(1 for i in range(9) if res["final"][i] == U), 1)

    def test_minimal_moves(self):
        ros = [prepped(i, f"f{i}", "F", F, 2.0, gt=LATER) for i in range(7)]
        ros += [prepped(70, "idle starter", "F", F, 1.0, gt=None),  # no game, nobody needs his slot
                prepped(71, "idle util", "D", U, 1.0, gt=None)]
        res = optimize(ros)
        self.assertEqual(res["items"], [])
        self.assertEqual(res["empty"][F], 2)
        self.assertEqual(res["empty"][U], 1)

    def test_out_player_not_started(self):
        ros = [prepped(1, "out", "F", F, 3.0, gt=LATER, inj="OUT"), prepped(2, "bench", "F", BE, 1.0, gt=LATER)]
        res = optimize(ros)
        self.assertEqual(res["final"][2], F)
        self.assertEqual(res["final"][1], F)  # OUT with a game: keeps slot (not over cap) but is not a starter
        self.assertEqual([p["id"] for p in res["hurt"]], [1])
        self.assertNotIn(1, [p["id"] for p in res["starters"]])


class LineupWriteTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(auto_execute=True)

    def tearDown(self):
        self.env.close()

    def test_roster_payload(self):
        r = actions.run_lineup(self.env.view(writes=True), execute=True)
        self.assertIn("SAVED 8 moves", r["lines"][0])
        (post,) = self.env.espn.posts
        b = post["body"]
        self.assertEqual(b["type"], "ROSTER")
        self.assertEqual(b["memberId"], TEST_SWID)
        self.assertEqual(b["teamId"], 4)
        self.assertEqual(b["scoringPeriodId"], 34)
        self.assertEqual(b["executionType"], "EXECUTE")
        self.assertFalse(b["isLeagueManager"])
        self.assertEqual({i["type"] for i in b["items"]}, {"LINEUP"})
        self.assertTrue(post["url"].startswith("https://lm-api-writes.fantasy.espn.com/apis/v3/games/fhl/seasons/2027/"
                                               "segments/0/leagues/488544617/transactions/"))
        self.assertEqual(post["headers"]["Content-Type"], "application/json")

    def test_future_roster_payload(self):
        r = actions.run_lineup(self.env.view(writes=True), execute=True, sp=35)
        self.assertTrue(r["ok"])
        self.assertEqual(self.env.espn.posts[0]["body"]["type"], "FUTURE_ROSTER")
        self.assertEqual(self.env.espn.posts[0]["body"]["scoringPeriodId"], 35)

    def test_dry_run_never_posts(self):
        actions.run_lineup(self.env.view(writes=False), execute=False)
        self.assertEqual(self.env.espn.posts, [])
        with self.assertRaises(WritesDisabled):
            self.env.client(writes=False).set_lineup([], 34, 34)
        self.assertEqual(self.env.espn.posts, [])

    def test_save_failure_reported_not_retried(self):
        self.env.espn.post_status = 409
        self.env.espn.post_body = {"messages": ["Lineup locked"]}
        r = actions.run_lineup(self.env.view(writes=True), execute=True)
        self.assertFalse(r["ok"])
        self.assertIn("SAVE FAILED", r["lines"][0])
        self.assertIn("Lineup locked", r["lines"][0])
        self.assertEqual(len(self.env.espn.posts), 1)


if __name__ == "__main__":
    unittest.main()
