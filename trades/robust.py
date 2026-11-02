"""Robustness check for candidate trades (TRADE_PLAYBOOK.md): re-run Ethan's season sim with every skater valued
three ways (our model, ESPN projection, season pts/g) plus a stress test, and the partner's sim under the same views.

  python3 -I robust.py league_<date>.txt "partner:Give A,Give B:Get A,Get B" ... [--inj Name=YYYY-MM-DD ...]

Stress = players Ethan receives score 0.15 pts/g less, injured players he receives return 14 days late, and
injured players he sends return 7 days early. A deal is ROBUST when Ethan gains (reg + playoffs > 0) in at least
2 of the 3 valuations AND under stress."""
import sys, os, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
MY_TEAM = os.environ.get("FHOCKEY_TEAM_ID", "4")  # your ESPN teamId in the league file
TE = os.path.join(HERE, "..", "model", "trade_eval.py")
args = sys.argv[1:]
league_file = args.pop(0)
inj_over = {}
if "--inj" in args:
    i = args.index("--inj"); inj_over = dict(a.split("=", 1) for a in args[i + 1:]); args = args[:i]
deals = [a.split(":") for a in args]

src = open(TE).read().split("rows = [l.rstrip")[0]
sys.argv = [TE, league_file]
g = {"__file__": TE, "__name__": "trade_eval"}
exec(compile(src, TE, "exec"), g)
player, season, fit = g["player"], g["season"], g["fit"]

rows = [l.rstrip("\n").split("|") for l in open(league_file) if l.strip() and not l.startswith("#")]
info = {r[1]: r for r in rows}

def make(name, view):
    r = info[name]
    p = player(name, r[2], inj_over.get(name, r[3]))
    if p["pos"] != "G" and view != "model":
        proj, avg, gp = float(r[4] or r[5] or 1.5), float(r[5] or r[4] or 1.5), int(r[6] or 0)
        if view == "espn": p["v"] = proj
        else:
            w = min(gp, 10) / 10  # season pts/g, shrunk toward ESPN's projection on small samples
            p["v"] = w * avg + (1 - w) * proj
    return p

def shift(back, days):
    return (dt.date.fromisoformat(back) + dt.timedelta(days=days)).isoformat() if back else back

def run(view, partner, give, get, stress=False):
    mine = [make(r[1], view) for r in rows if r[0] == MY_TEAM]
    theirs = [make(r[1], view) for r in rows if r[0] == partner]
    fa = [make(r[1], view) for r in rows if r[0] == "FA" and r[2] != "G" and not r[3]]
    gp = [p for p in theirs if p["n"] in get]
    if stress:
        gp = [dict(p, v=p["v"] - 0.15 if p["pos"] != "G" else p["v"], back=shift(p["back"], 14)) for p in gp]
        mine = [dict(p, back=shift(p["back"], -7)) if p["n"] in give and p["back"] else p for p in mine]
    b, n = season(fit(mine, fa)), season(fit([p for p in mine if p["n"] not in give] + gp, fa))
    tb = season(fit(theirs, fa))
    tn = season(fit([p for p in theirs if p["n"] not in get] + [p for p in mine if p["n"] in give], fa))
    return (n[0] - b[0], n[1] - b[1]), (tn[0] - tb[0], tn[1] - tb[1])

for partner, give, get in deals:
    give, get = set(give.split(",")), set(get.split(","))
    res = {v: run(v, partner, give, get) for v in ("model", "espn", "avg")}
    st = run("model", partner, give, get, stress=True)
    wins = sum(res[v][0][0] + res[v][0][1] > 0 for v in res)
    robust = wins >= 2 and st[0][0] + st[0][1] > 0
    print(f"team {partner}: give {' + '.join(sorted(give))} | get {' + '.join(sorted(get))}  -> {'ROBUST' if robust else 'FRAGILE'}")
    for v in ("model", "espn", "avg"):
        (a, b), (c, d) = res[v]
        print(f"   {v:5}  me {a:+6.1f}/{b:+5.1f}   partner {c:+6.1f}/{d:+5.1f}")
    print(f"   stress me {st[0][0]:+6.1f}/{st[0][1]:+5.1f}")
