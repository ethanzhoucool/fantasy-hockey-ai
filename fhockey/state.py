"""Small JSON files in the state directory, written atomically with mode 600."""
import fcntl
import json
import os
from pathlib import Path


def read_json(path, default=None):
    path = Path(path)
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_state(cfg):
    return read_json(cfg.state_file, {}) or {}


def save_state(cfg, st):
    write_json(cfg.state_file, st)


class FileLock:
    """Non-blocking exclusive lock; `acquired` is False if another process holds it."""

    def __init__(self, path):
        self.path = Path(path)
        self.fd = None
        self.acquired = False

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            if self.acquired:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        return False


def is_locked(path):
    with FileLock(path) as lk:
        return not lk.acquired
