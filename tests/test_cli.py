"""Tests for cli.py (integration round).

cli.py is 900 lines and had no test of its own — the lane that wrote it owned
tests/test_shell.py only. During integration an edit turned `_validate_token`
into `return _validate_token(data)`, infinite recursion on every token ctr is
ever given, and the whole 230-test suite stayed green. That is the gap this
file closes.

Every test drives `cli.main()` the way a user does. Nothing touches the real
keychain, the network, herdr, launchctl or the user's ~/.config/ctr: the
hidden --config-dir/--active-file/--rc-file flags point at a tempdir and each
subprocess seam is replaced.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import cli, keychain, launchd, rollover, shell  # noqa: E402
from ctr import usage as usage_module  # noqa: E402
from ctr.model import Decision, Usage  # noqa: E402

SENTINEL = "sk-ant-oat01-ZZSENTINELZZ-0123456789abcdefghijklmnop"
NOW = 1789480000


def usage(label, five_h=10.0, seven_d=5.0, ok=True, status="allowed", error=""):
    return Usage(
        label=label, five_h=five_h, seven_d=seven_d, five_h_reset=NOW + 600,
        seven_d_reset=NOW + 6000, status=status, probe="ratelimit_headers",
        ok=ok, error=error, checked_at=NOW,
    )


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_dir = os.path.join(self.tmp, "config")
        os.makedirs(self.config_dir, 0o700)
        self.active = os.path.join(self.config_dir, "active.sh")
        self.rc = os.path.join(self.tmp, "zshrc")

        self.keychain_items = {}
        self.probed = []

        self._patch(keychain, "set",
                    lambda service, account, secret: self.keychain_items.__setitem__(service, secret))
        self._patch(keychain, "delete",
                    lambda service: self.keychain_items.pop(service, None) is not None)
        self._patch(keychain, "token_for",
                    lambda label: self.keychain_items.get("ctr:" + label))
        self._patch(keychain, "claude_login_token", lambda: None)
        self._patch(keychain, "claude_login_info", lambda: {})

        def fake_probe(label, token, timeout_s=20, prefer=None):
            self.probed.append(label)
            return usage(label)

        def fake_probe_all(records, tokens, timeout_s=20, workers=4, prefers=None):
            labels = [getattr(r, "label", r) for r in records]
            self.probed.extend(labels)
            return [usage(label) for label in labels]

        self._patch(usage_module, "probe", fake_probe)
        self._patch(usage_module, "probe_all", fake_probe_all)
        self._patch(launchd, "status", lambda: {"installed": False, "loaded": False, "plist": ""})
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            self._unset_env(name)

    def _patch(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.addCleanup(setattr, module, name, original)

    def _unset_env(self, name):
        original = os.environ.get(name)
        os.environ.pop(name, None)
        self.addCleanup(lambda: os.environ.__setitem__(name, original)
                        if original is not None else os.environ.pop(name, None))

    def _set_env(self, name, value):
        self._unset_env(name)
        os.environ[name] = value

    def run_cli(self, *argv):
        """(exit code, stdout, stderr) for one `ctr ...` invocation."""
        out, err = io.StringIO(), io.StringIO()
        full = ["--config-dir", self.config_dir, "--active-file", self.active,
                "--rc-file", self.rc] + list(argv)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(full)
        return code, out.getvalue(), err.getvalue()

    def add_token(self, label="work", token=SENTINEL):
        stdin = sys.stdin
        sys.stdin = io.StringIO(token + "\n")
        try:
            return self.run_cli("add", label, "--token", "-")
        finally:
            sys.stdin = stdin

    def active_body(self):
        if not os.path.exists(self.active):
            return ""
        with open(self.active) as stream:
            return stream.read()


class TestTokenValidation(CliTestCase):
    def test_a_valid_token_is_returned_stripped(self):
        self.assertEqual(SENTINEL, cli._validate_token("  %s \n" % SENTINEL))

    def test_bad_input_is_rejected_rather_than_stored(self):
        for bad in ("", "   ", "a b", "hunter2"):
            with self.assertRaises(cli.CtrUsage):
                cli._validate_token(bad)

    def test_the_rejection_message_never_shows_the_whole_token(self):
        with self.assertRaises(cli.CtrUsage) as caught:
            cli._validate_token("not-a-" + SENTINEL)
        self.assertNotIn(SENTINEL, str(caught.exception))


class TestExceptionsNeverCarryATokenOut(CliTestCase):
    """The CLI's last-resort handlers are the only uninstrumented sinks left.

    Nothing reaches them with a secret today, so this makes the rule structural
    rather than a property of every sibling module continuing to scrub.
    """

    def test_an_unexpected_exception_message_is_redacted(self):
        def exploding(args):
            raise RuntimeError("curl failed: Authorization: Bearer %s" % SENTINEL)

        self._patch(cli, "cmd_list", exploding)
        code, _out, err = self.run_cli("list")
        self.assertEqual(1, code)
        self.assertNotIn(SENTINEL, err)
        self.assertIn("RuntimeError", err)

    def test_a_ctr_error_message_is_redacted_too(self):
        def exploding(args):
            raise cli.CtrError("bad token %s" % SENTINEL)

        self._patch(cli, "cmd_list", exploding)
        _code, _out, err = self.run_cli("list")
        self.assertNotIn(SENTINEL, err)

    def test_redaction_leaves_ordinary_messages_alone(self):
        self.assertEqual("plain failure", cli._safe_message(RuntimeError("plain failure")))


class TestAdd(CliTestCase):
    def test_stdin_registers_the_token_and_activates_the_first_one(self):
        code, out, _err = self.add_token()
        self.assertEqual(0, code, out)
        self.assertIn("ctr:work", self.keychain_items)
        self.assertEqual(SENTINEL, self.keychain_items["ctr:work"])
        self.assertIn("first token", out)
        self.assertIn("CTR_ACTIVE_LABEL='work'", self.active_body())

    def test_the_token_never_appears_in_any_output(self):
        _code, out, err = self.add_token()
        self.assertNotIn(SENTINEL, out + err)
        self.assertNotIn(SENTINEL[:20], out + err)
        self.assertNotIn(SENTINEL, self.active_body())

    def test_a_positional_token_works_and_warns_about_shell_history(self):
        """RULING 4: `ctr add <alias> <token>` must work exactly as written,
        and must warn afterwards because argv lands in ~/.zsh_history."""
        code, out, err = self.run_cli("add", "work", SENTINEL)
        self.assertEqual(0, code, out + err)
        self.assertEqual(SENTINEL, self.keychain_items["ctr:work"])
        self.assertIn("zsh_history", err)
        self.assertNotIn(SENTINEL, out + err)

    def test_stdin_form_does_not_warn_about_history(self):
        _code, _out, err = self.add_token()
        self.assertNotIn("zsh_history", err)

    def test_an_invalid_label_is_refused(self):
        code, _out, err = self.run_cli("add", "Bad Label", SENTINEL)
        self.assertEqual(2, code)
        self.assertIn("invalid label", err)
        self.assertEqual({}, self.keychain_items)

    def test_a_duplicate_label_is_refused(self):
        self.add_token()
        code, _out, err = self.run_cli("add", "work", SENTINEL)
        self.assertEqual(2, code)
        self.assertIn("already registered", err)


class TestListAndStatus(CliTestCase):
    def test_list_on_an_empty_registry_explains_what_to_do(self):
        code, out, _err = self.run_cli("list")
        self.assertEqual(0, code)
        self.assertIn("ctr add", out)

    def test_list_marks_the_active_token(self):
        self.add_token("work")
        self.add_token("home")
        code, out, _err = self.run_cli("list")
        self.assertEqual(0, code)
        self.assertIn("work", out)
        self.assertIn("home", out)
        self.assertIn("*", out)

    def test_status_json_is_machine_readable_and_carries_no_secret(self):
        self.add_token()
        code, out, _err = self.run_cli("status", "--json")
        self.assertEqual(0, code)
        payload = json.loads(out)
        self.assertEqual("work", payload["active"])
        self.assertNotIn(SENTINEL, out)
        self.assertEqual(1, len(payload["tokens"]))


class TestRemove(CliTestCase):
    def test_removing_the_active_token_clears_active_sh(self):
        """A dangling active.sh made `ctr doctor` go red straight after a
        clean `ctr remove`, and the guard that should have caught it was dead
        because store.remove() had already cleared the pointer."""
        self.add_token("work")
        self.assertIn("CTR_ACTIVE_LABEL='work'", self.active_body())

        code, out, _err = self.run_cli("remove", "work")

        self.assertEqual(0, code)
        self.assertIn("It was the active token", out)
        self.assertNotIn("CTR_ACTIVE_LABEL='work'", self.active_body())
        self.assertNotIn("ctr:work", self.keychain_items)

    def test_doctor_is_not_left_red_by_a_clean_remove(self):
        self.add_token("work")
        self.run_cli("install-shell")
        self.run_cli("remove", "work")
        _code, out, _err = self.run_cli("doctor")
        self.assertNotIn("but tokens.json says", out)

    def test_removing_a_non_active_token_leaves_active_alone(self):
        self.add_token("work")
        self.add_token("home")
        code, out, _err = self.run_cli("remove", "home")
        self.assertEqual(0, code)
        self.assertNotIn("It was the active token", out)
        self.assertIn("CTR_ACTIVE_LABEL='work'", self.active_body())

    def test_removing_an_unknown_label_is_a_usage_error(self):
        code, _out, err = self.run_cli("remove", "nope")
        self.assertEqual(2, code)
        self.assertIn("no token labelled", err)


class TestUseAndNext(CliTestCase):
    def test_use_rewrites_active_sh(self):
        self.add_token("work")
        self.add_token("home")
        code, out, _err = self.run_cli("use", "home")
        self.assertEqual(0, code)
        self.assertIn("CTR_ACTIVE_LABEL='home'", self.active_body())
        self.assertIn("rollover", out, "it must mention already-running sessions")

    def test_active_prints_the_label(self):
        self.add_token("work")
        _code, out, _err = self.run_cli("active")
        self.assertEqual("work", out.strip().split()[0])

    def test_active_with_nothing_registered(self):
        code, out, _err = self.run_cli("active")
        self.assertEqual(0, code)
        self.assertEqual("(none)", out.strip())


class TestDoctorEnvironment(CliTestCase):
    """RULING 2: two variables silently defeat ctr, and doctor must say so."""

    def test_a_clean_environment_passes_both_env_checks(self):
        _code, out, _err = self.run_cli("doctor")
        self.assertIn("ANTHROPIC_API_KEY", out)
        self.assertIn("ANTHROPIC_AUTH_TOKEN", out)

    def test_anthropic_api_key_is_reported_as_a_failure_with_its_symptom(self):
        """MEASURED: with this set `claude -p` HANGS and never returns, while
        every other check still passes — the single most likely way a user
        concludes ctr is broken."""
        self._set_env("ANTHROPIC_API_KEY", "sk-ant-api-bogus")
        code, out, _err = self.run_cli("doctor")
        self.assertEqual(1, code)
        self.assertIn("ANTHROPIC_API_KEY", out)
        self.assertIn("HANGS", out)
        self.assertNotIn("All good.", out)

    def test_anthropic_auth_token_is_reported_as_a_failure_with_its_symptom(self):
        self._set_env("ANTHROPIC_AUTH_TOKEN", "bogus")
        code, out, _err = self.run_cli("doctor")
        self.assertEqual(1, code)
        self.assertIn("Not logged in", out)

    def test_doctor_never_says_all_good_in_the_environment_measured_as_fatal(self):
        self.add_token("work")
        self.run_cli("install-shell")
        self._set_env("ANTHROPIC_API_KEY", "sk-ant-api-bogus")
        self._set_env("ANTHROPIC_AUTH_TOKEN", "bogus")
        code, out, _err = self.run_cli("doctor")
        self.assertNotEqual(0, code)
        self.assertNotIn("All good.", out)


class TestDoctorSuspectReading(CliTestCase):
    """A clamped reading is usable but not trustworthy, so doctor must not
    call it a clean `ok` (RULING 8.2: "ctr status shows something was wrong").
    Round 2 left the whole warning reachable only through `status --json`."""

    CLAMPED = "utilisation was outside 0..100 and was clamped — reading is suspect"

    def use_clamped_probe(self):
        def probe_all(records, tokens, timeout_s=20, workers=4, prefers=None):
            return [usage(getattr(r, "label", r), five_h=0.0, error=self.CLAMPED)
                    for r in records]

        self._patch(usage_module, "probe_all", probe_all)

    def token_row(self, out):
        return [line for line in out.split("\n") if "token work" in line][0]

    def test_doctor_warns_instead_of_reporting_a_clean_ok(self):
        self.add_token("work")
        self.use_clamped_probe()
        _code, out, _err = self.run_cli("doctor")
        row = self.token_row(out)
        self.assertTrue(row.startswith("WARN"), row)
        self.assertIn("clamped", row)
        self.assertNotIn("FAIL", row)  # a suspect reading is not a broken token

    def test_the_list_table_shows_the_warning_too(self):
        self.add_token("work")
        self.use_clamped_probe()
        _code, out, _err = self.run_cli("list")
        self.assertIn("clamped", out)

    def test_a_clean_probe_still_reports_ok_with_no_warning(self):
        self.add_token("work")
        _code, out, _err = self.run_cli("doctor")
        row = self.token_row(out)
        self.assertTrue(row.startswith("ok"), row)
        self.assertNotIn("clamped", row)


class TestRolloverCommand(CliTestCase):
    def fake_run(self, result):
        self._patch(rollover, "run", lambda dry_run=True, only=None: result)

    def test_dry_run_is_the_default_and_says_so(self):
        seen = {}
        self._patch(rollover, "run", lambda dry_run=True, only=None: seen.update(
            dry_run=dry_run) or {"dry_run": dry_run, "error": "", "planned": [],
                                 "acted": [], "skipped": []})
        code, out, _err = self.run_cli("rollover")
        self.assertEqual(0, code)
        self.assertTrue(seen["dry_run"])
        self.assertIn("DRY RUN", out)

    def test_apply_turns_the_dry_run_off(self):
        seen = {}
        self._patch(rollover, "run", lambda dry_run=True, only=None: seen.update(
            dry_run=dry_run) or {"dry_run": dry_run, "error": "", "planned": [],
                                 "acted": [], "skipped": []})
        self.run_cli("rollover", "--apply")
        self.assertFalse(seen["dry_run"])

    def test_a_failed_pane_exits_non_zero(self):
        """Otherwise `monitor --auto-rollover` and any wrapper script cannot
        tell that a claude session was left down."""
        self.fake_run({"dry_run": False, "error": "", "planned": [{"pane": "w1:pA"}],
                       "acted": [{"pane": "w1:pA", "ok": False,
                                  "detail": "herdr agent start failed (rc=1): boom"}],
                       "skipped": []})
        code, out, _err = self.run_cli("rollover", "--apply")
        self.assertEqual(1, code)
        self.assertIn("FAIL", out)

    def test_an_all_ok_apply_exits_zero(self):
        self.fake_run({"dry_run": False, "error": "", "planned": [{"pane": "w1:pA"}],
                       "acted": [{"pane": "w1:pA", "ok": True, "detail": "started"}],
                       "skipped": []})
        code, _out, _err = self.run_cli("rollover", "--apply")
        self.assertEqual(0, code)

    def test_a_herdr_level_error_exits_non_zero(self):
        self.fake_run({"dry_run": True, "error": "herdr agent list failed",
                       "planned": [], "acted": [], "skipped": []})
        code, _out, err = self.run_cli("rollover")
        self.assertEqual(1, code)
        self.assertIn("herdr", err)

    def test_a_none_heavy_result_does_not_traceback(self):
        self.fake_run({"dry_run": True, "error": None, "planned": None,
                       "acted": None, "skipped": None})
        code, _out, _err = self.run_cli("rollover")
        self.assertEqual(0, code)

    def test_a_filter_that_matched_nothing_is_not_reported_as_nothing_to_do(self):
        """`--only w9:pZZ` used to render as an empty plan, i.e. exactly like
        "the fleet is healthy" — the opposite of what a typo'd pane id means."""
        self.fake_run({"dry_run": True, "error": "no pane matched --only w9:pZZ",
                       "planned": [], "acted": [], "skipped": []})
        code, out, err = self.run_cli("rollover", "--only", "w9:pZZ")
        self.assertEqual(1, code)
        self.assertIn("no pane matched", err)
        self.assertIn("w9:pZZ", err)
        self.assertNotIn("No parked claude session needs a rollover", out)

    def test_only_is_split_on_commas_and_stripped(self):
        seen = {}
        self._patch(rollover, "run", lambda dry_run=True, only=None: seen.update(
            only=only) or {"dry_run": dry_run, "error": "", "planned": [],
                           "acted": [], "skipped": []})
        self.run_cli("rollover", "--only", "w6:pC, w6:pR ,")
        self.assertEqual(["w6:pC", "w6:pR"], seen["only"])


class TestDoctorProbeLeftovers(CliTestCase):
    """A SIGKILL landing mid-probe leaves a 0600 ctr-probe-* work directory
    behind, holding the curl config file — which has a bearer token in it.

    The contract sanctions that hole rather than complicating the probe path
    (RULING 8, "accept as-is"), so `ctr doctor` has to sweep for the wreckage
    and print the command that clears it.
    """

    def leftovers(self, *names):
        for name in names:
            os.makedirs(os.path.join(self.tmp, name), 0o700)
        return cli._probe_leftovers([self.tmp])

    def test_finds_a_stale_probe_directory(self):
        self.assertEqual([os.path.join(self.tmp, "ctr-probe-ab12")],
                         self.leftovers("ctr-probe-ab12"))

    def test_finds_every_one_of_them_in_a_stable_order(self):
        found = self.leftovers("ctr-probe-b", "ctr-probe-a", "ctr-probe-c")
        self.assertEqual([os.path.join(self.tmp, n)
                          for n in ("ctr-probe-a", "ctr-probe-b", "ctr-probe-c")], found)

    def test_ignores_temp_entries_that_are_not_ours(self):
        self.assertEqual([], self.leftovers("tmpXYZ", "ctr-other", "probe-ctr"))

    def test_a_clean_tempdir_reports_nothing(self):
        self.assertEqual([], cli._probe_leftovers([self.tmp]))

    def test_an_unreadable_base_is_skipped_rather_than_raising(self):
        self.assertEqual([], cli._probe_leftovers([os.path.join(self.tmp, "nope")]))

    def test_it_sweeps_both_tmpdir_and_slash_tmp(self):
        """A launchd job lands in /tmp while an interactive run honours
        $TMPDIR (per-user, under /var/folders on macOS)."""
        swept = []
        self._patch(os, "listdir", lambda base: swept.append(base) or [])
        cli._probe_leftovers()
        self.assertIn(os.path.realpath("/tmp"), swept)
        self.assertIn(os.path.realpath(tempfile.gettempdir()), swept)

    def test_doctor_reports_them_with_the_command_to_clear_them(self):
        self._patch(cli, "_probe_leftovers",
                    lambda bases=None: ["/tmp/ctr-probe-a", "/tmp/ctr-probe-b"])
        _code, out, _err = self.run_cli("doctor")
        self.assertIn("probe temp files", out)
        self.assertIn("2 stale", out)
        self.assertIn("rm -rf /tmp/ctr-probe-a /tmp/ctr-probe-b", out)

    def test_doctor_says_so_when_the_sweep_is_clean(self):
        self._patch(cli, "_probe_leftovers", lambda bases=None: [])
        _code, out, _err = self.run_cli("doctor")
        self.assertIn("no ctr-probe-* left behind", out)

    def test_a_leftover_is_a_warning_not_a_failure(self):
        """It is wreckage to clear, not a broken installation — it must not
        make `ctr doctor` exit non-zero on its own."""
        self._patch(cli, "_probe_leftovers", lambda bases=None: ["/tmp/ctr-probe-a"])
        self.assertEqual("warn", cli._doctor_probe_files()[0][0])
        self._patch(cli, "_probe_leftovers", lambda bases=None: [])
        self.assertEqual("ok", cli._doctor_probe_files()[0][0])


class TestParserSurface(CliTestCase):
    def test_every_documented_subcommand_is_registered(self):
        parser = cli.build_parser()
        actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
        registered = set(actions[0].choices)
        for name in ("add", "list", "remove", "status", "use", "next", "active",
                     "monitor", "install-monitor", "uninstall-monitor",
                     "rollover", "install-shell", "doctor"):
            self.assertIn(name, registered)

    def test_no_arguments_prints_help_and_exits_two(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main([])
        self.assertEqual(2, caught.exception.code)

    def test_an_unknown_subcommand_exits_two(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["bogus"])
        self.assertEqual(2, caught.exception.code)


if __name__ == "__main__":
    unittest.main()


class MonitorOnceOutputTests(unittest.TestCase):
    """`ctr monitor --once` speaks to a human, and stays quiet for launchd.

    Both halves were found by running the installed command, not by reading it:
    first it printed nothing at all and read as broken; then, once it printed,
    the launchd plist (which aims stdout AT ~/Library/Logs/ctr.log) turned that
    into ~288 "no change" lines a day — the exact noise the log is quiet to
    avoid. The RunAtLoad tick appended one before this was fixed.
    """

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    class _Pipe(io.StringIO):
        def isatty(self):
            return False

    def _run(self, stream, result):
        from ctr import cli as cli_mod

        class Args:
            once = True
            interval = 300
            auto_rollover = False
            config_dir = self.tmp
            active_file = None

        import ctr.monitor as monitor_mod

        saved_tick, monitor_mod.tick = monitor_mod.tick, lambda *a, **k: result
        try:
            with contextlib.redirect_stdout(stream):
                rc = cli_mod.cmd_monitor(Args())
        finally:
            monitor_mod.tick = saved_tick
        return stream.getvalue(), rc

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    @staticmethod
    def _quiet():
        return {
            "decision": Decision("hold", None, "below thresholds", False),
            "usages": [],
            "switched": False,
        }

    def test_a_tty_sees_a_quiet_tick(self):
        text, rc = self._run(self._Tty(), self._quiet())
        self.assertIn("no change", text)
        self.assertEqual(0, rc)

    def test_a_pipe_does_not_see_a_quiet_tick(self):
        text, rc = self._run(self._Pipe(), self._quiet())
        self.assertEqual("", text, "launchd would write this into ctr.log every 5 min")
        self.assertEqual(0, rc)

    def test_a_pipe_DOES_see_a_switch(self):
        result = {
            "decision": Decision("switch", "spare", "5h 91%", True),
            "usages": [],
            "switched": True,
        }
        text, rc = self._run(self._Pipe(), result)
        self.assertIn("switched to 'spare'", text)
        self.assertEqual(0, rc)

    def test_a_pipe_DOES_see_an_error_and_exits_1(self):
        text, rc = self._run(self._Pipe(), {"error": "monitor tick failed: OSError"})
        self.assertIn("OSError", text)
        self.assertEqual(1, rc)
