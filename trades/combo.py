"""Ethan's season-sim change if several offers are accepted together (rosters refit to 23, sim picks the drops).
  python3 -I combo.py league.txt "KEY=partner:Give,..:Get,.." ... [--inj Name=YYYY-MM-DD ...]"""
import sys, os, itertools
args = sys.argv[1:]; lf = args.pop(0); inj = []
if "--inj" in args: i = args.index("--inj"); inj = args[i + 1:]; args = args[:i]
D = {}
for a in args:
    k, rest = a.split("=", 1); p, gv, gt = rest.split(":"); D[k] = (p, set(gv.split(",")), set(gt.split(",")))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["robust.py", lf] + (["--inj"] + inj if inj else [])
src = open(os.path.join(HERE, "robust.py")).read().split("for partner, give, get in deals:")[0]
g = {"__file__": os.path.join(HERE, "robust.py"), "__name__": "r"}; exec(compile(src, "robust.py", "exec"), g)
make, season, fit, rows, shift = g["make"], g["season"], g["fit"], g["rows"], g["shift"]

def run(view, keys, stress=False):
    mine = [make(r[1], view) for r in rows if r[0] == "4"]
    fa = [make(r[1], view) for r in rows if r[0] == "FA" and r[2] != "G" and not r[3]]
    give = set().union(*[D[k][1] for k in keys]); gets = []
    for k in keys: gets += [make(r[1], view) for r in rows if r[0] == D[k][0] and r[1] in D[k][2]]
    if stress:
        gets = [dict(p, v=p["v"] - 0.15, back=shift(p["back"], 14)) for p in gets]
        mine = [dict(p, back=shift(p["back"], -7)) if p["n"] in give and p["back"] else p for p in mine]
    b = season(fit(mine, fa)); nr = fit([p for p in mine if p["n"] not in give] + gets, fa); n = season(nr)
    return (n[0] - b[0], n[1] - b[1]), [p["n"] for p in mine if p["n"] not in give and p not in nr]

for k in range(1, len(D) + 1):
    for keys in itertools.combinations(sorted(D), k):
        res = {v: run(v, keys) for v in ("model", "espn", "avg")}; st = run("model", keys, True)
        print("+".join(keys), " ".join(f"{v} {res[v][0][0]:+.1f}/{res[v][0][1]:+.1f}" for v in res),
              f"stress {st[0][0]:+.1f}/{st[0][1]:+.1f}", "drops", res["model"][1], flush=True)
