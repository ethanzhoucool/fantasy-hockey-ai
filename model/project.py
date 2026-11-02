"""Daily rest-of-season projections for every NHL skater, from the backtested ridge model.
Refreshes this season's stats (to date), then writes proj.json ({"proj": {normalized name: pts/g}, "sd", "date"},
read by the fhockey service) and proj.js (`window.__PROJ = {...}`, the old browser format).
Prior seasons + model.json are static in data/ (rebuild: see README "Rebuilding the model data").
Env: FHOCKEY_MODEL_DATA (default model/data), FHOCKEY_PROJ_DIR (default model/).
Usage: python3 -I project.py"""
import json, os, subprocess, sys, urllib.parse, unicodedata, datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("FHOCKEY_MODEL_DATA") or f"{HERE}/data"
OUT = os.environ.get("FHOCKEY_PROJ_DIR") or HERE
CUR = "20262027"

def curl(kind, rep, exp):
    q = urllib.parse.urlencode({"isAggregate": "false", "isGame": "false", "start": 0, "limit": -1, "cayenneExp": exp})
    out = subprocess.run(["curl", "-s", "--max-time", "90", f"https://api.nhle.com/stats/rest/en/{kind}/{rep}?{q}"],
                         capture_output=True, check=True).stdout
    return json.loads(out)

# this season to date is the model's "early" sample
today = dt.date.today().isoformat()
for rep in ("summary", "realtime", "timeonice"):
    d = curl("skater", rep, f'seasonId={CUR} and gameTypeId=2 and gameDate<="{today}"')["data"]
    if len(d) < 300: sys.exit(f"NHL API returned only {len(d)} rows for {rep}; keeping old projections")
    json.dump(d, open(f"{DATA}/skater_{rep}_{CUR}_early.json", "w"))
json.dump(curl("skater", "bios", f"seasonId={CUR} and gameTypeId=2"), open(f"{DATA}/skater_bios_{CUR}.json", "w"))
# trade_eval.py values goalies from this file, so it has to be refreshed with the skaters
g = curl("goalie", "summary", f'seasonId={CUR} and gameTypeId=2 and gameDate<="{today}"')["data"]
if len(g) < 40: sys.exit(f"NHL API returned only {len(g)} goalie rows; keeping old projections")
json.dump(g, open(f"{DATA}/goalie_summary_{CUR}_early.json", "w"))

sys.argv = [sys.argv[0], DATA]
sys.path.insert(0, HERE)
import bt_model as M

mdl = json.load(open(f"{DATA}/model.json"))
norm = lambda s: unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().replace(".", "").replace("-", " ").strip()
E, P1, P2 = M.EARLY[CUR], M.FULL["20252026"], M.FULL["20242025"]
proj = {}
for pid, e in E.items():
    x = M.features(pid, CUR, e, P1.get(pid), P2.get(pid))
    v = M.pred_ridge(mdl["beta"], mdl["cols"], x)
    if v >= 1.3: proj[norm(e["name"])] = round(v, 2)  # below this nobody is fantasy-relevant; keeps the paste small
with open(f"{OUT}/proj.js", "w") as f:
    f.write(f"window.__PROJ = {json.dumps(proj, separators=(',', ':'))}; window.__PROJ_SD = {mdl['sd']:.3f}; window.__PROJ_DATE = '{today}';\n")
tmp = f"{OUT}/proj.json.tmp"
with open(tmp, "w") as f:
    json.dump({"proj": proj, "sd": round(mdl["sd"], 3), "date": today}, f, separators=(",", ":"))
os.replace(tmp, f"{OUT}/proj.json")
print(f"wrote {len(proj)} projections for {today} -> {OUT}/proj.json")
