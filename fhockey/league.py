"""One run's view of the league: status, Ethan's roster, transactions, schedule and the add budget.
Every ESPN read goes through here so a run fetches each thing at most once."""
from .pickups import Budget
from .players import Projections, prep
from .state import read_json, write_json

TERMINAL_TRADE = {"CANCELED", "CANCELLED", "DECLINED", "REJECTED", "VETOED", "EXECUTED", "FAILED", "EXPIRED"}
DAY_MS = 86_400_000
# ESPN never updates a proposal's own record: it stays PENDING. Each answer is a new record whose
# relatedTransactionId points at the proposal. Ranked so the final outcome wins: a decline also writes a
# CANCELED record, and an accepted trade that falls through later gets one too.
_OUTCOME_RANK = {"EXECUTED": 4, "DECLINED": 3, "FAILED": 3, "CANCELED": 2, "ACCEPTED": 1}


def trade_outcomes(transactions):
    """{proposal id: status} from the records that answer a proposal (accept, decline, cancel, process)."""
    out = {}
    for t in transactions:
        rel, typ, status = t.get("relatedTransactionId"), t.get("type"), t.get("status")
        if not rel:
            continue
        if typ == "TRADE_ACCEPT":
            new = "EXECUTED" if status == "EXECUTED" else "FAILED" if status in TERMINAL_TRADE else "ACCEPTED"
        elif typ == "TRADE_DECLINE":
            new = "DECLINED"
        elif typ == "TRADE_PROPOSAL" and status in ("CANCELED", "CANCELLED"):
            new = "CANCELED"
        else:
            continue  # TRADE_UPHOLD / TRADE_VETO are one team's vote, not the outcome
        if _OUTCOME_RANK[new] > _OUTCOME_RANK.get(out.get(rel), 0):
            out[rel] = new
    return out


def trade_status(t, outcomes):
    return outcomes.get(t.get("id"), t.get("status"))


class Schedule:
    """NHL games per ESPN scoring period: {sp: {proTeamId: start ms}} plus team abbreviations."""

    def __init__(self, games=None, abbrev=None, fetched=0):
        self.games = {int(sp): {int(t): d for t, d in g.items()} for sp, g in (games or {}).items()}
        self.abbrev = {int(k): v for k, v in (abbrev or {}).items()}
        self.fetched = fetched

    @classmethod
    def from_espn(cls, data, fetched=0):
        games, abbrev = {}, {}
        for t in (data.get("settings") or {}).get("proTeams") or []:
            abbrev[t["id"]] = t.get("abbrev") or str(t["id"])
            for sp, gs in (t.get("proGamesByScoringPeriod") or {}).items():
                for g in gs or []:
                    games.setdefault(int(sp), {})[t["id"]] = g["date"]
        return cls(games, abbrev, fetched)

    def to_json(self):
        return {"fetched": self.fetched, "games": self.games, "abbrev": self.abbrev}

    def day(self, sp):
        return self.games.get(int(sp), {})

    def first_game(self, sp):
        g = self.day(sp)
        return min(g.values()) if g else None

    @classmethod
    def load(cls, cfg, client, now_ms, max_age_ms=20 * 3600 * 1000):
        """Cached in the state dir and refreshed every 20 hours (the response covers the whole season)."""
        d = read_json(cfg.schedule_cache)
        if d and d.get("season") == cfg.season and 0 <= now_ms - d.get("fetched", 0) < max_age_ms:
            return cls(d.get("games"), d.get("abbrev"), d.get("fetched", 0))
        s = cls.from_espn(client.pro_schedule(), now_ms)
        if s.games:  # an empty answer must not stick for 20 hours: without game times nobody looks locked
            write_json(cfg.schedule_cache, dict(s.to_json(), season=cfg.season))
        return s


def load_settings(cfg, client, now_ms, max_age_ms=DAY_MS):
    """matchupPeriods from mSettings, cached for a day."""
    path = cfg.state_dir / "settings.json"
    d = read_json(path)
    if d and d.get("season") == cfg.season and now_ms - d.get("fetched", 0) < max_age_ms:
        return d
    s = (client.settings().get("settings") or {})
    d = {
        "season": cfg.season,
        "fetched": now_ms,
        "matchupPeriods": (s.get("scheduleSettings") or {}).get("matchupPeriods") or {},
        "acquisition": s.get("acquisitionSettings") or {},
    }
    write_json(path, d)
    return d


def team_label(t):
    if not t:
        return "?"
    return t.get("name") or " ".join(x for x in (t.get("location"), t.get("nickname")) if x) or f"team {t.get('id')}"


class LeagueView:
    def __init__(self, client, cfg, now_ms, state=None, proj=None, schedule=None):
        self.client = client
        self.cfg = cfg
        self.now = now_ms
        self.state = state if state is not None else {}
        self.proj = proj if proj is not None else Projections.load(cfg.proj_path)
        self._schedule = schedule
        self._tx = {}
        self._settings = None
        self.team_id = cfg.team_id
        self.raw = None

    # ---- base read: status + my roster + mTeam
    def load(self):
        r = self.client.my_team()
        self.raw = r
        self.cur = int(r.get("scoringPeriodId") or (r.get("status") or {}).get("latestScoringPeriod"))
        self.mp = int((r.get("status") or {}).get("currentMatchupPeriod") or 0)
        self.teams = {t["id"]: t for t in r.get("teams") or []}
        self.members = {m.get("id", "").strip("{}").upper(): m for m in r.get("members") or []}
        return self

    @property
    def my_team(self):
        return self.teams.get(self.team_id) or {}

    @property
    def roster_entries(self):
        return (self.my_team.get("roster") or {}).get("entries") or []

    def team_name(self, tid):
        return team_label(self.teams.get(tid)) if tid in self.teams else f"team {tid}"

    def member_name(self, member_id):
        m = self.members.get((member_id or "").strip("{}").upper())
        if not m:
            return None
        return " ".join(x for x in (m.get("firstName"), m.get("lastName")) if x) or m.get("displayName")

    def member_team(self, member_id):
        mid = (member_id or "").strip("{}").upper()
        for t in self.teams.values():
            if any((o or "").strip("{}").upper() == mid for o in t.get("owners") or []):
                return t["id"]
        return None

    # ---- schedule / settings
    @property
    def schedule(self):
        if self._schedule is None:
            self._schedule = Schedule.load(self.cfg, self.client, self.now)
        return self._schedule

    def games(self, sp=None):
        return self.schedule.day(self.cur if sp is None else sp)

    def abbrev(self, pro_team_id):
        return self.schedule.abbrev.get(pro_team_id, str(pro_team_id))

    @property
    def settings(self):
        if self._settings is None:
            self._settings = load_settings(self.cfg, self.client, self.now)
        return self._settings

    def days_left_in_week(self):
        from .stream import days_left_in_week
        import datetime as dt
        wd = dt.datetime.fromtimestamp(self.now / 1000, self.cfg.tz()).weekday()
        return days_left_in_week(self.settings.get("matchupPeriods"), self.mp, self.cur, wd)

    # ---- transactions
    def tx(self, sp):
        sp = int(sp)
        if sp < 1:
            return []
        if sp not in self._tx:
            txs = self.client.transactions(sp).get("transactions") or []
            for t in txs:
                t.setdefault("_sp", sp)
            self._tx[sp] = txs
        return self._tx[sp]

    def tx_range(self, back):
        out = []
        for sp in range(max(1, self.cur - back), self.cur + 1):
            out.extend(self.tx(sp))
        return out

    def pending_claims(self):
        seen, out = set(), []
        for t in self.tx_range(2):
            if t.get("type") != "WAIVER" or t.get("status") != "PENDING" or t.get("teamId") != self.team_id:
                continue
            if t.get("id") in seen:
                continue
            seen.add(t.get("id"))
            items = t.get("items") or []
            add = next((i["playerId"] for i in items if i.get("type") == "ADD"), None)
            drop = next((i["playerId"] for i in items if i.get("type") == "DROP"), None)
            out.append({"add": add, "drop": drop, "id": t.get("id")})
        return out

    def fa_adds_this_period(self):
        n = sum(
            1 for t in self.tx(self.cur)
            if t.get("teamId") == self.team_id and t.get("type") in ("FREEAGENT", "WAIVER")
            and t.get("status") == "EXECUTED"
            and any(i.get("type") == "ADD" and i.get("toTeamId", self.team_id) == self.team_id for i in t.get("items") or [])
        )
        if self.state.get("fa_add_sp") == self.cur:
            n = max(n, 1)
        return n

    def pending_trades(self, back=3):
        """Live proposals and accepted trades not yet processed that involve Ethan."""
        txs = self.tx_range(back)
        outcomes = trade_outcomes(txs)
        out, seen = [], set()
        for t in txs:
            if t.get("type") != "TRADE_PROPOSAL" or t.get("relatedTransactionId") or t.get("id") in seen:
                continue
            status = trade_status(t, outcomes)
            if status in TERMINAL_TRADE:
                continue
            if status == "PENDING" and t.get("expirationDate") and t["expirationDate"] < self.now:
                continue
            items = t.get("items") or []
            if not any(i.get("fromTeamId") == self.team_id or i.get("toTeamId") == self.team_id for i in items):
                continue
            seen.add(t.get("id"))
            out.append(t)
        return out

    def pending_trade_ids(self, back=3):
        return {i["playerId"] for t in self.pending_trades(back) for i in t.get("items") or [] if "playerId" in i}

    def add_counter(self):
        """Acquisitions per matchup period, or None if ESPN left the counter or the matchup period out."""
        counter = self.my_team.get("transactionCounter")
        totals = counter.get("matchupAcquisitionTotals") if isinstance(counter, dict) else None
        return totals if self.mp and isinstance(totals, dict) else None

    def weekly_used(self):
        """Acquisitions this matchup period. Without the counter, assume the limit is reached: no add is better
        than an add over the limit."""
        totals = self.add_counter()
        if totals is None:
            return int(self.cfg["rules"]["weekly_add_limit"])
        return int(totals.get(str(self.mp), totals.get(self.mp, 0)) or 0)

    def budget(self, trade_back=3):
        return Budget(self.weekly_used(), self.pending_claims(), self.fa_adds_this_period(),
                      self.pending_trade_ids(trade_back), self.cfg["rules"]["weekly_add_limit"])

    # ---- players
    def prep_entry(self, e, games=None):
        return prep(e["playerPoolEntry"]["player"], e.get("lineupSlotId"), self.games() if games is None else games,
                    self.now, self.proj, self.cfg.season, self.cfg["value_overrides"])

    def roster(self):
        g = self.games()
        return [self.prep_entry(e, g) for e in self.roster_entries]

    def fa_pool(self, limit, statuses=("FREEAGENT", "WAIVERS"), extra_ids=()):
        resp = self.client.free_agents(self.cur, limit=limit, statuses=statuses)
        entries = list(resp.get("players") or [])
        have = {x.get("id") for x in entries}
        extra = [i for i in extra_ids if i not in have]
        if extra:
            entries += [x for x in self.client.players_by_id(extra, self.cur).get("players") or []
                        if x.get("status") in statuses]
        g = self.games()
        return [prep(x["player"], None, g, self.now, self.proj, self.cfg.season, self.cfg["value_overrides"],
                     status=x.get("status"), waiver_date=x.get("waiverProcessDate")) for x in entries if x.get("player")]
