"""For a 1-for-2 trade, Ethan must name the player he drops. Score the deal with each realistic drop forced,
under all three valuations plus stress, so the named drop is the best one (not the sim's automatic choice).
  python3 -I dropcheck.py league.txt "partner:Give:GetA,GetB" "Drop A" "Drop B" ... [--inj Name=YYYY-MM-DD ...]"""
import sys, os
args = sys.argv[1:]; lf = args.pop(0); inj = []
if "--inj" in args: i = args.index("--inj"); inj = args[i + 1:]; args = args[:i]
deal, drops = args[0], args[1:]
partner, give, get = deal.split(":"); give, get = set(give.split(",")), set(get.split(","))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.argv = ["robust.py", lf] + (["--inj"] + inj if inj else [])
src = open(os.path.join(HERE, "robust.py")).read().split("for partner, give, get in deals:")[0]
g = {"__file__": os.path.join(HERE, "robust.py"), "__name__": "r"}; exec(compile(src, "robust.py", "exec"), g)
make, season, fit, rows, shift = g["make"], g["season"], g["fit"], g["rows"], g["shift"]

def run(view, drop, stress=False):
    mine = [make(r[1], view) for r in rows if r[0] == "4"]
    fa = [make(r[1], view) for r in rows if r[0] == "FA" and r[2] != "G" and not r[3]]
    gets = [make(r[1], view) for r in rows if r[0] == partner and r[1] in get]
    if stress:
        gets = [dict(p, v=p["v"] - 0.15, back=shift(p["back"], 14)) for p in gets]
        mine = [dict(p, back=shift(p["back"], -7)) if p["n"] in give and p["back"] else p for p in mine]
    b = season(fit(mine, fa))
    n = season([p for p in mine if p["n"] not in give and p["n"] != drop] + gets)  # exactly 23, drop forced
    return n[0] - b[0], n[1] - b[1]

print(f"deal: give {' + '.join(give)} | get {' + '.join(get)}")
for d in drops:
    res = [run(v, d) for v in ("model", "espn", "avg")]; st = run("model", d, True)
    tot = [a + b for a, b in res] + [st[0] + st[1]]
    print(f"  drop {d:18} model {res[0][0]:+5.1f}/{res[0][1]:+5.1f}  espn {res[1][0]:+5.1f}/{res[1][1]:+5.1f}  avg {res[2][0]:+5.1f}/{res[2][1]:+5.1f}  stress {st[0]:+5.1f}/{st[1]:+5.1f}  | worst total {min(tot):+.1f}", flush=True)
