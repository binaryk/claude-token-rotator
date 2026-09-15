"""Tests for ctr.usage — parsing, probe strategy selection, and token hygiene.

Nothing here touches the network: `ctr.usage._run` (the single subprocess seam)
is replaced by a fake curl. The fixtures under tests/fixtures were captured from
the live API on 2026-09-15.

Run: PYTHONPATH=src python3 -m unittest tests.test_usage -v
"""

import glob
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import usage  # noqa: E402
from ctr.model import (  # noqa: E402
    NO_TOKEN_ERROR,
    PROBE_OAUTH_USAGE,
    PROBE_RATELIMIT_HEADERS,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

#: Independently computed with calendar.timegm() and cross-checked against the
#: unix-second reset values in ratelimit_headers_allowed.txt.
RESET_5H = 1789483200
RESET_7D = 1790053200

FAKE_TOKEN = "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE-notreal"
NOW = 1789480000


def fixture(name):
    with open(os.path.join(FIXTURES, name), "r") as handle:
        return handle.read()


class Response(object):
    """One canned curl result."""

    def __init__(self, status, body="", headers=None, rc=0, stderr=""):
        self.status = status
        self.body = body
        self.headers = headers
        self.rc = rc
        self.stderr = stderr


class FakeCurl(object):
    """Stand-in for ctr.usage._run that also audits token handling."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.config_files = []

    def __call__(self, cmd, timeout_s=20):
        self.calls.append(list(cmd))
        config_path = cmd[cmd.index("--config") + 1]
        mode = os.stat(config_path).st_mode & 0o777
        with open(config_path, "r") as handle:
            self.config_files.append((mode, handle.read()))
        response = self.responses.pop(0) if self.responses else Response(500, "{}")
        if "--dump-header" in cmd and response.headers is not None:
            with open(cmd[cmd.index("--dump-header") + 1], "w") as handle:
                handle.write(response.headers)
        out = "%s\n%s%s" % (response.body, usage._HTTP_MARKER, response.status)
        return response.rc, out, response.stderr

    def urls(self):
        return [call[call.index("--url") + 1] for call in self.calls]


class ProbeTestCase(unittest.TestCase):
    """Base class that installs the fake curl."""

    def install(self, *responses):
        fake = FakeCurl(*responses)
        original = usage._run
        usage._run = fake
        self.addCleanup(lambda: setattr(usage, "_run", original))
        return fake


# ---------------------------------------------------------------------------
# parse_oauth_usage
# ---------------------------------------------------------------------------


class TestParseOauthUsage(unittest.TestCase):
    def test_200_body_parses_to_percentages_and_resets(self):
        result = usage.parse_oauth_usage("social", fixture("oauth_usage_200.json"), NOW)
        self.assertTrue(result.ok)
        self.assertEqual(result.five_h, 50.0)
        self.assertEqual(result.seven_d, 18.0)
        self.assertEqual(result.five_h_reset, RESET_5H)
        self.assertEqual(result.seven_d_reset, RESET_7D)
        self.assertEqual(result.status, "allowed")
        self.assertEqual(result.probe, PROBE_OAUTH_USAGE)
        self.assertEqual(result.checked_at, NOW)
        self.assertTrue(result.usable)

    def test_values_are_already_percentages_not_fractions(self):
        result = usage.parse_oauth_usage("social", fixture("oauth_usage_200.json"), NOW)
        self.assertGreater(result.five_h, 1.0)  # 50.0, never 0.5

    def test_full_five_hour_window_is_not_usable(self):
        body = '{"five_hour":{"utilization":100.0},"seven_day":{"utilization":20.0}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.usable)

    def test_locked_reason_marks_rejected(self):
        body = '{"five_hour":{"utilization":10.0,"locked_reason":"over_limit"}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.usable)

    def test_malformed_input_returns_failed_without_raising(self):
        for body in ("", "   ", "not json at all", "[]", "null", "{}", '{"five_hour":null}'):
            result = usage.parse_oauth_usage("social", body, NOW)
            self.assertFalse(result.ok, body)
            self.assertIsNone(result.five_h, body)
            self.assertFalse(result.usable, body)
            self.assertEqual(result.checked_at, NOW, body)

    def test_iso_timestamp_variants(self):
        body = '{"five_hour":{"utilization":1.0,"resets_at":"2026-09-15T14:40:00Z"}}'
        self.assertEqual(usage.parse_oauth_usage("s", body, NOW).five_h_reset, RESET_5H)
        body = '{"five_hour":{"utilization":1.0,"resets_at":"2026-09-15T14:40:00.784584999+00:00"}}'
        self.assertEqual(usage.parse_oauth_usage("s", body, NOW).five_h_reset, RESET_5H)
        body = '{"five_hour":{"utilization":1.0,"resets_at":"garbage"}}'
        self.assertIsNone(usage.parse_oauth_usage("s", body, NOW).five_h_reset)


# ---------------------------------------------------------------------------
# parse_ratelimit_headers
# ---------------------------------------------------------------------------


class TestParseRatelimitHeaders(unittest.TestCase):
    def test_allowed_headers_are_fractions_scaled_to_percent(self):
        result = usage.parse_ratelimit_headers(
            "social", fixture("ratelimit_headers_allowed.txt"), NOW
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.five_h, 51.0)  # header said 0.51
        self.assertEqual(result.seven_d, 18.0)  # header said 0.18
        self.assertEqual(result.five_h_reset, RESET_5H)
        self.assertEqual(result.seven_d_reset, RESET_7D)
        self.assertEqual(result.status, "allowed")
        self.assertEqual(result.probe, PROBE_RATELIMIT_HEADERS)
        self.assertTrue(result.usable)

    def test_rejected_headers_yield_rejected_and_unusable(self):
        result = usage.parse_ratelimit_headers(
            "social", fixture("ratelimit_headers_rejected.txt"), NOW
        )
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.usable)
        self.assertTrue(result.ok)  # the reading itself succeeded
        self.assertEqual(result.five_h, 100.0)
        self.assertEqual(result.seven_d, 42.0)

    def test_header_names_parse_case_insensitively(self):
        raw = "HTTP/2 200\r\nAnthropic-RateLimit-Unified-5h-Utilization: 0.25\r\n"
        result = usage.parse_ratelimit_headers("social", raw, NOW)
        self.assertEqual(result.five_h, 25.0)
        self.assertEqual(result.status, "unknown")

    def test_malformed_or_missing_headers_return_failed(self):
        for raw in ("", "HTTP/2 400\r\n\r\n", "garbage", "content-type: application/json"):
            result = usage.parse_ratelimit_headers("social", raw, NOW)
            self.assertFalse(result.ok, raw)
            self.assertIsNone(result.five_h, raw)
            self.assertFalse(result.usable, raw)


# ---------------------------------------------------------------------------
# clamping — a utilisation outside 0..100 is a broken or hostile response
# ---------------------------------------------------------------------------


class TestUtilisationIsClamped(unittest.TestCase):
    """Both parsers force utilisation into 0..100 and flag that they did.

    A negative value is the dangerous one: selector.choose_best ranks by
    LOWEST five_h and the headroom gate only guards the upper bound, so an
    unclamped -5 would sit at the head of the queue as the most attractive
    token in the fleet.
    """

    def test_oauth_negative_clamps_to_zero_and_says_so(self):
        body = '{"five_hour":{"utilization":-5.0},"seven_day":{"utilization":18.0}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertEqual(result.five_h, 0.0)
        self.assertEqual(result.seven_d, 18.0)
        self.assertIn("clamped", result.error)
        self.assertTrue(result.ok)
        self.assertTrue(result.usable)  # still a usable reading, just flagged

    def test_oauth_above_hundred_clamps_to_hundred(self):
        body = '{"five_hour":{"utilization":150.0},"seven_day":{"utilization":18.0}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertEqual(result.five_h, 100.0)
        self.assertEqual(result.seven_d, 18.0)
        self.assertIn("clamped", result.error)
        self.assertEqual(result.status, "rejected")

    def test_oauth_seven_day_window_is_clamped_too(self):
        body = '{"five_hour":{"utilization":10.0},"seven_day":{"utilization":-3.0}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertEqual(result.five_h, 10.0)
        self.assertEqual(result.seven_d, 0.0)
        self.assertIn("clamped", result.error)

    def test_header_fractions_are_clamped_after_scaling(self):
        raw = (
            "anthropic-ratelimit-unified-5h-utilization: -0.05\r\n"
            "anthropic-ratelimit-unified-7d-utilization: 1.5\r\n"
            "anthropic-ratelimit-unified-status: allowed\r\n"
        )
        result = usage.parse_ratelimit_headers("social", raw, NOW)
        self.assertEqual(result.five_h, 0.0)
        self.assertEqual(result.seven_d, 100.0)
        self.assertIn("clamped", result.error)
        self.assertTrue(result.usable)

    def test_clamp_note_is_appended_to_an_existing_error(self):
        raw = (
            "anthropic-ratelimit-unified-5h-utilization: 1.4\r\n"
            "anthropic-ratelimit-unified-status: rejected\r\n"
        )
        result = usage.parse_ratelimit_headers("social", raw, NOW)
        self.assertEqual(result.five_h, 100.0)
        self.assertIn("rate limit reached", result.error)
        self.assertIn("clamped", result.error)

    def test_an_unknown_seven_day_window_stays_unknown(self):
        result = usage.parse_oauth_usage("social", '{"five_hour":{"utilization":7.0}}', NOW)
        self.assertIsNone(result.seven_d)  # unknown is not zero
        self.assertEqual(result.error, "")

    def test_normal_readings_are_unchanged_and_carry_no_note(self):
        oauth = usage.parse_oauth_usage("social", fixture("oauth_usage_200.json"), NOW)
        self.assertEqual((oauth.five_h, oauth.seven_d), (50.0, 18.0))
        self.assertEqual(oauth.error, "")
        headers = usage.parse_ratelimit_headers(
            "social", fixture("ratelimit_headers_allowed.txt"), NOW
        )
        self.assertEqual((headers.five_h, headers.seven_d), (51.0, 18.0))
        self.assertEqual(headers.error, "")

    def test_no_parsed_reading_can_ever_be_out_of_band(self):
        for value in (-1000000000.0, -0.001, 0.0, 50.0, 100.0, 100.001, 1000000000.0):
            body = '{"five_hour":{"utilization":%s},"seven_day":{"utilization":%s}}' % (
                value,
                -value,
            )
            result = usage.parse_oauth_usage("social", body, NOW)
            self.assertGreaterEqual(result.five_h, 0.0, value)
            self.assertLessEqual(result.five_h, 100.0, value)
            self.assertGreaterEqual(result.seven_d, 0.0, value)
            self.assertLessEqual(result.seven_d, 100.0, value)

    def test_the_non_finite_values_json_accepts_are_also_out_of_band(self):
        """`json.loads` accepts bare NaN / Infinity / -Infinity by default, so a
        hostile or regressed body can carry them. NaN is the dangerous one: it
        is neither `< 0` nor `> 100`, so the clamp's two comparisons both miss
        it and it used to arrive at `selector.choose_best` as a live reading,
        making the sort ORDER-DEPENDENT (measured: [nan, good] picked the bad
        token, [good, nan] the good one)."""
        for literal in ("NaN", "Infinity", "-Infinity"):
            body = '{"five_hour":{"utilization":%s},"seven_day":{"utilization":1.0}}' % literal
            result = usage.parse_oauth_usage("social", body, NOW)
            if result.ok:  # +/-Infinity clamps to a legal bound
                self.assertGreaterEqual(result.five_h, 0.0, literal)
                self.assertLessEqual(result.five_h, 100.0, literal)
                self.assertEqual(usage.CLAMP_NOTE, result.error, literal)
            else:  # NaN is not a number, so there is no reading to salvage
                self.assertEqual("NaN", literal)
                self.assertIsNone(result.five_h)
                self.assertIn("NaN", result.error)

    def test_a_nan_seven_day_becomes_unknown_and_is_flagged(self):
        body = '{"five_hour":{"utilization":3.0},"seven_day":{"utilization":NaN}}'
        result = usage.parse_oauth_usage("social", body, NOW)
        self.assertTrue(result.ok)
        self.assertEqual(3.0, result.five_h)
        self.assertIsNone(result.seven_d)  # unknown, not zero
        self.assertEqual(usage.CLAMP_NOTE, result.error)

    def test_a_nan_in_the_headers_is_refused_too(self):
        raw = "HTTP/2 200\r\n%s: nan\r\n%s: 0.18\r\n\r\n" % (
            usage.HEADER_5H_UTIL, usage.HEADER_7D_UTIL
        )
        result = usage.parse_ratelimit_headers("social", raw, NOW)
        self.assertFalse(result.ok)
        self.assertIsNone(result.five_h)
        self.assertIn("NaN", result.error)

    def test_a_nan_reading_can_never_win_a_selection(self):
        """The end of the chain the clamp exists to protect: whatever the
        parser does, `choose_best` must be order-independent."""
        from ctr import selector
        from ctr.model import merged_config

        body = '{"five_hour":{"utilization":NaN},"seven_day":{"utilization":1.0}}'
        bad = usage.parse_oauth_usage("bad", body, NOW)
        good = usage.parse_oauth_usage(
            "good", '{"five_hour":{"utilization":3.0},"seven_day":{"utilization":1.0}}', NOW
        )
        config = merged_config({})
        state = {"last_switch_at": 0, "parked": {}, "cache": {}}
        self.assertEqual("good", selector.choose_best([bad, good], (), config, state, NOW))
        self.assertEqual("good", selector.choose_best([good, bad], (), config, state, NOW))

# ---------------------------------------------------------------------------
# classify_http
# ---------------------------------------------------------------------------


class TestClassifyHttp(unittest.TestCase):
    def test_each_error_fixture_maps_to_its_classification(self):
        self.assertEqual(usage.classify_http(200, fixture("oauth_usage_200.json")), "ok")
        self.assertEqual(
            usage.classify_http(403, fixture("oauth_usage_403_scope.json")),
            "scope_insufficient",
        )
        self.assertEqual(usage.classify_http(401, fixture("oauth_usage_401.json")), "auth_failed")
        self.assertEqual(usage.classify_http(429, fixture("oauth_usage_429.json")), "rate_limited")

    def test_other_statuses(self):
        self.assertEqual(usage.classify_http(500, ""), "http_error")
        self.assertEqual(usage.classify_http(0, ""), "http_error")
        self.assertEqual(usage.classify_http(404, '{"error":"not_found"}'), "http_error")
        self.assertEqual(usage.classify_http(403, '{"error":"forbidden"}'), "auth_failed")


# ---------------------------------------------------------------------------
# probe() — strategy order, fallback, failure handling
# ---------------------------------------------------------------------------


class TestProbeStrategies(ProbeTestCase):
    def test_oauth_first_then_header_fallback_on_scope_403(self):
        fake = self.install(
            Response(403, fixture("oauth_usage_403_scope.json")),
            Response(200, "{}", headers=fixture("ratelimit_headers_allowed.txt")),
        )
        result = usage.probe("social", FAKE_TOKEN)
        self.assertTrue(result.ok)
        self.assertEqual(result.probe, PROBE_RATELIMIT_HEADERS)
        self.assertEqual(result.five_h, 51.0)
        self.assertEqual(fake.urls(), [usage.OAUTH_USAGE_URL, usage.MESSAGES_URL])

    def test_oauth_success_does_not_make_a_second_call(self):
        fake = self.install(Response(200, fixture("oauth_usage_200.json")))
        result = usage.probe("social", FAKE_TOKEN)
        self.assertEqual(result.probe, PROBE_OAUTH_USAGE)
        self.assertEqual(result.five_h, 50.0)
        self.assertEqual(len(fake.calls), 1)

    def test_prefer_starts_with_the_remembered_strategy(self):
        fake = self.install(
            Response(200, "{}", headers=fixture("ratelimit_headers_allowed.txt"))
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertEqual(result.probe, PROBE_RATELIMIT_HEADERS)
        self.assertEqual(fake.urls(), [usage.MESSAGES_URL])

    def test_auth_failure_does_not_fall_back(self):
        fake = self.install(Response(401, fixture("oauth_usage_401.json")))
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        self.assertEqual(len(fake.calls), 1)
        self.assertIn("revoked", result.error)

    def test_header_probe_uses_the_zero_output_token_body(self):
        fake = self.install(
            Response(200, "{}", headers=fixture("ratelimit_headers_allowed.txt"))
        )
        usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        body = fake.calls[0][fake.calls[0].index("--data") + 1]
        self.assertIn('"max_tokens": 0', body)
        self.assertIn(usage.PROBE_MODEL, body)

    def test_empty_token_never_reaches_curl(self):
        fake = self.install()
        result = usage.probe("social", "")
        self.assertFalse(result.ok)
        self.assertEqual(fake.calls, [])


class TestProbeRateLimitIsTransient(ProbeTestCase):
    def test_429_without_headers_is_transient_not_exhausted(self):
        self.install(
            Response(429, fixture("oauth_usage_429.json")),
            Response(429, fixture("oauth_usage_429.json")),
        )
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        # Crucially NOT "rejected": a 429 is also what an unauthenticated
        # request returns, so it must never park the token as exhausted.
        self.assertNotEqual(result.status, "rejected")
        self.assertEqual(result.status, "unknown")
        self.assertIn("transient", result.error)

    def test_429_with_ratelimit_headers_is_a_real_rejection(self):
        self.install(
            Response(
                429,
                "{}",
                headers=fixture("ratelimit_headers_rejected.txt"),
            )
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "rejected")
        self.assertFalse(result.usable)
        self.assertEqual(result.five_h, 100.0)


# ---------------------------------------------------------------------------
# probe() reports the MOST INFORMATIVE error, not the last strategy's
# ---------------------------------------------------------------------------


class TestProbeReportsTheMostInformativeError(ProbeTestCase):
    """A routine scope 403 must never be reported as the cause of a failure.

    A long-lived `oat` token ALWAYS gets 403 oauth_scope_insufficient from the
    usage endpoint — that is expected, and it explains nothing. Reporting the
    last strategy's error therefore diagnosed an unrelated 500 as "this token
    lacks the user:profile scope": confident, and wrong.
    """

    SERVER_ERROR = Response(500, '{"type":"error","error":{"type":"api_error"}}')

    def test_oat_steady_state_header_500_is_not_blamed_on_the_scope(self):
        # An `oat` token's remembered strategy IS the header probe, so the 500
        # is attempted FIRST and the routine scope 403 lands LAST.
        fake = self.install(
            self.SERVER_ERROR,
            Response(403, fixture("oauth_usage_403_scope.json")),
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertEqual(fake.urls(), [usage.MESSAGES_URL, usage.OAUTH_USAGE_URL])
        self.assertFalse(result.ok)
        self.assertIn("HTTP 500", result.error)
        self.assertFalse(result.error.startswith(usage._ERROR_TEXT["scope_insufficient"]))

    def test_the_losing_attempt_survives_as_trailing_context(self):
        self.install(
            self.SERVER_ERROR,
            Response(403, fixture("oauth_usage_403_scope.json")),
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertIn("[also:", result.error)
        self.assertIn("user:profile", result.error)

    def test_scope_403_then_header_500_also_names_the_500(self):
        # The same pair in the default order.
        self.install(
            Response(403, fixture("oauth_usage_403_scope.json")),
            self.SERVER_ERROR,
        )
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        # Head of the string, for the same reason as the 429 test below: the
        # scope 403 must be the trailing context, never the diagnosis.
        self.assertTrue(result.error.startswith(usage._ERROR_TEXT["http_error"]), result.error)
        self.assertIn("HTTP 500", result.error.split("[also:", 1)[0])
        self.assertIn("user:profile", result.error.split("[also:", 1)[1])

    def test_a_transient_429_does_not_mask_a_concrete_failure(self):
        # Header probe: 200 carrying no ratelimit headers — a real, measured
        # failure. Usage endpoint: 429, which proves nothing (an
        # unauthenticated request returns 429 too), so it must not win.
        self.install(
            Response(200, "{}"),
            Response(429, fixture("oauth_usage_429.json")),
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertFalse(result.ok)
        # POSITION, not membership: under the old last-wins ordering the
        # no_headers text still APPEARS in result.error - inside the
        # "[also: ...]" tail - so `assertIn` passed either way and guarded
        # nothing. Only the head of the string says which attempt won.
        self.assertTrue(
            result.error.startswith(usage._ERROR_TEXT["no_headers"]), result.error
        )
        self.assertIn("[also:", result.error)
        self.assertIn("rate limited", result.error.split("[also:", 1)[1])

    def test_both_attempts_failing_the_same_way_is_not_said_twice(self):
        """An Anthropic outage 500s BOTH endpoints. Repeating the identical
        text as its own '[also: ...]' tail says nothing new and eats the
        120-char budget selector._short() allows a reason line."""
        self.install(
            Response(500, '{"type":"error","error":{"type":"api_error"}}'),
            Response(500, '{"type":"error","error":{"type":"api_error"}}'),
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertFalse(result.ok)
        self.assertNotIn("[also:", result.error)
        self.assertEqual(1, result.error.count("HTTP 500"))

    def test_authentication_is_named_when_the_usage_endpoint_401s(self):
        self.install(Response(401, fixture("oauth_usage_401.json")))
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        self.assertIn("revoked", result.error)
        self.assertNotIn("user:profile", result.error)

    def test_authentication_is_named_when_the_header_probe_401s_first(self):
        self.install(
            Response(401, fixture("oauth_usage_401.json")),
            Response(401, fixture("oauth_usage_401.json")),
        )
        result = usage.probe("social", FAKE_TOKEN, prefer=PROBE_RATELIMIT_HEADERS)
        self.assertFalse(result.ok)
        self.assertIn("revoked", result.error)
        self.assertNotIn("user:profile", result.error)

    def test_a_successful_usage_probe_attempts_no_fallback_at_all(self):
        fake = self.install(Response(200, fixture("oauth_usage_200.json")))
        result = usage.probe("social", FAKE_TOKEN)
        self.assertTrue(result.ok)
        self.assertEqual(fake.urls(), [usage.OAUTH_USAGE_URL])
        self.assertEqual(result.error, "")

    def test_a_lone_failure_is_reported_verbatim(self):
        self.install(Response(429, fixture("oauth_usage_429.json")))
        result = usage.probe("social", FAKE_TOKEN)
        self.assertIn("transient", result.error)
        self.assertNotIn("[also:", result.error)


# ---------------------------------------------------------------------------
# token hygiene
# ---------------------------------------------------------------------------


class TestTokenNeverLeaks(ProbeTestCase):
    def test_token_is_never_an_argv_element(self):
        fake = self.install(Response(200, fixture("oauth_usage_200.json")))
        usage.probe("social", FAKE_TOKEN)
        for call in fake.calls:
            for element in call:
                self.assertNotIn(FAKE_TOKEN, element)
                self.assertNotIn(FAKE_TOKEN[:20], element)

    def test_token_travels_in_a_0600_config_file_that_is_removed(self):
        fake = self.install(Response(200, fixture("oauth_usage_200.json")))
        before = set(glob.glob(os.path.join(tempfile.gettempdir(), "ctr-probe-*")))
        usage.probe("social", FAKE_TOKEN)
        mode, text = fake.config_files[0]
        self.assertEqual(mode, 0o600)
        self.assertIn("Authorization: Bearer " + FAKE_TOKEN, text)
        after = set(glob.glob(os.path.join(tempfile.gettempdir(), "ctr-probe-*")))
        self.assertEqual(after - before, set())

    def test_error_messages_are_scrubbed_of_the_token(self):
        self.install(
            Response(
                "000",
                "",
                rc=1,
                stderr="curl: (35) handshake failure while sending %s" % FAKE_TOKEN,
            ),
            Response("000", "", rc=1, stderr="curl: (35) handshake failure"),
        )
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        self.assertNotIn(FAKE_TOKEN, result.error)
        self.assertIn("***", result.error)

    def test_curl_failure_returns_failed_usage(self):
        self.install(
            Response("000", "", rc=6, stderr="curl: (6) Could not resolve host"),
            Response("000", "", rc=6, stderr="curl: (6) Could not resolve host"),
        )
        result = usage.probe("social", FAKE_TOKEN)
        self.assertFalse(result.ok)
        self.assertIn("probe failed", result.error)


# ---------------------------------------------------------------------------
# probe_all
# ---------------------------------------------------------------------------


class TestProbeAll(unittest.TestCase):
    def test_order_is_preserved_and_missing_tokens_fail_cleanly(self):
        calls = []

        def fake_probe(label, token, timeout_s=20, prefer=None):
            calls.append(label)
            from ctr.model import Usage

            if not token:
                return Usage.failed(label, NO_TOKEN_ERROR, NOW)
            return Usage(label, 10.0, 2.0, None, None, "allowed", PROBE_OAUTH_USAGE, True, "", NOW)

        original = usage.probe
        usage.probe = fake_probe
        self.addCleanup(lambda: setattr(usage, "probe", original))

        labels = ["alpha", "bravo", "charlie", "delta"]
        results = usage.probe_all(labels, {"alpha": "t1", "charlie": "t3", "delta": "t4"})
        self.assertEqual([item.label for item in results], labels)
        self.assertFalse(results[1].ok)
        self.assertIn("no token", results[1].error)
        self.assertTrue(results[0].ok)
        self.assertEqual(sorted(calls), labels)

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(usage.probe_all([], {}), [])


if __name__ == "__main__":
    unittest.main()
