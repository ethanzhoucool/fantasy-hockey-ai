"""The three things the service may change on ESPN: the lineup, rule-based pickups and daily streaming.
Each returns report lines. With execute=False nothing is written; the lines say what WOULD happen."""
from . import lineup as L
from . import pickups as P
from . import stream as S
from .espn import EspnError
from .players import prep


def run_lineup(view, execute, sp=None):
    sp = view.cur if sp is None else int(sp)
    if sp == view.cur:
        ros = view.roster()
    else:
        r = view.client.my_team(sp)
        team = next((t for t in r.get("teams") or [] if t["id"] == view.team_id), {})
        g = view.games(sp)
        ros = [prep(e["playerPoolEntry"]["player"], e.get("lineupSlotId"), g, view.now, view.proj,
                    view.cfg.season, view.cfg["value_overrides"])
               for e in (team.get("roster") or {}).get("entries") or []]
    res = L.optimize(ros)
    n = len(res["items"])
    ok = True
    if not n:
        saved = "no changes needed"
    elif execute:
        try:
            view.client.set_lineup(res["items"], sp, view.cur)
            saved = f"SAVED {n} moves"
        except EspnError as e:
            ok = False
            saved = f"SAVE FAILED: {e}"
    else:
        saved = f"DRY RUN: {n} moves not saved"
    lines = L.report(res, sp, saved)
    if n:
        lines.append("moves: " + ", ".join(L.describe_moves(res, ros)))
    if not len(view.proj):
        lines.append("WARNING: no model projections loaded (model/proj.json), used fallback blend")
    return {"lines": lines, "moves": n, "ok": ok, "result": res, "roster": ros, "saved": saved}


def run_pickups(view, execute, extra_ids=(), state=None):
    rules = view.cfg["rules"]
    ros = view.roster()
    fas = view.fa_pool(rules["fa_pool_limit"], ("FREEAGENT", "WAIVERS"), extra_ids)
    cands = P.candidates(ros, fas, view.proj.sd or 0.27, rules["candidate_min_diff"])
    budget = view.budget()
    moves, first_fail = P.decide(cands, budget, rules)
    lines, executed = [], []
    for m in moves:
        desc = P.describe(m)
        if not execute:
            verb = "would add" if m["kind"] == "FREEAGENT" else "would claim"
            lines.append(f"pickup (dry run): {verb} {desc}")
            continue
        try:
            if m["kind"] == "FREEAGENT":
                view.client.add_free_agent(m["add"]["id"], m["drop"]["id"], view.cur)
                if state is not None:
                    state["fa_add_sp"] = view.cur
                lines.append(f"pickup: ADDED {desc}")
            else:
                view.client.claim_waiver(m["add"]["id"], m["drop"]["id"], view.cur)
                lines.append(f"pickup: WAIVER CLAIM placed {desc}")
            executed.append(m)
        except EspnError as e:
            lines.append(f"pickup FAILED ({m['kind']} {desc}): {e}")
    if not moves:
        if first_fail:
            c, fails = first_fail
            lines.append(f"no move: best option {P.describe(c)} failed: {'; '.join(fails)}")
        else:
            lines.append("no move: no free agent beats your weakest players by 0.2+")
    lines.append(f"adds this week {budget.committed}/{rules['weekly_add_limit']} incl. pending claims"
                 + (" after these moves" if moves else ""))
    return {"lines": lines, "moves": moves, "executed": executed, "candidates": cands}


def run_stream(view, execute, state=None):
    scfg, rules = view.cfg["stream"], view.cfg["rules"]
    state = state if state is not None else {}
    ros = view.roster()
    res = L.optimize(ros)
    budget = view.budget()
    streamed = [int(pid) for pid, sp in (state.get("stream_adds") or {}).items() if sp == view.cur]
    days_left = view.days_left_in_week()
    # cheap exits first: no need to pull the free-agent pool if nothing can happen
    plan = S.plan(ros, res, [], budget, scfg, rules, view.now, view.cur, streamed, days_left)
    if plan["reason"].startswith("no free agent"):
        fas = view.fa_pool(scfg["fa_pool_limit"], ("FREEAGENT",))
        plan = S.plan(ros, res, fas, budget, scfg, rules, view.now, view.cur, streamed, days_left)
    if not plan["ok"]:
        return {"line": f"stream: no move ({plan['reason']})", "ok": False, "executed": False, "plan": plan}
    abbrev = view.abbrev(plan["add"]["pro_team"])
    if not execute:
        return {"line": S.describe(plan, abbrev, dry=True), "ok": True, "executed": False, "plan": plan}
    try:
        view.client.add_free_agent(plan["add"]["id"], plan["drop"]["id"], view.cur)
    except EspnError as e:
        return {"line": f"stream FAILED (+{plan['add']['n']} -{plan['drop']['n']}): {e}", "ok": False,
                "executed": False, "plan": plan}
    state.setdefault("stream_adds", {})[str(plan["add"]["id"])] = view.cur
    state["fa_add_sp"] = view.cur
    return {"line": S.describe(plan, abbrev), "ok": True, "executed": True, "plan": plan}
