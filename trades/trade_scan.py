"""Search every 1-for-1, 1-for-2, 2-for-1 and 2-for-2 skater trade with one partner and keep the ones that help
Ethan AND give the partner a real reason to accept. Rules: TRADE_PLAYBOOK.md.

  python3 -I trade_scan.py league_<date>.txt <partnerTeamId> [Name=YYYY-MM-DD ...]

league file: '#' comments, then team|name|pos|inj|espnProjAvg|seasonAvg|gp|pctOwned|lastNews (team 'FA' = free agents).
Name=date sets a player's expected return date from fresh injury news (overrides the generic IR/OUT guesses)."""
import sys, os, re, time, random, itertools, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
MY_TEAM = os.environ.get("FHOCKEY_TEAM_ID", "4")  # your ESPN teamId in the league file
TE = os.path.join(HERE, "..", "model", "trade_eval.py")
league_file, partner = sys.argv[1], sys.argv[2]
overrides = dict(a.split("=", 1) for a in sys.argv[3:])

# ---- freshness checks: refuse to run on stale inputs ----
now = time.time()
pulled = re.search(r"pulled (\S+)", open(league_file).readline())
age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(pulled[1].replace("Z", "+00:00"))).total_seconds() / 3600
data_dir = os.environ.get("FHOCKEY_MODEL_DATA") or os.path.join(HERE, "..", "model", "data")
stale = [f for f in os.listdir(data_dir) if "20262027" in f and now - os.path.getmtime(os.path.join(data_dir, f)) > 30 * 3600]
if age_h > 24: sys.exit(f"league snapshot is {age_h:.0f}h old: pull rosters again")
if stale: sys.exit(f"model data older than 30h: {stale}. Run model/project.py")

# ---- load the sim from trade_eval.py (everything above its input parsing) ----
src = open(TE).read().split("rows = [l.rstrip")[0]
sys.argv = [TE, league_file]
g = {"__file__": TE, "__name__": "trade_eval"}
exec(compile(src, TE, "exec"), g)
player, season, fit, SD = g["player"], g["season"], g["fit"], g["SD"]

rows = [l.rstrip("\n").split("|") for l in open(league_file) if l.strip() and not l.startswith("#")]
info = {r[1]: dict(team=r[0], pos=r[2], inj=r[3], proj=r[4], avg=r[5], gp=int(r[6] or 0), news=r[8]) for r in rows}
today = dt.date.today()

def make(name):
    i = info[name]
    p = player(name, i["pos"], overrides.get(name, i["inj"]))
    # perceived value: what the other manager sees on ESPN (season pts/g and ESPN's projection)
    s, pr = float(i["avg"] or i["proj"] or 1.5), float(i["proj"] or i["avg"] or 1.5)
    w = min(i["gp"], 8) / 8
    p["perc"] = w * (0.6 * s + 0.4 * pr) + (1 - w) * pr
    if p["back"]:  # managers discount injured players by how long they are out
        out = (dt.date.fromisoformat(p["back"]) - today).days
        p["perc"] *= 1 - min(max(out, 0), 60) / 150
    return p

mine = [make(r[1]) for r in rows if r[0] == MY_TEAM]
theirs = [make(r[1]) for r in rows if r[0] == partner]
fa = [make(r[1]) for r in rows if r[0] == "FA" and r[2] != "G" and not r[3]]
best_fa = {pos: max((p["perc"] for p in fa if p["pos"] == pos), default=0) for pos in "FD"}
best_fa_model = {pos: max((p["v"] for p in fa if p["pos"] == pos), default=0) for pos in "FD"}

def perceived_lineup(r):
    """Partner's view: starting 9F/5D/1UTIL by perceived value, plus a little credit for bench depth."""
    f = sorted((p["perc"] for p in r if p["pos"] == "F"), reverse=True)
    d = sorted((p["perc"] for p in r if p["pos"] == "D"), reverse=True)
    rest = sorted(f[9:] + d[5:], reverse=True)
    return sum(f[:9]) + sum(d[:5]) + sum(rest[:1]) + 0.1 * sum(rest[1:4])

def partner_after(give, get):
    """The partner drops his lowest perceived skater for each extra player he takes in."""
    gn = {p["n"] for p in get}
    r = [p for p in theirs if p["n"] not in gn] + list(give)
    drops = []
    while len(r) > len(theirs):
        x = min((p for p in r if p["pos"] != "G" and not (p["back"] and p["back"] > today.isoformat())), key=lambda p: p["perc"])
        r.remove(x); drops.append(x["n"])
    for x in sorted(fa, key=lambda p: -p["perc"]):  # a freed roster spot gets filled from free agency
        if len(r) >= len(theirs): break
        r.append(x)
    return r, drops

mb, tb_sim, tb_perc = season(mine), season(fit(theirs, fa)), perceived_lineup(theirs)
cands = [p for p in mine if p["pos"] != "G"]
tcands = [p for p in theirs if p["pos"] != "G"]
keep, near = [], []
for a, b in [(1, 1), (1, 2), (2, 1), (2, 2)]:
    for give in itertools.combinations(cands, a):
        for get in itertools.combinations(tcands, b):
            # fairness: partner must not hand over the clearly best player in the deal
            if max(p["perc"] for p in get) > max(p["perc"] for p in give) + 0.1: continue
            # every player a team takes in must beat the free agent it could add instead
            if any(p["perc"] <= best_fa[p["pos"]] for p in give): continue
            if any(p["v"] <= best_fa_model[p["pos"]] for p in get): continue
            tr, tdrops = partner_after(give, get)
            if set(tdrops) & {p["n"] for p in give}: continue  # extra player would just be cut
            dperc = perceived_lineup(tr) - tb_perc
            if dperc < -0.05: continue
            gn = {p["n"] for p in give}
            me_r = fit([p for p in mine if p["n"] not in gn] + list(get), fa)
            me = season(me_r)
            dm = (me[0] - mb[0], me[1] - mb[1])
            if dm[0] + dm[1] < 8: continue
            th = season(fit([p for p in theirs if p["n"] not in {p["n"] for p in get}] + list(give), fa))
            dt_ = (th[0] - tb_sim[0], th[1] - tb_sim[1])
            my_drops = [p["n"] for p in mine if p["n"] not in gn and p not in me_r]
            rec = dict(give=[p["n"] for p in give], get=[p["n"] for p in get], me=dm, them=dt_, dperc=dperc,
                       tdrops=tdrops, mdrops=my_drops, objs=list(give) + list(get))
            (keep if dt_[0] + dt_[1] >= 0 else near).append(rec)

def pwin(rec, N=60):
    random.seed(7); w = 0
    gn = set(rec["give"])
    get = [p for p in rec["objs"] if p["n"] in rec["get"]]
    for _ in range(N):
        jit = {p["n"]: max(0.5, p["v"] + random.gauss(0, SD)) for p in rec["objs"]}
        sh = lambda r: [dict(p, v=jit[p["n"]]) if p["n"] in jit else p for p in r]
        b, n = season(sh(mine)), season(sh(fit([p for p in mine if p["n"] not in gn] + get, fa)))
        w += (n[0] + n[1]) > (b[0] + b[1])
    return w / N

def show(lst, k):
    for rec in sorted(lst, key=lambda r: -(r["me"][0] + r["me"][1]))[:k]:
        p = pwin(rec)
        bar = "CLEARS-BAR" if rec["me"][0] >= 15 and rec["me"][1] >= 0 and p >= 0.75 else ""
        inj = [f"{n} (inj {info[n]['inj'] or '-'}, news {info[n]['news']}, return {'override ' + overrides[n] if n in overrides else 'known ' + g['KNOWN_BACK'][n] if n in g['KNOWN_BACK'] else 'GENERIC GUESS'}{', NEWS >7 DAYS OLD' if info[n]['news'] < (today - dt.timedelta(days=7)).isoformat() else ''})"
               for n in rec["give"] + rec["get"] if info[n]["inj"] or n in overrides]
        print(f"give {' + '.join(rec['give'])} | get {' + '.join(rec['get'])}\n"
              f"   me {rec['me'][0]:+.1f}/{rec['me'][1]:+.1f} P{p:.0%} {bar} | partner sim {rec['them'][0]:+.1f}/{rec['them'][1]:+.1f}"
              f" | partner perceived lineup {rec['dperc']:+.2f}/g | partner drops {rec['tdrops'] or '-'} | I drop {rec['mdrops'] or '-'}"
              + (f"\n   injuries: {'; '.join(inj)}" if inj else ""))

print(f"snapshot {age_h:.1f}h old | partner {partner} | best FA perceived F {best_fa['F']:.2f} D {best_fa['D']:.2f}")
print(f"== MUTUAL (partner gains in his own view AND in the sim): {len(keep)}")
show(keep, 12)
print(f"== NEAR (partner gains in his own view, loses a bit in the sim): {len(near)}")
show(near, 6)
