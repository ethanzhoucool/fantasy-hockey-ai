"""Shared test scaffolding: a fake ESPN server built from tests/fixtures/*.json and a throwaway config."""
import copy
import json
import tempfile
import urllib.parse
from pathlib import Path

from fhockey.config import Config
from fhockey.espn import Auth, EspnClient, save_auth
from fhockey.league import LeagueView
from fhockey.players import Projections

FIX = Path(__file__).parent / "fixtures"
TEST_SWID = "{TEST-SWID-0004}"
TEST_S2 = "TESTS2COOKIE%2BsecretValue%3D%3Dabc123"


def fixture(name):
    return json.loads((FIX / f"{name}.json").read_text())


NOW = fixture("clock")["now_ms"]
MIN = 60_000
HOUR = 60 * MIN


class FakeESPN:
    """Transport stand-in. Routes by URL like the real API; records every call."""

    def __init__(self):
        self.d = {k: fixture(k) for k in ("league_team4", "league_all", "players_pool", "transactions",
                                          "communication", "schedule", "settings", "matchup")}
        self.calls = []
        self.post_status = 200
        self.post_body = {"status": "EXECUTED"}
        self.fail = None  # (status, body) returned for every GET when set

    # convenience accessors for tests that tweak data
    @property
    def team4(self):
        return next(t for t in self.d["league_team4"]["teams"] if t["id"] == 4)

    def roster_player(self, pid):
        return next(e for e in self.team4["roster"]["entries"] if e["playerId"] == pid)

    def pool_player(self, pid):
        return next(x for x in self.d["players_pool"]["players"] if x["id"] == pid)

    def tx(self, sp):
        return self.d["transactions"].setdefault(str(sp), {"transactions": []})["transactions"]

    @property
    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]

    def _all_players(self):
        out = {x["id"]: x for x in self.d["players_pool"]["players"]}
        for league in (self.d["league_all"], self.d["league_team4"]):
            for t in league["teams"]:
                for e in (t.get("roster") or {}).get("entries") or []:
                    out.setdefault(e["playerId"], {"id": e["playerId"], "status": "ONTEAM", "onTeamId": t["id"],
                                                   "player": e["playerPoolEntry"]["player"]})
        return out

    def __call__(self, method, url, headers, body=None):
        u = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qs(u.query)
        views = q.get("view", [])
        self.calls.append({"method": method, "url": url, "views": views, "query": q, "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        if method == "POST":
            return self.post_status, json.dumps(self.post_body).encode()
        if self.fail:
            return self.fail
        ok = lambda d: (200, json.dumps(d).encode())
        if u.path.endswith("/communication/"):
            return ok(self.d["communication"])
        if "proTeamSchedules_wl" in views:
            return ok(self.d["schedule"])
        if "mTransactions2" in views:
            return ok(self.d["transactions"].get(q["scoringPeriodId"][0], {"transactions": []}))
        if "kona_player_info" in views:
            f = json.loads(headers["X-Fantasy-Filter"])["players"]
            allp = self._all_players()
            if "filterIds" in f:
                return ok({"players": [copy.deepcopy(allp[i]) for i in f["filterIds"]["value"] if i in allp]})
            sts = set(f.get("filterStatus", {}).get("value", []))
            ps = [x for x in self.d["players_pool"]["players"] if x["status"] in sts]
            if "filterSlotIds" in f:
                slots = set(f["filterSlotIds"]["value"])
                ps = [x for x in ps if slots & set(x["player"]["eligibleSlots"])]
            ps.sort(key=lambda x: -x["player"]["ownership"]["percentOwned"])
            return ok({"players": ps[: f.get("limit", 50)]})
        if "mSettings" in views:
            return ok(self.d["settings"])
        if "mMatchupScore" in views:
            return ok(self.d["matchup"])
        if "mRoster" in views and "forTeamId" in q:
            return ok(self.d["league_team4"])
        if "mRoster" in views:
            return ok(self.d["league_all"])
        if "mTeam" in views or "mStatus" in views:
            return ok(self.d["league_team4"])
        return 404, b'{"messages":["not found"]}'


class Env:
    """A temp config/state/log dir with saved test cookies and a fake ESPN."""

    def __init__(self, **cfg_overrides):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        data = {"league_id": 488544617, "team_id": 4, "season": 2027, "mac_notifications": False,
                "timezone": "UTC", "refresh_projections": False, "proj_file": str(FIX / "proj.json"),
                "trades_dir": str(root / "trades"), "memory_dir": str(root / "memory")}
        data.update(cfg_overrides)
        self.cfg = Config(data, config_dir=root / "config", state_dir=root / "state", log_dir=root / "logs")
        self.cfg.save()
        save_auth(self.cfg.auth_file, TEST_SWID, TEST_S2)
        self.espn = FakeESPN()
        self.root = root

    def client(self, writes=False):
        return EspnClient(self.cfg, Auth(TEST_SWID, TEST_S2), transport=self.espn, writes_enabled=writes,
                          sleep=lambda s: None)

    def view(self, now=NOW, state=None, writes=False):
        return LeagueView(self.client(writes), self.cfg, now, state if state is not None else {},
                          proj=Projections.load(FIX / "proj.json")).load()

    def all_output_files(self):
        return [p for p in self.root.rglob("*") if p.is_file() and p != self.cfg.auth_file]

    def close(self):
        self.tmp.cleanup()


def prepped(pid, n, pos, slot, v, gt=None, inj="ACTIVE", locked=False, status=None, gp=10, sv=None, team=1):
    """Hand-built players.prep() record for pure optimizer/rule tests."""
    from fhockey.players import OUT
    raw = v
    return {"id": pid, "n": n, "pos": pos, "slot": slot, "inj": inj, "gt": gt,
            "v": raw * 0.5 if inj == "DAY_TO_DAY" else raw, "proj": raw, "sv": raw if sv is None else sv, "gp": gp,
            "plays": gt is not None and inj not in OUT, "locked": locked, "pro_team": team, "status": status,
            "waiver_date": None}
