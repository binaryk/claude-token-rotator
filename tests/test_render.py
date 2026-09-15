"""Focused tests for render.py — the table/JSON/notice formatting layer.

RULING 8.5 split render.py out of cli.py (1010 lines against a house limit of
800) and required it to have its own focused tests. Round 2 put them in
tests/test_cli.py because no lane owned this file; this is that cut-and-paste,
plus the cases round 2 left uncovered (an `ok` reading that still carries a
warning, and the rollover "nothing to do" line).

Nothing here touches the keychain, the network, herdr or the user's config:
every function under test is pure formatting over values built in the test.
"""

import contextlib
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import render  # noqa: E402
from ctr.model import Decision, TokenRecord, Usage, merged_config  # noqa: E402

NOW = 1789480000


def usage(label, five_h=10.0, seven_d=5.0, ok=True, status="allowed", error=""):
    return Usage(
        label=label, five_h=five_h, seven_d=seven_d, five_h_reset=NOW + 600,
        seven_d_reset=NOW + 6000, status=status, probe="ratelimit_headers",
        ok=ok, error=error, checked_at=NOW,
    )


def captured(fn, *args, **kwargs):
    """(stdout, stderr) for one render call. render.out/warn look the streams
    up at call time, so redirecting them here really does capture."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        fn(*args, **kwargs)
    return out.getvalue(), err.getvalue()


class FakeStore(object):
    """Just enough store for the two render functions that read config/state."""

    def __init__(self, config=None, state=None):
        self._config = merged_config(config)
        self._state = state or {}

    def config(self):
        return dict(self._config)

    def state(self):
        return dict(self._state)


def record(label="work", account="a@b.c", subscription="max", kind="oat", note=""):
    return TokenRecord(label=label, account=account, subscription=subscription,
                       added_at="2026-09-15T10:00:00", kind=kind, note=note)


class TestRenderTable(unittest.TestCase):
    def test_columns_are_padded_to_the_widest_cell(self):
        text = render.table(["A", "BB"], [["xxxx", "y"], ["z", "wwww"]])
        self.assertEqual(["A     BB", "xxxx  y", "z     wwww"], text.split("\n"))

    def test_trailing_padding_is_stripped_from_every_line(self):
        for line in render.table(["A", "B"], [["xxxx", "y"]]).split("\n"):
            self.assertEqual(line, line.rstrip())

    def test_headers_alone_still_render(self):
        self.assertEqual("A  B", render.table(["A", "B"], []))


class TestRenderTokenRows(unittest.TestCase):
    def rows(self, records, usages, active=None):
        return render.token_rows(records, usages, active, NOW)

    def test_the_active_token_is_the_only_one_marked(self):
        rows = self.rows([record("work"), record("home")],
                         [usage("work"), usage("home")], active="home")
        self.assertEqual([" ", "*"], [row[0] for row in rows])

    def test_a_failed_probe_shows_its_error_in_the_note_column(self):
        rows = self.rows([record("work")],
                         [usage("work", ok=False, error="401 authentication_error")])
        self.assertEqual("401 authentication_error", rows[0][-1])

    def test_a_failed_probe_with_no_error_still_says_something(self):
        rows = self.rows([record("work")], [usage("work", ok=False, error="")])
        self.assertEqual("probe failed", rows[0][-1])

    def test_a_rejected_token_is_called_out(self):
        rows = self.rows([record("work")], [usage("work", status="rejected")])
        self.assertEqual("REJECTED (limit reached)", rows[0][-1])

    def test_a_record_with_no_reading_at_all(self):
        rows = self.rows([record("work")], [])
        self.assertEqual("no reading", rows[0][-1])
        self.assertEqual(["  ?", "-", "  ?", "-"], rows[0][6:10])

    def test_a_healthy_token_keeps_its_own_note(self):
        rows = self.rows([record("work", note="second account")], [usage("work")])
        self.assertEqual("second account", rows[0][-1])

    def test_empty_fields_render_as_a_dash(self):
        rows = self.rows([record("work", account="", subscription="")], [usage("work")])
        self.assertEqual(["-", "-"], rows[0][2:4])

    def test_the_added_date_is_trimmed_to_the_day(self):
        self.assertEqual("2026-09-15", self.rows([record()], [usage("work")])[0][5])

    def test_print_tokens_tells_you_when_nothing_is_active(self):
        out, _err = captured(render.print_tokens, None, [record("work")],
                             [usage("work")], None, NOW)
        self.assertIn("No active token", out)

    def test_print_tokens_stays_quiet_when_something_is_active(self):
        out, _err = captured(render.print_tokens, None, [record("work")],
                             [usage("work")], "work", NOW)
        self.assertNotIn("No active token", out)


class TestRenderNotices(unittest.TestCase):
    def test_the_stored_notice_shows_only_the_redacted_token(self):
        out, err = captured(render.stored_notice, "sk-ant…", "work", False, "a@b.c")
        self.assertIn("ctr:work", out)
        self.assertNotIn("zsh_history", err)

    def test_an_argv_token_is_warned_about_on_stderr(self):
        _out, err = captured(render.stored_notice, "sk-ant…", "work", True, "a@b.c")
        self.assertIn("zsh_history", err)
        self.assertIn("ctr add work", err)

    def test_a_missing_account_is_pointed_out(self):
        out, _err = captured(render.stored_notice, "sk-ant…", "work", False, "")
        self.assertIn("--account", out)

    def test_a_successful_probe_reports_both_windows(self):
        out, _err = captured(render.probe_result, "work", usage("work"))
        self.assertIn("Probe ok", out)
        self.assertIn("ratelimit_headers", out)

    def test_a_failed_probe_is_a_warning_that_does_not_lose_the_token(self):
        _out, err = captured(render.probe_result, "work",
                             usage("work", ok=False, error="403 scope"))
        self.assertIn("403 scope", err)
        self.assertIn("registered anyway", err)

    def test_the_switch_notice_explains_running_sessions(self):
        out, _err = captured(render.switch_notice, "home", "/tmp/active.sh")
        self.assertIn("home", out)
        self.assertIn("/tmp/active.sh", out)
        self.assertIn("ctr rollover", out)

    def test_the_setup_help_never_offers_to_run_setup_token_for_you(self):
        out, _err = captured(render.print_setup_instructions, "work")
        self.assertIn("claude setup-token", out)
        self.assertIn("ctr add work --token -", out)

    def test_token_check_detail_ok_and_failed(self):
        self.assertIn("ratelimit_headers", render.token_check_detail(usage("work")))
        self.assertEqual("boom", render.token_check_detail(
            usage("work", ok=False, error="boom")))
        self.assertEqual("probe failed", render.token_check_detail(
            usage("work", ok=False, error="")))

    def test_probe_leftover_detail_when_there_is_nothing(self):
        self.assertEqual("no ctr-probe-* left behind",
                         render.probe_leftover_detail("ctr-probe-", []))

    def test_probe_leftover_detail_names_the_files_and_the_remedy(self):
        detail = render.probe_leftover_detail("ctr-probe-", ["/tmp/ctr-probe-a"])
        self.assertIn("1 stale", detail)
        self.assertIn("rm -rf /tmp/ctr-probe-a", detail)

    def test_probe_leftover_detail_truncates_a_long_list(self):
        detail = render.probe_leftover_detail(
            "ctr-probe-", ["/tmp/ctr-probe-%d" % i for i in range(5)])
        self.assertIn("5 stale", detail)
        self.assertIn("...", detail)
        self.assertNotIn("ctr-probe-4", detail)


class TestRenderStatus(unittest.TestCase):
    def test_status_json_shape(self):
        payload = render.status_json(FakeStore(), [record("work")],
                                     [usage("work")], "work", NOW)
        self.assertEqual(1, payload["version"])
        self.assertEqual("work", payload["active"])
        self.assertTrue(payload["tokens"][0]["active"])
        self.assertEqual(85.0, payload["config"]["switch_at_5h"])

    def test_status_json_fills_in_a_token_that_was_never_probed(self):
        payload = render.status_json(FakeStore(), [record("work")], [], None, NOW)
        self.assertFalse(payload["tokens"][0]["usage"]["ok"])
        self.assertEqual("not probed", payload["tokens"][0]["usage"]["error"])

    def test_headroom_line_with_no_active_token(self):
        self.assertIn("No active token",
                      render.headroom_line(FakeStore(), [], [], None, NOW))

    def test_headroom_line_when_the_active_reading_failed(self):
        line = render.headroom_line(FakeStore(), [record("work")],
                                    [usage("work", ok=False)], "work", NOW)
        self.assertIn("no usable reading", line)

    def test_headroom_line_below_the_thresholds(self):
        line = render.headroom_line(FakeStore(), [record("work")],
                                    [usage("work", five_h=10.0)], "work", NOW)
        self.assertIn("below the switch thresholds", line)

    def test_headroom_line_over_the_threshold_with_somewhere_to_go(self):
        line = render.headroom_line(
            FakeStore(), [record("work"), record("home")],
            [usage("work", five_h=90.0), usage("home", five_h=10.0)], "work", NOW)
        self.assertIn("would switch to 'home'", line)

    def test_headroom_line_over_the_threshold_with_nowhere_to_go(self):
        line = render.headroom_line(FakeStore(), [record("work")],
                                    [usage("work", five_h=90.0)], "work", NOW)
        self.assertIn("no other token has headroom", line)


class TestRenderRollover(unittest.TestCase):
    ACTED = {"pane": "w6:pC", "ok": True, "detail": "started and resumed",
             "commands": ["herdr agent get w6:pC", "herdr agent send-keys w6:pC esc"]}

    def result(self, **overrides):
        payload = {"dry_run": True, "error": "", "planned": [], "acted": [],
                   "skipped": []}
        payload.update(overrides)
        return payload

    def test_a_dry_run_lists_every_command_and_runs_none(self):
        out, _err = captured(render.print_rollover,
                             self.result(planned=[{"pane": "w6:pC"}], acted=[self.ACTED]),
                             True)
        self.assertIn("would roll over w6:pC", out)
        self.assertIn("    herdr agent get w6:pC", out)
        self.assertIn("1 claude pane(s) examined, 1 to roll over.", out)
        self.assertIn("ctr rollover --apply", out)

    def test_a_real_run_marks_each_pane_ok_or_fail(self):
        out, _err = captured(render.print_rollover, self.result(
            dry_run=False, planned=[{"pane": "w6:pC"}, {"pane": "w6:pR"}],
            acted=[self.ACTED, {"pane": "w6:pR", "ok": False, "detail": "boom"}]), False)
        self.assertIn("ok   w6:pC", out)
        self.assertIn("FAIL w6:pR — boom", out)
        self.assertNotIn("ctr rollover --apply", out)

    def test_every_skipped_pane_gets_its_reason(self):
        out, _err = captured(render.print_rollover, self.result(
            planned=[{"pane": "w6:pY"}],
            skipped=[{"pane": "w6:pY", "reason": "the caller's own pane"}]), True)
        self.assertIn("skip w6:pY — the caller's own pane", out)

    def test_a_healthy_fleet_says_there_is_nothing_to_do(self):
        out, _err = captured(render.print_rollover, self.result(), True)
        self.assertIn("No parked claude session needs a rollover.", out)

    def test_an_error_is_warned_and_never_reads_as_nothing_to_do(self):
        """The `--only` no-match message would otherwise be printed next to
        "No parked claude session needs a rollover" — which is exactly the
        impression it exists to correct."""
        out, err = captured(render.print_rollover,
                            self.result(error="no pane matched --only w9:pZZ"), True)
        self.assertIn("no pane matched --only w9:pZZ", err)
        self.assertNotIn("No parked claude session needs a rollover.", out)

    def test_a_partial_failure_still_says_there_is_nothing_to_do(self):
        """Regression. Suppressing the line on ANY error also silenced it for
        the pre-existing partial-failure case — one pane whose tab label could
        not be resolved, nothing to act on — where it is both true and the
        plainest thing to say. The claim is about panes we EXAMINED, so it may
        be made whenever the census is non-empty."""
        out, err = captured(render.print_rollover, self.result(
            error="could not resolve a tab label for 1 pane(s) (w9:pQ)",
            planned=[{"pane": "w9:pQ"}],
            skipped=[{"pane": "w9:pQ", "reason": "tab label unknown"}]), True)
        self.assertIn("could not resolve a tab label", err)
        self.assertIn("No parked claude session needs a rollover.", out)
        self.assertIn("1 claude pane(s) examined, 0 to roll over.", out)

    def test_nothing_examined_plus_an_error_makes_no_claim_at_all(self):
        """The other half of the same rule: with an empty census and an error
        explaining why, "nothing needs a rollover" would be a guess."""
        out, err = captured(render.print_rollover,
                            self.result(error="herdr agent list failed"), True)
        self.assertIn("herdr agent list failed", err)
        self.assertNotIn("No parked claude session needs a rollover.", out)

    def test_a_result_full_of_nones_does_not_explode(self):
        out, _err = captured(render.print_rollover,
                             {"error": None, "planned": None, "acted": None,
                              "skipped": None}, True)
        self.assertIn("No parked claude session", out)


class TestRenderDoctorReport(unittest.TestCase):
    def report(self, checks):
        """(exit code, stdout) for one doctor report."""
        held = {}
        out, _err = captured(lambda: held.update(code=render.doctor_report(checks)))
        return held["code"], out

    def test_a_clean_report_exits_zero(self):
        code, out = self.report([("ok", "config dir", "0700")])
        self.assertEqual(0, code)
        self.assertIn("All good.", out)

    def test_a_failure_is_counted_and_exits_one(self):
        code, out = self.report([("ok", "a", "x"), ("fail", "b", "y"), ("warn", "c", "z")])
        self.assertEqual(1, code)
        self.assertIn("1 check(s) failed.", out)
        self.assertIn("FAIL", out)
        self.assertIn("WARN", out)
        self.assertNotIn("All good.", out)

    def test_a_warning_alone_is_not_a_failure(self):
        self.assertEqual(0, self.report([("warn", "a", "x")])[0])


if __name__ == "__main__":
    unittest.main()


class TestRenderSuspectReadings(unittest.TestCase):
    """RULING 8.2 asked that `ctr status` show that something was wrong.

    A clamped reading is `ok` — the numbers are usable — but carries the clamp
    note in `Usage.error`. Round 2 only set the NOTE cell for `not ok`
    readings, so the note reached `ctr status --json` (which serialises the
    whole Usage) and NOTHING else: `ctr list` and `ctr status` showed a
    hostile 0% with no warning, and `ctr doctor` called it `ok`.
    """

    CLAMPED = "utilisation was outside 0..100 and was clamped — reading is suspect"

    def clamped(self, label="work"):
        return usage(label, five_h=0.0, seven_d=1.0, error=self.CLAMPED)

    def test_the_note_cell_carries_the_clamp_warning(self):
        rows = render.token_rows([record("work")], [self.clamped()], None, NOW)
        self.assertEqual(self.CLAMPED, rows[0][-1])

    def test_a_clean_reading_still_shows_the_record_note(self):
        rows = render.token_rows([record("work", note="spare")], [usage("work")], None, NOW)
        self.assertEqual("spare", rows[0][-1])

    def test_rejected_still_wins_over_the_clamp_note(self):
        reading = usage("work", status="rejected", error=self.CLAMPED)
        rows = render.token_rows([record("work")], [reading], None, NOW)
        self.assertEqual("REJECTED (limit reached)", rows[0][-1])

    def test_doctor_detail_names_the_clamp(self):
        detail = render.token_check_detail(self.clamped())
        self.assertIn("clamped", detail)
        self.assertIn("(ratelimit_headers)", detail)

    def test_doctor_calls_a_suspect_reading_warn_not_ok(self):
        self.assertEqual("warn", render.token_check_level(self.clamped()))
        self.assertEqual("ok", render.token_check_level(usage("work")))
        self.assertEqual("fail", render.token_check_level(
            usage("work", ok=False, error="probe failed")))

    def test_the_stale_probe_wording_does_not_assert_what_it_did_not_check(self):
        """The sweep finds leftovers by NAME; it never opens them. On this Mac
        7 of 19 held no curl config at all (six were config dirs, one a 0-byte
        file), so "a work dir still holds a bearer token" over-claimed."""
        detail = render.probe_leftover_detail("ctr-probe-", ["/tmp/ctr-probe-a"])
        self.assertNotIn("still holds", detail)
        self.assertIn("may still hold", detail)
        self.assertIn("rm -rf /tmp/ctr-probe-a", detail)


class TickSummaryTests(unittest.TestCase):
    """`ctr monitor --once` must SAY something. It printed nothing at first.

    An uneventful tick deliberately writes no log line (a 5-minute launchd job
    would add ~288 a day), so stdout is the only place a human running it by
    hand learns anything. Found by running the installed command, not by
    reading the code.
    """

    @staticmethod
    def _reading(label, ok=True):
        return Usage(
            label=label, five_h=39.0, seven_d=38.0, five_h_reset=0, seven_d_reset=0,
            status="allowed", probe="ratelimit_headers", ok=ok, error="", checked_at=0,
        )

    def test_a_quiet_tick_still_reports(self):
        result = {
            "decision": Decision("hold", None, "active 'social' below thresholds", False),
            "usages": [self._reading("social")],
            "switched": False,
        }
        line = render.tick_summary(result)
        self.assertIn("no change", line)
        self.assertIn("1 token(s) probed", line)
        self.assertIn("below thresholds", line)
        self.assertNotIn("\n", line)

    def test_a_switch_names_the_target_and_the_rollover_step(self):
        result = {
            "decision": Decision("switch", "spare", "5h 91% >= 85%", True),
            "usages": [self._reading("social"), self._reading("spare")],
            "switched": True,
        }
        line = render.tick_summary(result)
        self.assertIn("switched to 'spare'", line)
        self.assertIn("ctr rollover", line)

    def test_unreadable_tokens_are_counted_separately(self):
        result = {
            "decision": Decision("hold", None, "ok", False),
            "usages": [self._reading("a"), self._reading("b", ok=False)],
            "switched": False,
        }
        self.assertIn("1 unreadable", render.tick_summary(result))

    def test_an_errored_tick_leads_with_the_error(self):
        line = render.tick_summary({"error": "monitor tick failed: OSError"})
        self.assertTrue(line.startswith("monitor: monitor tick failed"))

    def test_a_missing_decision_never_raises(self):
        for bad in ({}, {"usages": None}, {"decision": None, "usages": [None]}):
            self.assertIsInstance(render.tick_summary(bad), str)
