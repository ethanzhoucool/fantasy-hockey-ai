"""Daily streaming: fill an empty starting skater slot with a free agent who plays today.

All of these must hold (dry run unless auto_execute):
  - after the optimal lineup, today has an empty F, D or UTIL slot (locked starters count as filled)
  - adds this matchup week (counter + pending waiver claims) < limit, keeping `reserve_adds` in reserve until the
    last `reserve_until_days_left` days of the matchup week, and no free-agent add yet this scoring period
  - add: a FREEAGENT (not WAIVERS), ACTIVE, >= 5 GP, whose team plays today with puck drop > 20 min away,
    highest value at a position with an empty slot (UTIL takes F or D)
  - drop: lowest-value skater who is not OUT/IR, projects <= 1.80 (never >= 1.85), is not in a pending trade or
    pending claim, was not streamed in today, has not started playing, and projects <= add + 0.10.
    Players without a game today are preferred.
"""
from .players import OUT, SLOT


def days_left_in_week(matchup_periods, current_mp, sp, weekday=None):
    """Scoring days left in the matchup week, today included (weekday: Monday=0).
    mSettings scheduleSettings.matchupPeriods maps a matchup to scoring periods in some leagues, but in ESPN
    hockey it maps to week numbers ({"6": [6], "24": [24, 25]}). Week lists use a Monday-Sunday week, and a
    multi-week matchup counts as if today were in its first week, which keeps the reserve add longer."""
    sps = (matchup_periods or {}).get(str(current_mp)) or (matchup_periods or {}).get(current_mp)
    if sps and len(sps) >= 3:
        return sum(1 for s in sps if s >= sp)
    return 7 - (weekday or 0) + 7 * (len(sps or [1]) - 1)


def plan(ros, lineup_res, fas, budget, scfg, rules, now_ms, sp, streamed_today=(), days_left=7):
    """Returns {"ok": bool, "reason": str, "add": p, "drop": p, "committed_after": n}."""
    e = lineup_res["empty"]
    empty_f, empty_d, empty_u = e[SLOT["F"]], e[SLOT["D"]], e[SLOT["UTIL"]]
    if empty_f + empty_d + empty_u <= 0:
        return {"ok": False, "reason": "no empty skater slot today"}

    limit = rules["weekly_add_limit"]
    reserve = scfg["reserve_adds"] if days_left > scfg["reserve_until_days_left"] else 0
    if budget.committed + 1 > limit - reserve:
        return {"ok": False, "reason": f"add budget: {budget.committed}/{limit} used, {reserve} kept in reserve"}
    if budget.fa_adds_this_period >= scfg["adds_per_day"]:
        return {"ok": False, "reason": "free-agent add already used this scoring period"}

    positions = set()
    if empty_f > 0:
        positions.add("F")
    if empty_d > 0:
        positions.add("D")
    if empty_u > 0:
        positions.update(("F", "D"))
    cutoff = now_ms + scfg["min_start_minutes"] * 60_000
    adds = [
        p for p in fas
        if p["status"] == "FREEAGENT" and p["inj"] == "ACTIVE" and p["gp"] >= scfg["min_gp"]
        and p["gt"] is not None and p["gt"] > cutoff and p["pos"] in positions
        and p["id"] not in budget.claimed_adds() and p["id"] not in budget.consumed
    ]
    if not adds:
        return {"ok": False, "reason": f"no free agent at {'/'.join(sorted(positions))} with a game later today"}
    add = max(adds, key=lambda p: p["sv"])

    streamed = set(streamed_today)
    drops = [
        p for p in ros
        if p["pos"] != "G" and p["inj"] not in OUT and p["slot"] != SLOT["IR"]
        and p["sv"] <= scfg["max_drop_proj"] and p["proj"] < rules["max_drop_proj"]
        and p["id"] not in budget.pending_trade_ids and p["id"] not in budget.claimed_drops()
        and p["id"] not in budget.consumed and p["id"] not in streamed
        and not p["locked"] and p["sv"] <= add["sv"] + scfg["drop_margin"]
    ]
    if not drops:
        return {"ok": False, "reason": f"no eligible drop for {add['n']} ({add['sv']:.2f})", "add": add}
    drop = min(drops, key=lambda p: (p["gt"] is not None, p["sv"]))
    return {"ok": True, "reason": "", "add": add, "drop": drop, "committed_after": budget.committed + 1,
            "limit": limit}


def describe(res, abbrev=None, dry=False):
    a, d = res["add"], res["drop"]
    team = abbrev or "?"
    return (f"stream{' (dry run)' if dry else ''}: +{a['n']} ({team}, plays today) -{d['n']}, "
            f"adds this week {res['committed_after']}/{res['limit']}")
