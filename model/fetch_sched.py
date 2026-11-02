"""Pull every NHL team's regular-season schedule (public NHL API) into data/sched/sched_<TEAM>.json.
trade_eval.py uses these for its daily lineup season sim.
Usage: python3 -I fetch_sched.py [outdir]   (default: model/data/sched, or $FHOCKEY_MODEL_DATA/sched)"""
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SEASON = os.environ.get("FHOCKEY_NHL_SEASON", "20262027")
TEAMS = ("ANA BOS BUF CAR CBJ CGY CHI COL DAL DET EDM FLA LAK MIN MTL NJD NSH NYI NYR OTT PHI PIT SEA SJS STL "
         "TBL TOR UTA VAN VGK WPG WSH").split()
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.environ.get("FHOCKEY_MODEL_DATA") or f"{HERE}/data", "sched")
os.makedirs(out, exist_ok=True)
for t in TEAMS:
    raw = subprocess.run(["curl", "-s", "--max-time", "60", f"https://api-web.nhle.com/v1/club-schedule-season/{t}/{SEASON}"],
                         capture_output=True, check=True).stdout
    d = json.loads(raw)
    json.dump(d, open(f"{out}/sched_{t}.json", "w"))
    print(t, sum(1 for g in d.get("games", []) if g.get("gameType") == 2), "regular-season games")
