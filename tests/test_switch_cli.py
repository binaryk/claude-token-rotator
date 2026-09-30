"""Tests for v2's `ctr switch`, the switcher, session scanning and Fable probes.

Everything runs against fakes: FakeSecurity for Claude's credentials store,
an in-memory dict for ctr's own keychain items, a canned `ps` listing, and a
tempdir config. No real keychain, process table or network.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from ctr import claude_login, cli, fable, keychain, sessions, shell, switcher  # noqa: E402
from ctr import usage as usage_module  # noqa: E402
from ctr.model import TokenRecord, Usage  # noqa: E402
from ctr.store import Store  # noqa: E402
from fake_security import FakeSecurity  # noqa: E402

TOKENS = {
    "social": "sk-ant-oat01-SOCIALSOCIAL-0123456789abcdefghij",
    "spare": "sk-ant-oat01-SPARESPARESP-0123456789abcdefghij",
}
LOGIN = {"accessToken": "login-access", "refreshToken": "login-refresh",
         "expiresAt": 4102444800000, "scopes": ["user:inference", "user:profile"]}
PS = (
    "  101     1 /Users/x/.local/share/claude/versions/2.1.285 --model x TERM=xterm\n"
    "  102     1 claude CLAUDE_CODE_OAUTH_TOKEN=%s PATH=/bin\n"
    "  103     1 claude CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-NOT-A-CTR-TOKEN-000 PATH=/bin\n"
    "  104     1 /usr/bin/vim CLAUDE_CODE_OAUTH_TOKEN=%s\n"
    "  105     1 node /x/claude-helper.js\n"
    "  106   102 claude CLAUDE_CODE_OAUTH_TOKEN=%s PATH=/bin\n"
) % (TOKENS["spare"], TOKENS["social"], TOKENS["spare"])


def read(path):
    with open(path) as handle:
        return handle.read()


def reading(label, five=10.0):
    return Usage(label=label, five_h=five, seven_d=5.0, five_h_reset=None, seven_d_reset=None,
                 status="allowed", probe="ratelimit_headers", ok=True, checked_at=1)


class SwitchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-switch-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "cfg")
        self.active = os.path.join(self.config, "active.sh")
        self.store = Store(self.config)
        for label in TOKENS:
            self.store.add(TokenRecord(label=label, account=label + "@x", subscription="max",
                                       added_at="2026-09-30", kind="oat", note=""))
        self.sec = FakeSecurity()
        self._patch(claude_login, "_run", self.sec.run)
        self._patch(keychain, "token_for", lambda label: TOKENS.get(label))
        # Guard: nothing in this file may reach the real /usr/bin/security for
        # ctr's own items. (A `ctr remove` here once deleted a real ctr:<label>
        # item because only token_for was faked — 2026-09-30.)
        self._patch(keychain, "_run", self._refuse_real_keychain)
        self._patch(keychain, "delete", lambda service: True)
        self._patch(sessions, "_ps", lambda: PS)
        self._patch(usage_module, "probe_all",
                    lambda records, tokens, timeout_s=20, workers=4, prefers=None:
                    [reading(r.label) for r in records])
        self.acct = claude_login.keychain_account()
        self._patch(os, "environ", dict(os.environ))
        for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"):
            os.environ.pop(name, None)

    @staticmethod
    def _refuse_real_keychain(cmd, stdin_data=None, timeout_s=15):
        raise AssertionError("test tried to run the real security tool: %s" % cmd[1:2])

    def _patch(self, obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        self.addCleanup(setattr, obj, name, original)

    def claude_doc(self):
        return json.loads(self.sec.items[(claude_login.service_name(), self.acct)])

    def ctr(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config-dir", self.config, "--active-file", self.active] + list(argv))
        return code, out.getvalue(), err.getvalue()


class TestCtrSwitch(SwitchTestCase):
    def test_switch_writes_claudes_store_and_keychain_mode_active_sh(self):
        self.sec.items[(claude_login.service_name(), self.acct)] = json.dumps(
            {"claudeAiOauth": LOGIN, "mcpOAuth": {"a": 1}})
        code, out, _err = self.ctr("switch", "spare")
        self.assertEqual(code, 0)
        self.assertEqual(self.claude_doc()["claudeAiOauth"]["accessToken"], TOKENS["spare"])
        self.assertEqual(self.claude_doc()["mcpOAuth"], {"a": 1})
        self.assertEqual(self.store.active(), "spare")
        self.assertEqual(self.store.switch_mode(), "keychain")
        body = read(self.active)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN", body)
        self.assertNotIn("find-generic-password", body)
        self.assertIn("login was saved", out)

    def test_switch_json_is_secret_free_and_counts_sessions(self):
        code, out, _err = self.ctr("switch", "social", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["claude_store_holds"], "social")
        self.assertEqual(payload["sessions"], {
            "following": 1, "pinned": 2, "pinned_by_label": {"spare": 1, "unknown": 1}})
        for token in TOKENS.values():
            self.assertNotIn(token, out)
        self.assertNotIn("sk-ant", out)

    def test_human_output_names_pinned_sessions_and_rollover(self):
        _code, out, _err = self.ctr("switch", "social")
        self.assertIn("2 session(s) were started with CLAUDE_CODE_OAUTH_TOKEN", out)
        self.assertIn("ctr rollover", out)
        self.assertNotIn("sk-ant", out)

    def test_unknown_label_is_a_usage_error_and_touches_nothing(self):
        code, _out, err = self.ctr("switch", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no token labelled", err)
        self.assertEqual(self.sec.items, {})

    def test_a_failed_store_write_changes_neither_registry_nor_active_sh(self):
        self.sec.fail_writes = True
        code, _out, err = self.ctr("switch", "spare")
        self.assertEqual(code, 1)
        self.assertIsNone(self.store.active())
        self.assertFalse(os.path.exists(self.active))
        self.assertNotIn("sk-ant", err)

    def test_restore_login_round_trip(self):
        self.sec.items[(claude_login.service_name(), self.acct)] = json.dumps({"claudeAiOauth": LOGIN})
        self.ctr("switch", "spare")
        code, out, _err = self.ctr("switch", "--restore-login")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.claude_doc()["claudeAiOauth"], LOGIN)
        self.assertEqual(self.store.switch_mode(), "env")
        self.assertIsNone(self.store.active())

    def test_use_goes_back_to_env_mode(self):
        self.ctr("switch", "spare")
        self.ctr("use", "social")
        self.assertEqual(self.store.switch_mode(), "env")
        self.assertIn("find-generic-password -s 'ctr:social'", read(self.active))

    def test_next_follows_the_current_mode(self):
        self.ctr("switch", "spare")
        self._patch(usage_module, "probe_all",
                    lambda records, tokens, timeout_s=20, workers=4, prefers=None:
                    [reading(r.label, 95.0 if r.label == "spare" else 5.0) for r in records])
        code, _out, err = self.ctr("next", "--fresh")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.claude_doc()["claudeAiOauth"]["accessToken"], TOKENS["social"])

    def test_status_json_adds_mode_holder_sessions_and_fable(self):
        self.ctr("switch", "spare")
        _code, out, _err = self.ctr("status", "--json")
        payload = json.loads(out)
        self.assertEqual(payload["mode"], "keychain")
        self.assertEqual(payload["claude_store_holds"], "spare")
        self.assertIn("pinned", payload["sessions"])
        self.assertEqual(set(payload["fable"]), set(TOKENS))
        self.assertNotIn("sk-ant", out)

    def test_ui_without_textual_prints_the_install_hint(self):
        import importlib.util
        self._patch(importlib.util, "find_spec", lambda name: None)
        code, _out, err = self.ctr("ui")
        self.assertEqual(code, 2)
        self.assertIn("pip install", err)

    def test_use_after_switch_hands_the_login_back(self):
        # Verifier finding 3: leaving keychain mode must not strand a ctr token
        # in Claude's store where nothing will ever move it again.
        self.sec.items[(claude_login.service_name(), self.acct)] = json.dumps({"claudeAiOauth": LOGIN})
        self.ctr("switch", "spare")
        self.ctr("use", "social")
        self.assertEqual(self.claude_doc()["claudeAiOauth"], LOGIN)

    def test_removing_the_active_token_in_keychain_mode_restores_and_leaves_env_mode(self):
        self.sec.items[(claude_login.service_name(), self.acct)] = json.dumps({"claudeAiOauth": LOGIN})
        self.ctr("switch", "spare")
        code, _out, err = self.ctr("remove", "spare")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.claude_doc()["claudeAiOauth"], LOGIN)
        self.assertEqual(self.store.switch_mode(), "env")

    def test_monitor_switch_in_keychain_mode_moves_claudes_store(self):
        from ctr import monitor
        self.ctr("switch", "spare")
        state = self.store.state()
        self.assertTrue(monitor._apply_switch(self.store, state, "spare", "social", 100))
        self.assertEqual(self.claude_doc()["claudeAiOauth"]["accessToken"], TOKENS["social"])


class TestSessions(unittest.TestCase):
    def test_scan_classifies_without_keeping_tokens(self):
        original = sessions._ps
        sessions._ps = lambda: PS
        try:
            found = sessions.scan(TOKENS)
        finally:
            sessions._ps = original
        self.assertEqual([(s.pid, s.pinned, s.label) for s in found],
                         [(101, False, ""), (102, True, "spare"), (103, True, "unknown")])
        self.assertNotIn("sk-ant", repr(found))


class TestShellKeychainMode(unittest.TestCase):
    def test_keychain_body_exports_nothing_and_parses_back(self):
        body = shell.active_sh_contents("spare", "keychain")
        self.assertNotIn("export CLAUDE_CODE_OAUTH_TOKEN", body)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN", body)
        self.assertEqual(shell.parse_active_label(body), "spare")

    def test_bad_mode_and_label_are_refused(self):
        with self.assertRaises(ValueError):
            shell.active_sh_contents("spare", "magic")
        with self.assertRaises(ValueError):
            shell.active_sh_contents("bad label;rm", "keychain")


class TestFable(unittest.TestCase):
    def test_classify(self):
        ok = json.dumps({"is_error": False, "result": "ok"})
        limited = json.dumps({"is_error": True, "result": "You've reached your Fable limit"})
        other = json.dumps({"is_error": True, "result": "Not logged in"})
        self.assertEqual(fable.classify(0, ok, "")[0], fable.YES)
        self.assertEqual(fable.classify(1, limited, "")[0], fable.NO)
        self.assertEqual(fable.classify(1, other, "")[0], fable.UNKNOWN)
        self.assertEqual(fable.classify(124, "", "timed out")[0], fable.UNKNOWN)

    def test_classify_scrubs_tokens(self):
        leaked = json.dumps({"is_error": True, "result": "bad sk-ant-oat01-LEAKLEAKLEAK"})
        self.assertNotIn("LEAK", fable.classify(1, leaked, "")[1])

    def test_probe_passes_the_token_in_env_not_argv(self):
        seen = {}

        def fake_run(cmd, env, cwd):
            seen.update(cmd=cmd, env=env)
            return 0, json.dumps({"is_error": False, "result": "ok"}), ""

        original = fable._run
        fable._run = fake_run
        tmp = tempfile.mkdtemp()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp
        try:
            verdict, _ = fable.probe(TOKENS["social"])
        finally:
            fable._run = original
            os.environ["HOME"] = old_home
            shutil.rmtree(tmp, True)
        self.assertEqual(verdict, fable.YES)
        self.assertFalse(any(TOKENS["social"] in a for a in seen["cmd"]))
        self.assertEqual(seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"], TOKENS["social"])
        self.assertIn("CLAUDE_CONFIG_DIR", seen["env"])
        self.assertNotIn("--bare", seen["cmd"], "--bare ignores the env token (measured)")

    def test_cache_ttl(self):
        state = {"fable": {"a": {"verdict": "yes", "detail": "", "checked_at": 1000}}}
        self.assertIsNotNone(fable.cached(state, "a", 1000 + fable.FABLE_TTL_S))
        self.assertIsNone(fable.cached(state, "a", 1001 + fable.FABLE_TTL_S))
        self.assertIsNone(fable.cached(state, "b", 1000))


class TestOverage(unittest.TestCase):
    def test_overage_header_is_parsed(self):
        path = os.path.join(HERE, "fixtures", "ratelimit_headers_allowed.txt")
        parsed = usage_module.parse_ratelimit_headers("a", read(path), 1)
        self.assertEqual(parsed.overage, "rejected (out_of_credits)")

    def test_old_cache_entries_without_overage_still_load(self):
        data = reading("a").to_json()
        data.pop("overage")
        self.assertEqual(Usage.from_json(data).overage, "")


if __name__ == "__main__":
    unittest.main()
