import unittest

from fhockey import actions
from fhockey.config import DEFAULTS
from fhockey.lineup import optimize
from fhockey.pickups import Budget
from fhockey.players import SLOT
from fhockey.stream import days_left_in_week, plan

from .helpers import MIN, NOW, TEST_SWID, Env, prepped

SCFG = DEFAULTS["stream"]
RULES = DEFAULTS["rules"]
F, D, U, BE, IR = SLOT["F"], SLOT["D"], SLOT["UTIL"], SLOT["BE"], SLOT["IR"]
GAME = NOW + 3 * 60 * MIN


def roster(n_f_playing=8, extra=()):
    """9 F + 5 D slots; n_f_playing forwards have games (so 9 - n F slots are empty); D all play; 2 G play."""
    ros = [prepped(i, f"F{i}", "F", F, 2.5 - i * 0.05, gt=GAME if i < n_f_playing else None) for i in range(9)]
    ros += [prepped(100 + i, f"D{i}", "D", D, 2.2 - i * 0.05, gt=GAME) for i in range(5)]
    ros += [prepped(200, "G0", "G", SLOT["G"], 4.0, gt=GAME), prepped(201, "G1", "G", SLOT["G"], 3.5, gt=GAME)]
    ros += list(extra)
    return ros


def fa(pid=900, v=1.7, pos="F", gt=GAME, status="FREEAGENT", inj="ACTIVE", gp=10):
    return prepped(pid, f"FA{pid}", pos, None, v, gt=gt, status=status, inj=inj, gp=gp, team=7)


def run(ros, fas, budget=None, days_left=5, streamed=()):
    return plan(ros, optimize(ros), fas, budget or Budget(weekly_used=2), SCFG, RULES, NOW, 34, streamed, days_left)


class StreamPlanTest(unittest.TestCase):
    def test_no_empty_slot_no_stream(self):
        ros = roster(n_f_playing=9, extra=[prepped(50, "util guy", "F", BE, 1.5, gt=GAME)])
        r = run(ros, [fa()])
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no empty skater slot today")

    def test_streams_into_empty_slot(self):
        ros = roster(n_f_playing=8, extra=[prepped(50, "bench idle", "F", BE, 1.4, gt=None)])
        r = run(ros, [fa(900, 1.7), fa(901, 1.6)])
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual((r["add"]["id"], r["drop"]["id"]), (900, 50))

    def test_util_slot_accepts_defense(self):
        ros = roster(n_f_playing=9, extra=[prepped(50, "idle", "F", BE, 1.3, gt=None)])  # only UTIL empty
        r = run(ros, [fa(900, 1.6, pos="D"), fa(901, 1.5, pos="F"), fa(902, 3.0, pos="G")])
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual(r["add"]["id"], 900)

    def test_empty_d_slot_only_takes_defense(self):
        ros = roster(n_f_playing=9, extra=[prepped(50, "util", "F", BE, 1.5, gt=GAME), prepped(51, "idle", "F", BE, 1.2)])
        ros = [dict(p, gt=None, plays=False) if p["id"] == 104 else p for p in ros]  # one D idle -> D slot empty
        r = run(ros, [fa(900, 1.9, pos="F"), fa(901, 1.5, pos="D")])
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual(r["add"]["id"], 901)

    # ---- budget / reserve
    def test_reserve_kept_until_last_two_days(self):
        ros = roster(extra=[prepped(50, "idle", "F", BE, 1.4)])
        self.assertTrue(run(ros, [fa()], Budget(weekly_used=5), days_left=3)["ok"])  # 6th add, the 7th kept back
        r = run(ros, [fa()], Budget(weekly_used=6), days_left=3)  # 7th add only in the last 2 days
        self.assertFalse(r["ok"])
        self.assertIn("1 kept in reserve", r["reason"])
        self.assertTrue(run(ros, [fa()], Budget(weekly_used=6), days_left=2)["ok"])  # reserve released

    def test_weekly_limit_counts_pending_claims(self):
        ros = roster(extra=[prepped(50, "idle", "F", BE, 1.4)])
        b = Budget(weekly_used=5, pending_claims=[{"add": 1, "drop": 2}])
        self.assertTrue(run(ros, [fa()], b, days_left=1)["ok"])  # 6 committed + 1 = 7
        b = Budget(weekly_used=6, pending_claims=[{"add": 1, "drop": 2}])
        r = run(ros, [fa()], b, days_left=1)
        self.assertFalse(r["ok"])
        self.assertIn("7/7", r["reason"])

    def test_one_free_agent_add_per_day(self):
        ros = roster(extra=[prepped(50, "idle", "F", BE, 1.4)])
        r = run(ros, [fa()], Budget(weekly_used=0, fa_adds_this_period=1))
        self.assertFalse(r["ok"])
        self.assertIn("already used", r["reason"])

    def test_days_left_in_week(self):
        mps = {"5": [30, 31, 32, 33, 34, 35, 36]}
        self.assertEqual(days_left_in_week(mps, 5, 34), 3)
        self.assertEqual(days_left_in_week(mps, 5, 36), 1)
        self.assertEqual(days_left_in_week({}, 5, 34, weekday=5), 2)  # Saturday, Monday-Sunday fallback

    # ---- game-start cutoff and add eligibility
    def test_game_start_cutoff(self):
        ros = roster(extra=[prepped(50, "idle", "F", BE, 1.4)])
        self.assertFalse(run(ros, [fa(gt=NOW + 15 * MIN)])["ok"])  # puck drop too soon
        self.assertTrue(run(ros, [fa(gt=NOW + 25 * MIN)])["ok"])
        self.assertFalse(run(ros, [fa(gt=NOW - 5 * MIN)])["ok"])  # already started
        self.assertFalse(run(ros, [fa(gt=None)])["ok"])  # no game today

    def test_add_must_be_active_free_agent_with_games_played(self):
        ros = roster(extra=[prepped(50, "idle", "F", BE, 1.4)])
        self.assertFalse(run(ros, [fa(status="WAIVERS")])["ok"])
        self.assertFalse(run(ros, [fa(inj="DAY_TO_DAY")])["ok"])
        self.assertFalse(run(ros, [fa(gp=4)])["ok"])
        self.assertFalse(run(ros, [fa(900)], Budget(pending_claims=[{"add": 900, "drop": 7}]))["ok"])

    # ---- drop eligibility
    def test_never_drops_core_ir_out_goalie_or_traded(self):
        add = fa(900, 2.4)
        extra = [prepped(60, "core", "F", BE, 1.85, gt=None, sv=1.79),  # bot.js value 1.85 -> core
                 prepped(61, "ir", "F", IR, 1.2, gt=None),
                 prepped(62, "out", "F", BE, 1.2, gt=None, inj="OUT"),
                 prepped(63, "goalie", "G", BE, 1.0, gt=None),
                 prepped(64, "traded", "F", BE, 1.1, gt=None),
                 prepped(65, "too good", "F", BE, 1.81, gt=None),
                 prepped(66, "fine", "F", BE, 1.5, gt=GAME)]
        ros = roster(extra=extra)
        r = run(ros, [add], Budget(pending_trade_ids={64}))
        self.assertTrue(r["ok"], r["reason"])
        self.assertEqual(r["drop"]["id"], 66)
        r = run([p for p in ros if p["id"] != 66], [add], Budget(pending_trade_ids={64}))
        self.assertFalse(r["ok"])
        self.assertIn("no eligible drop", r["reason"])

    def test_drop_cannot_be_much_better_than_add(self):
        only = [prepped(70, "bench", "F", BE, 1.75, gt=None)]
        ros = roster(n_f_playing=8, extra=only)
        ros = [dict(p, sv=1.9, proj=1.9) if p["pos"] in "FD" and p["id"] != 70 else p for p in ros]
        self.assertFalse(run(ros, [fa(900, 1.6)])["ok"])  # 1.75 > 1.6 + 0.10
        self.assertTrue(run(ros, [fa(900, 1.65)])["ok"])  # 1.75 <= 1.65 + 0.10

    def test_prefers_drop_without_game_today(self):
        ros = roster(n_f_playing=8, extra=[prepped(50, "plays today", "F", BE, 1.2, gt=GAME),
                                           prepped(51, "idle", "F", BE, 1.5, gt=None)])
        r = run(ros, [fa(900, 1.7)])
        self.assertEqual(r["drop"]["id"], 51)

    def test_not_dropped_if_streamed_today_or_game_started(self):
        ros = roster(n_f_playing=8, extra=[prepped(50, "streamed in", "F", BE, 1.2, gt=None),
                                           prepped(51, "in game", "F", BE, 1.2, gt=NOW - MIN, locked=True)])
        ros = [dict(p, sv=2.5, proj=2.5) if p["id"] < 50 and p["pos"] != "G" else p for p in ros]
        r = run(ros, [fa(900, 1.7)], streamed=[50])
        self.assertFalse(r["ok"])
        self.assertIn("no eligible drop", r["reason"])


class StreamFixtureTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_fixture_plan(self):
        r = actions.run_stream(self.env.view(), execute=False, state={})
        self.assertEqual(r["line"], "stream (dry run): +Yan Yankee (DET, plays today) -Kai Kilo, adds this week 3/7")
        self.assertEqual(self.env.espn.posts, [])

    def test_execute_records_add_and_posts_free_agent(self):
        st = {}
        r = actions.run_stream(self.env.view(writes=True), execute=True, state=st)
        self.assertTrue(r["executed"])
        (post,) = self.env.espn.posts
        self.assertEqual(post["body"]["type"], "FREEAGENT")
        self.assertEqual(post["body"]["memberId"], TEST_SWID)
        self.assertEqual([i["playerId"] for i in post["body"]["items"]], [1405, 1011])
        self.assertEqual(st["stream_adds"], {"1405": 34})
        self.assertEqual(st["fa_add_sp"], 34)
        # a second stream the same day is blocked by the one-add-per-day rule
        r2 = actions.run_stream(self.env.view(state=st, writes=True), execute=True, state=st)
        self.assertFalse(r2["ok"])
        self.assertEqual(len(self.env.espn.posts), 1)

    def test_reserve_from_league_settings(self):
        self.env.espn.team4["transactionCounter"]["matchupAcquisitionTotals"]["5"] = 6
        r = actions.run_stream(self.env.view(), execute=False, state={})  # sp 34 of 30-36: 3 days left
        self.assertIn("reserve", r["line"])
        self.env.espn.d["settings"]["settings"]["scheduleSettings"]["matchupPeriods"]["5"] = [29, 30, 31, 32, 33, 34, 35]
        self.env.cfg.state_dir.joinpath("settings.json").unlink()
        r = actions.run_stream(self.env.view(), execute=False, state={})  # now 2 days left
        self.assertTrue(r["ok"], r["line"])


if __name__ == "__main__":
    unittest.main()
