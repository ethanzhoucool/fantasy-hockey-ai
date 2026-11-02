"""Backtest rest-of-season (ROS) fantasy pts/game projections for skaters.

Train/test setup per season Y: features = first ~13 games of Y (early cut) + full seasons Y-1, Y-2 + age;
target = actual fantasy pts/g over the rest of Y. Leave-one-season-out CV over 2022-23..2025-26.
Compares: last-season only, early only, the current bot blend, and a fitted ridge model.
Also fits a games-played (availability) model by age / prior durability.
Usage: python3 -I bt_model.py <bt dir>   -> prints CV report, writes <bt dir>/model.json"""
import json, sys, math, statistics as st

D = sys.argv[1]
SEASONS = ["20212022", "20222023", "20232024", "20242025", "20252026", "20262027"]
TEST = ["20222023", "20232024", "20242025", "20252026"]
SEASON_GAMES = 82

def fp(s, r):
    return (2*s["goals"] + s["assists"] + .5*(s.get("ppPoints") or 0) + .5*(s.get("shPoints") or 0)
            + .1*(s.get("shots") or 0) + .1*(r.get("hits") or 0) + .5*(r.get("blockedShots") or 0))

def load(season, tag):
    j = lambda rep: {p["playerId"]: p for p in json.load(open(f"{D}/skater_{rep}_{season}_{tag}.json"))}
    sm, rt, ti = j("summary"), j("realtime"), j("timeonice")
    out = {}
    for pid, s in sm.items():
        r, t = rt.get(pid, {}), ti.get(pid, {})
        gp = s["gamesPlayed"]
        if not gp: continue
        out[pid] = dict(gp=gp, fp=fp(s, r), sog=s.get("shots") or 0, g=s["goals"], a=s["assists"],
                        phys=.1*(r.get("hits") or 0) + .5*(r.get("blockedShots") or 0),
                        toi=(s.get("timeOnIcePerGame") or 0)/60, pptoi=(t.get("ppTimeOnIcePerGame") or 0)/60,
                        pos=s["positionCode"], name=s["skaterFullName"], team=s["teamAbbrevs"].split(",")[-1])
    return out

FULL = {s: load(s, "full") for s in SEASONS if s != "20262027"}
EARLY = {s: load(s, "early") for s in SEASONS if s != "20212022"}
BIRTH = {}
for s in SEASONS[1:]:
    for p in json.load(open(f"{D}/skater_bios_{s}.json"))["data"]:
        BIRTH[p["playerId"]] = p["birthDate"]

def age(pid, season):
    b = BIRTH.get(pid)
    if not b: return 27.0
    y = int(season[:4])
    return (y - int(b[:4])) + (10 - int(b[5:7]))/12  # age on Oct 1

def per_g(d, k): return d[k]/d["gp"]

def features(pid, season, e, p1, p2):
    """Feature vector for a player at the early cut of `season`. Missing priors fall back to shrunk early rates."""
    a = age(pid, season)
    efp = per_g(e, "fp")
    has1 = p1 is not None and p1["gp"] >= 20
    p1fp = per_g(p1, "fp") if has1 else None
    p2fp = per_g(p2, "fp") if p2 is not None and p2["gp"] >= 20 else None
    base = p1fp if has1 else 0.6*efp + 0.4*1.3  # no real prior: shrink early rate toward a replacement-level 1.3
    return dict(
        efp=efp, egp=e["gp"], p1fp=base, p2fp=p2fp if p2fp is not None else base, has1=1.0 if has1 else 0.0,
        etoi=e["toi"], epp=e["pptoi"], p1toi=p1["toi"] if has1 else e["toi"], p1pp=p1["pptoi"] if has1 else e["pptoi"],
        esog=per_g(e, "sog"), ephys=per_g(e, "phys"), eg=per_g(e, "g"),
        p1sog=per_g(p1, "sog") if has1 else per_g(e, "sog"), p1g=per_g(p1, "g") if has1 else per_g(e, "g"),
        age=a, old=max(0.0, a - 31), young=max(0.0, 24 - a), isD=1.0 if e["pos"] == "D" else 0.0,
        p1gp=(p1["gp"] if has1 else 60)/SEASON_GAMES)

def dataset(season):
    i = SEASONS.index(season)
    full, early = FULL[season], EARLY[season]
    prev1, prev2 = FULL[SEASONS[i-1]], (FULL.get(SEASONS[i-2]) if i >= 2 else {})
    rows = []
    for pid, e in early.items():
        f = full.get(pid)
        if e["gp"] < 5 or not f: continue
        rgp = f["gp"] - e["gp"]
        x = features(pid, season, e, prev1.get(pid), prev2.get(pid))
        y = (f["fp"] - e["fp"])/rgp if rgp > 0 else None
        rows.append(dict(pid=pid, name=e["name"], x=x, y=y, rgp=rgp, a=x["age"], has1=x["has1"]))
    return rows

# ---------- models ----------
COLS = ["efp", "p1fp", "p2fp", "etoi", "epp", "p1toi", "p1pp", "esog", "ephys", "p1sog", "eg", "p1g", "old", "young", "isD", "has1"]

def solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c and M[c][c]:
                f = M[r][c]/M[c][c]
                for k in range(c, n+1): M[r][k] -= f*M[c][k]
    return [M[i][n]/M[i][i] for i in range(n)]

def ridge(rows, cols, lam=2.0):
    X = [[1.0] + [r["x"][c] for c in cols] for r in rows]
    w = [r["rgp"] for r in rows]
    y = [r["y"] for r in rows]
    k = len(X[0])
    A = [[sum(w[i]*X[i][a]*X[i][b] for i in range(len(X))) + (lam*sum(w) / 1000 if a == b and a > 0 else 0) for b in range(k)] for a in range(k)]
    bb = [sum(w[i]*X[i][a]*y[i] for i in range(len(X))) for a in range(k)]
    return solve(A, bb)

def pred_ridge(beta, cols, x): return beta[0] + sum(b*x[c] for b, c in zip(beta[1:], cols))
def pred_bot(x): w = x["egp"]/(x["egp"]+25); return w*x["efp"] + (1-w)*x["p1fp"]
def pred_last(x): return x["p1fp"]
def pred_early(x): return x["efp"]

def evaluate(name, rows, f):
    errs = [(abs(f(r["x"]) - r["y"]), r["rgp"]) for r in rows]
    mae = sum(e*w for e, w in errs)/sum(w for _, w in errs)
    # pairwise ordering accuracy among fantasy-relevant players (actual ROS 1.3-2.6), predicted gap >= .15
    rel = [r for r in rows if 1.3 <= r["y"] <= 2.6 and r["rgp"] >= 30]
    ok = tot = 0
    P = [(f(r["x"]), r["y"]) for r in rel]
    for i in range(len(P)):
        for j in range(i+1, len(P)):
            d = P[i][0] - P[j][0]
            if abs(d) < .15: continue
            tot += 1; ok += (d > 0) == (P[i][1] > P[j][1])
    return mae, ok/max(tot, 1), tot

def main():
    data = {s: [r for r in dataset(s) if r["y"] is not None and r["rgp"] >= 15] for s in TEST}
    print("rows per test season:", {s: len(v) for s, v in data.items()})
    res = {}
    for s in TEST:
        train = [r for t in TEST if t != s for r in data[t]]
        beta = ridge(train, COLS)
        for nm, f in (("last season only", pred_last), ("early games only", pred_early),
                      ("bot blend (gp/(gp+25))", pred_bot), ("ridge model", lambda x, b=beta: pred_ridge(b, COLS, x))):
            res.setdefault(nm, []).append(evaluate(nm, data[s], f))
    print(f"\n{'model':28s}  MAE(pts/g)  pairwise-order-acc  (leave-one-season-out, 4 folds)")
    for nm, v in res.items():
        print(f"{nm:28s}  {st.mean(x[0] for x in v):.3f}       {st.mean(x[1] for x in v):.3f}   (pairs/fold ~{int(st.mean(x[2] for x in v))})")

    beta = ridge([r for t in TEST for r in data[t]], COLS)
    allrows = [r for t in TEST for r in data[t]]
    resid = [r["y"] - pred_ridge(beta, COLS, r["x"]) for r in allrows if r["rgp"] >= 40]
    sd = st.pstdev(resid)
    print("\nfinal ridge coefficients:"); print("  intercept", round(beta[0], 3))
    for c, b in zip(COLS, beta[1:]): print(f"  {c:6s} {b:+.3f}")
    print(f"residual SD (ROS>=40 GP): {sd:.3f} pts/g")

    # availability: share of remaining games played, by age band (all players with early gp>=5)
    av = {}
    for t in TEST:
        for r in dataset(t):
            band = "<=30" if r["a"] < 31 else "31-34" if r["a"] < 35 else "35-37" if r["a"] < 38 else "38+"
            team_rem = SEASON_GAMES - r["x"]["egp"] - 1  # approx remaining team games
            av.setdefault(band, []).append(min(1.0, max(0, r["rgp"])/team_rem))
    avail = {b: round(st.mean(v), 3) for b, v in av.items()}
    print("ROS availability (share of remaining games played) by age:", avail, {b: len(v) for b, v in av.items()})
    json.dump(dict(cols=COLS, beta=beta, sd=sd, avail=avail), open(f"{D}/model.json", "w"), indent=1)

if __name__ == "__main__":
    main()
