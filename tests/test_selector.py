"""Unit tests for ctr.selector — the pure switch/hold decision.

Every test passes an explicit `now`; nothing here reads the wall clock, the
network, the keychain or the user's config. Run with:

    PYTHONPATH=src python3 -m unittest tests.test_selector -v
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from ctr import selector  # noqa: E402
from ctr.model import (  # noqa: E402
    FAILURE_HTTP,
    FAILURE_TRANSPORT,
    NO_TOKEN_ERROR,
    PROBE_RATELIMIT_HEADERS,
    Usage,
    merged_config,
)

NOW = 1_700_000_000


def usage(
    label,
    five_h,
    seven_d=10.0,
    status="allowed",
    ok=True,
    error="",
    checked_at=NOW,
):
    """A successful reading unless told otherwise."""
    return Usage(
        label=label,
        five_h=five_h,
        seven_d=seven_d,
        five_h_reset=checked_at + 3600,
        seven_d_reset=checked_at + 86400,
        status=status,
        probe=PROBE_RATELIMIT_HEADERS,
        ok=ok,
        error=error,
        checked_at=checked_at,
    )


def failed(label, error="probe failed: connection reset"):
    return Usage.failed(label, error, checked_at=NOW)


def state(last_switch_at=0, parked=None):
    return {"last_switch_at": last_switch_at, "parked": dict(parked or {}), "cache": {}}


CONFIG = merged_config({})  # switch 85/95, improve 10, interval 600, recover 60/80


class ConsecutiveProbeFailureTests(unittest.TestCase):
    """RULING 8.1 — a probe failure only triggers at MIN_CONSECUTIVE_FAILURES.

    `_evaluate_trigger` used to set triggered=True on any `not usage.ok`, so one
    transient 429 / curl timeout switched away from a healthy active token AND
    parked it, costing a full recovery cooldown. A measured `rejected` is a
    different thing — the API stating the token is spent — and still fires at
    once.
    """

    def test_one_failed_probe_holds_and_counts_one(self):
        usages = [failed("a"), usage("b", 4.0)]
        current = selector.record_probe_results(state(), usages)
        self.assertEqual(1, selector.consecutive_failures(current, "a"))
        decision = selector.decide("a", usages, current, CONFIG, NOW)
        self.assertEqual("hold", decision.action)
        self.assertIsNone(decision.target)
        self.assertIn("1/2", decision.reason)

    def test_a_second_consecutive_failure_triggers(self):
        usages = [failed("a"), usage("b", 4.0)]
        current = selector.record_probe_results(state(), usages)
        current = selector.record_probe_results(current, usages)
        self.assertEqual(2, selector.consecutive_failures(current, "a"))
        decision = selector.decide("a", usages, current, CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_a_success_in_between_resets_the_counter(self):
        bad = [failed("a"), usage("b", 4.0)]
        good = [usage("a", 12.0), usage("b", 4.0)]
        current = selector.record_probe_results(state(), bad)
        current = selector.record_probe_results(current, good)
        self.assertEqual(0, selector.consecutive_failures(current, "a"))

        current = selector.record_probe_results(current, bad)
        self.assertEqual(1, selector.consecutive_failures(current, "a"))
        self.assertEqual("hold", selector.decide("a", bad, current, CONFIG, NOW).action)

    def test_a_rejected_active_triggers_immediately_whatever_the_counter(self):
        usages = [usage("a", 100.0, status="rejected"), usage("b", 4.0)]
        current = selector.record_probe_results(state(), usages)
        self.assertEqual(0, selector.consecutive_failures(current, "a"))
        decision = selector.decide("a", usages, current, CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)
        self.assertTrue(decision.triggered)

    def test_a_missing_reading_still_triggers_immediately(self):
        # 'a' was never probed (no keychain item): waiting for a second failed
        # probe would mean waiting for a probe that never happens.
        decision = selector.decide("a", [usage("b", 4.0)], state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_record_probe_results_returns_a_new_state(self):
        original = state()
        snapshot = {"last_switch_at": 0, "parked": {}, "cache": {}}
        fresh = selector.record_probe_results(original, [failed("a")])
        self.assertEqual(snapshot, original)
        self.assertIsNot(fresh, original)
        self.assertEqual(1, selector.consecutive_failures(fresh, "a"))

    def test_the_counter_survives_a_state_json_round_trip(self):
        fresh = selector.record_probe_results(state(), [failed("a")])
        reloaded = json.loads(json.dumps(fresh))
        self.assertEqual(1, selector.consecutive_failures(reloaded, "a"))

    def test_a_label_absent_from_this_tick_keeps_its_count(self):
        current = selector.record_probe_results(state(), [failed("a"), failed("b")])
        current = selector.record_probe_results(current, [usage("b", 5.0)])
        self.assertEqual(1, selector.consecutive_failures(current, "a"))
        self.assertEqual(0, selector.consecutive_failures(current, "b"))

    def test_a_corrupt_counter_block_never_crashes(self):
        broken = state()
        broken[selector.PROBE_FAILURES_KEY] = "not a dict"
        self.assertEqual(0, selector.consecutive_failures(broken, "a"))
        decision = selector.decide("a", [failed("a"), usage("b", 4.0)], broken, CONFIG, NOW)
        self.assertEqual("hold", decision.action)

    def test_the_debounced_hold_is_still_worth_a_log_line(self):
        usages = [failed("a", "http 429 rate_limit_error"), usage("b", 4.0)]
        current = selector.record_probe_results(state(), usages)
        decision = selector.decide("a", usages, current, CONFIG, NOW)
        self.assertTrue(decision.triggered, "monitor.py logs on `triggered`")
        self.assertIn("429", decision.reason)
        self.assertNotIn("\n", decision.reason)

    def test_parking_is_unaffected_by_the_counters(self):
        parked_state = selector.park(state(), "a", NOW)
        counted = selector.record_probe_results(parked_state, [failed("a")])
        self.assertIn("a", counted["parked"])
        self.assertEqual({"parked_at": NOW}, counted["parked"]["a"])


class DecideThresholdTests(unittest.TestCase):
    def test_below_threshold_holds(self):
        usages = [usage("a", 50.0), usage("b", 5.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("hold", decision.action)
        self.assertFalse(decision.triggered)
        self.assertIsNone(decision.target)

    def test_active_hot_switches_to_the_token_with_headroom(self):
        usages = [usage("a", 90.0), usage("b", 10.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)
        self.assertTrue(decision.triggered)

    def test_exactly_at_the_threshold_triggers(self):
        usages = [usage("a", 85.0), usage("b", 1.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)

    def test_seven_day_threshold_triggers_even_when_five_hour_is_cold(self):
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 20.0, seven_d=20.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_improvement_below_min_improvement_holds(self):
        # 90 -> 84 is only 6 points; min_improvement is 10. (84, not 85: at 85
        # the candidate is itself in switch territory, so RULING 5's
        # eligibility filter refuses it before the margin is ever measured, and
        # this test would pass for the wrong reason.)
        usages = [usage("a", 90.0), usage("b", 84.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("hold", decision.action)
        self.assertTrue(decision.triggered)
        self.assertIsNone(decision.target)
        self.assertIn("min_improvement", decision.reason)

    def test_rejected_active_switches_without_the_improvement_margin(self):
        # A rejected active is unusable: any ELIGIBLE token beats it, however
        # small the margin. 80 -> 75 is 5 points against a min_improvement of
        # 10, and it still switches. (A `rejected` reading does not have to be
        # at 100%: the status comes from the unified ratelimit header, or from
        # a `locked_reason`, independently of the percentage.)
        usages = [usage("a", 80.0, status="rejected"), usage("b", 75.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_a_repeated_failed_active_probe_triggers_a_switch(self):
        """RULING 8.1: ONE failed probe holds, the second one switches.

        An unauthenticated request to the usage endpoint also answers 429
        (lane-context.md, MEASURED FACT 1), so a single failure is evidence of
        nothing. Two in a row is.
        """
        usages = [failed("a"), usage("b", 4.0)]
        once = selector.record_probe_results(state(), usages)
        self.assertEqual("hold", selector.decide("a", usages, once, CONFIG, NOW).action)

        twice = selector.record_probe_results(once, usages)
        decision = selector.decide("a", usages, twice, CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)
        self.assertTrue(decision.triggered)


class SevenDayWindowTests(unittest.TestCase):
    """A 7d-triggered switch is judged on the 7d window (see _improvement)."""

    def test_seven_day_trigger_switches_on_seven_day_headroom(self):
        # 5h comparison alone would refuse this (3% -> 20% is *worse* on 5h).
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 20.0, seven_d=20.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)
        self.assertIn("7d", decision.reason)

    def test_seven_day_trigger_holds_without_seven_day_headroom(self):
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 1.0, seven_d=94.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("hold", decision.action)
        self.assertTrue(decision.triggered)

    def test_five_hour_trigger_still_uses_the_five_hour_margin(self):
        # Both windows hot: 5h is checked first, so the 5h margin applies.
        # 84, not 85 - at 85 the candidate is itself in switch territory and
        # RULING 5 refuses it before the margin is measured.
        usages = [usage("a", 90.0, seven_d=96.0), usage("b", 84.0, seven_d=1.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("hold", decision.action)
        self.assertIn("5h points better", decision.reason)

    def test_seven_day_trigger_refuses_a_candidate_with_no_5h_headroom(self):
        """RULING 5, with Fable's exact numbers.

        Judging the margin on 7d alone let a huge 7d win buy a move onto a
        token whose HOURLY window was spent: active 5h 3%/7d 96% -> candidate
        5h 99%/7d 10% returned `switch`, pointing the shell at the token with
        1% of its 5h budget left. The outgoing token was then parked, so the
        next tick found no candidate at all and both were stranded.

        The ruling puts the eligibility filter in `choose_best`, so 'b' is not
        a candidate at all and the answer is `no_candidate` - "there is
        genuinely nowhere better to go", which is what the user is told.
        Round 2 implemented it as a 7d-only gate inside `decide()` that
        returned `hold`; this asserts the ruled outcome instead."""
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 99.0, seven_d=10.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)
        self.assertIsNone(decision.target)
        self.assertTrue(decision.triggered)
        self.assertIsNone(selector.choose_best(usages, ["a"], CONFIG, state(), NOW))

    def test_a_candidate_just_below_the_5h_threshold_is_still_allowed(self):
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 84.0, seven_d=10.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_seven_day_trigger_falls_back_to_five_hour_when_7d_unknown(self):
        usages = [usage("a", 3.0, seven_d=96.0), usage("b", 1.0, seven_d=None)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("hold", decision.action)  # 3 - 1 = 2 points, need 10


class AntiFlapIntervalTests(unittest.TestCase):
    def setUp(self):
        self.usages = [usage("a", 90.0), usage("b", 10.0)]

    def test_switch_sixty_seconds_ago_holds(self):
        decision = selector.decide(
            "a", self.usages, state(last_switch_at=NOW - 60), CONFIG, NOW
        )
        self.assertEqual("hold", decision.action)
        self.assertTrue(decision.triggered)
        self.assertIn("anti-flap", decision.reason)

    def test_same_input_after_the_interval_switches(self):
        decision = selector.decide(
            "a", self.usages, state(last_switch_at=NOW - 601), CONFIG, NOW
        )
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_exactly_at_the_interval_switches(self):
        decision = selector.decide(
            "a", self.usages, state(last_switch_at=NOW - 600), CONFIG, NOW
        )
        self.assertEqual("switch", decision.action)


class ParkingTests(unittest.TestCase):
    def test_park_returns_a_new_state_and_does_not_mutate(self):
        original = state()
        snapshot = {"last_switch_at": 0, "parked": {}, "cache": {}}
        parked = selector.park(original, "a", NOW)
        self.assertEqual(snapshot, original)
        self.assertIn("a", parked["parked"])
        self.assertIsNot(parked, original)
        self.assertIsNot(parked["parked"], original["parked"])

    def test_parked_token_is_not_selected_while_still_hot(self):
        # 'a' was parked at 90%; it is now the LOWEST of the three but still
        # hot (65 >= recover_below_5h 60). 'b' and 'c' are below switch_at_5h,
        # so parking - not RULING 5's eligibility filter - is what excludes 'a'.
        parked_state = selector.park(state(), "a", NOW - 7200)
        usages = [usage("a", 65.0), usage("b", 80.0), usage("c", 70.0)]
        self.assertEqual("c", selector.choose_best(usages, (), CONFIG, parked_state, NOW))
        self.assertTrue(selector.is_parked("a", parked_state, {u.label: u for u in usages}, CONFIG))

    def test_parked_token_still_hot_on_seven_day_is_not_selected(self):
        parked_state = selector.park(state(), "a", NOW - 7200)
        # 5h recovered (10 < 60) but 7d has not (85 >= 80).
        usages = [usage("a", 10.0, seven_d=85.0), usage("b", 40.0, seven_d=40.0)]
        self.assertEqual("b", selector.choose_best(usages, (), CONFIG, parked_state, NOW))

    def test_parked_token_becomes_eligible_once_both_windows_recover(self):
        parked_state = selector.park(state(), "a", NOW - 7200)
        usages = [usage("a", 59.0, seven_d=79.0), usage("b", 70.0, seven_d=10.0)]
        self.assertFalse(
            selector.is_parked("a", parked_state, {u.label: u for u in usages}, CONFIG)
        )
        self.assertEqual("a", selector.choose_best(usages, (), CONFIG, parked_state, NOW))

    def test_parked_stays_parked_without_a_usable_reading(self):
        parked_state = selector.park(state(), "a", NOW - 7200)
        usages = [failed("a"), usage("b", 50.0)]
        self.assertTrue(
            selector.is_parked("a", parked_state, {u.label: u for u in usages}, CONFIG)
        )

    def test_unpark_recovered_returns_a_new_state_and_drops_only_cool_tokens(self):
        parked_state = selector.park(selector.park(state(), "a", NOW), "b", NOW)
        usages = [usage("a", 10.0, seven_d=10.0), usage("b", 70.0, seven_d=10.0)]
        fresh = selector.unpark_recovered(parked_state, usages, CONFIG)
        self.assertEqual(["a", "b"], sorted(parked_state["parked"].keys()))  # unchanged
        self.assertEqual(["b"], sorted(fresh["parked"].keys()))
        self.assertIsNot(fresh["parked"], parked_state["parked"])

    def test_decide_parks_nothing_by_itself(self):
        # decide() is pure: parking is the caller's job (monitor.tick).
        original = state()
        selector.decide("a", [usage("a", 99.0), usage("b", 1.0)], original, CONFIG, NOW)
        self.assertEqual({}, original["parked"])


class EligibilityTests(unittest.TestCase):
    def test_failed_probe_is_never_selected(self):
        usages = [usage("a", 90.0), failed("b"), usage("c", 50.0)]
        self.assertEqual("c", selector.choose_best(usages, ("a",), CONFIG, state(), NOW))
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("c", decision.target)

    def test_rejected_status_is_never_selected(self):
        usages = [usage("a", 90.0), usage("b", 1.0, status="rejected"), usage("c", 50.0)]
        self.assertEqual("c", selector.choose_best(usages, ("a",), CONFIG, state(), NOW))
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("c", decision.target)

    def test_a_fully_exhausted_token_is_never_selected(self):
        usages = [usage("a", 90.0), usage("b", 100.0), usage("c", 70.0)]
        self.assertEqual("c", selector.choose_best(usages, ("a",), CONFIG, state(), NOW))

    def test_excluded_labels_are_never_selected(self):
        usages = [usage("a", 1.0), usage("b", 2.0)]
        self.assertEqual("b", selector.choose_best(usages, ("a",), CONFIG, state(), NOW))
        self.assertIsNone(selector.choose_best(usages, ("a", "b"), CONFIG, state(), NOW))


class TieBreakTests(unittest.TestCase):
    def test_lowest_five_hour_wins(self):
        usages = [usage("a", 30.0), usage("b", 10.0), usage("c", 20.0)]
        self.assertEqual("b", selector.choose_best(usages, (), CONFIG, state(), NOW))

    def test_equal_five_hour_breaks_on_lower_seven_day(self):
        usages = [usage("a", 20.0, seven_d=40.0), usage("b", 20.0, seven_d=15.0)]
        self.assertEqual("b", selector.choose_best(usages, (), CONFIG, state(), NOW))

    def test_equal_five_hour_and_seven_day_breaks_on_label_ascending(self):
        usages = [usage("zeta", 20.0, seven_d=5.0), usage("alpha", 20.0, seven_d=5.0)]
        self.assertEqual("alpha", selector.choose_best(usages, (), CONFIG, state(), NOW))

    def test_tie_break_is_order_independent(self):
        pair = [usage("m", 20.0, seven_d=5.0), usage("k", 20.0, seven_d=5.0)]
        self.assertEqual(
            selector.choose_best(pair, (), CONFIG, state(), NOW),
            selector.choose_best(list(reversed(pair)), (), CONFIG, state(), NOW),
        )

    def test_known_seven_day_beats_unknown_at_equal_five_hour(self):
        usages = [usage("a", 20.0, seven_d=None), usage("b", 20.0, seven_d=90.0)]
        self.assertEqual("b", selector.choose_best(usages, (), CONFIG, state(), NOW))


class NoActiveTests(unittest.TestCase):
    def test_no_active_still_proposes_a_target(self):
        usages = [usage("a", 40.0), usage("b", 12.0)]
        decision = selector.decide(None, usages, state(), CONFIG, NOW)
        self.assertEqual("no_active", decision.action)
        self.assertEqual("b", decision.target)
        self.assertFalse(decision.triggered)

    def test_no_active_and_nothing_usable(self):
        decision = selector.decide("", [failed("a")], state(), CONFIG, NOW)
        self.assertEqual("no_active", decision.action)
        self.assertIsNone(decision.target)


class NoCandidateTests(unittest.TestCase):
    def test_every_token_exhausted_yields_no_candidate(self):
        usages = [usage("a", 100.0), usage("b", 100.0), usage("c", 100.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)
        self.assertIsNone(decision.target)
        self.assertTrue(decision.triggered)

    def test_every_other_token_rejected_yields_no_candidate(self):
        usages = [
            usage("a", 99.0),
            usage("b", 100.0, status="rejected"),
            failed("c", "http 429"),
        ]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)

    def test_never_switches_to_a_worse_token(self):
        # Every alternative is worse than the (hot) active one.
        usages = [usage("a", 88.0), usage("b", 93.0), usage("c", 99.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertNotEqual("switch", decision.action)
        self.assertIsNone(decision.target)

    def test_single_token_registry_never_switches_to_itself(self):
        usages = [usage("a", 99.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)


class FlapSimulationTests(unittest.TestCase):
    """Drive decide() over a long tick sequence: at most one switch per interval."""

    def _simulate(self, labels, start, ticks, step_s, config):
        active = labels[0]
        levels = dict((label, 5.0) for label in labels)
        levels[active] = 88.0  # the active token is always hot -> always triggered
        current = dict(last_switch_at=0, parked={}, cache={})
        switches = []
        last_action = None
        for index in range(ticks):
            now = start + index * step_s
            usages = [usage(label, levels[label]) for label in labels]
            current = selector.unpark_recovered(current, usages, config)
            decision = selector.decide(active, usages, current, config, now)
            last_action = decision.action
            if decision.action == "switch":
                switches.append(now)
                current = selector.park(current, active, now)
                current["last_switch_at"] = now
                active = decision.target
                levels[active] = 88.0
        return switches, last_action

    def test_at_most_one_switch_per_min_switch_interval(self):
        labels = ["a", "b", "c", "d", "e", "f"]
        interval = int(CONFIG["min_switch_interval_s"])
        switches, last_action = self._simulate(labels, NOW, ticks=121, step_s=30, config=CONFIG)

        self.assertEqual(len(labels) - 1, len(switches))  # each token used once
        for earlier, later in zip(switches, switches[1:]):
            self.assertGreaterEqual(later - earlier, interval)
        # Everything is parked and hot by the end: nothing left to switch to.
        self.assertEqual("no_candidate", last_action)

    def test_a_short_interval_config_is_honoured_too(self):
        config = merged_config({"min_switch_interval_s": 120})
        switches, _ = self._simulate(
            ["a", "b", "c", "d", "e", "f"], NOW, ticks=61, step_s=30, config=config
        )
        for earlier, later in zip(switches, switches[1:]):
            self.assertGreaterEqual(later - earlier, 120)
        self.assertGreaterEqual(len(switches), 2)  # it does still switch

    def test_a_cooling_token_is_reused_only_after_it_recovers(self):
        config = merged_config({})
        current = dict(last_switch_at=0, parked={}, cache={})
        # 'a' hot, 'b' cool -> switch to b, park a.
        first = selector.decide("a", [usage("a", 90.0), usage("b", 5.0)], current, config, NOW)
        self.assertEqual("switch", first.action)
        current = selector.park(current, "a", NOW)
        current["last_switch_at"] = NOW

        # Much later: 'b' is hot, 'a' has cooled to 59 -> 'a' is eligible again.
        later = NOW + 20000
        usages = [usage("a", 59.0, seven_d=20.0), usage("b", 95.0)]
        current = selector.unpark_recovered(current, usages, config)
        second = selector.decide("b", usages, current, config, later)
        self.assertEqual("switch", second.action)
        self.assertEqual("a", second.target)


class ConfigRobustnessTests(unittest.TestCase):
    def test_missing_config_keys_fall_back_to_defaults(self):
        usages = [usage("a", 90.0), usage("b", 10.0)]
        decision = selector.decide("a", usages, {}, {}, NOW)
        self.assertEqual("switch", decision.action)

    def test_garbage_config_values_fall_back_to_defaults(self):
        usages = [usage("a", 90.0), usage("b", 10.0)]
        decision = selector.decide("a", usages, {}, {"min_improvement": "lots"}, NOW)
        self.assertEqual("switch", decision.action)

    def test_custom_thresholds_are_honoured(self):
        usages = [usage("a", 50.0), usage("b", 10.0)]
        config = merged_config({"switch_at_5h": 40.0})
        self.assertEqual("switch", selector.decide("a", usages, state(), config, NOW).action)
        self.assertEqual("hold", selector.decide("a", usages, state(), CONFIG, NOW).action)

    def test_empty_usages_never_crash(self):
        self.assertEqual("no_candidate", selector.decide("a", [], state(), CONFIG, NOW).action)
        self.assertIsNone(selector.choose_best([], (), CONFIG, state(), NOW))
        self.assertEqual("no_active", selector.decide(None, [], state(), CONFIG, NOW).action)

    def test_reasons_are_single_line_and_safe_to_log(self):
        usages = [failed("a", "boom\nsecond line\tand more"), usage("b", 1.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertNotIn("\n", decision.reason)
        self.assertTrue(decision.reason)


if __name__ == "__main__":
    unittest.main()


class RulingFiveEligibilityTests(unittest.TestCase):
    """RULING 5 — a token already in switch territory is not a refuge.

    Round 1 implemented the filter as `_five_hour_headroom_gate` inside
    `decide()`, which only ran when the trigger window was "7d". Every other
    path bypassed it, and two of them are shipped commands: `ctr next` and the
    `ctr status` headroom line both call `choose_best` DIRECTLY. Each number
    below was reproduced against the real module before the filter moved.
    """

    def test_a_twice_failed_probe_will_not_move_onto_a_spent_token(self):
        # verify:S2-brain finding 1, exact numbers: active 'a' fails twice, the
        # only candidate is at 5h 99% / 7d 10%. Used to return `switch -> b`,
        # pointing the shell at 1% of an hourly window and parking 'a'.
        usages = [failed("a"), usage("b", 99.0, seven_d=10.0)]
        twice = selector.record_probe_results(
            selector.record_probe_results(state(), usages), usages
        )
        decision = selector.decide("a", usages, twice, CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)
        self.assertIsNone(decision.target)
        self.assertTrue(decision.triggered)

    def test_a_rejected_active_will_not_move_onto_a_spent_token_either(self):
        # REWRITTEN with the RULING 5 REFINEMENT. This test used to assert that
        # a rejected active with a candidate at 5h 97% returns `no_candidate`.
        # That numbers-choice was wrong, not the intent: the active is MEASURED
        # dead, so refusing a candidate with 3% of its hour left strands the
        # user on a token that cannot serve a single request. The refinement
        # switches there instead (see RelaxedPassTests below).
        # The test's PURPOSE — "a rejected active will not move onto a token
        # that is genuinely spent" — is intact and asserted here with a
        # candidate that really is spent.
        spent = [usage("a", 100.0, status="rejected"), usage("b", 100.0)]
        decision = selector.decide("a", spent, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)
        self.assertIsNone(decision.target)

        # ...nor onto one that is itself rejected, however low its number reads.
        both = [
            usage("a", 100.0, status="rejected"),
            usage("b", 4.0, status="rejected"),
        ]
        self.assertEqual("no_candidate", selector.decide("a", both, state(), CONFIG, NOW).action)

    def test_a_five_hour_trigger_will_not_move_onto_a_token_over_the_line(self):
        # 99 -> 86 clears min_improvement (13 >= 10) but 86 is already past
        # switch_at_5h, so ctr would abandon it again on the very next tick.
        usages = [usage("a", 99.0), usage("b", 86.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("no_candidate", decision.action)

    def test_adoption_never_proposes_a_token_with_no_headroom(self):
        # `ctr next` / `decide(active=None, ...)` used to announce a 96%/99%
        # token as the one with "the most headroom".
        usages = [usage("a", 96.0, seven_d=99.0), usage("b", 97.0, seven_d=99.0)]
        decision = selector.decide("", usages, state(), CONFIG, NOW)
        self.assertEqual("no_active", decision.action)
        self.assertIsNone(decision.target)
        self.assertIsNone(selector.choose_best(usages, (), CONFIG, state(), NOW))

    def test_the_seven_day_threshold_excludes_a_candidate_too(self):
        # switch_at_7d is 95: a candidate at 7d 96 is in switch territory even
        # though its 5h window is empty.
        usages = [usage("a", 90.0, seven_d=10.0), usage("b", 1.0, seven_d=96.0)]
        self.assertIsNone(selector.choose_best(usages, ["a"], CONFIG, state(), NOW))
        self.assertEqual("no_candidate", selector.decide("a", usages, state(), CONFIG, NOW).action)

    def test_an_unknown_seven_day_is_eligible_and_an_unknown_five_hour_is_not(self):
        self.assertTrue(
            selector.is_eligible_target(usage("a", 10.0, seven_d=None), CONFIG)
        )
        unknown_5h = usage("b", 10.0)._replace(five_h=None)
        self.assertFalse(selector.is_eligible_target(unknown_5h, CONFIG))

    def test_the_boundary_is_strictly_below_the_threshold(self):
        self.assertTrue(selector.is_eligible_target(usage("a", 84.999), CONFIG))
        self.assertFalse(selector.is_eligible_target(usage("a", 85.0), CONFIG))

    def test_a_healthy_switch_is_untouched_by_the_filter(self):
        # The over-correction guard: the ordinary case must still switch.
        usages = [usage("a", 90.0), usage("b", 10.0)]
        decision = selector.decide("a", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)

    def test_a_clamped_hostile_zero_does_not_beat_a_healthy_token(self):
        """RULING 8.2's STATED RATIONALE, which the clamp alone did not close.

        Clamping -5 to 0.0 leaves it the LOWEST legal value, so the hostile
        reading still sorted FIRST and ctr still selected it (measured). The
        clamp note now makes the reading visible to a human; what keeps it out
        of the shell is that a suspect reading is not a refuge either.
        """
        from ctr import usage as usage_mod

        hostile = usage_mod.parse_oauth_usage(
            "hostile", '{"five_hour":{"utilization":-5.0},"seven_day":{"utilization":1.0}}', NOW
        )
        healthy = usage_mod.parse_oauth_usage(
            "healthy", '{"five_hour":{"utilization":3.0},"seven_day":{"utilization":1.0}}', NOW
        )
        self.assertEqual(0.0, hostile.five_h)
        self.assertEqual(usage_mod.CLAMP_NOTE, hostile.error)
        for order in ([hostile, healthy], [healthy, hostile]):
            self.assertEqual(
                "healthy", selector.choose_best(order, (), CONFIG, state(), NOW)
            )


class MissingKeychainItemTests(unittest.TestCase):
    """A registered label with NO keychain item is not a probe failure.

    `usage.probe_all` returns one Usage per RECORD, so it hands back
    `Usage.failed(label, NO_TOKEN_ERROR)` without having sent anything. The
    RULING 8.1 de-bounce would otherwise make the monitor sit on a token it can
    never read for a whole extra interval (300 s by default) waiting for a
    second failure that carries no more information than the first.
    """

    def missing(self, label):
        return Usage.failed(label, NO_TOKEN_ERROR, checked_at=NOW)

    def test_it_triggers_on_the_first_tick(self):
        usages = [self.missing("a"), usage("b", 4.0)]
        once = selector.record_probe_results(state(), usages)
        decision = selector.decide("a", usages, once, CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("b", decision.target)
        self.assertTrue(decision.triggered)

    def test_the_reason_says_keychain_not_probe_failure(self):
        usages = [self.missing("a"), usage("b", 4.0)]
        reason = selector.decide("a", usages, state(), CONFIG, NOW).reason
        self.assertIn("keychain", reason)
        self.assertNotIn("consecutive", reason)

    def test_a_real_probe_failure_is_still_de_bounced(self):
        # The guard against over-correcting this into "every failure is immediate".
        usages = [failed("a", "probe rate limited (429)"), usage("b", 4.0)]
        once = selector.record_probe_results(state(), usages)
        self.assertEqual("hold", selector.decide("a", usages, once, CONFIG, NOW).action)


class RelaxedPassTests(unittest.TestCase):
    """RULING 5 REFINEMENT — a spent active token accepts a merely-hot refuge.

    The round-2 gate flagged the literal RULING 5 filter as its lead risk: with
    the active token `rejected` and the only other token at 5h 90%, `decide`
    returned `no_candidate`, leaving the user on a DEAD token while one with 10%
    of its hour left sat unused. Strictly worse than switching, and worse than
    the behaviour before the ruling. Re-derived against the real module before
    the refinement was written.

    The relaxed pass drops ONLY the `switch_at_*` ceilings, only as a second
    pass, and only when the active token is measurably spent. Everything else —
    parked, excluded, suspect, >= 100%, and `rejected` candidates — still holds.
    """

    def test_a_rejected_active_takes_a_hot_refuge(self):
        usages = [usage("dead", 100.0, status="rejected"), usage("refuge", 90.0)]
        decision = selector.decide("dead", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("refuge", decision.target)
        self.assertTrue(decision.triggered)
        # the reason must say plainly that nothing was under the thresholds
        self.assertIn("no token is below the thresholds", decision.reason)

    def test_an_active_with_no_keychain_item_takes_a_hot_refuge(self):
        usages = [Usage.failed("gone", NO_TOKEN_ERROR, NOW), usage("refuge", 90.0)]
        decision = selector.decide("gone", usages, state(), CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("refuge", decision.target)

    def test_a_merely_hot_active_does_NOT_unlock_the_relaxed_pass(self):
        # The whole point of RULING 5: an active at 90% still has headroom, so
        # it is protected from being traded for a token at 86%.
        usages = [usage("hot", 90.0), usage("other", 86.0)]
        self.assertEqual("no_candidate", selector.decide("hot", usages, state(), CONFIG, NOW).action)

    def test_a_failed_probe_does_NOT_unlock_the_relaxed_pass(self):
        # A 429 proves nothing — an unauthenticated request returns one too —
        # so a probe failure must never relax the ceilings.
        usages = [failed("x"), usage("y", 90.0)]
        twice = selector.record_probe_results(
            selector.record_probe_results(state(), usages), usages
        )
        self.assertEqual("no_candidate", selector.decide("x", usages, twice, CONFIG, NOW).action)

    def test_the_relaxed_pass_still_refuses_a_spent_candidate(self):
        usages = [usage("dead", 100.0, status="rejected"), usage("b", 100.0)]
        self.assertEqual("no_candidate", selector.decide("dead", usages, state(), CONFIG, NOW).action)

    def test_the_relaxed_pass_still_refuses_a_parked_candidate(self):
        usages = [usage("dead", 100.0, status="rejected"), usage("b", 90.0)]
        parked = selector.park(state(), "b", NOW)
        self.assertEqual("no_candidate", selector.decide("dead", usages, parked, CONFIG, NOW).action)

    def test_the_relaxed_pass_still_refuses_a_suspect_reading(self):
        suspect = usage("b", 90.0)._replace(error="utilisation was clamped — reading is suspect")
        usages = [usage("dead", 100.0, status="rejected"), suspect]
        self.assertEqual("no_candidate", selector.decide("dead", usages, state(), CONFIG, NOW).action)

    def test_the_relaxed_pass_still_ranks_by_least_used(self):
        usages = [
            usage("dead", 100.0, status="rejected"),
            usage("hotter", 95.0),
            usage("cooler", 88.0),
        ]
        self.assertEqual("cooler", selector.decide("dead", usages, state(), CONFIG, NOW).target)

    def test_choose_best_relax_is_opt_in_and_does_not_leak(self):
        usages = [usage("b", 90.0)]
        self.assertIsNone(selector.choose_best(usages, (), CONFIG, state(), NOW))
        self.assertEqual("b", selector.choose_best(usages, (), CONFIG, state(), NOW, relax=True))


# ---------------------------------------------------------------------------
# v1.1 — a transport error says nothing about the token
# ---------------------------------------------------------------------------


def transport_failed(label, error="probe failed: curl: (6) Could not resolve host: api.anthropic.com"):
    """A failure where no HTTP status came back at all."""
    return Usage.failed(label, error, checked_at=NOW, kind=FAILURE_TRANSPORT)


def http_failed(label, error="probe failed (HTTP 401): authentication_error"):
    """A failure where the API answered ABOUT this token."""
    return Usage.failed(label, error, checked_at=NOW, kind=FAILURE_HTTP)


class TransportFailureTests(unittest.TestCase):
    """RULING v1.1 — only an HTTP-level answer counts toward the de-bounce.

    Reproduced from the live log on 2026-09-16. Two ticks 7.5 HOURS apart, both
    of them the Mac asleep or just woken with no DNS:

        01:26:36 hold — active 'social' probe failed (1/2 consecutive) ...
                 curl: (28) Resolving timed out after 5305244 milliseconds
        09:00:06 no_candidate — active 'social' probe failed 2 ticks in a row
                 curl: (6) Could not resolve host: api.anthropic.com

    `state.json` then read `probe_failures: {"social": 2}`. The API said nothing
    about the token at either tick. With a second token registered, that exact
    sequence switches the fleet off a healthy token and parks it for a full
    cooldown, during a network blip the next token would hit identically.
    """

    def test_the_exact_live_sequence_never_switches(self):
        first = [transport_failed("social", "probe failed: curl: (28) Resolving timed out"), usage("spare", 4.0)]
        state_1 = selector.record_probe_results(state(), first, now=NOW)
        decision_1 = selector.decide("social", first, state_1, CONFIG, NOW)
        self.assertEqual("hold", decision_1.action)
        self.assertIsNone(decision_1.target)
        self.assertEqual(0, selector.consecutive_failures(state_1, "social"))

        later = NOW + int(7.5 * 3600)  # the real gap between the two log lines
        second = [transport_failed("social"), usage("spare", 4.0)]
        state_2 = selector.record_probe_results(state_1, second, now=later)
        decision_2 = selector.decide("social", second, state_2, CONFIG, later)
        self.assertEqual("hold", decision_2.action, "a network blip must never move the fleet")
        self.assertIsNone(decision_2.target)
        self.assertEqual(0, selector.consecutive_failures(state_2, "social"))

    def test_a_transport_hold_says_network(self):
        usages = [transport_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        decision = selector.decide("social", usages, current, CONFIG, NOW)
        self.assertIn("network", decision.reason.lower())
        self.assertTrue(decision.triggered, "monitor.py logs on `triggered`")

    def test_a_transport_failure_never_parks_anything(self):
        usages = [transport_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        self.assertEqual({}, current.get("parked", {}))

    def test_two_http_401s_in_a_row_still_switch(self):
        usages = [http_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        self.assertEqual("hold", selector.decide("social", usages, current, CONFIG, NOW).action)
        current = selector.record_probe_results(current, usages, now=NOW + 300)
        self.assertEqual(2, selector.consecutive_failures(current, "social"))
        decision = selector.decide("social", usages, current, CONFIG, NOW + 300)
        self.assertEqual("switch", decision.action)
        self.assertEqual("spare", decision.target)

    def test_a_success_between_two_http_failures_resets(self):
        bad = [http_failed("social"), usage("spare", 4.0)]
        good = [usage("social", 12.0), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), bad, now=NOW)
        self.assertEqual(1, selector.consecutive_failures(current, "social"))
        current = selector.record_probe_results(current, good, now=NOW + 300)
        self.assertEqual(0, selector.consecutive_failures(current, "social"))
        current = selector.record_probe_results(current, bad, now=NOW + 600)
        self.assertEqual(1, selector.consecutive_failures(current, "social"))
        self.assertEqual("hold", selector.decide("social", bad, current, CONFIG, NOW + 600).action)

    def test_a_long_gap_restarts_the_count_at_one(self):
        """Two failures 7.5h apart are not 'consecutive' in any useful sense."""
        bad = [http_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), bad, now=NOW)
        self.assertEqual(1, selector.consecutive_failures(current, "social"))
        later = NOW + int(7.5 * 3600)
        current = selector.record_probe_results(current, bad, now=later)
        self.assertEqual(1, selector.consecutive_failures(current, "social"),
                         "the Mac slept; this is a fresh first failure")
        self.assertEqual("hold", selector.decide("social", bad, current, CONFIG, later).action)

    def test_failures_inside_the_gap_still_accumulate(self):
        bad = [http_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), bad, now=NOW)
        within = NOW + selector.FAILURE_GAP_RESET_S - 1
        current = selector.record_probe_results(current, bad, now=within)
        self.assertEqual(2, selector.consecutive_failures(current, "social"))

    def test_a_transport_failure_does_not_clear_an_http_count(self):
        """A blip in the middle must neither add to nor erase real evidence."""
        bad = [http_failed("social"), usage("spare", 4.0)]
        blip = [transport_failed("social"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), bad, now=NOW)
        current = selector.record_probe_results(current, blip, now=NOW + 300)
        self.assertEqual(1, selector.consecutive_failures(current, "social"))

    def test_rejected_still_triggers_immediately(self):
        usages = [usage("social", 100.0, status="rejected"), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        decision = selector.decide("social", usages, current, CONFIG, NOW)
        self.assertEqual("switch", decision.action)
        self.assertEqual("spare", decision.target)

    def test_a_missing_keychain_item_still_triggers_immediately(self):
        usages = [Usage.failed("social", NO_TOKEN_ERROR, NOW), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        self.assertEqual("switch", selector.decide("social", usages, current, CONFIG, NOW).action)

    def test_record_probe_results_still_returns_a_new_state(self):
        original = state()
        snapshot = json.loads(json.dumps(original))
        fresh = selector.record_probe_results(original, [transport_failed("a")], now=NOW)
        self.assertEqual(snapshot, original)
        self.assertIsNot(fresh, original)

    def test_the_counters_survive_a_state_json_round_trip(self):
        current = selector.record_probe_results(state(), [http_failed("a")], now=NOW)
        reloaded = json.loads(json.dumps(current))
        self.assertEqual(1, selector.consecutive_failures(reloaded, "a"))
        current = selector.record_probe_results(reloaded, [http_failed("a")], now=NOW + 300)
        self.assertEqual(2, selector.consecutive_failures(current, "a"))

    def test_an_unclassified_failure_still_counts(self):
        """Anything not explicitly marked transport keeps v1 behaviour."""
        usages = [Usage.failed("social", "something odd", NOW), usage("spare", 4.0)]
        current = selector.record_probe_results(state(), usages, now=NOW)
        self.assertEqual(1, selector.consecutive_failures(current, "social"))
