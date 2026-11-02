"""Daily lineup optimizer, ported from bot.js.

Starts the highest-value players whose NHL team plays today: 9 F, 5 D, 1 UTIL (any skater), 2 G.
Players whose game has started (locked) and the IR slot are never moved. Moves are minimal: an idle starter
keeps his slot unless a playing player needs it.
"""
from .players import CAP, OUT, SLOT, SLOT_NAME, STARTING


def optimize(ros):
    """ros: prepped players (players.prep). Returns a dict with the ESPN items and a summary."""
    fixed = {p["id"] for p in ros if p["locked"] or p["slot"] == SLOT["IR"]}
    left = dict(CAP)
    for p in ros:
        if p["id"] in fixed and p["slot"] in left:
            left[p["slot"]] -= 1
    want = {}
    cands = sorted((p for p in ros if p["id"] not in fixed and p["plays"]), key=lambda p: -p["v"])
    for p in cands:
        s = SLOT["G"] if p["pos"] == "G" else SLOT["D"] if p["pos"] == "D" else SLOT["F"]
        if left[s] > 0:
            want[p["id"]] = s
            left[s] -= 1
    util = next((p for p in cands if p["id"] not in want and p["pos"] != "G"), None)
    if util and left[SLOT["UTIL"]] > 0:
        want[util["id"]] = SLOT["UTIL"]
        left[SLOT["UTIL"]] -= 1

    # Minimal moves: idle players keep their slot unless it's needed; playing non-starters go to bench.
    to = {}
    for p in ros:
        if p["id"] in fixed:
            continue
        to[p["id"]] = want.get(p["id"]) or (p["slot"] if p["slot"] in CAP and not p["plays"] else SLOT["BE"])
    for s in sorted(CAP):
        occ = [p for p in ros if to.get(p["id"], p["slot"]) == s]
        over = len(occ) - CAP[s]
        for p in occ:
            if p["id"] in fixed or p["id"] in want:
                continue
            if over > 0:
                to[p["id"]] = SLOT["BE"]
            over -= 1

    final = {p["id"]: to.get(p["id"], p["slot"]) for p in ros}
    items = [
        {"playerId": p["id"], "type": "LINEUP", "fromLineupSlotId": p["slot"], "toLineupSlotId": to[p["id"]]}
        for p in ros
        if p["id"] not in fixed and to[p["id"]] != p["slot"]
    ]
    starters = [p for p in ros if final[p["id"]] in STARTING and p["plays"]]
    filled = {s: sum(1 for p in starters if final[p["id"]] == s) for s in CAP}
    return {
        "items": items,
        "final": final,
        "starters": starters,
        "proj_pts": sum(p["v"] for p in starters),
        "empty": {s: CAP[s] - filled[s] for s in CAP},
        "benched": [p for p in ros if p["plays"] and final[p["id"]] == SLOT["BE"]],
        "hurt": [p for p in ros if p["inj"] in OUT and p["slot"] != SLOT["IR"]],
        "ir_ok": [p for p in ros if p["slot"] == SLOT["IR"] and p["inj"] not in OUT],
    }


def report(res, sp, saved):
    lines = [f"sp {sp} | {saved} | {len(res['starters'])} starters, ~{res['proj_pts']:.1f} proj pts"]
    if res["benched"]:
        lines.append("benched despite game: " + ", ".join(f"{p['n']}({p['v']:.1f})" for p in res["benched"]))
    if res["hurt"]:
        lines.append("out but not on IR: " + ", ".join(f"{p['n']} {p['inj']}" for p in res["hurt"]))
    if res["ir_ok"]:
        lines.append("IR slot player now healthy: " + ", ".join(f"{p['n']} {p['inj']}" for p in res["ir_ok"]))
    empty = {SLOT_NAME[s]: n for s, n in res["empty"].items() if n > 0 and s != SLOT["G"]}
    if empty:
        lines.append("empty skater slots: " + ", ".join(f"{k} {v}" for k, v in empty.items()))
    return lines


def describe_moves(res, ros):
    names = {p["id"]: p["n"] for p in ros}
    return [
        f"{names.get(i['playerId'], i['playerId'])} {SLOT_NAME.get(i['fromLineupSlotId'], i['fromLineupSlotId'])}"
        f"->{SLOT_NAME.get(i['toLineupSlotId'], i['toLineupSlotId'])}"
        for i in res["items"]
    ]
