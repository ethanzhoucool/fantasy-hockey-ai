import unittest

from fhockey import tools
from fhockey.league import trade_outcomes
from fhockey.watch import find_offers

from .helpers import HOUR, MIN, NOW, Env, fixture
from .test_watch import run_watch, text


def answer(id_, typ, status, rel, team=5):
    """A record ESPN adds when a proposal is answered; the proposal itself stays PENDING."""
    return {"id": id_, "type": typ, "status": status, "teamId": team, "relatedTransactionId": rel, "items": []}


class OutcomeTest(unittest.TestCase):
    def test_answers_set_the_status(self):
        txs = [answer("a", "TRADE_PROPOSAL", "CANCELED", "p-cancel", team=4),
               answer("b", "TRADE_DECLINE", "EXECUTED", "p-decline"),
               answer("c", "TRADE_PROPOSAL", "CANCELED", "p-decline", team=4),  # a decline also writes a cancel
               answer("d", "TRADE_ACCEPT", None, "p-accept"),
               answer("e", "TRADE_UPHOLD", "EXECUTED", "p-accept", team=1),  # votes are not outcomes
               answer("f", "TRADE_VETO", "EXECUTED", "p-accept", team=3),
               answer("g", "TRADE_ACCEPT", None, "p-done"),
               answer("h", "TRADE_ACCEPT", "EXECUTED", "p-done", team=4),
               {"id": "p-live", "type": "TRADE_PROPOSAL", "status": "PENDING", "teamId": 4, "items": []}]
        self.assertEqual(trade_outcomes(txs), {"p-cancel": "CANCELED", "p-decline": "DECLINED",
                                               "p-accept": "ACCEPTED", "p-done": "EXECUTED"})

    def test_find_offers_skips_answer_records(self):
        txs = fixture("transactions")["34"]["transactions"] + fixture("transactions")["33"]["transactions"]
        mine = next(t for t in txs if t["id"] == "tx-offer-mine")
        txs.append(dict(answer("x", "TRADE_PROPOSAL", "CANCELED", "tx-offer-mine", team=4), items=mine["items"]))
        cur = find_offers(txs, 4)
        self.assertEqual(set(cur), {"tx-offer-in", "tx-offer-mine"})
        self.assertEqual(cur["tx-offer-mine"]["status"], "CANCELED")
        self.assertEqual(cur["tx-offer-in"]["status"], "PENDING")


class LiveOfferTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_declined_offer_is_reported_and_unprotected(self):
        run_watch(self.env, NOW)
        self.assertIn(1009, self.env.view().pending_trade_ids())
        self.env.espn.tx(34).append(answer("dec", "TRADE_DECLINE", "EXECUTED", "tx-offer-mine"))
        _, sent, _ = run_watch(self.env, NOW + 2 * MIN)
        self.assertEqual(text(sent, "trade"), "your offer to Team Five (Ian India for Zane Bear): PENDING -> DECLINED")
        self.assertNotIn(1009, self.env.view().pending_trade_ids())
        offers = tools.pending_offers(self.env.client(), 4, 34, NOW)
        self.assertEqual([o["id"] for o in offers], ["tx-offer-in"])

    def test_accepted_trade_stays_protected_past_expiry(self):
        self.env.espn.tx(34).append(answer("acc", "TRADE_ACCEPT", None, "tx-offer-mine"))
        later = NOW + 48 * HOUR  # past the proposal's expirationDate, still waiting to process
        self.assertIn(1009, self.env.view(now=later).pending_trade_ids())
        self.env.espn.tx(34).append(answer("run", "TRADE_ACCEPT", "EXECUTED", "tx-offer-mine", team=4))
        self.assertNotIn(1009, self.env.view(now=later).pending_trade_ids())


if __name__ == "__main__":
    unittest.main()
