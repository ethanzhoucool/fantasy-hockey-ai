"""Pull NHL skater/goalie stats for backtesting: full seasons + an early-season cut (~12-13 GP),
so we can learn how to predict rest-of-season fantasy pts/g from early games + prior seasons.
Usage: python3 -I bt_fetch.py <outdir>"""
import json, sys, urllib.parse, os, subprocess

OUT = sys.argv[1]
CUTS = {"20212022": "2021-11-01", "20222023": "2022-10-31", "20232024": "2023-11-03",
        "20242025": "2024-11-01", "20252026": "2025-10-31", "20262027": "2026-10-31"}

def pull(kind, rep, season, cut=None):
    exp = f"seasonId={season} and gameTypeId=2" + (f' and gameDate<="{cut}"' if cut else "")
    q = urllib.parse.urlencode({"isAggregate": "false", "isGame": "false", "start": 0, "limit": -1, "cayenneExp": exp})
    out = subprocess.run(["curl", "-s", "--max-time", "90", f"https://api.nhle.com/stats/rest/en/{kind}/{rep}?{q}"],
                         capture_output=True, check=True).stdout
    return json.loads(out)["data"]

for s, cut in CUTS.items():
    for kind, reps in (("skater", ("summary", "realtime", "timeonice")), ("goalie", ("summary",))):
        for rep in reps:
            for tag, c in (("full", None), ("early", cut)):
                if s == "20262027" and tag == "full": continue  # current season = early cut only
                f = f"{OUT}/{kind}_{rep}_{s}_{tag}.json"
                if os.path.exists(f): continue
                d = pull(kind, rep, s, c)
                json.dump(d, open(f, "w"))
                print(f, len(d))
