"""Client for ESPN's unofficial fantasy API (game code fhl = hockey).

Private leagues authenticate with two cookies, SWID and espn_s2, sent as a Cookie header over HTTPS.
They are read from auth.json (mode 600) and never logged, printed or put in exception messages:
every value is registered with scrub(), which the logger and notifier run over all output.

Reads:  https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/<season>/segments/0/leagues/<id>?view=...
Writes: https://lm-api-writes.fantasy.espn.com/apis/v3/games/fhl/seasons/<season>/segments/0/leagues/<id>/transactions/
The API is undocumented and can change without notice.
"""
import http.client
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "fantasy-hockey-ai/1.0 (+https://github.com/ethanzhoucool/fantasy-hockey-ai)"
AUTH_HINT = "ESPN cookies expired or invalid. Run: python3 -m fhockey setup"

# ---------------------------------------------------------------- secrets

_SECRETS = set()


def register_secret(value):
    """Remember a secret (and its URL-encoded/decoded forms) so scrub() can mask it anywhere."""
    if not value or len(value) < 6:
        return
    for v in (value, urllib.parse.quote(value, safe=""), urllib.parse.unquote(value), value.strip("{}")):
        if v and len(v) >= 6:
            _SECRETS.add(v)


def scrub(text):
    if not isinstance(text, str):
        text = str(text)
    for s in sorted(_SECRETS, key=len, reverse=True):
        if s in text:
            text = text.replace(s, "***")
    return text


class Auth:
    """The two cookies. repr/str never show the values."""

    __slots__ = ("swid", "espn_s2")

    def __init__(self, swid, espn_s2):
        self.swid = normalize_swid(swid)
        self.espn_s2 = normalize_s2(espn_s2)
        register_secret(self.swid)
        register_secret(self.espn_s2)

    def __repr__(self):
        return "Auth(swid=***, espn_s2=***)"

    __str__ = __repr__

    def cookie_header(self):
        return f"SWID={self.swid}; espn_s2={self.espn_s2}"

    @property
    def member_id(self):
        """ESPN memberId used in write payloads: the SWID with braces, e.g. {ABC-...}."""
        return self.swid

    @property
    def bare_id(self):
        return self.swid.strip("{}").upper()


def normalize_swid(raw):
    v = urllib.parse.unquote((raw or "").strip().strip('"').strip("'"))
    if v and not v.startswith("{"):
        v = "{" + v
    if v and not v.endswith("}"):
        v = v + "}"
    return v


def normalize_s2(raw):
    # Kept exactly as Chrome stores it (already URL-encoded); the browser sends it that way too.
    return (raw or "").strip().strip('"').strip("'")


def load_auth(path):
    path = Path(path)
    if not path.exists():
        raise AuthError(f"no cookies saved yet ({path}). Run: python3 -m fhockey setup")
    try:
        d = json.loads(path.read_text())
        return Auth(d["swid"], d["espn_s2"])
    except (ValueError, KeyError):
        raise AuthError(f"{path} is unreadable. Run: python3 -m fhockey setup") from None


def save_auth(path, swid, espn_s2):
    """Write auth.json with mode 600 (directory 700). Returns the Auth object."""
    auth = Auth(swid, espn_s2)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"swid": auth.swid, "espn_s2": auth.espn_s2}, f)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return auth


# ---------------------------------------------------------------- errors


class EspnError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(scrub(msg))
        self.status = status


class AuthError(EspnError):
    def __init__(self, msg=AUTH_HINT, status=None):
        super().__init__(msg, status)


class TransientError(EspnError):
    pass


class WritesDisabled(EspnError):
    pass


# ---------------------------------------------------------------- transport


_SSL = None


def https_context():
    """Default TLS context, falling back to the macOS system bundle when Python has no CA certificates
    (python.org installers ship without them until 'Install Certificates.command' is run)."""
    global _SSL
    if _SSL is None:
        _SSL = ssl.create_default_context()
        paths = ssl.get_default_verify_paths()
        if not (paths.cafile or paths.capath) or not _SSL.get_ca_certs():
            for bundle in ("/etc/ssl/cert.pem", "/usr/local/etc/openssl/cert.pem", "/opt/homebrew/etc/openssl@3/cert.pem"):
                if os.path.exists(bundle):
                    _SSL.load_verify_locations(cafile=bundle)
                    break
    return _SSL


def urlopen(req, timeout=25):
    return urllib.request.urlopen(req, timeout=timeout, context=https_context())


def urllib_transport(method, url, headers, body=None, timeout=25):
    """Returns (status, body bytes). Raises TransientError on network failures."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()
        finally:
            e.close()
    except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, TLS, resets, a response cut off midway
        raise TransientError(f"network error: {type(e).__name__}") from None


def _short(url):
    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(p.query)
    return p.path.rsplit("/leagues/", 1)[-1][:40] + ("?view=" + ",".join(q["view"]) if "view" in q else "")


# ---------------------------------------------------------------- client


class EspnClient:
    def __init__(self, cfg, auth, transport=None, writes_enabled=False, sleep=time.sleep, retries=3):
        cfg.require_league()
        self.cfg = cfg
        self.auth = auth
        self.transport = transport or urllib_transport
        self.writes_enabled = bool(writes_enabled)
        self.sleep = sleep
        self.retries = retries
        self.league_id = int(cfg["league_id"])
        self.team_id = cfg.team_id
        self.season = cfg.season
        self.season_url = f"{cfg['reads_base']}/seasons/{self.season}"
        self.reads_url = f"{self.season_url}/segments/0/leagues/{self.league_id}"
        self.writes_url = f"{cfg['writes_base']}/seasons/{self.season}/segments/0/leagues/{self.league_id}/transactions/"
        self.request_count = 0

    def __repr__(self):
        return f"EspnClient(league={self.league_id}, team={self.team_id})"

    def _headers(self, extra=None):
        h = {
            "Cookie": self.auth.cookie_header(),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-cache",
        }
        h.update(extra or {})
        return h

    def _call(self, method, url, headers, body=None, retry=True):
        delay = 2.0
        attempts = self.retries + 1 if retry else 1
        for attempt in range(attempts):
            last = attempt == attempts - 1
            try:
                status, raw = self.transport(method, url, headers, body)
            except TransientError as e:
                if last:
                    raise TransientError(f"{e} ({_short(url)})") from None
                self.sleep(delay)
                delay *= 3
                continue
            self.request_count += 1
            if status in (401, 403):
                raise AuthError(status=status)
            if status == 429 or status >= 500:
                if last:
                    raise TransientError(f"ESPN HTTP {status} ({_short(url)})", status=status)
                self.sleep(delay)
                delay *= 3
                continue
            text = (raw or b"").decode("utf-8", "replace")
            if status >= 400:
                raise EspnError(f"ESPN HTTP {status} ({_short(url)}): {_error_text(text)}", status=status)
            try:
                return json.loads(text) if text.strip() else {}
            except ValueError:
                raise EspnError(f"ESPN returned non-JSON ({_short(url)}); cookies may be expired") from None
        raise TransientError("unreachable")

    # ------------------------------------------------------------ reads

    def league(self, views, params=None, xfilter=None, path=""):
        q = [("view", v) for v in views] + [(k, v) for k, v in (params or {}).items() if v is not None]
        url = f"{self.reads_url}{path}?{urllib.parse.urlencode(q)}"
        extra = {"X-Fantasy-Filter": json.dumps(xfilter, separators=(",", ":"))} if xfilter else None
        return self._call("GET", url, self._headers(extra))

    def status(self):
        return self.league(["mStatus"])

    def my_team(self, scoring_period=None):
        """Status + this team's roster + all teams' mTeam data (transactionCounter, owners) + members."""
        return self.league(["mStatus", "mRoster", "mTeam"], {"forTeamId": self.team_id, "scoringPeriodId": scoring_period})

    def all_rosters(self):
        return self.league(["mRoster", "mTeam", "mStatus"])

    def teams(self):
        return self.league(["mTeam"])

    def transactions(self, scoring_period):
        return self.league(["mTransactions2"], {"scoringPeriodId": scoring_period})

    def settings(self):
        return self.league(["mSettings"])

    def matchup(self):
        return self.league(["mMatchupScore", "mTeam"])

    def players(self, players_filter, scoring_period=None):
        return self.league(["kona_player_info"], {"scoringPeriodId": scoring_period}, {"players": players_filter})

    def players_by_id(self, ids, scoring_period=None):
        ids = [int(i) for i in ids]
        if not ids:
            return {"players": []}
        return self.players({"filterIds": {"value": ids}}, scoring_period)

    def free_agents(self, scoring_period, limit=80, statuses=("FREEAGENT", "WAIVERS"), slot_ids=None):
        f = {
            "filterStatus": {"value": list(statuses)},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
        if slot_ids:
            f["filterSlotIds"] = {"value": list(slot_ids)}
        return self.players(f, scoring_period)

    def communication(self, dm_limit=50, league_limit=5):
        flt = {
            "topicsByType": {
                "CHAT_DIRECT_MESSAGE": {"limit": dm_limit, "sortMessageDate": {"sortPriority": 1, "sortAsc": False}},
                "CHAT_ALL_MEMBERS": {"limit": league_limit, "sortMessageDate": {"sortPriority": 1, "sortAsc": False}},
            }
        }
        url = f"{self.reads_url}/communication/?view=kona_league_communication"
        return self._call("GET", url, self._headers({"X-Fantasy-Filter": json.dumps(flt, separators=(",", ":"))}))

    def pro_schedule(self):
        return self._call("GET", f"{self.season_url}?view=proTeamSchedules_wl", self._headers())

    # ------------------------------------------------------------ writes (only when auto_execute is on)

    def _post(self, payload):
        if os.environ.get("FHOCKEY_READ_ONLY"):  # set for Claude subprocesses, whatever auto_execute says
            raise WritesDisabled("FHOCKEY_READ_ONLY is set; refusing to write to ESPN")
        if not self.writes_enabled:
            raise WritesDisabled("auto_execute is off; refusing to write to ESPN")
        headers = self._headers({
            "Content-Type": "application/json",
            "Origin": "https://fantasy.espn.com",
            "Referer": "https://fantasy.espn.com/",
        })
        # never retried: a timed-out POST may still have gone through
        return self._call("POST", self.writes_url, headers, json.dumps(payload).encode(), retry=False)

    def set_lineup(self, items, scoring_period, current_period):
        return self._post(lineup_payload(self.team_id, self.auth.member_id, scoring_period, current_period, items))

    def add_free_agent(self, add_id, drop_id, scoring_period):
        return self._post(add_drop_payload("FREEAGENT", self.team_id, self.auth.member_id, scoring_period, add_id, drop_id))

    def claim_waiver(self, add_id, drop_id, scoring_period):
        return self._post(add_drop_payload("WAIVER", self.team_id, self.auth.member_id, scoring_period, add_id, drop_id))


def _error_text(text):
    """Pull ESPN's error message out of a JSON error body, without echoing anything long."""
    try:
        d = json.loads(text)
        msgs = d.get("messages") or [x.get("message") for x in d.get("details") or [] if isinstance(x, dict)]
        if msgs:
            return scrub("; ".join(str(m) for m in msgs if m)[:200])
    except (ValueError, AttributeError):
        pass
    return scrub(text[:200])


def lineup_payload(team_id, member_id, scoring_period, current_period, items):
    return {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": "FUTURE_ROSTER" if scoring_period > current_period else "ROSTER",
        "memberId": member_id,
        "scoringPeriodId": scoring_period,
        "executionType": "EXECUTE",
        "items": items,
    }


def add_drop_payload(kind, team_id, member_id, scoring_period, add_id, drop_id):
    assert kind in ("FREEAGENT", "WAIVER")
    return {
        "isLeagueManager": False,
        "teamId": team_id,
        "type": kind,
        "memberId": member_id,
        "scoringPeriodId": scoring_period,
        "executionType": "EXECUTE",
        "items": [
            {"playerId": int(add_id), "type": "ADD", "toTeamId": team_id},
            {"playerId": int(drop_id), "type": "DROP", "fromTeamId": team_id},
        ],
    }
