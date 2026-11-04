import math
import unittest

from fhockey import actions
from fhockey.pickups import Budget, candidates, check, decide
from fhockey.players import SLOT, erf, p_better

from .helpers import NOW, TEST_SWID, Env, prepped

RULES = {"min_edge": 0.25, "min_p": 0.75, "min_gp": 5, "max_drop_proj": 1.85, "weekly_add_limit": 7}


def cand(add=None, drop=None, edge=0.5, p=0.9, st="FREEAGENT"):
    add = add or prepped(900, "Add Guy", "F", None, 2.0, status=st, gp=10)
    drop = drop or prepped(100, "Drop Guy", "F", SLOT["BE"], 1.5)
    return {"add": add, "drop": drop, "edge": edge, "p": p, "st": st}


class RuleTest(unittest.TestCase):
    def test_passing_candidate(self):
        self.assertEqual(check(cand(), Budget(), RULES), [])

    def assertFailsWith(self, c, budget, text):
        fails = check(c, budget, RULES)
        self.assertTrue(any(text in f for f in fails), f"{text!r} not in {fails}")

    def test_edge_too_small(self):
        self.assertFailsWith(cand(edge=0.24), Budget(), "edge 0.24")

    def test_probability_too_low(self):
        self.assertFailsWith(cand(p=0.74), Budget(), "P 0.74")

    def test_add_not_active(self):
        self.assertFailsWith(cand(add=prepped(900, "Hurt", "F", None, 2.0, inj="OUT", status="FREEAGENT")),
                             Budget(), "Hurt is OUT")
        self.assertFailsWith(cand(add=prepped(900, "Dtd", "F", None, 2.0, inj="DAY_TO_DAY", status="FREEAGENT")),
                             Budget(), "Dtd is DAY_TO_DAY")

    def test_add_too_few_games(self):
        self.assertFailsWith(cand(add=prepped(900, "Rookie", "F", None, 2.0, gp=4, status="FREEAGENT")),
                             Budget(), "4 GP < 5")

    def test_drop_in_pending_trade(self):
        self.assertFailsWith(cand(), Budget(pending_trade_ids={100}), "pending trade")

    def test_drop_out_or_ir(self):
        self.assertFailsWith(cand(drop=prepped(100, "Out", "F", SLOT["BE"], 1.5, inj="OUT")), Budget(), "OUT/IR")
        self.assertFailsWith(cand(drop=prepped(100, "Irslot", "F", SLOT["IR"], 1.5)), Budget(), "OUT/IR")

    def test_drop_core_player(self):
        self.assertFailsWith(cand(drop=prepped(100, "Core", "F", SLOT["F"], 1.85)), Budget(), "(core)")
        self.assertEqual(check(cand(drop=prepped(100, "Fine", "F", SLOT["F"], 1.84)), Budget(), RULES), [])

    def test_duplicate_pending_claims(self):
        self.assertFailsWith(cand(), Budget(pending_claims=[{"add": 900, "drop": 555}]), "already in a pending claim")
        self.assertFailsWith(cand(), Budget(pending_claims=[{"add": 555, "drop": 100}]), "already the drop")

    def test_weekly_limit(self):
        # 6 used + this move = 7: allowed. 5 used + 2 pending + this = 8: blocked.
        self.assertEqual(check(cand(), Budget(weekly_used=6), RULES), [])
        self.assertFailsWith(cand(), Budget(weekly_used=7), "weekly limit")
        self.assertFailsWith(cand(), Budget(weekly_used=5, pending_claims=[{"add": 1, "drop": 2}, {"add": 3, "drop": 4}]),
                             "weekly limit: 7/7")

    def test_pickups_have_no_daily_limit(self):
        self.assertEqual(check(cand(), Budget(fa_adds_this_period=1), RULES), [])
        self.assertEqual(check(cand(st="WAIVERS", add=prepped(900, "W", "F", None, 2.0, status="WAIVERS")),
                               Budget(fa_adds_this_period=1), RULES), [])

    def test_unavailable_player(self):
        self.assertFailsWith(cand(st="ONTEAM"), Budget(), "not available")

    def test_decide_takes_first_pass_and_at_most_one_of_each(self):
        a1 = prepped(901, "A1", "F", None, 2.2, status="FREEAGENT")
        a2 = prepped(902, "A2", "F", None, 2.1, status="WAIVERS")
        a3 = prepped(903, "A3", "F", None, 2.0, status="FREEAGENT")
        a4 = prepped(904, "A4", "F", None, 2.0, status="WAIVERS")
        d1, d2 = prepped(101, "D1", "F", SLOT["BE"], 1.4), prepped(102, "D2", "F", SLOT["BE"], 1.45)
        cs = [cand(a1, d1, 0.8, 0.98, "FREEAGENT"), cand(a2, d1, 0.7, 0.97, "WAIVERS"),
              cand(a3, d2, 0.55, 0.93, "FREEAGENT"), cand(a4, d2, 0.5, 0.9, "WAIVERS")]
        b = Budget(weekly_used=2)
        moves, _ = decide(cs, b, RULES)
        self.assertEqual([(m["kind"], m["add"]["id"], m["drop"]["id"]) for m in moves],
                         [("FREEAGENT", 901, 101), ("WAIVER", 904, 102)])  # 902 reused d1, which is consumed
        self.assertEqual(b.committed, 4)


class FixturePickupTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_candidates_and_decisions(self):
        r = actions.run_pickups(self.env.view(), execute=False)
        top = [(c["st"], c["add"]["n"], c["drop"]["n"], c["edge"]) for c in r["candidates"][:3]]
        self.assertEqual(top, [("FREEAGENT", "Abe Low", "Kai Kilo", 0.8), ("FREEAGENT", "Abe Low", "Jon Juliet", 0.75),
                               ("WAIVERS", "Xavi Xray", "Kai Kilo", 0.6)])
        self.assertNotIn("Zed Zulu", [c["add"]["n"] for c in r["candidates"]])  # DAY_TO_DAY free agent skipped
        self.assertEqual([(m["kind"], m["add"]["n"], m["drop"]["n"]) for m in r["moves"]],
                         [("WAIVER", "Xavi Xray", "Kai Kilo"), ("FREEAGENT", "Walt Whiskey", "Jon Juliet")])
        self.assertEqual(self.env.espn.posts, [])

    def test_weekly_count_includes_pending_waiver_claims(self):
        e = self.env.espn
        e.tx(34).append({"id": "c1", "type": "WAIVER", "status": "PENDING", "teamId": 4, "items": [
            {"playerId": 1408, "type": "ADD", "toTeamId": 4}, {"playerId": 1106, "type": "DROP", "fromTeamId": 4}]})
        e.tx(33).append({"id": "c2", "type": "WAIVER", "status": "PENDING", "teamId": 4, "items": [
            {"playerId": 1407, "type": "ADD", "toTeamId": 4}, {"playerId": 1105, "type": "DROP", "fromTeamId": 4}]})
        e.tx(34).append({"id": "c3", "type": "WAIVER", "status": "PENDING", "teamId": 2, "items": []})  # not mine
        e.tx(34).append({"id": "c4", "type": "WAIVER", "status": "CANCELED", "teamId": 4, "items": []})  # not pending
        v = self.env.view()
        self.assertEqual(v.weekly_used(), 2)  # matchupAcquisitionTotals["5"]
        b = v.budget()
        self.assertEqual(len(b.pending_claims), 2)
        self.assertEqual(b.committed, 4)
        self.assertEqual(b.claimed_drops(), {1106, 1105})

    def test_missing_counter_is_treated_as_limit_reached(self):
        del self.env.espn.team4["transactionCounter"]
        self.assertEqual(self.env.view().weekly_used(), 7)
        r = actions.run_pickups(self.env.view(), execute=False)
        self.assertEqual(r["moves"], [])

    def test_weekly_limit_blocks_everything(self):
        self.env.espn.team4["transactionCounter"]["matchupAcquisitionTotals"]["5"] = 7
        r = actions.run_pickups(self.env.view(), execute=False)
        self.assertEqual(r["moves"], [])
        self.assertIn("weekly limit", r["lines"][0])

    def test_free_agent_already_added_today(self):
        self.env.espn.tx(34).append({"id": "fa-today", "type": "FREEAGENT", "status": "EXECUTED", "teamId": 4,
                                     "items": [{"playerId": 1408, "type": "ADD", "toTeamId": 4},
                                               {"playerId": 1106, "type": "DROP", "fromTeamId": 4}]})
        v = self.env.view()
        self.assertEqual(v.fa_adds_this_period(), 1)
        r = actions.run_pickups(v, execute=False)
        self.assertEqual([m["kind"] for m in r["moves"]], ["WAIVER", "FREEAGENT"])

    def test_state_remembers_todays_free_agent_add(self):
        self.assertEqual(self.env.view(state={"fa_add_sp": 34}).fa_adds_this_period(), 1)
        self.assertEqual(self.env.view(state={"fa_add_sp": 33}).fa_adds_this_period(), 0)

    def test_pending_trade_players_protected(self):
        ids = self.env.view().pending_trade_ids()
        self.assertTrue({1101, 1009} <= ids)  # offer to me (Max) and my offer (Ian)

    def test_execute_payloads(self):
        self.env.cfg.set("auto_execute", True)
        st = {}
        r = actions.run_pickups(self.env.view(writes=True), execute=True, state=st)
        self.assertEqual(len(r["executed"]), 2)
        claim, fa = [p["body"] for p in self.env.espn.posts]
        self.assertEqual(claim["type"], "WAIVER")
        self.assertEqual(claim["items"], [{"playerId": 1402, "type": "ADD", "toTeamId": 4},
                                          {"playerId": 1011, "type": "DROP", "fromTeamId": 4}])
        self.assertEqual(fa["type"], "FREEAGENT")
        self.assertEqual(fa["items"][0]["playerId"], 1403)
        for b in (claim, fa):
            self.assertEqual((b["teamId"], b["memberId"], b["scoringPeriodId"], b["executionType"]),
                             (4, TEST_SWID, 34, "EXECUTE"))
        self.assertEqual(st["fa_add_sp"], 34)

    def test_rejected_write_is_reported_once(self):
        self.env.espn.post_status = 400
        self.env.espn.post_body = {"details": [{"message": "TRAN_PLAYER_NOT_AVAILABLE"}]}
        r = actions.run_pickups(self.env.view(writes=True), execute=True, state={})
        self.assertEqual(len(self.env.espn.posts), 2)  # one attempt per move, no retries
        self.assertEqual(sum("FAILED" in l and "TRAN_PLAYER_NOT_AVAILABLE" in l for l in r["lines"]), 2)


class MathTest(unittest.TestCase):
    def test_erf_matches_math_erf(self):
        for x in (-2, -0.5, 0, 0.3, 1, 2.5):
            self.assertAlmostEqual(erf(x), math.erf(x), places=6)

    def test_p_better(self):
        self.assertAlmostEqual(p_better(0, 0.27), 0.5, places=6)
        # P = Phi(edge / (sd * sqrt 2)): 0.25 pts/g at the model's SD is just under the 0.75 bar
        self.assertAlmostEqual(p_better(0.25, 0.266), 0.7468, places=3)
        self.assertGreater(p_better(0.26, 0.266), 0.75)

    def test_candidates_use_undiscounted_projection(self):
        ros = [prepped(1, "dtd vet", "F", SLOT["F"], 1.9, inj="DAY_TO_DAY"), prepped(2, "weak", "F", SLOT["BE"], 1.3)]
        fas = [prepped(9, "fa", "F", None, 1.7, status="FREEAGENT")]
        cs = candidates(ros, fas, 0.266)
        self.assertEqual([(c["drop"]["n"], c["edge"]) for c in cs], [("weak", 0.4)])


if __name__ == "__main__":
    unittest.main()
