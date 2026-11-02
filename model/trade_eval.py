"""Score a trade for Ethan (team 4) with the backtested ridge projections + a daily lineup season sim.

Input file, one line per item (pos = F, D or G; inj = blank, DTD, OUT, IR, or a return date YYYY-MM-DD):
  MINE|name|pos|inj        every player on Ethan's roster
  THEIRS|name|pos|inj      every player on the partner's roster (optional; enables the partner's side)
  GIVE|name                players Ethan sends
  GET|name                 players Ethan receives
  DROP|name                Ethan's named drop on an uneven trade (leaves his roster, partner doesn't get him)
  FA|name|pos              best free agents (optional; used to refill a roster that ends up short)
Prints the season-sim gain (regular season | playoffs), P(trade helps), the partner's gain, and a verdict.
Usage: python3 -I trade_eval.py trade.txt"""
import json, os, sys, random, itertools, unicodedata, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("FHOCKEY_MODEL_DATA") or f"{HERE}/data"
CUR = "20262027"
REG_END, PO_START, PO_END = "2027-03-07", "2027-03-08", "2027-04-04"
ROSTER_SIZE = 23
OVERRIDE = {"Aleksander Barkov": 2.0}  # missed all of last season; elite when healthy
# from news checked Nov 1 2026; re-check anything older than 7 days (TRADE_PLAYBOOK.md)
KNOWN_BACK = {"Nick Suzuki": "2026-11-18", "Aleksander Barkov": "2026-11-26", "Bryan Rust": "2026-12-05"}

trade_file = sys.argv[1]
sys.argv = [sys.argv[0], DATA]
sys.path.insert(0, HERE)
import bt_model as M

norm = lambda s: unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace(".", "").replace("-", " ").strip()
mdl = json.load(open(f"{DATA}/model.json"))
SD = mdl["sd"]
TODAY = dt.date.today().isoformat()

# ---- skater values: ridge rest-of-season pts/g ----
SK = {}
E, P1, P2 = M.EARLY[CUR], M.FULL["20252026"], M.FULL["20242025"]
for pid, e in E.items():
    x = M.features(pid, CUR, e, P1.get(pid), P2.get(pid))
    SK[norm(e["name"])] = (M.pred_ridge(mdl["beta"], mdl["cols"], x), e["team"])
for pid, p in P1.items():  # hurt / not yet played this season: fall back to last season
    SK.setdefault(norm(p["name"]), (0.85 * M.per_g(p, "fp") + 0.15 * 1.5 if p["gp"] >= 20 else 1.4, p["team"]))

# ---- goalie values: (start share, pts per start), blended this season vs last ----
def goalies(f):
    out = {}
    for g in json.load(open(f)):
        gs = g.get("gamesStarted") or 0
        if not g["gamesPlayed"]: continue
        pts = 4*g["wins"] - 2*g["goalsAgainst"] + .2*(g.get("saves") or 0) + 3*g["shutouts"] + (g.get("otLosses") or 0)
        out[norm(g["goalieFullName"])] = dict(pps=pts/max(gs, 1), gs=gs, team=g["teamAbbrevs"].split(",")[-1])
    return out
G_NOW, G_LAST = goalies(f"{DATA}/goalie_summary_{CUR}_early.json"), goalies(f"{DATA}/goalie_summary_20252026_full.json")

GAMES, TGP = {}, {}
for f in os.listdir(f"{DATA}/sched"):
    gs = [g for g in json.load(open(f"{DATA}/sched/{f}"))["games"] if g.get("gameType") == 2]
    GAMES[f[6:-5]] = {g["gameDate"] for g in gs}
    TGP[f[6:-5]] = sum(1 for g in gs if g["gameDate"] < TODAY)

def goalie_value(k):
    c, l = G_NOW.get(k), G_LAST.get(k)
    team = (c or l or {}).get("team")
    tg = max(TGP.get(team, 1), 1)
    wsh, w = tg/(tg+20), (c["gs"]/(c["gs"]+15)) if c else 0
    share = wsh*((c["gs"]/tg) if c else 0) + (1-wsh)*(min(l["gs"]/82, 0.75) if l else 0.35)
    pps = w*(c["pps"] if c else 0) + (1-w)*(l["pps"] if l and l["gs"] >= 10 else 2.6)
    return share, pps, team

def back_date(name, inj):
    if inj[:2] == "20": return inj
    if name in KNOWN_BACK and inj: return KNOWN_BACK[name]
    add = {"DTD": 2, "OUT": 14, "IR": 30, "SUSPENSION": 7}.get(inj.upper())
    return (dt.date.today() + dt.timedelta(days=add)).isoformat() if add else None

UNKNOWN = []
def player(name, pos, inj=""):
    k = norm(name)
    p = dict(n=name, pos="G" if pos == "G" else "D" if pos == "D" else "F", back=back_date(name, inj or ""))
    if p["pos"] == "G":
        p["share"], p["pps"], p["team"] = goalie_value(k)
        p["v"] = p["share"]*p["pps"]
    else:
        v, p["team"] = SK.get(k, (1.4, None))
        p["v"] = OVERRIDE.get(name, v)
    if p["team"] is None: UNKNOWN.append(name)
    return p

def days(a, b):
    d, e = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    while d <= e:
        yield d.isoformat(); d += dt.timedelta(days=1)
REG_DAYS, PO_DAYS = list(days(TODAY, REG_END)), list(days(PO_START, PO_END))

def day_pts(roster, day):
    f, d, g = [], [], []
    for p in roster:
        if p["team"] is None or day not in GAMES.get(p["team"], ()) or (p["back"] and day < p["back"]): continue
        (f if p["pos"] == "F" else d if p["pos"] == "D" else g).append(p)
    fv, dv = sorted((p["v"] for p in f), reverse=True), sorted((p["v"] for p in d), reverse=True)
    util = sorted(fv[9:] + dv[5:], reverse=True)
    pts = sum(fv[:9]) + sum(dv[:5]) + (util[0] if util else 0)
    for st in itertools.product((0, 1), repeat=len(g)):  # expected best-2 goalie starts
        pr = 1.0
        for s, x in zip(st, g): pr *= x["share"] if s else 1 - x["share"]
        pts += pr * sum(sorted((x["pps"] for s, x in zip(st, g) if s), reverse=True)[:2])
    return pts

def season(r): return sum(day_pts(r, d) for d in REG_DAYS), sum(day_pts(r, d) for d in PO_DAYS)

def fit(roster, fa):
    """Back to 23: drop the healthy skater whose loss costs the least in the sim, or add the best unused free agent."""
    roster = list(roster)
    while len(roster) > ROSTER_SIZE:
        cands = sorted((p for p in roster if not p["back"] and p["pos"] != "G"), key=lambda x: x["v"])[:6]
        roster.remove(max(cands, key=lambda x: sum(season([q for q in roster if q is not x]))))
    used = {p["n"] for p in roster}
    for p in sorted(fa, key=lambda x: -x["v"]):
        if len(roster) >= ROSTER_SIZE: break
        if p["n"] not in used: roster.append(p)
    while len(roster) < ROSTER_SIZE: roster.append(dict(n="replacement", pos="F", team="NSH", back=None, v=1.6))
    return roster

rows = [l.rstrip("\n").split("|") for l in open(trade_file) if l.strip()]
mine = [player(r[1], r[2], r[3] if len(r) > 3 else "") for r in rows if r[0] == "MINE"]
theirs = [player(r[1], r[2], r[3] if len(r) > 3 else "") for r in rows if r[0] == "THEIRS"]
fa = [player(r[1], r[2]) for r in rows if r[0] == "FA"]
give = {r[1] for r in rows if r[0] == "GIVE"}
drop = {r[1] for r in rows if r[0] == "DROP"}
get = {r[1] for r in rows if r[0] == "GET"}
missing = ((give | drop) - {p["n"] for p in mine}) | (get - {p["n"] for p in theirs} if theirs else set())
if missing: sys.exit(f"names not found on the rosters: {sorted(missing)}")
get_p = [p for p in theirs if p["n"] in get] if theirs else [player(n, "F") for n in get]

def after_me(m, gp): return fit([p for p in m if p["n"] not in give | drop] + gp, fa)
base, new = season(mine), season(after_me(mine, get_p))
reg, po = new[0] - base[0], new[1] - base[1]

# uncertainty: redraw every traded skater's true rate from the model's residual SD and re-run the sim
random.seed(7)
moved = [p for p in mine if p["n"] in give] + get_p
wins, N = 0, 60
for _ in range(N):
    jit = {p["n"]: max(0.5, p["v"] + random.gauss(0, SD)) if p["pos"] != "G" else p["v"] for p in moved}
    shake = lambda r: [dict(p, v=jit[p["n"]]) if p["n"] in jit and p["pos"] != "G" else p for p in r]
    b, n = season(shake(mine)), season(shake(after_me(mine, get_p)))
    wins += (n[0] + n[1]) > (b[0] + b[1])
pwin = wins / N

print("GIVE " + ", ".join(f"{p['n']} {p['v']:.2f}" for p in mine if p["n"] in give))
if drop: print("DROP " + ", ".join(f"{p['n']} {p['v']:.2f}" for p in mine if p["n"] in drop))
print("GET  " + ", ".join(f"{p['n']} {p['v']:.2f}" for p in get_p))
print(f"me: reg {reg:+.1f} | playoffs {po:+.1f} | P(helps) {pwin:.0%}")
if theirs:
    tb = season(fit(theirs, fa))
    tn = season(fit([p for p in theirs if p["n"] not in get] + [p for p in mine if p["n"] in give], fa))
    print(f"partner: reg {tn[0]-tb[0]:+.1f} | playoffs {tn[1]-tb[1]:+.1f}")
if UNKNOWN: print("WARNING no NHL data for: " + ", ".join(UNKNOWN))
verdict = ("ACCEPT" if reg >= 15 and po >= 0 and pwin >= 0.75 else
           "CLOSE - counter for a bit more" if reg + po > 0 else "DECLINE")
print("VERDICT " + verdict)
