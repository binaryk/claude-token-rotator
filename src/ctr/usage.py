"""Measure a token's 5h / 7d utilisation, normalised to percentages 0..100.

Two probes, because no single one works for every kind of token
(both MEASURED on this Mac 2026-09-15):

1. PROBE_OAUTH_USAGE — `GET /api/oauth/usage`. Cheap and rich, but it needs the
   `user:profile` scope, which an **interactive login token has and a
   long-lived `claude setup-token` token does NOT**: the latter gets a 403
   `oauth_scope_insufficient`. Its `utilization` fields are already 0..100.
2. PROBE_RATELIMIT_HEADERS — `POST /v1/messages` with `max_tokens: 0`
   (8 input tokens, 0 output tokens — the cheapest call found). The reply
   carries `anthropic-ratelimit-unified-{5h,7d}-utilization`, which are
   **fractions 0..1 and must be multiplied by 100**.

`probe()` tries (1) then falls back to (2) on `scope_insufficient`; `prefer`
starts from whichever strategy last worked for that label, so steady state is
one request.

A **429 is a transient probe failure, never "this token is exhausted"** — an
unauthenticated request to the usage endpoint returns 429 too. Only ratelimit
headers that actually say `rejected` mark a token unusable.

Both parsers **clamp utilisation into 0..100**. A negative value would sort
FIRST in `selector.choose_best` (which ranks by lowest 5h) while the headroom
gate only guards the upper bound, so a hostile or regressed response would
otherwise steer ctr straight at the most broken token. A clamped reading stays
usable, but says in `Usage.error` that it was clamped.

When every strategy fails, `probe()` surfaces the **most informative** error,
not the last one: a `scope_insufficient` 403 is routine for a long-lived `oat`
token, so it is context and must never be reported as the cause of an
unrelated failure (a 500 on the header probe, say).

Security: the bearer token goes into a 0600 `curl --config` file that is
deleted in a `finally`, so it never appears in argv (`ps`) and never in an
error message — every string we surface is scrubbed.

Python 3.8 compatible. stdlib only; all HTTPS goes through `curl` because this
Mac's python3 TLS trust store is broken.
"""

import concurrent.futures
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from ctr.model import (
    NO_TOKEN_ERROR,
    PROBE_OAUTH_USAGE,
    PROBE_RATELIMIT_HEADERS,
    Usage,
)

CURL = "curl"
API_ROOT = "https://api.anthropic.com"
OAUTH_USAGE_URL = API_ROOT + "/api/oauth/usage"
MESSAGES_URL = API_ROOT + "/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA = "oauth-2025-04-20"
#: Cheapest probe body found: 8 input tokens, 0 output tokens.
PROBE_MODEL = "claude-haiku-4-5-20251001"
PROBE_BODY = json.dumps(
    {
        "model": PROBE_MODEL,
        "max_tokens": 0,
        "messages": [{"role": "user", "content": "hi"}],
    },
    sort_keys=True,
)

HEADER_5H_UTIL = "anthropic-ratelimit-unified-5h-utilization"
HEADER_7D_UTIL = "anthropic-ratelimit-unified-7d-utilization"
HEADER_5H_RESET = "anthropic-ratelimit-unified-5h-reset"
HEADER_7D_RESET = "anthropic-ratelimit-unified-7d-reset"
HEADER_5H_STATUS = "anthropic-ratelimit-unified-5h-status"
HEADER_7D_STATUS = "anthropic-ratelimit-unified-7d-status"
HEADER_STATUS = "anthropic-ratelimit-unified-status"

#: Utilisation is a percentage; anything outside this range is a broken or
#: hostile response, never a real reading.
UTIL_MIN = 0.0
UTIL_MAX = 100.0
#: Appended to Usage.error when a value had to be clamped, so `ctr status`
#: can show that the reading, while usable, is not trustworthy.
CLAMP_NOTE = "utilisation was outside 0..100 and was clamped — reading is suspect"

_HTTP_MARKER = "__CTR_HTTP__"
_STRATEGIES = (PROBE_OAUTH_USAGE, PROBE_RATELIMIT_HEADERS)
#: classify_http outcomes that make it worth trying the other strategy.
_FALLBACK_FROM = {
    PROBE_OAUTH_USAGE: ("scope_insufficient",),
    PROBE_RATELIMIT_HEADERS: ("no_headers", "http_error"),
}
_ERROR_TEXT = {
    "scope_insufficient": "token lacks the user:profile scope for /api/oauth/usage",
    "auth_failed": "token rejected (401/403) — it may be revoked or expired",
    "rate_limited": "probe rate limited (429) — transient, token state unknown",
    "http_error": "probe failed",
    "no_headers": "response carried no anthropic-ratelimit headers",
    "five_h_not_a_number": "five-hour utilisation was not a number (NaN) — "
                           "refusing the reading",
}
#: How much a failed attempt EXPLAINS the failure, highest wins. A
#: `scope_insufficient` 403 is the routine, expected answer for a long-lived
#: `oat` token, so it ranks last: it is context, never the cause. A 429 says
#: outright that the token's state is unknown, so it loses to a concrete HTTP
#: failure. Ties keep the later attempt, matching the old last-wins behaviour.
_OUTCOME_RANK = {
    "scope_insufficient": 0,
    "rate_limited": 1,
    "no_headers": 2,
    "http_error": 3,
    "auth_failed": 4,
}
_UNKNOWN_RANK = _OUTCOME_RANK["http_error"]


# ---------------------------------------------------------------------------
# subprocess seam — tests replace this single function
# ---------------------------------------------------------------------------


def _run(cmd: List[str], timeout_s: int = 20) -> Tuple[int, str, str]:
    """Run curl. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return 127, "", "cannot run %s: %s" % (cmd[0], exc.strerror or "os error")
    try:
        out, err = proc.communicate(timeout=timeout_s + 5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 124, "", "curl timed out after %ds" % timeout_s
    return proc.returncode, out, err


def _scrub(text: str, secret: Optional[str]) -> str:
    """One-line, secret-free version of subprocess output."""
    cleaned = " ".join((text or "").split())
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned[:200]


# ---------------------------------------------------------------------------
# pure parsing
# ---------------------------------------------------------------------------


def classify_http(status: int, body: str) -> str:
    """-> "ok" | "scope_insufficient" | "auth_failed" | "rate_limited" | "http_error" """
    try:
        code = int(status)
    except (TypeError, ValueError):
        code = 0
    if 200 <= code < 300:
        return "ok"
    low = (body or "").lower()
    if "oauth_scope_insufficient" in low or "scope requirement" in low:
        return "scope_insufficient"
    if code in (401, 403) or "authentication_error" in low or "invalid_api_key" in low:
        return "auth_failed"
    if code == 429 or "rate_limit_error" in low:
        return "rate_limited"
    return "http_error"


def parse_oauth_usage(label: str, body: str, now: int) -> Usage:
    """Parse a `GET /api/oauth/usage` 200 body. Its values are ALREADY 0..100."""
    data = _load_json(body)
    if data is None:
        return Usage.failed(label, "usage response was not JSON", now)
    five = data.get("five_hour")
    seven = data.get("seven_day")
    five = five if isinstance(five, dict) else {}
    seven = seven if isinstance(seven, dict) else {}
    raw_five = _as_float(five.get("utilization"))
    if raw_five is None:
        return Usage.failed(label, "usage response had no five_hour.utilization", now)
    five_h, clamped_5h = _clamp_util(raw_five)
    if five_h is None:  # NaN: not a reading, and everything downstream needs 5h.
        return Usage.failed(label, _ERROR_TEXT["five_h_not_a_number"], now)
    seven_d, clamped_7d = _clamp_util(_as_float(seven.get("utilization")))
    locked = five.get("locked_reason") or seven.get("locked_reason")
    status = "rejected" if (locked or five_h >= UTIL_MAX) else "allowed"
    return Usage(
        label=label,
        five_h=five_h,
        seven_d=seven_d,
        five_h_reset=_iso_epoch(five.get("resets_at")),
        seven_d_reset=_iso_epoch(seven.get("resets_at")),
        status=status,
        probe=PROBE_OAUTH_USAGE,
        ok=True,
        error=_note_clamped("", clamped_5h or clamped_7d),
        checked_at=now,
    )


def parse_ratelimit_headers(label: str, raw_headers: str, now: int) -> Usage:
    """Parse `anthropic-ratelimit-unified-*` headers. Values are FRACTIONS 0..1."""
    headers = _header_map(raw_headers)
    raw_five = _as_percent(headers.get(HEADER_5H_UTIL))
    if raw_five is None:
        return Usage.failed(label, _ERROR_TEXT["no_headers"], now)
    five_h, clamped_5h = _clamp_util(raw_five)
    if five_h is None:  # NaN: not a reading, and everything downstream needs 5h.
        return Usage.failed(label, _ERROR_TEXT["five_h_not_a_number"], now)
    seven_d, clamped_7d = _clamp_util(_as_percent(headers.get(HEADER_7D_UTIL)))
    status = _unified_status(headers)
    base = "" if status != "rejected" else "rate limit reached for this token"
    return Usage(
        label=label,
        five_h=five_h,
        seven_d=seven_d,
        five_h_reset=_as_epoch(headers.get(HEADER_5H_RESET)),
        seven_d_reset=_as_epoch(headers.get(HEADER_7D_RESET)),
        status=status,
        probe=PROBE_RATELIMIT_HEADERS,
        ok=True,
        error=_note_clamped(base, clamped_5h or clamped_7d),
        checked_at=now,
    )


def _clamp_util(value: Optional[float]) -> Tuple[Optional[float], bool]:
    """Force a utilisation percentage into 0..100.

    Returns (safe value, whether it had to be changed). `None` — an absent
    seven-day window — passes through untouched, because "unknown" is not the
    same as "zero" and the selector already handles it.

    NaN is the one value the `<`/`>` comparisons cannot catch: every comparison
    against NaN is False, so a bare `NaN` — which `json.loads` accepts by
    default, and which a hostile or regressed body can therefore carry — used
    to walk straight through unflagged and make `choose_best`'s sort
    order-dependent. It is not a number, so it becomes "unknown" (None) and is
    reported as clamped; the callers below refuse the reading outright when it
    is the FIVE-HOUR window, which they cannot do without.
    """
    if value is None:
        return None, False
    if value != value:  # NaN — not <, not >, not ==, so test it directly.
        return None, True
    if value < UTIL_MIN:
        return UTIL_MIN, True
    if value > UTIL_MAX:
        return UTIL_MAX, True
    return round(value, 3), False


def _note_clamped(error: str, clamped: bool) -> str:
    """Append the clamp warning to an otherwise fine reading's error text."""
    if not clamped:
        return error
    return (error + "; " + CLAMP_NOTE) if error else CLAMP_NOTE


def _unified_status(headers: Dict[str, str]) -> str:
    values = [
        (headers.get(name) or "").strip().lower()
        for name in (HEADER_5H_STATUS, HEADER_7D_STATUS, HEADER_STATUS)
    ]
    if "rejected" in values:
        return "rejected"
    if "allowed" in values:
        return "allowed"
    return "unknown"


def _header_map(raw_headers: str) -> Dict[str, str]:
    """Lowercase header name -> value. Status lines and junk are ignored."""
    headers = {}
    for line in (raw_headers or "").replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.upper().startswith("HTTP/"):
            continue
        if ":" not in line:
            continue
        name, _sep, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def _load_json(text: str):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_percent(value) -> Optional[float]:
    """A 0..1 fraction -> a 0..100 percentage, without float noise."""
    number = _as_float(value)
    if number is None:
        return None
    return round(number * 100.0, 3)


def _as_epoch(value) -> Optional[int]:
    """Unix seconds from a header value (digits) or an ISO timestamp."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _iso_epoch(text)


def _iso_epoch(value) -> Optional[int]:
    """ISO-8601 -> unix seconds. Handles `Z`, `+00:00` and >6 fractional digits."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _trim_fraction(text)
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return int(parsed.timestamp())


def _trim_fraction(text: str) -> str:
    """Python 3.8's fromisoformat accepts exactly 3 or 6 fractional digits."""
    dot = text.find(".")
    if dot < 0:
        return text
    end = dot + 1
    while end < len(text) and text[end].isdigit():
        end += 1
    digits = text[dot + 1 : end]
    if len(digits) in (3, 6):
        return text
    if not digits:
        return text[:dot] + text[end:]
    digits = (digits + "000000")[:6]
    return text[:dot] + "." + digits + text[end:]


# ---------------------------------------------------------------------------
# probing
# ---------------------------------------------------------------------------


def probe(
    label: str,
    token: str,
    timeout_s: int = 20,
    prefer: Optional[str] = None,
) -> Usage:
    """Measure one token. Never raises; never puts the token in `error`."""
    now = int(time.time())
    if not token:
        return Usage.failed(label, NO_TOKEN_ERROR, now)
    attempts = []
    for strategy in _strategy_order(prefer):
        result, outcome = _probe_once(label, token, strategy, timeout_s, now)
        if result.ok:
            return result
        attempts.append((outcome, result))
        if outcome not in _FALLBACK_FROM.get(strategy, ()):
            break
    return _most_informative(label, attempts, now)


def _most_informative(label: str, attempts: List[Tuple[str, Usage]], now: int) -> Usage:
    """The failed attempt that best explains why the probe failed.

    Reporting the LAST strategy's error is actively misleading: an `oat` token
    probes headers-first, so a 500 there falls back to the usage endpoint and
    collects the routine `scope_insufficient` 403 — a confident, wrong
    diagnosis of a different failure. Rank instead, and keep the loser as
    trailing context rather than discarding it.
    """
    if not attempts:
        return Usage.failed(label, "no probe ran", now)
    ranked = sorted(attempts, key=lambda item: _OUTCOME_RANK.get(item[0], _UNKNOWN_RANK))
    best = ranked[-1][1]
    # Both attempts can fail the SAME way — both endpoints 500, both time out —
    # which is exactly what an Anthropic outage looks like. Repeating the text
    # verbatim as its own "[also: ...]" tail adds nothing and burns the
    # 120-char reason budget selector._short() allows, so only keep context
    # that actually says something new.
    seen = {best.error}
    context = []  # type: List[str]
    for item in ranked[:-1]:
        if item[1].error and item[1].error not in seen:
            seen.add(item[1].error)
            context.append(item[1].error)
    if not context:
        return best
    return best._replace(error="%s [also: %s]" % (best.error, "; ".join(context)))


def _strategy_order(prefer: Optional[str]) -> List[str]:
    if prefer in _STRATEGIES:
        return [prefer] + [name for name in _STRATEGIES if name != prefer]
    return list(_STRATEGIES)


def _probe_once(
    label: str, token: str, strategy: str, timeout_s: int, now: int
) -> Tuple[Usage, str]:
    if strategy == PROBE_RATELIMIT_HEADERS:
        return _probe_headers(label, token, timeout_s, now)
    return _probe_oauth(label, token, timeout_s, now)


def _probe_oauth(label: str, token: str, timeout_s: int, now: int) -> Tuple[Usage, str]:
    status, body, _headers, error = _request(
        token,
        timeout_s,
        ["--request", "GET", "--url", OAUTH_USAGE_URL],
        want_headers=False,
    )
    if error:
        return Usage.failed(label, error, now), "http_error"
    outcome = classify_http(status, body)
    if outcome != "ok":
        return Usage.failed(label, _describe(outcome, status), now), outcome
    parsed = parse_oauth_usage(label, body, now)
    return parsed, "ok" if parsed.ok else "http_error"


def _probe_headers(label: str, token: str, timeout_s: int, now: int) -> Tuple[Usage, str]:
    status, body, headers, error = _request(
        token,
        timeout_s,
        [
            "--request",
            "POST",
            "--url",
            MESSAGES_URL,
            "--header",
            "anthropic-version: " + ANTHROPIC_VERSION,
            "--header",
            "anthropic-beta: " + ANTHROPIC_BETA,
            "--header",
            "content-type: application/json",
            "--data",
            PROBE_BODY,
        ],
        want_headers=True,
    )
    if error:
        return Usage.failed(label, error, now), "http_error"
    parsed = parse_ratelimit_headers(label, headers, now)
    if parsed.ok:
        # Headers are authoritative even on a 429: they say `rejected`.
        return parsed, "ok"
    outcome = classify_http(status, body)
    if outcome == "ok":
        # 200 with no ratelimit headers: nothing measurable here.
        return Usage.failed(label, _ERROR_TEXT["no_headers"], now), "no_headers"
    return Usage.failed(label, _describe(outcome, status), now), outcome


def _describe(outcome: str, status: int) -> str:
    text = _ERROR_TEXT.get(outcome, "probe failed")
    return "%s (HTTP %s)" % (text, status if status else "no response")


def _request(
    token: str,
    timeout_s: int,
    args: List[str],
    want_headers: bool,
) -> Tuple[int, str, str, str]:
    """Run one curl request. Returns (status, body, raw_headers, error).

    The bearer token is written to a 0600 curl config file and removed in a
    `finally`, so it never reaches argv and never reaches an error string.
    """
    workdir = tempfile.mkdtemp(prefix="ctr-probe-")
    config_path = os.path.join(workdir, "curl.cfg")
    headers_path = os.path.join(workdir, "headers.txt")
    try:
        _write_private(config_path, 'header = "Authorization: Bearer %s"\n' % _cfg(token))
        cmd = [
            CURL,
            "--silent",
            "--show-error",
            "--max-time",
            str(int(timeout_s)),
            "--config",
            config_path,
            "--write-out",
            "\n" + _HTTP_MARKER + "%{http_code}",
        ]
        if want_headers:
            cmd += ["--dump-header", headers_path]
        cmd += list(args)
        rc, out, err = _run(cmd, timeout_s=int(timeout_s))
        status, body = _split_status(out)
        if status == 0:
            detail = _scrub(err or out, token) or "curl exited %d" % rc
            return 0, body, "", "probe failed: %s" % detail
        raw_headers = _read_text(headers_path) if want_headers else ""
        return status, body, raw_headers, ""
    except OSError as exc:
        return 0, "", "", "probe failed: %s" % (exc.strerror or "os error")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _write_private(path: str, text: str) -> None:
    """Create a fresh 0600 file (O_EXCL) and write `text` into it."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _cfg(value: str) -> str:
    """Escape a value for a double-quoted curl config entry."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _read_text(path: str) -> str:
    try:
        with open(path, "r") as handle:
            return handle.read()
    except (IOError, OSError):
        return ""


def _split_status(stdout: str) -> Tuple[int, str]:
    """Split curl's body from the `--write-out` status marker."""
    text = stdout or ""
    index = text.rfind(_HTTP_MARKER)
    if index < 0:
        return 0, text
    code = text[index + len(_HTTP_MARKER) :].strip()
    body = text[:index]
    if body.endswith("\n"):
        body = body[:-1]
    try:
        return int(code), body
    except ValueError:
        return 0, body


def probe_all(
    records,
    tokens_by_label: Dict[str, str],
    timeout_s: int = 20,
    workers: int = 4,
    prefers: Optional[Dict[str, str]] = None,
) -> List[Usage]:
    """Probe several tokens concurrently. Order matches `records`.

    `records` may be TokenRecords or plain labels. `prefers` optionally maps a
    label to the strategy that last worked for it (see Store.probe_strategy).
    """
    labels = [getattr(item, "label", item) for item in (records or [])]
    if not labels:
        return []
    prefers = prefers or {}

    def _one(label: str) -> Usage:
        try:
            return probe(
                label,
                (tokens_by_label or {}).get(label) or "",
                timeout_s=timeout_s,
                prefer=prefers.get(label),
            )
        except Exception as exc:  # a probe must never break the whole sweep
            return Usage.failed(label, "probe crashed: %s" % type(exc).__name__)

    count = max(1, min(int(workers or 1), len(labels)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(_one, labels))


__all__ = [
    "probe",
    "probe_all",
    "parse_oauth_usage",
    "parse_ratelimit_headers",
    "classify_http",
    "OAUTH_USAGE_URL",
    "MESSAGES_URL",
    "PROBE_BODY",
]
