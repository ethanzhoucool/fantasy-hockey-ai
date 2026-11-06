"""Settings and file locations.

Settings live in <config_dir>/config.json (no secrets). Cookies live in <config_dir>/auth.json (mode 600),
handled by espn.py. Every directory can be moved with an environment variable:
  FHOCKEY_CONFIG_DIR  default ~/.config/fantasy-hockey
  FHOCKEY_STATE_DIR   default ~/.local/state/fantasy-hockey
  FHOCKEY_LOG_DIR     default ~/Library/Logs/fantasy-hockey
"""
import copy
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "league_id": None,
    "team_id": None,
    "season": 2027,
    # false = dry run: log and notify what it WOULD do, never write to ESPN
    "auto_execute": False,
    "timezone": "",  # IANA name for "morning" checks; empty = system local time
    "mac_notifications": True,
    "ntfy_topic": "",  # phone push via ntfy.sh; off when empty. Use a long random string.
    "ntfy_server": "https://ntfy.sh",
    "notify_all_drops": True,
    "poll_interval_seconds": 120,
    "lineup_lead_minutes": 30,
    "morning_hour": 9,  # local hour for the daily pickup + stream pass
    "refresh_projections": True,  # run model/project.py once a day (needs model/data, see README)
    "proj_file": "",  # default <repo>/model/proj.json, falling back to proj.js
    "model_data_dir": "",  # NHL stats for model/project.py; default <repo>/model/data
    "trades_dir": "",  # where `snapshot` writes league_<date>.txt; default <repo>/trades
    "watch_player_ids": [],  # extra players to watch for WAIVERS -> FREEAGENT
    # pre-approved adds run once: [{"add": id, "drop": id, "from_sp": n, "note": ""}]
    "planned_moves": [],
    "value_overrides": {"Aleksander Barkov": 2.0},  # from bot.js: missed last season, elite when healthy
    "rules": {
        "min_edge": 0.25,
        "min_p": 0.75,
        "min_gp": 5,
        "max_drop_proj": 1.85,
        "weekly_add_limit": 7,
        "candidate_min_diff": 0.2,
        "fa_pool_limit": 80,
    },
    "stream": {
        "enabled": True,
        "pregame_minutes": 60,
        "min_start_minutes": 20,
        "max_drop_proj": 1.80,
        "drop_margin": 0.10,
        "adds_per_day": 1,  # the service's own pacing for the weekly adds; ESPN has no daily limit
        "reserve_adds": 1,
        "reserve_until_days_left": 2,
        "min_gp": 5,
        "fa_pool_limit": 120,
    },
    "claude_hook": False,  # opt-in: run `claude -p` for events that need judgment
    "claude_bin": "",  # default: ~/.local/bin/claude, then PATH
    "claude_max_per_day": 4,
    "claude_min_gap_minutes": 30,
    "claude_timeout_minutes": 20,
    "claude_extra_prompt": "",
    # the hook reads other managers' chat, so it only gets read-only commands (claude_hook.LOCKDOWN is always added)
    "claude_args": [
        "--model",
        "opus",
        "--tools",
        "Read,Grep,Glob,Bash",
        "--allowedTools",
        "Read",
        "Grep",
        "Glob",
        "Bash(python3 -m fhockey offers:*)",
        "Bash(python3 -m fhockey chat:*)",
        "Bash(python3 -m fhockey trade-input:*)",
        "Bash(python3 -m fhockey snapshot:*)",
        "Bash(python3 -m fhockey status:*)",
        "Bash(python3 -I model/trade_eval.py:*)",
        "Bash(python3 -I trades/trade_scan.py:*)",
        "Bash(python3 -I trades/robust.py:*)",
    ],
    # the Opus GM (fhockey/gm.py): while enabled, Opus makes every add and drop instead of the `rules` and `stream`
    # passes. The numbers below are hard limits Python enforces whatever Opus says.
    "gm": {
        "enabled": False,
        "model": "opus",
        "effort": "high",
        "timeout_minutes": 15,
        "max_runs_per_day": 4,
        "min_gap_minutes": 30,
        "max_moves_per_run": 2,
        "max_moves_per_day": 3,
        "max_plan_age_minutes": 60,
        "core_floor": 2.05,  # never drop a skater whose median of model, ESPN projection and season average reaches this
        # goalies score on a higher scale: with the skater floor nearly any goalie, even a one-week streamer, could
        # never be dropped again. So this is only for elite ones; protect your real starters by name below.
        "core_floor_goalie": 3.5,
        "protected_players": [],  # ids or full names that can never be dropped
        "trigger_margin": 0.2,  # a newly available player wakes Opus if he beats my weakest by this much
        "fa_pool_limit": 120,
        "goalie_pool_limit": 30,
        "candidates_per_position": {"F": 25, "D": 15, "G": 8},
        "news_per_position": {"F": 10, "D": 6, "G": 4},
        "lock_wait_seconds": 300,
    },
    "memory_dir": "",  # default <repo>/memory (LEAGUE.local.md and GM_LOG.local.md, both gitignored)
    "launchd_label": "com.ethanzhoucool.fantasy-hockey",
    "reads_base": "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl",
    "writes_base": "https://lm-api-writes.fantasy.espn.com/apis/v3/games/fhl",
}


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_dir(name, default):
    return Path(os.environ.get(name) or default).expanduser()


class Config:
    def __init__(self, data=None, config_dir=None, state_dir=None, log_dir=None):
        self.config_dir = Path(config_dir) if config_dir else _env_dir("FHOCKEY_CONFIG_DIR", "~/.config/fantasy-hockey")
        self.state_dir = Path(state_dir) if state_dir else _env_dir("FHOCKEY_STATE_DIR", "~/.local/state/fantasy-hockey")
        self.log_dir = Path(log_dir) if log_dir else _env_dir("FHOCKEY_LOG_DIR", "~/Library/Logs/fantasy-hockey")
        self.user_data = dict(data or {})
        self.data = _merge(DEFAULTS, self.user_data)

    @classmethod
    def load(cls, **kw):
        cfg = cls(**kw)
        if cfg.config_file.exists():
            try:
                user = json.loads(cfg.config_file.read_text())
            except ValueError as e:
                raise ConfigError(f"{cfg.config_file} is not valid JSON: {e}") from None
            cfg.user_data = user
            cfg.data = _merge(DEFAULTS, user)
        return cfg

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        self.user_data[k] = v
        self.data[k] = v

    def save(self):
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_file.write_text(json.dumps(self.user_data, indent=2) + "\n")

    def require_league(self):
        if not self["league_id"] or not self["team_id"]:
            raise ConfigError(f"league_id / team_id not set: run `python3 -m fhockey setup` (or edit {self.config_file})")

    @property
    def team_id(self):
        return int(self["team_id"])

    @property
    def season(self):
        return int(self["season"])

    # ---- paths ----
    @property
    def config_file(self):
        return self.config_dir / "config.json"

    @property
    def auth_file(self):
        return self.config_dir / "auth.json"

    @property
    def state_file(self):
        return self.state_dir / "state.json"

    @property
    def schedule_cache(self):
        return self.state_dir / "schedule.json"

    @property
    def inbox_dir(self):
        return self.state_dir / "inbox"

    @property
    def events_log(self):
        return self.log_dir / "events.log"

    @property
    def service_log(self):
        return self.log_dir / "service.log"

    @property
    def proj_path(self):
        return Path(self["proj_file"]).expanduser() if self["proj_file"] else REPO_ROOT / "model" / "proj.json"

    @property
    def model_data_dir(self):
        return Path(self["model_data_dir"]).expanduser() if self["model_data_dir"] else REPO_ROOT / "model" / "data"

    @property
    def memory_dir(self):
        return Path(self["memory_dir"]).expanduser() if self["memory_dir"] else REPO_ROOT / "memory"

    @property
    def trades_dir(self):
        return Path(self["trades_dir"]).expanduser() if self["trades_dir"] else REPO_ROOT / "trades"

    def tz(self):
        """tzinfo for local-time decisions (None = system local)."""
        if not self["timezone"]:
            return None
        from zoneinfo import ZoneInfo
        return ZoneInfo(self["timezone"])
