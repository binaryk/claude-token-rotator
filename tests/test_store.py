"""Tests for ctr.store and ctr.keychain.

Every test runs against a throwaway config directory; `ctr.keychain._run` (the
single subprocess seam) is replaced, so the real macOS keychain and the user's
real ~/.config/ctr are never touched.

Run: PYTHONPATH=src python3 -m unittest tests.test_store -v
"""

import glob
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import keychain, store  # noqa: E402
from ctr.model import (  # noqa: E402
    PROBE_OAUTH_USAGE,
    PROBE_RATELIMIT_HEADERS,
    TokenRecord,
    Usage,
)

#: The shape of a real `claude setup-token` token — never a real one.
FAKE_TOKEN = "sk-ant-oat01-" + ("Zq7" * 20)
NOW = 1789480000


def record(label, **kwargs):
    fields = {
        "account": "alice@example.com",
        "subscription": "max",
        "added_at": "2026-09-15T16:00:00",
        "kind": "oat",
        "note": "",
    }
    fields.update(kwargs)
    return TokenRecord(label=label, **fields)


def usage_for(label, five_h=10.0, probe=PROBE_OAUTH_USAGE, ok=True, checked_at=NOW):
    return Usage(
        label=label,
        five_h=five_h,
        seven_d=5.0,
        five_h_reset=NOW + 3600,
        seven_d_reset=NOW + 86400,
        status="allowed",
        probe=probe,
        ok=ok,
        error="",
        checked_at=checked_at,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ctr-test-")
        self.addCleanup(shutil.rmtree, self.root, True)
        # A directory that does NOT exist yet: ensure_dir must create it 0700.
        self.config_dir = os.path.join(self.root, "config", "ctr")
        self.store = store.Store(self.config_dir)

    def read_text(self, path):
        with open(path, "r") as handle:
            return handle.read()

    def temp_leftovers(self):
        return glob.glob(os.path.join(self.config_dir, ".ctr-*"))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


class TestTokenRegistry(StoreTestCase):
    def test_empty_store_reads_as_empty(self):
        self.assertEqual(self.store.tokens(), [])
        self.assertIsNone(self.store.active())
        self.assertIsNone(self.store.get("nope"))

    def test_add_get_and_list(self):
        self.store.add(record("social"))
        self.store.add(record("devops", account="bob@example.com"))
        self.assertEqual([item.label for item in self.store.tokens()], ["social", "devops"])
        self.assertEqual(self.store.get("devops").account, "bob@example.com")

    def test_duplicate_label_is_rejected(self):
        self.store.add(record("social"))
        with self.assertRaises(ValueError) as caught:
            self.store.add(record("social", account="other@example.com"))
        self.assertIn("already registered", str(caught.exception))
        self.assertEqual(len(self.store.tokens()), 1)

    def test_invalid_label_is_rejected(self):
        for label in ("", "Social", "-lead", "has space", "x" * 41, "emoji✅"):
            with self.assertRaises(ValueError):
                self.store.add(record(label))

    def test_remove(self):
        self.store.add(record("social"))
        self.assertTrue(self.store.remove("social"))
        self.assertFalse(self.store.remove("social"))
        self.assertEqual(self.store.tokens(), [])

    def test_removing_the_active_token_clears_active(self):
        self.store.add(record("social"))
        self.store.set_active("social")
        self.assertEqual(self.store.active(), "social")
        self.store.remove("social")
        self.assertIsNone(self.store.active())

    def test_set_active_requires_a_registered_label(self):
        with self.assertRaises(ValueError):
            self.store.set_active("ghost")
        self.store.add(record("social"))
        self.store.set_active("social")
        self.store.set_active(None)
        self.assertIsNone(self.store.active())

    def test_corrupt_tokens_json_is_an_explicit_error(self):
        self.store.ensure_dir()
        with open(self.store.tokens_path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(store.StoreError):
            self.store.tokens()


# ---------------------------------------------------------------------------
# secrets must never reach disk
# ---------------------------------------------------------------------------


class TestNoSecretsOnDisk(StoreTestCase):
    def test_written_registry_never_contains_the_keychain_token(self):
        calls = []

        def fake_security(cmd, stdin_data=None, timeout_s=15):
            calls.append((list(cmd), stdin_data))
            if cmd[1] == "find-generic-password":
                return 0, FAKE_TOKEN + "\n", ""
            return 0, "", ""

        original = keychain._run
        keychain._run = fake_security
        self.addCleanup(lambda: setattr(keychain, "_run", original))

        keychain.set("ctr:social", "alice@example.com", FAKE_TOKEN)
        self.store.add(record("social"))
        self.store.set_active("social")
        self.store.cache_put(usage_for("social"))

        for path in (self.store.tokens_path, self.store.state_path):
            text = self.read_text(path)
            self.assertNotIn(FAKE_TOKEN, text)
            self.assertNotIn(FAKE_TOKEN[:24], text)

    def test_no_registry_value_looks_like_a_secret(self):
        self.store.add(record("social", note="max plan, personal"))
        data = json.loads(self.read_text(self.store.tokens_path))
        for entry in data["tokens"]:
            for key, value in entry.items():
                if not isinstance(value, str):
                    continue
                self.assertLessEqual(len(value), 40, "%s=%r is suspiciously long" % (key, value))
                self.assertFalse(store.looks_like_secret(value), "%s looks like a secret" % key)

    def test_a_record_carrying_a_token_is_refused(self):
        for field in ("note", "account", "subscription"):
            with self.assertRaises(ValueError) as caught:
                self.store.add(record("social", **{field: FAKE_TOKEN}))
            self.assertIn("keychain", str(caught.exception))
        self.assertEqual(self.store.tokens(), [])

    def test_looks_like_secret_heuristics(self):
        self.assertTrue(store.looks_like_secret(FAKE_TOKEN))
        self.assertTrue(store.looks_like_secret("a" * 40))
        self.assertFalse(store.looks_like_secret("alice@example.com"))
        self.assertFalse(store.looks_like_secret("2026-09-15T16:00:00"))
        self.assertFalse(store.looks_like_secret("max"))


# ---------------------------------------------------------------------------
# file modes and atomic writes
# ---------------------------------------------------------------------------


class TestFilePermissionsAndAtomicity(StoreTestCase):
    def test_directory_is_0700_and_files_are_0600(self):
        self.store.add(record("social"))
        self.store.save_state({"last_switch_at": NOW, "parked": {}, "cache": {}})
        self.assertEqual(stat.S_IMODE(os.stat(self.config_dir).st_mode), 0o700)
        for path in (self.store.tokens_path, self.store.state_path):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600, path)

    def test_an_existing_loose_directory_is_tightened(self):
        os.makedirs(self.config_dir, 0o755)
        os.chmod(self.config_dir, 0o755)
        self.store.add(record("social"))
        self.assertEqual(stat.S_IMODE(os.stat(self.config_dir).st_mode), 0o700)

    def test_writes_leave_no_temp_files_behind(self):
        self.store.add(record("social"))
        self.store.cache_put(usage_for("social"))
        self.assertEqual(self.temp_leftovers(), [])

    def test_a_failed_write_keeps_the_previous_file_intact(self):
        self.store.add(record("social"))
        before = self.read_text(self.store.tokens_path)
        with self.assertRaises(TypeError):
            self.store.save_state({"bad": set(["not serialisable"])})
        self.assertEqual(self.read_text(self.store.tokens_path), before)
        self.assertFalse(os.path.exists(self.store.state_path))
        self.assertEqual(self.temp_leftovers(), [])

    def test_write_is_a_rename_not_a_truncate(self):
        """os.replace means a reader never sees a half-written registry."""
        self.store.add(record("social"))
        inode_before = os.stat(self.store.tokens_path).st_ino
        self.store.add(record("devops"))
        self.assertNotEqual(inode_before, os.stat(self.store.tokens_path).st_ino)


# ---------------------------------------------------------------------------
# monitor state, cache and config
# ---------------------------------------------------------------------------


class TestStateAndConfig(StoreTestCase):
    def test_state_defaults_when_absent(self):
        state = self.store.state()
        self.assertEqual(state["last_switch_at"], 0)
        self.assertEqual(state["parked"], {})
        self.assertEqual(state["cache"], {})

    def test_state_round_trip(self):
        self.store.save_state({"last_switch_at": NOW, "parked": {"social": {"until_5h": 1.0}}})
        state = self.store.state()
        self.assertEqual(state["last_switch_at"], NOW)
        self.assertEqual(state["parked"]["social"]["until_5h"], 1.0)
        self.assertEqual(state["cache"], {})

    def test_corrupt_state_json_falls_back_to_defaults(self):
        self.store.ensure_dir()
        with open(self.store.state_path, "w") as handle:
            handle.write("}{ not json")
        self.assertEqual(self.store.state()["last_switch_at"], 0)

    def test_config_merges_user_overrides_and_ignores_unknown_keys(self):
        self.store.ensure_dir()
        with open(self.store.config_path, "w") as handle:
            json.dump({"switch_at_5h": 70.0, "bogus": 1}, handle)
        config = self.store.config()
        self.assertEqual(config["switch_at_5h"], 70.0)
        self.assertEqual(config["switch_at_7d"], 95.0)
        self.assertNotIn("bogus", config)

    def test_config_defaults_when_absent(self):
        self.assertEqual(self.store.config()["min_switch_interval_s"], 600)


class TestUsageCache(StoreTestCase):
    def test_cache_round_trip_within_ttl(self):
        self.store.cache_put(usage_for("social", five_h=42.0))
        cached = self.store.cache_get("social", 60, NOW + 30)
        self.assertIsNotNone(cached)
        self.assertEqual(cached.five_h, 42.0)
        self.assertEqual(cached.label, "social")

    def test_cache_expires_after_ttl(self):
        self.store.cache_put(usage_for("social"))
        self.assertIsNone(self.store.cache_get("social", 60, NOW + 61))

    def test_cache_miss_for_unknown_label(self):
        self.assertIsNone(self.store.cache_get("ghost", 60, NOW))

    def test_entry_from_the_future_is_ignored(self):
        self.store.cache_put(usage_for("social", checked_at=NOW + 500))
        self.assertIsNone(self.store.cache_get("social", 60, NOW))

    def test_malformed_cache_entry_is_a_miss_not_a_crash(self):
        state = self.store.state()
        state["cache"] = {"social": {"label": "social", "unexpected": True}}
        self.store.save_state(state)
        self.assertIsNone(self.store.cache_get("social", 60, NOW))

    def test_cache_put_remembers_the_working_probe_strategy(self):
        self.store.cache_put(usage_for("social", probe=PROBE_RATELIMIT_HEADERS))
        self.assertEqual(self.store.probe_strategy("social"), PROBE_RATELIMIT_HEADERS)
        self.assertIsNone(self.store.probe_strategy("ghost"))

    def test_a_failed_reading_does_not_overwrite_the_strategy(self):
        self.store.cache_put(usage_for("social", probe=PROBE_RATELIMIT_HEADERS))
        self.store.cache_put(Usage.failed("social", "probe failed", NOW))
        self.assertEqual(self.store.probe_strategy("social"), PROBE_RATELIMIT_HEADERS)

    def test_cache_put_preserves_other_state(self):
        self.store.save_state({"last_switch_at": NOW, "parked": {"a": {}}})
        self.store.cache_put(usage_for("social"))
        state = self.store.state()
        self.assertEqual(state["last_switch_at"], NOW)
        self.assertEqual(state["parked"], {"a": {}})


# ---------------------------------------------------------------------------
# keychain wrapper (subprocess stubbed — the real keychain is never touched)
# ---------------------------------------------------------------------------


class FakeSecurity(object):
    def __init__(self, stored=None, add_rc=0, stderr=""):
        self.stored = dict(stored or {})
        self.add_rc = add_rc
        self.stderr = stderr
        self.calls = []

    def __call__(self, cmd, stdin_data=None, timeout_s=15):
        self.calls.append((list(cmd), stdin_data))
        action = cmd[1]
        service = cmd[cmd.index("-s") + 1]
        if action == "add-generic-password":
            if self.add_rc == 0 and stdin_data:
                halves = stdin_data.split("\n")
                if len(halves) >= 2 and halves[0] == halves[1]:
                    self.stored[service] = halves[0]
                else:  # what the real `security` does with a single line
                    self.stored[service] = ""
            return self.add_rc, "", self.stderr
        if action == "find-generic-password":
            if service not in self.stored:
                return 44, "", "The specified item could not be found."
            if "-w" in cmd:
                return 0, self.stored[service] + "\n", ""
            return 0, 'attributes:\n    "svce"<blob>="%s"\n' % service, ""
        if action == "delete-generic-password":
            return (0, "", "") if self.stored.pop(service, None) is not None else (44, "", "")
        return 1, "", "unexpected command"


class KeychainTestCase(unittest.TestCase):
    def install(self, fake):
        original = keychain._run
        keychain._run = fake
        self.addCleanup(lambda: setattr(keychain, "_run", original))
        return fake


class TestKeychain(KeychainTestCase):
    def test_set_sends_the_secret_on_stdin_twice_and_never_in_argv(self):
        fake = self.install(FakeSecurity())
        keychain.set("ctr:social", "alice@example.com", FAKE_TOKEN)
        # set() purges any pre-existing item for the service before writing, so
        # the add is no longer calls[0]. Locate it by verb rather than index.
        adds = [c for c in fake.calls if c[0][1] == "add-generic-password"]
        self.assertEqual(1, len(adds), "exactly one add-generic-password call")
        cmd, stdin_data = adds[0]
        self.assertEqual(cmd[-1], "-w", "-w must stay last so security reads stdin")
        for element in cmd:
            self.assertNotIn(FAKE_TOKEN, element)
        self.assertEqual(stdin_data, "%s\n%s\n" % (FAKE_TOKEN, FAKE_TOKEN))
        self.assertEqual(keychain.get("ctr:social"), FAKE_TOKEN)

    def test_set_refuses_an_empty_secret(self):
        fake = self.install(FakeSecurity())
        for secret in ("", "   ", None):
            with self.assertRaises(ValueError):
                keychain.set("ctr:social", "a@b.com", secret)
        self.assertEqual(fake.calls, [])

    def test_set_verifies_the_read_back(self):
        """security exits 0 after storing an EMPTY password — catch that."""
        fake = self.install(FakeSecurity())

        def broken(cmd, stdin_data=None, timeout_s=15):
            if cmd[1] == "add-generic-password":
                return 0, "", ""  # pretend it stored nothing
            return fake(cmd, stdin_data, timeout_s)

        self.install(broken)
        with self.assertRaises(keychain.KeychainError) as caught:
            keychain.set("ctr:social", "a@b.com", FAKE_TOKEN)
        self.assertIn("could not be verified", str(caught.exception))
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_failure_message_is_scrubbed_of_the_secret(self):
        self.install(FakeSecurity(add_rc=1, stderr="failed near " + FAKE_TOKEN))
        with self.assertRaises(keychain.KeychainError) as caught:
            keychain.set("ctr:social", "a@b.com", FAKE_TOKEN)
        message = str(caught.exception)
        self.assertNotIn(FAKE_TOKEN, message)
        self.assertIn("***", message)

    def test_get_returns_none_when_absent_or_empty(self):
        self.install(FakeSecurity(stored={"ctr:blank": ""}))
        self.assertIsNone(keychain.get("ctr:missing"))
        self.assertIsNone(keychain.get("ctr:blank"))
        self.assertIsNone(keychain.get(""))

    def test_token_for_uses_the_ctr_prefix(self):
        fake = self.install(FakeSecurity(stored={"ctr:social": FAKE_TOKEN}))
        self.assertEqual(keychain.token_for("social"), FAKE_TOKEN)
        self.assertIsNone(keychain.token_for("devops"))
        self.assertIsNone(keychain.token_for(""))
        self.assertTrue(any("ctr:social" in cmd for cmd, _ in fake.calls))

    def test_delete_reports_whether_the_item_existed(self):
        self.install(FakeSecurity(stored={"ctr:social": FAKE_TOKEN}))
        self.assertTrue(keychain.delete("ctr:social"))
        self.assertFalse(keychain.delete("ctr:social"))

    def test_exists_does_not_read_the_secret(self):
        fake = self.install(FakeSecurity(stored={"ctr:social": FAKE_TOKEN}))
        self.assertTrue(keychain.exists("ctr:social"))
        cmd, _stdin = fake.calls[-1]
        self.assertNotIn("-w", cmd)

    def test_list_ctr_services_filters_by_real_presence(self):
        self.install(FakeSecurity(stored={"ctr:social": FAKE_TOKEN}))
        self.assertEqual(
            keychain.list_ctr_services(["social", "devops"]), ["ctr:social"]
        )
        self.assertEqual(keychain.list_ctr_services([]), [])

    def test_claude_login_token_and_info(self):
        blob = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": FAKE_TOKEN,
                    "subscriptionType": "max",
                    "expiresAt": 1789476798862,  # milliseconds
                }
            }
        )
        self.install(FakeSecurity(stored={"Claude Code-credentials": blob}))
        self.assertEqual(keychain.claude_login_token(), FAKE_TOKEN)
        info = keychain.claude_login_info()
        self.assertEqual(info["subscription"], "max")
        self.assertEqual(info["expires_at"], 1789476798)
        self.assertNotIn(FAKE_TOKEN, json.dumps(info))

    def test_claude_login_handles_missing_or_garbage_item(self):
        self.install(FakeSecurity())
        self.assertIsNone(keychain.claude_login_token())
        self.assertEqual(keychain.claude_login_info()["subscription"], "")
        self.install(FakeSecurity(stored={"Claude Code-credentials": "not json"}))
        self.assertIsNone(keychain.claude_login_token())
        self.assertEqual(keychain.claude_login_info()["expires_at"], 0)


if __name__ == "__main__":
    unittest.main()


class CachePutClearsTheFailureCounterTests(StoreTestCase):
    """v1.1 item 2 — a success resets the count whichever command probed.

    `ctr status` at 09:38 on 2026-09-16 probed fine and cached the reading,
    but `probe_failures.social` stayed at 2 because only a monitor tick ever
    called `record_probe_results`. Every command's success path goes through
    `cache_put`, so the reset belongs there.
    """

    def _reading(self, label="social", ok=True):
        return Usage(
            label=label, five_h=4.0, seven_d=45.0, five_h_reset=0, seven_d_reset=0,
            status="allowed", probe=PROBE_RATELIMIT_HEADERS, ok=ok, error="",
            checked_at=1789540730,
        )

    def test_a_successful_reading_clears_the_counter(self):
        state = self.store.state()
        state["probe_failures"] = {"social": 2}
        state["probe_failed_at"] = {"social": 1789500000}
        self.store.save_state(state)

        self.store.cache_put(self._reading())

        after = self.store.state()
        self.assertEqual(0, after["probe_failures"]["social"])
        self.assertEqual({}, after.get("probe_failed_at", {}))

    def test_it_leaves_other_labels_alone(self):
        state = self.store.state()
        state["probe_failures"] = {"social": 2, "spare": 1}
        self.store.save_state(state)
        self.store.cache_put(self._reading("social"))
        after = self.store.state()
        self.assertEqual(0, after["probe_failures"]["social"])
        self.assertEqual(1, after["probe_failures"]["spare"])

    def test_the_cached_reading_is_still_written(self):
        self.store.cache_put(self._reading())
        cached = self.store.state()["cache"]["social"]
        self.assertEqual(4.0, cached["five_h"])

    def test_a_state_with_no_counters_is_fine(self):
        self.store.cache_put(self._reading())  # must not raise
        self.assertEqual(0, self.store.state().get("probe_failures", {}).get("social", 0))


class CacheHitIsNotAProbeTests(StoreTestCase):
    """A cache HIT must not reset the counter — only a real probe does.

    Found by running the installed command: `ctr status` inside the 60 s cache
    TTL served the cached reading, never called `cache_put`, and left the
    counter alone. That is correct (no probe, no new evidence) but it is a real
    distinction, so it is pinned here. The brief's live case — a `ctr status`
    after a 7.5 h sleep — has a stale cache, really probes, and does reset;
    `--fresh` forces a probe at any time.
    """

    def test_a_cache_hit_leaves_the_counter_alone(self):
        reading = Usage(
            label="social", five_h=4.0, seven_d=45.0, five_h_reset=0, seven_d_reset=0,
            status="allowed", probe=PROBE_RATELIMIT_HEADERS, ok=True, error="",
            checked_at=1000,
        )
        self.store.cache_put(reading)
        state = self.store.state()
        state["probe_failures"] = {"social": 1}
        self.store.save_state(state)

        hit = self.store.cache_get("social", ttl_s=60, now=1030)
        self.assertIsNotNone(hit, "still inside the TTL")
        self.assertEqual(1, self.store.state()["probe_failures"]["social"])

    def test_an_expired_entry_is_a_miss(self):
        reading = Usage(
            label="social", five_h=4.0, seven_d=45.0, five_h_reset=0, seven_d_reset=0,
            status="allowed", probe=PROBE_RATELIMIT_HEADERS, ok=True, error="",
            checked_at=1000,
        )
        self.store.cache_put(reading)
        self.assertIsNone(self.store.cache_get("social", ttl_s=60, now=1100))
