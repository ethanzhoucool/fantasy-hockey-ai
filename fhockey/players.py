"""Player helpers: positions, injuries, ESPN stat lines, model projections and the bot.js valuation."""
import json
import math
import re
import unicodedata
from pathlib import Path

# ESPN lineup slot ids for this league format
SLOT = {"F": 3, "D": 4, "UTIL": 6, "G": 5, "BE": 7, "IR": 8}
CAP = {3: 9, 4: 5, 6: 1, 5: 2}  # starting slots: 9 F, 5 D, 1 UTIL, 2 G
STARTING = (3, 4, 6, 5)
OUT = ("OUT", "INJURY_RESERVE", "SUSPENSION")
INJ_SHORT = {"DAY_TO_DAY": "DTD", "OUT": "OUT", "INJURY_RESERVE": "IR", "SUSPENSION": "SUSPENSION"}
POS_BY_DEFAULT = {1: "F", 2: "F", 3: "F", 4: "D", 5: "G"}
SLOT_NAME = {v: k for k, v in SLOT.items()}


def norm(name):
    """Projection key: accents stripped, lowercase, dots removed, hyphens as spaces (same as model/project.py)."""
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return s.lower().replace(".", "").replace("-", " ").strip()


def js_round(x):
    """JavaScript Math.round (halves round up), so ports match bot.js / snapshot.js output."""
    return int(math.floor(x + 0.5))


def position(p):
    es = p.get("eligibleSlots") or []
    return "G" if 5 in es else "D" if 4 in es else "F"


def stat(p, season, source, split=0):
    """ESPN stat line: source 0 = actual, 1 = ESPN projection; split 0 = whole season."""
    for s in p.get("stats") or []:
        if s.get("seasonId") == season and s.get("statSourceId") == source and s.get("statSplitTypeId") == split:
            return s
    return None


def _gp_raw(s):
    return s["appliedTotal"] / s["appliedAverage"] if s and s.get("appliedAverage") else 0


def games_played(p, season):
    return js_round(_gp_raw(stat(p, season, 0)))


def espn_proj_avg(p, season):
    s = stat(p, season, 1)
    return s.get("appliedAverage") if s and s.get("appliedAverage") else None


class Projections:
    """Rest-of-season pts/g by normalized name, written daily by model/project.py (proj.json or proj.js)."""

    def __init__(self, values=None, sd=0.27, date=None, source=None):
        self.values = dict(values or {})
        self.sd = sd
        self.date = date
        self.source = source

    def __len__(self):
        return len(self.values)

    def get(self, name):
        return self.values.get(norm(name))

    @classmethod
    def load(cls, path):
        path = Path(path)
        tries = [path]
        for ext in (".json", ".js"):
            alt = path.with_suffix(ext)
            if alt not in tries:
                tries.append(alt)
        for f in tries:
            if not f.exists():
                continue
            text = f.read_text()
            if f.suffix == ".json":
                d = json.loads(text)
                return cls(d.get("proj", {}), d.get("sd", 0.27), d.get("date"), str(f))
            m = re.search(r"__PROJ\s*=\s*(\{.*?\});", text, re.S)
            sd = re.search(r"__PROJ_SD\s*=\s*([0-9.]+)", text)
            dt = re.search(r"__PROJ_DATE\s*=\s*'([^']*)'", text)
            if m:
                return cls(json.loads(m.group(1)), float(sd.group(1)) if sd else 0.27, dt.group(1) if dt else None, str(f))
        return cls()


def model_value(p, proj, season, overrides=None):
    """bot.js value(): model projection for skaters, else a blend of this season with last season / ESPN's projection."""
    name = p.get("fullName", "")
    if position(p) != "G":
        v = proj.get(name)
        if v is not None:
            return v
    if overrides and name in overrides:
        return overrides[name]
    cur, last, pr = stat(p, season, 0), stat(p, season - 1, 0), stat(p, season, 1)
    gp = _gp_raw(cur)
    last_gp = _gp_raw(last)
    if last_gp >= 20:
        base = last["appliedAverage"]
    elif pr and pr.get("appliedAverage"):
        base = pr["appliedAverage"]
    else:
        base = 1.2
    w = gp / (gp + 25)
    return w * ((cur.get("appliedAverage") or 0) if cur else 0) + (1 - w) * base


def stream_value(p, proj, season, overrides=None):
    """Streaming value: model projection, else ESPN's projection, else the bot.js blend."""
    name = p.get("fullName", "")
    if position(p) != "G":
        v = proj.get(name)
        if v is not None:
            return v
    if overrides and name in overrides:
        return overrides[name]
    e = espn_proj_avg(p, season)
    return e if e is not None else model_value(p, proj, season, overrides)


def prep(p, slot, games, now_ms, proj, season, overrides=None, status=None, waiver_date=None):
    """bot.js prep(): one flat record per player.
    v = lineup value (DAY_TO_DAY at half), proj = undiscounted value, sv = streaming value,
    gt = today's game start (ms) or None, plays = has a game and is not OUT, locked = game already started."""
    inj = p.get("injuryStatus") or "ACTIVE"
    gt = games.get(p.get("proTeamId"))
    raw = model_value(p, proj, season, overrides)
    return {
        "id": p["id"],
        "n": p.get("fullName", str(p["id"])),
        "pos": position(p),
        "slot": slot,
        "inj": inj,
        "gt": gt,
        "v": raw * 0.5 if inj == "DAY_TO_DAY" else raw,
        "proj": raw,
        "sv": stream_value(p, proj, season, overrides),
        "gp": games_played(p, season),
        "plays": gt is not None and inj not in OUT,
        "locked": gt is not None and gt <= now_ms,
        "pro_team": p.get("proTeamId"),
        "status": status,
        "waiver_date": waiver_date,
    }


def erf(x):
    """Abramowitz-Stegun erf approximation used in bot.js (max error 1.5e-7)."""
    t = 1 / (1 + 0.3275911 * abs(x))
    y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * math.exp(-x * x)
    return y if x >= 0 else -y


def p_better(edge, sd):
    """P(add outscores drop over the rest of the season), both projections carrying the model's residual SD."""
    return 0.5 * (1 + erf(edge / (sd * math.sqrt(2)) / math.sqrt(2)))
