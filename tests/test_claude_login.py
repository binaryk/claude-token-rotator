"""Tests for claude_login.py — the seamless-switch write path (v2).

Nothing here touches the real keychain: `_run` is replaced by FakeSecurity.
The behaviours pinned are the ones measured live on 2026-09-30 (see the
module docstring): the stored shape, the per-config-dir service name, that
every other key in the document survives, and that the displaced /login is
saved and restorable.
"""

import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

from ctr import claude_login as cl  # noqa: E402
from fake_security import FakeSecurity  # noqa: E402

SOCIAL = "sk-ant-oat01-SOCIALSOCIAL-0123456789abcdefghij"
SPARE = "sk-ant-oat01-SPARESPARESP-0123456789abcdefghij"
LOGIN = {
    "accessToken": "sk-ant-oat01-LOGINACCESS-0123456789",
    "refreshToken": "sk-ant-ort01-LOGINREFRESH-0123456789",
    "expiresAt": 4102444800000,
    "scopes": ["user:inference", "user:profile"],
    "subscriptionType": "max",
}
MCP = {"atlassian|1": {"accessToken": "mcp-secret", "refreshToken": "mcp-refresh"}}
ENV = {}  # default service name


class Base(unittest.TestCase):
    def setUp(self):
        self.sec = FakeSecurity()
        original = cl._run
        cl._run = self.sec.run
        self.addCleanup(setattr, cl, "_run", original)
        self.acct = cl.keychain_account()

    def seed(self, doc, environ=None):
        self.sec.items[(cl.service_name(environ or ENV), self.acct)] = json.dumps(doc)

    def doc(self, environ=None):
        return json.loads(self.sec.items[(cl.service_name(environ or ENV), self.acct)])


class TestServiceName(unittest.TestCase):
    def test_default_is_claudes_own_item(self):
        self.assertEqual(cl.service_name({}), "Claude Code-credentials")

    def test_config_dir_gets_the_hashed_suffix_claude_uses(self):
        directory = "/tmp/some/cfg"
        expected = hashlib.sha256(directory.encode()).hexdigest()[:8]
        self.assertEqual(cl.service_name({"CLAUDE_CONFIG_DIR": directory}),
                         "Claude Code-credentials-" + expected)

    def test_securestorage_dir_wins_and_empty_means_default(self):
        self.assertEqual(cl.service_name({"CLAUDE_SECURESTORAGE_CONFIG_DIR": "",
                                          "CLAUDE_CONFIG_DIR": "/x"}),
                         "Claude Code-credentials")
        self.assertTrue(cl.service_name({"CLAUDE_SECURESTORAGE_CONFIG_DIR": "/y"})
                        .startswith("Claude Code-credentials-"))

    def test_backup_slot_is_per_service(self):
        self.assertNotEqual(cl.backup_service({}), cl.backup_service({"CLAUDE_CONFIG_DIR": "/x"}))


class TestSwitch(Base):
    def test_stores_the_measured_setup_token_shape(self):
        cl.switch_to(SOCIAL, "max", ENV)
        oauth = self.doc()["claudeAiOauth"]
        self.assertEqual(oauth, {"accessToken": SOCIAL, "refreshToken": None, "expiresAt": None,
                                 "scopes": ["user:inference"], "subscriptionType": "max"})

    def test_refresh_token_is_null_never_empty_string(self):
        # Claude reads refreshToken == "" as "dead login" (measured in the binary).
        cl.switch_to(SOCIAL, "", ENV)
        self.assertIsNone(self.doc()["claudeAiOauth"]["refreshToken"])
        self.assertIsNone(self.doc()["claudeAiOauth"]["subscriptionType"])

    def test_every_other_key_survives(self):
        self.seed({"claudeAiOauth": LOGIN, "mcpOAuth": MCP, "organizationUuid": "org"})
        cl.switch_to(SOCIAL, "", ENV)
        doc = self.doc()
        self.assertEqual(doc["mcpOAuth"], MCP)
        self.assertEqual(doc["organizationUuid"], "org")

    def test_the_displaced_login_is_backed_up_and_restorable(self):
        self.seed({"claudeAiOauth": LOGIN, "mcpOAuth": MCP})
        result = cl.switch_to(SOCIAL, "", ENV)
        self.assertTrue(result["backed_up_login"])
        cl.switch_to(SPARE, "", ENV)  # switching between tokens keeps the backup
        cl.restore_login(ENV)
        self.assertEqual(self.doc()["claudeAiOauth"], LOGIN)
        self.assertEqual(self.doc()["mcpOAuth"], MCP)

    def test_backup_is_recaptured_after_a_refresh_rotation(self):
        self.seed({"claudeAiOauth": LOGIN})
        cl.switch_to(SOCIAL, "", ENV)
        cl.restore_login(ENV)
        rotated = dict(LOGIN, refreshToken="sk-ant-ort01-ROTATED-99", accessToken="new-access")
        self.seed({"claudeAiOauth": rotated})  # a running session refreshed meanwhile
        cl.switch_to(SOCIAL, "", ENV)
        cl.restore_login(ENV)
        self.assertEqual(self.doc()["claudeAiOauth"]["refreshToken"], "sk-ant-ort01-ROTATED-99")

    def test_switching_to_the_held_token_is_a_no_op(self):
        cl.switch_to(SOCIAL, "", ENV)
        writes = len(self.sec.calls)
        self.assertTrue(cl.switch_to(SOCIAL, "", ENV)["unchanged"])
        self.assertEqual(len(self.sec.calls), writes + 1)  # one read, no write

    def test_refuses_to_clobber_a_document_it_cannot_parse(self):
        self.sec.items[(cl.service_name(ENV), self.acct)] = "not json{"
        with self.assertRaises(cl.LoginStoreError):
            cl.switch_to(SOCIAL, "", ENV)
        self.assertEqual(self.sec.items[(cl.service_name(ENV), self.acct)], "not json{")

    def test_empty_token_is_refused(self):
        with self.assertRaises(ValueError):
            cl.switch_to("  ", "", ENV)

    def test_restore_without_backup_explains(self):
        with self.assertRaises(cl.LoginStoreError) as ctx:
            cl.restore_login(ENV)
        self.assertIn("no saved interactive login", str(ctx.exception))

    def test_write_failure_never_echoes_the_secret(self):
        self.sec.fail_writes = True
        with self.assertRaises(cl.LoginStoreError) as ctx:
            cl.switch_to(SOCIAL, "", ENV)
        self.assertNotIn(SOCIAL, str(ctx.exception))
        self.assertNotIn(SOCIAL.encode().hex(), str(ctx.exception))


class TestUnreadableStore(Base):
    """Verifier finding 1: a failed read is not an empty store."""

    def test_a_locked_keychain_aborts_the_switch_and_keeps_everything(self):
        self.seed({"claudeAiOauth": LOGIN, "mcpOAuth": MCP})
        real = self.sec.run

        def locked(cmd, stdin_data=None):
            if "find-generic-password" in cmd:
                return 36, "", "User interaction is not allowed."
            return real(cmd, stdin_data)

        cl._run = locked
        with self.assertRaises(cl.LoginStoreError):
            cl.switch_to(SOCIAL, "", ENV)
        cl._run = real
        self.assertEqual(self.doc(), {"claudeAiOauth": LOGIN, "mcpOAuth": MCP})

    def test_only_not_found_means_absent(self):
        self.assertEqual(cl.read_store(ENV), {})
        self.assertEqual(cl.holder({}, ENV), "none")


class TestRestoreNeverDowngrades(Base):
    """Verifier finding 2: a newer /login in the store beats ctr's older copy."""

    def test_restore_keeps_a_newer_login_and_adopts_it_as_the_backup(self):
        self.seed({"claudeAiOauth": LOGIN})
        cl.switch_to(SOCIAL, "", ENV)
        newer = dict(LOGIN, accessToken="newer-access", refreshToken="sk-ant-ort01-NEWER")
        self.seed({"claudeAiOauth": newer})  # the user ran `claude /login` meanwhile
        result = cl.restore_login(ENV)
        self.assertTrue(result["already_login"])
        self.assertEqual(self.doc()["claudeAiOauth"], newer)
        cl.switch_to(SOCIAL, "", ENV)
        cl.restore_login(ENV)
        self.assertEqual(self.doc()["claudeAiOauth"], newer)


class TestWritePath(Base):
    def test_small_documents_go_through_stdin_not_argv(self):
        cl.switch_to(SOCIAL, "", ENV)
        for argv in self.sec.calls:
            self.assertFalse(any(SOCIAL in a or SOCIAL.encode().hex() in a for a in argv))

    def test_large_documents_use_argv_hex_like_claude_does(self):
        big = {"x%d" % i: "y" * 200 for i in range(60)}  # ~13 KB, like a real item
        self.seed({"claudeAiOauth": LOGIN, "mcpOAuth": big})
        cl.switch_to(SOCIAL, "", ENV)
        self.assertEqual(self.doc()["mcpOAuth"], big)
        argv_writes = [c for c in self.sec.calls if c[1:2] == ["add-generic-password"]]
        self.assertTrue(argv_writes, "a >4032-byte line cannot go through security -i")

    def test_write_is_an_upsert_never_a_delete(self):
        # delete + add would drop the item's ACL, which Claude Code relies on.
        self.seed({"claudeAiOauth": LOGIN})
        cl.switch_to(SOCIAL, "", ENV)
        self.assertFalse(any("delete-generic-password" in c for c in self.sec.calls))


class TestHolder(Base):
    def test_names_the_ctr_label_the_store_holds(self):
        cl.switch_to(SPARE, "", ENV)
        self.assertEqual(cl.holder({"social": SOCIAL, "spare": SPARE}, ENV), "spare")

    def test_login_none_and_other(self):
        self.assertEqual(cl.holder({}, ENV), "none")
        self.seed({"claudeAiOauth": LOGIN})
        self.assertEqual(cl.holder({"social": SOCIAL}, ENV), "login")
        self.seed({"claudeAiOauth": {"accessToken": "sk-ant-oat01-unknown"}})
        self.assertEqual(cl.holder({"social": SOCIAL}, ENV), "other")


if __name__ == "__main__":
    unittest.main()
