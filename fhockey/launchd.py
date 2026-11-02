"""launchd agent that runs `python3 -m fhockey watch --once` every 2 minutes (macOS)."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import REPO_ROOT


def plist_path(cfg):
    return Path("~/Library/LaunchAgents").expanduser() / f"{cfg['launchd_label']}.plist"


def build_plist(cfg, python=None):
    python = python or sys.executable
    path = os.pathsep.join([str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin", "/usr/local/bin",
                            "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    env = {"PATH": path, "PYTHONPATH": str(REPO_ROOT)}
    for k in ("FHOCKEY_CONFIG_DIR", "FHOCKEY_STATE_DIR", "FHOCKEY_LOG_DIR"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {
        "Label": cfg["launchd_label"],
        "ProgramArguments": [python, "-m", "fhockey", "watch", "--once"],
        "WorkingDirectory": str(REPO_ROOT),
        "StartInterval": int(cfg["poll_interval_seconds"]),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(cfg.log_dir / "watch.log"),
        "StandardErrorPath": str(cfg.log_dir / "watch.log"),
        "EnvironmentVariables": env,
    }


def install(cfg, runner=subprocess.run, load=True):
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    p = plist_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump(build_plist(cfg), f)
    if load:
        domain = f"gui/{os.getuid()}"
        runner(["launchctl", "bootout", f"{domain}/{cfg['launchd_label']}"], capture_output=True)
        r = runner(["launchctl", "bootstrap", domain, str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            return False, f"launchctl bootstrap failed: {(r.stderr or '').strip()}"
    return True, str(p)


def uninstall(cfg, runner=subprocess.run):
    p = plist_path(cfg)
    runner(["launchctl", "bootout", f"gui/{os.getuid()}/{cfg['launchd_label']}"], capture_output=True)
    existed = p.exists()
    if existed:
        p.unlink()
    return existed, str(p)
