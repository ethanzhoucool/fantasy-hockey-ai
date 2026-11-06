"""Cookies must never reach stdout, stderr, logs, notifications, state or inbox files."""
import contextlib
import io
import json
import os
import stat
import subprocess
import unittest
import urllib.parse
from unittest import mock

from fhockey import cli
from fhockey.espn import (AuthError, Auth, EspnError, TransientError, WritesDisabled, load_auth, normalize_swid,
                          save_auth, scrub)

from .helpers import NOW, TEST_S2, TEST_SWID, Env

SECRET_FORMS = [TEST_SWID, TEST_SWID.strip("{}"), TEST_S2, urllib.parse.unquote(TEST_S2), "secretValue"]


class FakeProc:
    def __init__(self, *a, **k):
        pass


def fake_runner(cmd, **kw):
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def run_cli(env, argv, getpass_values=None):
    vals = iter(getpass_values or [])
    deps = cli.Deps(transport=env.espn, getpass=lambda prompt="": next(vals), input_fn=lambda prompt="": "",
                    now=NOW, runner=fake_runner, spawn=FakeProc, opener=lambda *a, **k: None, cfg=env.cfg)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, deps)
    return code, out.getvalue(), err.getvalue()


class SecretHandlingTest(unittest.TestCase):
    def assertNoSecrets(self, text, where=""):
        for s in SECRET_FORMS:
            self.assertNotIn(s, text, f"secret leaked in {where}")

    def test_auth_file_mode_and_repr(self):
        env = Env()
        try:
            mode = stat.S_IMODE(os.stat(env.cfg.auth_file).st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(env.cfg.config_dir).st_mode), 0o700)
            a = load_auth(env.cfg.auth_file)
            self.assertNoSecrets(repr(a) + str(a) + repr(env.client()), "repr")
            self.assertEqual(a.cookie_header(), f"SWID={TEST_SWID}; espn_s2={TEST_S2}")
        finally:
            env.close()

    def test_swid_normalization(self):
        self.assertEqual(normalize_swid("ABC-123-DEF"), "{ABC-123-DEF}")
        self.assertEqual(normalize_swid("%7BABC-123-DEF%7D"), "{ABC-123-DEF}")
        self.assertEqual(normalize_swid(' "{ABC-123-DEF}" '), "{ABC-123-DEF}")

    def test_scrub(self):
        Auth(TEST_SWID, TEST_S2)
        for s in SECRET_FORMS[:4]:
            self.assertNotIn(s, scrub(f"x {s} y"))

    def test_requests_carry_cookie_and_filter(self):
        env = Env()
        try:
            env.client().free_agents(34, limit=5)
            call = env.espn.calls[-1]
            self.assertEqual(call["headers"]["Cookie"], f"SWID={TEST_SWID}; espn_s2={TEST_S2}")
            flt = json.loads(call["headers"]["X-Fantasy-Filter"])
            self.assertEqual(flt["players"]["filterStatus"]["value"], ["FREEAGENT", "WAIVERS"])
            self.assertTrue(call["url"].startswith("https://lm-api-reads.fantasy.espn.com/apis/v3/games/fhl/seasons/2027/"
                                                   "segments/0/leagues/488544617?view=kona_player_info"))
        finally:
            env.close()

    def test_errors_never_echo_cookies(self):
        env = Env()
        try:
            c = env.client()
            env.espn.fail = (401, b"{}")
            with self.assertRaises(AuthError) as cm:
                c.status()
            self.assertIn("python3 -m fhockey setup", str(cm.exception))
            env.espn.fail = (400, json.dumps({"messages": [f"bad cookie {TEST_S2} {TEST_SWID}"]}).encode())
            with self.assertRaises(EspnError) as cm:
                c.status()
            self.assertNoSecrets(str(cm.exception), "EspnError")
            env.espn.fail = (503, f"<html>{TEST_S2}</html>".encode())
            with self.assertRaises(TransientError) as cm:
                c.status()
            self.assertNoSecrets(str(cm.exception), "TransientError")
            self.assertEqual(sum(1 for x in env.espn.calls if x["method"] == "GET"), 1 + 1 + 4)  # 503 retried 3x
        finally:
            env.close()

    def test_no_command_prints_or_stores_cookies(self):
        for live in (False, True):
            env = Env(auto_execute=live, ntfy_topic="test-topic-not-real")
            try:
                outputs = []
                code, out, err = run_cli(env, ["setup"], [TEST_SWID, TEST_S2])
                self.assertEqual((code, out.strip().splitlines()[-1]), (0, "ok"))
                outputs += [out, err]
                for argv in (["status"], ["lineup"], ["lineup", "--dry-run"], ["pickups", "-v"], ["stream"],
                             ["watch", "--once"], ["watch", "--once"], ["snapshot"], ["offers"], ["chat"],
                             ["trade-input", "--offer", "tx-offer-in"], ["inbox"]):
                    code, out, err = run_cli(env, argv)
                    self.assertIn(code, (0, 1), f"{argv}: {err}")
                    outputs += [out, err]
                for text in outputs:
                    self.assertNoSecrets(text, "cli output")
                for f in env.all_output_files():
                    self.assertNoSecrets(f.read_text(errors="replace"), str(f))
                posts = env.espn.posts
                if live:
                    self.assertTrue(posts)  # lineup / pickups / stream really wrote (to the fake)
                    self.assertTrue(all(p["body"]["memberId"] == TEST_SWID for p in posts))
                else:
                    self.assertEqual(posts, [])
            finally:
                env.close()

    def test_setup_reports_failure(self):
        env = Env()
        try:
            env.espn.fail = (401, b"{}")
            code, out, err = run_cli(env, ["setup"], ["{WRONG-SWID-1}", "wrongcookievalue"])
            self.assertEqual((code, out.strip().splitlines()[-1]), (1, "failed"))
            self.assertNotIn("wrongcookievalue", out + err)
            self.assertNotIn("WRONG-SWID-1", out + err)
            self.assertEqual(load_auth(env.cfg.auth_file).espn_s2, "wrongcookievalue")
        finally:
            env.close()

    def test_missing_cookies_message(self):
        env = Env()
        try:
            env.cfg.auth_file.unlink()
            code, out, err = run_cli(env, ["status"])
            self.assertEqual(code, 2)
            self.assertIn("python3 -m fhockey setup", err)
        finally:
            env.close()

    def test_save_auth_overwrites_with_600(self):
        env = Env()
        try:
            os.chmod(env.cfg.auth_file, 0o644)
            save_auth(env.cfg.auth_file, "{NEW-SWID-VALUE}", "newcookie123")
            self.assertEqual(stat.S_IMODE(os.stat(env.cfg.auth_file).st_mode), 0o600)
        finally:
            env.close()


class ReadOnlyTest(unittest.TestCase):
    """FHOCKEY_READ_ONLY is set for Claude subprocesses and must beat auto_execute."""

    def setUp(self):
        self.env = Env(auto_execute=True)

    def tearDown(self):
        self.env.close()

    def test_client_refuses_every_write(self):
        c = self.env.client(writes=True)
        with mock.patch.dict(os.environ, {"FHOCKEY_READ_ONLY": "1"}):
            for write in (lambda: c.add_free_agent(1, 2, 34), lambda: c.claim_waiver(1, 2, 34),
                          lambda: c.set_lineup([], 34, 34)):
                with self.assertRaises(WritesDisabled):
                    write()
        self.assertEqual(self.env.espn.posts, [])

    def test_cli_commands_cannot_write(self):
        with mock.patch.dict(os.environ, {"FHOCKEY_READ_ONLY": "1"}):
            for argv in (["lineup"], ["pickups"], ["stream"], ["watch", "--once"]):
                run_cli(self.env, argv)
        self.assertEqual(self.env.espn.posts, [])

    def test_out_stays_inside_trades_dir(self):
        target = self.env.root / "precious.txt"
        target.write_text("keep me")
        with mock.patch.dict(os.environ, {"FHOCKEY_READ_ONLY": "1"}):
            code, _, err = run_cli(self.env, ["trade-input", "--offer", "tx-offer-in", "--out", str(target)])
            self.assertEqual(code, 1)
            self.assertIn("read-only mode", err)
            code, _, err = run_cli(self.env, ["snapshot", "--out", str(target)])
            self.assertEqual(code, 1)
            self.assertEqual(target.read_text(), "keep me")
            self.env.cfg.trades_dir.mkdir(parents=True, exist_ok=True)
            code, _, err = run_cli(self.env, ["snapshot", "--out", str(self.env.cfg.trades_dir / "x.txt")])
            self.assertEqual((code, err), (0, ""))


if __name__ == "__main__":
    unittest.main()
