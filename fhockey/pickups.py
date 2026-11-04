"""Free-agent / waiver pickup rules (from the daily task's step 5) on top of bot.js's candidate list.

Doing nothing is the default. Candidates are sorted by edge; the first one that passes every rule is taken,
at most one FREEAGENT add and one new WAIVER claim per run:
  - edge >= 0.25 pts/g and P(add outscores drop) >= 0.75
  - the add is ACTIVE with >= 5 games played this season
  - the drop is not in a pending trade, not OUT/IR, and projects below 1.85 (core players are never dropped)
  - no duplicate pending claim: neither the add nor the drop is already in one of our pending waiver claims
  - weekly limit: acquisitions this matchup period + pending waiver claims + this move <= 7
  - WAIVERS players get a claim of type WAIVER
"""
from .players import OUT, SLOT, p_better


class Budget:
    """Acquisition bookkeeping shared by pickups and streaming. Updated in place as moves are made."""

    def __init__(self, weekly_used=0, pending_claims=None, fa_adds_this_period=0, pending_trade_ids=(), limit=7):
        self.weekly_used = int(weekly_used)
        self.pending_claims = list(pending_claims or [])  # [{"add": id, "drop": id}]
        self.fa_adds_this_period = int(fa_adds_this_period)
        self.pending_trade_ids = set(pending_trade_ids)
        self.limit = limit
        self.consumed = set()  # players already used by a move this run

    @property
    def committed(self):
        return self.weekly_used + len(self.pending_claims)

    def claimed_adds(self):
        return {c["add"] for c in self.pending_claims}

    def claimed_drops(self):
        return {c["drop"] for c in self.pending_claims}

    def apply(self, kind, add_id, drop_id):
        if kind == "WAIVER":
            self.pending_claims.append({"add": add_id, "drop": drop_id})
        else:
            self.weekly_used += 1
            self.fa_adds_this_period += 1
        self.consumed.update((add_id, drop_id))


def candidates(ros, fas, sd, min_diff=0.2):
    """bot.js: best 3 available at each position vs my 2 weakest healthy players there.
    Values are the undiscounted projections (a DAY_TO_DAY player is not worth half for the rest of the season)."""
    pool = [p for p in fas if p["inj"] not in OUT and p["inj"] != "DAY_TO_DAY" and p["gp"] >= 3]
    out = []
    for pos in ("F", "D", "G"):
        mine = sorted((p for p in ros if p["pos"] == pos and p["inj"] not in OUT and p["slot"] != SLOT["IR"]),
                      key=lambda p: p["proj"])
        best = sorted((p for p in pool if p["pos"] == pos), key=lambda p: -p["proj"])[:3]
        for b in best:
            for m in mine[:2]:
                d = b["proj"] - m["proj"]
                if d >= min_diff:
                    out.append({"add": b, "drop": m, "edge": round(d, 2), "p": round(p_better(d, sd), 2),
                                "st": b["status"]})
    out.sort(key=lambda c: -c["edge"])
    return out


def check(c, budget, rules):
    """Every rule this candidate fails (empty list = passes)."""
    a, d = c["add"], c["drop"]
    fails = []
    if c["edge"] < rules["min_edge"]:
        fails.append(f"edge {c['edge']:.2f} < {rules['min_edge']}")
    if c["p"] < rules["min_p"]:
        fails.append(f"P {c['p']:.2f} < {rules['min_p']}")
    if c["st"] not in ("FREEAGENT", "WAIVERS"):
        fails.append(f"{a['n']} is not available ({c['st']})")
    if a["inj"] != "ACTIVE":
        fails.append(f"{a['n']} is {a['inj']}")
    if a["gp"] < rules["min_gp"]:
        fails.append(f"{a['n']} has {a['gp']} GP < {rules['min_gp']}")
    if d["id"] in budget.pending_trade_ids:
        fails.append(f"{d['n']} is in a pending trade")
    if d["inj"] in OUT or d["slot"] == SLOT["IR"]:
        fails.append(f"{d['n']} is OUT/IR")
    if d["proj"] >= rules["max_drop_proj"]:
        fails.append(f"{d['n']} projects {d['proj']:.2f} >= {rules['max_drop_proj']} (core)")
    if a["id"] in budget.claimed_adds():
        fails.append(f"{a['n']} already in a pending claim")
    if d["id"] in budget.claimed_drops():
        fails.append(f"{d['n']} already the drop in a pending claim")
    if a["id"] in budget.consumed or d["id"] in budget.consumed:
        fails.append("player already used by a move this run")
    if budget.committed + 1 > rules["weekly_add_limit"]:
        fails.append(f"weekly limit: {budget.committed}/{rules['weekly_add_limit']} used incl. pending claims")
    return fails


def decide(cands, budget, rules):
    """Walk the list in order. Returns (moves, first_rejection). Updates budget as if each move is made."""
    moves, done, first_fail = [], set(), None
    for c in cands:
        kind = "FREEAGENT" if c["st"] == "FREEAGENT" else "WAIVER"
        if kind in done:
            continue
        fails = check(c, budget, rules)
        if fails:
            if first_fail is None:
                first_fail = (c, fails)
            continue
        moves.append(dict(c, kind=kind))
        done.add(kind)
        budget.apply(kind, c["add"]["id"], c["drop"]["id"])
        if len(done) == 2:
            break
    return moves, first_fail


def describe(c):
    tag = " (waivers)" if c["st"] == "WAIVERS" else ""
    return f"+{c['add']['n']}{tag} -{c['drop']['n']} edge +{c['edge']:.2f}/g, P {round(c['p'] * 100)}%"
