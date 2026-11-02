"""How many starting slots does Ethan leave empty because his players have no game that day? Each empty slot-day is
points a streamer (a free agent whose team plays that day) could fill, within the 7 adds/week limit.
  python3 -I streaming.py league_<date>.txt [Name=YYYY-MM-DD ...]"""
import sys, os, datetime as dt, collections
HERE = os.path.dirname(os.path.abspath(__file__))
MY_TEAM = os.environ.get("FHOCKEY_TEAM_ID", "4")  # your ESPN teamId in the league file
TE = os.path.join(HERE, "..", "model", "trade_eval.py")
lf = sys.argv[1]; over = dict(a.split("=", 1) for a in sys.argv[2:])
src = open(TE).read().split("rows = [l.rstrip")[0]
sys.argv = [TE, lf]; g = {"__file__": TE, "__name__": "te"}; exec(compile(src, TE, "exec"), g)
rows = [l.rstrip("\n").split("|") for l in open(lf) if l.strip() and not l.startswith("#")]
mine = [g["player"](r[1], r[2], over.get(r[1], r[3])) for r in rows if r[0] == MY_TEAM]
fa = [g["player"](r[1], r[2], r[3]) for r in rows if r[0] == "FA" and r[2] != "G" and not r[3]]
GAMES = g["GAMES"]
week = collections.defaultdict(lambda: [0, 0, 0, 0])  # empty F, empty D, empty UTIL, best FA value sum
for day in g["REG_DAYS"]:
    on = lambda p: p["team"] and day in GAMES.get(p["team"], ()) and not (p["back"] and day < p["back"])
    f = sum(1 for p in mine if p["pos"] == "F" and on(p)); d = sum(1 for p in mine if p["pos"] == "D" and on(p))
    ef, ed = max(0, 9 - f), max(0, 5 - d)
    eu = 1 if f + d - min(f, 9) - min(d, 5) < 1 else 0
    w = dt.date.fromisoformat(day).isocalendar()[1]
    k = week[w]; k[0] += ef; k[1] += ed; k[2] += eu
    best = sorted((p["v"] for p in fa if p["team"] and day in GAMES.get(p["team"], ())), reverse=True)
    k[3] += sum(best[:ef + ed + eu]) if ef + ed + eu else 0
tot = [0, 0, 0]; capped = 0.0
for w, (ef, ed, eu, val) in sorted(week.items()):
    n = ef + ed + eu
    avg = val / n if n else 0
    capped += min(n, 7) * avg
    tot[0] += ef; tot[1] += ed; tot[2] += eu
print(f"rest of regular season: empty slot-days F {tot[0]}, D {tot[1]}, UTIL {tot[2]} over {len(week)} weeks")
print(f"streaming upper bound (best FA with a game fills each empty slot, max 7/week): ~{capped:.0f} pts")
for w, (ef, ed, eu, val) in list(sorted(week.items()))[:4]:
    print(f"  week {w}: empty F {ef} D {ed} UTIL {eu}")
