"""Tests for monitor.py, notify.py and launchd.py (integration round).

These three modules had NO test anywhere in the repo, including monitor.py —
the 218-line module that actually performs the switch. The lane's report
asserted the behaviours below were verified, but the assertions were throwaway
probes that never landed in the tree, so nothing guarded them against the next
edit. That is what this file fixes.

Nothing here touches the network, the keychain, launchctl, osascript or the
user's real ~/.config/ctr: every subprocess seam is replaced, and every path is
a tempfile.
"""

import json
import os
import plistlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import launchd, monitor, notify, selector  # noqa: E402
from ctr.model import Decision, TokenRecord, Usage, merged_config  # noqa: E402

NOW = 1789480000
SECRET = "sk-ant-oat01-ZZSENTINELZZ-do-not-log-me-0123456789"


def usage(label, five_h, seven_d=10.0, ok=True, status="allowed"):
    return Usage(
        label=label, five_h=five_h, seven_d=seven_d, five_h_reset=NOW + 600,
        seven_d_reset=NOW + 6000, status=status, probe="ratelimit_headers",
        ok=ok, error="", checked_at=NOW,
    )


class FakeStore(object):
    """Just enough Store for monitor.tick, with per-method failure injection."""

    def __init__(self, labels=("a", "b"), active="a", usages=None):
        self._records = [
            TokenRecord(label=name, account="", subscription="", added_at="",
                        kind="oat", note="")
            for name in labels
        ]
        self._active = active
        self._state = {"last_switch_at": 0, "parked": {}, "cache": {}, "strategies": {}}
        self._config = {}
        self.usages = usages or [usage("a", 95.0), usage("b", 5.0)]
        self.saved_state = None
        self.raises = set()
        self.strategy_calls = []

    def _maybe_raise(self, name):
        if name in self.raises:
            raise RuntimeError("boom in %s" % name)

    def tokens(self):
        self._maybe_raise("tokens")
        return list(self._records)

    def active(self):
        self._maybe_raise("active")
        return self._active

    def set_active(self, label):
        self._maybe_raise("set_active")
        self._active = label

    def state(self):
        self._maybe_raise("state")
        return dict(self._state)

    def save_state(self, state):
        self._maybe_raise("save_state")
        self.saved_state = dict(state)

    def config(self):
        self._maybe_raise("config")
        return dict(self._config)

    def cache_put(self, reading):
        self._maybe_raise("cache_put")

    def probe_strategy(self, label):
        self.strategy_calls.append(label)
        return "ratelimit_headers"


class MonitorTestCase(unittest.TestCase):
    """Replaces every seam monitor.tick reaches out through."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-monitor-")
        self.log = os.path.join(self.tmp, "ctr.log")
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.logged = []
        self.notifications = []
        self.written = []
        self.rollovers = []
        self.probe_calls = []

        real_log_line = monitor.log_line

        def fake_log_line(message, log_path=None):
            self.logged.append(message)
            real_log_line(message, log_path or self.log)

        self._patch(monitor, "log_line", fake_log_line)
        self._patch(monitor, "notify", lambda t, m: self.notifications.append((t, m)))

        import ctr.keychain
        import ctr.shell
        import ctr.usage
        self._patch(ctr.keychain, "token_for", lambda label: SECRET)
        self._patch(ctr.shell, "write_active", lambda label, path=None: (
            self.written.append(label) or os.path.join(self.tmp, "active.sh")))

        store = self.store = FakeStore()

        def fake_probe_all(records, tokens, timeout_s=20, workers=4, prefers=None):
            self.probe_calls.append({"labels": [r.label for r in records],
                                     "prefers": dict(prefers or {})})
            return list(store.usages)

        self._patch(ctr.usage, "probe_all", fake_probe_all)

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def log_text(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log) as stream:
            return stream.read()


class TestTick(MonitorTestCase):
    def test_a_triggered_switch_writes_the_shell_parks_and_notifies(self):
        result = monitor.tick(self.store, NOW)
        self.assertTrue(result["switched"])
        self.assertEqual("switch", result["decision"].action)
        self.assertEqual(["b"], self.written)
        self.assertEqual("b", self.store._active)
        self.assertIn("a", self.store.saved_state["parked"], "the outgoing token parks")
        self.assertEqual(NOW, self.store.saved_state["last_switch_at"])
        self.assertEqual(1, len(self.notifications))

    def test_apply_false_changes_nothing(self):
        result = monitor.tick(self.store, NOW, apply=False)
        self.assertFalse(result["switched"])
        self.assertEqual([], self.written)
        self.assertEqual("a", self.store._active)
        self.assertEqual([], self.notifications)

    def test_a_healthy_active_token_holds(self):
        self.store.usages = [usage("a", 10.0), usage("b", 5.0)]
        result = monitor.tick(self.store, NOW)
        self.assertEqual("hold", result["decision"].action)
        self.assertFalse(result["switched"])
        self.assertEqual([], self.written)

    def test_tick_survives_every_store_method_raising(self):
        for name in ("tokens", "active", "state", "save_state", "config",
                     "cache_put", "set_active"):
            store = FakeStore()
            store.raises = {name}
            result = monitor.tick(store, NOW)
            self.assertIsInstance(result["decision"], Decision,
                                  "%s must not escape tick()" % name)

    def test_the_probe_starts_from_the_remembered_strategy(self):
        """Otherwise every long-lived oat token pays 2 HTTP calls per tick."""
        monitor.tick(self.store, NOW)
        self.assertEqual({"a": "ratelimit_headers", "b": "ratelimit_headers"},
                         self.probe_calls[0]["prefers"])

    def test_a_store_without_probe_strategy_still_works(self):
        class OlderStore(FakeStore):
            probe_strategy = None  # an older Store that never had the method

        result = monitor.tick(OlderStore(), NOW)
        self.assertEqual({}, self.probe_calls[0]["prefers"])
        self.assertTrue(result["switched"])

    def test_no_active_token_is_adopted_rather_than_logged_forever(self):
        """After `ctr remove` of the active token the monitor used to log the
        same no_active line every tick and never pick one."""
        store = FakeStore(active=None)
        result = monitor.tick(store, NOW)
        self.assertEqual("no_active", result["decision"].action)
        self.assertTrue(result["switched"])
        self.assertEqual("b", store._active)


class TestNoSecretReachesTheLog(MonitorTestCase):
    def test_an_exception_carrying_a_token_never_reaches_the_log(self):
        """monitor.py is one upstream mistake from a plaintext token on disk.

        ~/Library/Logs/ctr.log is persistent and gets pasted into bug reports,
        and `decision.reason` is returned to the caller. Interpolating a raw
        exception put both one bad sibling away from a leak, so the handlers
        now record the class name and nothing else.
        """
        import ctr.usage

        def exploding_probe_all(records, tokens, timeout_s=20, workers=4, prefers=None):
            raise ValueError("curl failed: Authorization: Bearer %s" % SECRET)

        self._patch(ctr.usage, "probe_all", exploding_probe_all)
        result = monitor.tick(self.store, NOW)

        self.assertIn("error", result)
        self.assertNotIn(SECRET, result["decision"].reason)
        self.assertNotIn(SECRET, result["error"])
        self.assertNotIn("sk-ant", result["decision"].reason)
        self.assertNotIn(SECRET, self.log_text())
        self.assertNotIn("sk-ant", self.log_text())
        self.assertIn("ValueError", result["decision"].reason, "still diagnosable")

    def test_a_failing_write_active_cannot_log_its_message(self):
        import ctr.shell

        def exploding_write(label, path=None):
            raise OSError("cannot write /tmp/x: token was %s" % SECRET)

        self._patch(ctr.shell, "write_active", exploding_write)
        result = monitor.tick(self.store, NOW)
        self.assertFalse(result["switched"])
        self.assertNotIn(SECRET, self.log_text())
        self.assertNotIn("sk-ant", self.log_text())

    def test_a_normal_switch_logs_labels_and_percentages_only(self):
        monitor.tick(self.store, NOW)
        text = self.log_text()
        self.assertIn("switched a -> b", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("sk-ant", text)


class TestLogLine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-log-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "sub", "ctr.log")

    def test_creates_the_file_0600_and_appends(self):
        monitor.log_line("first", self.path)
        monitor.log_line("second", self.path)
        self.assertEqual(0o600, os.stat(self.path).st_mode & 0o777)
        with open(self.path) as stream:
            lines = stream.read().strip().splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(lines[0].endswith("first"))

    def test_a_pathological_message_cannot_flood_the_file(self):
        monitor.log_line("x" * 5000, self.path)
        with open(self.path) as stream:
            self.assertLessEqual(len(stream.read()), monitor.MAX_LOG_CHARS + 60)

    def test_newlines_are_folded_into_one_line(self):
        monitor.log_line("a\nb\nc", self.path)
        with open(self.path) as stream:
            self.assertEqual(1, len(stream.read().strip().splitlines()))

    def test_an_unwritable_path_is_swallowed(self):
        monitor.log_line("x", os.path.join(self.tmp, "nope.log", "deeper.log"))

    def test_an_existing_log_file_is_tightened_to_0600(self):
        """RULING 8.6. `os.open(..., 0o600)` only applies its mode when it
        CREATES the file. launchd with RunAtLoad opens StandardOutPath itself,
        at the default umask, before any ctr code runs — so the very first
        append can land in a 0644 file that then stays world-readable for the
        life of the machine. Tighten it, without losing what is already there.
        """
        path = os.path.join(self.tmp, "preexisting.log")
        with open(path, "w") as stream:
            stream.write("created by launchd at the default umask\n")
        os.chmod(path, 0o644)

        monitor.log_line("appended by ctr", path)

        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        with open(path) as stream:
            text = stream.read()
        self.assertIn("created by launchd", text, "pre-existing content survives")
        self.assertIn("appended by ctr", text)

    def test_a_log_file_already_at_0600_is_left_alone(self):
        path = os.path.join(self.tmp, "fine.log")
        monitor.log_line("one", path)
        monitor.log_line("two", path)
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)


class TestSwitchThatDidNotPersist(MonitorTestCase):
    """RULING 8.7 — `_apply_switch` must not report True when set_active raises."""

    def test_a_failing_set_active_is_not_reported_as_a_switch(self):
        self.store.raises = {"set_active"}
        result = monitor.tick(self.store, NOW)

        self.assertEqual("switch", result["decision"].action, "the decision stands")
        self.assertFalse(result["switched"], "but nothing was recorded")
        self.assertEqual("a", self.store._active, "the registry still points at 'a'")
        self.assertEqual([], self.notifications, "no notification for a non-event")
        self.assertNotIn("switched a -> b", self.log_text())

    def test_a_failing_set_active_does_not_stamp_last_switch_at(self):
        """Stamping it suppressed the REAL switch for a whole
        min_switch_interval_s, so the failure hid the fix as well."""
        self.store.raises = {"set_active"}
        monitor.tick(self.store, NOW)
        self.assertEqual(0, self.store.saved_state["last_switch_at"])
        self.assertEqual({}, self.store.saved_state["parked"], "'a' must not park")

    def test_the_next_tick_retries_the_switch(self):
        self.store.raises = {"set_active"}
        monitor.tick(self.store, NOW)
        self.store._state = dict(self.store.saved_state)

        self.store.raises = set()
        result = monitor.tick(self.store, NOW + 1)
        self.assertTrue(result["switched"])
        self.assertEqual("b", self.store._active)
        self.assertEqual(NOW + 1, self.store.saved_state["last_switch_at"])


class TestProbeFailureDebounce(MonitorTestCase):
    """RULING 8.1, end to end through tick(): one failed probe changes nothing."""

    def setUp(self):
        MonitorTestCase.setUp(self)
        self.failing = [Usage.failed("a", "http 429 rate_limit_error", checked_at=NOW),
                        usage("b", 5.0)]

    def test_one_failed_probe_does_not_switch(self):
        self.store.usages = self.failing
        result = monitor.tick(self.store, NOW)

        self.assertEqual("hold", result["decision"].action)
        self.assertFalse(result["switched"])
        self.assertEqual([], self.written, "active.sh untouched")
        self.assertEqual("a", self.store._active)
        self.assertEqual([], self.notifications)
        self.assertEqual(1, selector.consecutive_failures(self.store.saved_state, "a"))

    def test_the_second_consecutive_failed_probe_switches(self):
        self.store.usages = self.failing
        monitor.tick(self.store, NOW)
        self.store._state = dict(self.store.saved_state)  # state.json persists

        result = monitor.tick(self.store, NOW + 300)
        self.assertTrue(result["switched"])
        self.assertEqual("b", self.store._active)
        self.assertEqual(2, selector.consecutive_failures(self.store.saved_state, "a"))

    def test_a_success_between_two_failures_resets_the_counter(self):
        self.store.usages = self.failing
        monitor.tick(self.store, NOW)
        self.store._state = dict(self.store.saved_state)

        self.store.usages = [usage("a", 10.0), usage("b", 5.0)]
        monitor.tick(self.store, NOW + 300)
        self.store._state = dict(self.store.saved_state)
        self.assertEqual(0, selector.consecutive_failures(self.store.saved_state, "a"))

        self.store.usages = self.failing
        result = monitor.tick(self.store, NOW + 600)
        self.assertFalse(result["switched"], "back to 1 of 2 — hold")
        self.assertEqual("a", self.store._active)


class TestRun(MonitorTestCase):
    def test_once_returns_zero_and_ticks_exactly_once(self):
        ticks = []
        self._patch(monitor, "tick", lambda *a, **k: ticks.append(a) or {"decision": None})
        self.assertEqual(0, monitor.run(self.store, once=True))
        self.assertEqual(1, len(ticks))

    def test_once_returns_one_when_the_tick_failed(self):
        self._patch(monitor, "tick", lambda *a, **k: {"error": "boom"})
        self.assertEqual(1, monitor.run(self.store, once=True))

    def test_the_interval_has_a_floor(self):
        slept = []
        self._patch(monitor.time, "sleep", lambda s: slept.append(s) or (_ for _ in ()).throw(KeyboardInterrupt()))
        self._patch(monitor, "tick", lambda *a, **k: {"decision": None})
        monitor.run(self.store, once=False, interval_s=1)
        self.assertEqual([monitor.MIN_INTERVAL_S], slept)


class TestNotify(unittest.TestCase):
    def test_applescript_quoting_survives_an_injection_attempt(self):
        hostile = 'x" & do shell script "rm -rf ~'
        script = notify.applescript(hostile, "body")
        self.assertNotIn('& do shell script', script.split("with title")[0])
        self.assertIn('\\"', script)

    def test_control_characters_are_stripped(self):
        script = notify.applescript("a\nb\x00c", "d\te")
        self.assertNotIn("\n", script)
        self.assertNotIn("\x00", script)

    def test_long_text_is_truncated(self):
        script = notify.applescript("t" * 500, "m" * 500)
        self.assertLessEqual(len(script), notify.MAX_TITLE + notify.MAX_MESSAGE + 80)

    # NOTE: these two must stub BOTH `_run` and `_log`. Stubbing only `_run`
    # keeps osascript out of it but leaves `notify()`'s failure path calling
    # `monitor.log_line(message)` with no path — which defaults to the REAL
    # ~/Library/Logs/ctr.log. That leaked 2 lines into the real log on
    # every single suite run (297 of them had accumulated before it was caught
    # by running the tool, not by reading the code). Stub both, and assert the
    # message while we are here, so the failure path is actually tested rather
    # than merely survived.

    def _capture_notify_log(self):
        """Redirect notify's failure log into a list. Returns that list."""
        captured = []
        original = notify._log
        notify._log = captured.append
        self.addCleanup(lambda: setattr(notify, "_log", original))
        return captured

    def test_notify_never_raises_when_osascript_explodes(self):
        logged = self._capture_notify_log()
        original = notify._run
        notify._run = lambda cmd, timeout_s=10: (_ for _ in ()).throw(OSError("no osascript"))
        try:
            notify.notify("title", "message")  # must not raise
        finally:
            notify._run = original
        self.assertEqual(["notification failed: OSError"], logged)

    def test_notify_never_raises_on_a_nonzero_exit(self):
        logged = self._capture_notify_log()
        original = notify._run
        notify._run = lambda cmd, timeout_s=10: 1
        try:
            notify.notify("title", "message")
        finally:
            notify._run = original
        self.assertEqual(["notification failed (osascript exit 1)"], logged)

    def test_the_suite_never_writes_to_the_real_ctr_log(self):
        """Guard the isolation leak above so it cannot come back."""
        import ctr.model as model

        real_log = os.path.expanduser(model.LOG_FILE)
        before = os.path.getsize(real_log) if os.path.exists(real_log) else -1

        logged = self._capture_notify_log()
        original = notify._run
        notify._run = lambda cmd, timeout_s=10: 1
        try:
            notify.notify("t", "m")
        finally:
            notify._run = original

        after = os.path.getsize(real_log) if os.path.exists(real_log) else -1
        self.assertEqual(before, after, "notify() wrote to the real ctr log")
        self.assertEqual(1, len(logged))


class TestLaunchd(unittest.TestCase):
    def test_plist_is_valid_and_carries_the_agreed_arguments(self):
        xml = launchd.plist_xml("/Users/x/.local/bin/ctr", interval_s=300)
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual(["/Users/x/.local/bin/ctr", "monitor", "--once"],
                         parsed["ProgramArguments"])
        self.assertEqual(300, parsed["StartInterval"])
        self.assertIsInstance(parsed["StartInterval"], int)
        self.assertTrue(parsed["RunAtLoad"])
        self.assertEqual(parsed["StandardOutPath"], parsed["StandardErrorPath"])

    def test_a_path_with_xml_metacharacters_is_escaped(self):
        xml = launchd.plist_xml("/Users/a&b/<ctr>", interval_s=60)
        parsed = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual("/Users/a&b/<ctr>", parsed["ProgramArguments"][0])

    def test_the_agent_path_is_spelled_out_for_launchd(self):
        parsed = plistlib.loads(launchd.plist_xml("/x/ctr").encode("utf-8"))
        path = parsed["EnvironmentVariables"]["PATH"]
        for needed in ("/usr/bin", "/opt/homebrew/bin"):
            self.assertIn(needed, path)


if __name__ == "__main__":
    unittest.main()
