"""Notifications: an append-only event log, a macOS banner, and an optional ntfy.sh push.

ntfy topics are public to anyone who knows the name, so use a long random one (e.g. `python3 -c
"import secrets; print('fh-' + secrets.token_urlsafe(18))"`). Pushes are off when ntfy_topic is empty.
Every message passes through espn.scrub() first, so a cookie value can never be sent or logged.
"""
import datetime as dt
import json
import logging
import subprocess
import sys
import urllib.request
from logging.handlers import RotatingFileHandler

from .espn import scrub

PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}


class ScrubFilter(logging.Filter):
    def filter(self, record):
        record.msg = scrub(record.getMessage())
        record.args = ()
        return True


def get_logger(cfg):
    log = logging.getLogger(f"fhockey.{cfg.log_dir}")
    if not log.handlers:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(cfg.service_log, maxBytes=1_000_000, backupCount=3)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.addFilter(ScrubFilter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


class Notifier:
    def __init__(self, cfg, runner=subprocess.run, opener=urllib.request.urlopen, echo=False):
        self.cfg = cfg
        self.runner = runner
        self.opener = opener
        self.echo = echo
        self.sent = []

    def send(self, kind, message, title="Fantasy hockey", priority="default"):
        message, title = scrub(message).strip(), scrub(title).strip()
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with open(self.cfg.events_log, "a") as f:
            f.write(f"{stamp} [{kind}] {message.replace(chr(10), ' / ')}\n")
        self.sent.append((kind, message))
        if self.echo:
            print(f"[{kind}] {message}")
        if self.cfg["mac_notifications"] and sys.platform == "darwin":
            self._mac(title, message)
        if self.cfg["ntfy_topic"]:
            self._ntfy(title, message, priority, kind)

    def _mac(self, title, message):
        # text goes in as argv, never spliced into the AppleScript source
        script = ["-e", "on run argv", "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
                  "-e", "end run"]
        try:
            self.runner(["osascript", *script, message[:240], title[:60]], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    def _ntfy(self, title, message, priority, kind):
        body = json.dumps({
            "topic": self.cfg["ntfy_topic"],
            "title": title,
            "message": message[:1000],
            "priority": PRIORITY.get(priority, 3),
            "tags": [kind],
        }).encode()
        req = urllib.request.Request(self.cfg["ntfy_server"].rstrip("/") + "/", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=10) as r:
                r.read()
        except Exception as e:  # a failed push must never break the run
            get_logger(self.cfg).warning("ntfy push failed: %s", type(e).__name__)
