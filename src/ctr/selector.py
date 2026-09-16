"""Pure selection logic for ctr — decides whether to switch the active token.

This module is deliberately PURE: no I/O, no subprocess, no clock reads. Every
time value arrives as the `now` argument so the behaviour is fully reproducible
in tests. It is the piece most likely to flap in production, so every rule here
is explicit and independently testable.

Selection rule (frozen contract):
  among ELIGIBLE, non-parked tokens excluding `exclude`, pick the lowest 5h
  utilisation; tie-break on the lowest 7d; final tie-break on label ascending.
  Eligible (RULING 5) means usable AND below both switch thresholds — a token
  already in switch territory is not a refuge. See `is_eligible_target`.

Switch rule (frozen contract) — ALL must hold:
  * the active token crossed `switch_at_5h` or `switch_at_7d`
  * a candidate exists
  * the candidate's 5h is at least `min_improvement` points below the active's
  * `now - state["last_switch_at"] >= min_switch_interval_s`

A FAILED probe on the active token is a fourth way to trigger, but only after
`MIN_CONSECUTIVE_FAILURES` failures in a row — a single 429 or timeout proves
nothing. The counter lives in `state`; the caller folds the current tick in
with `record_probe_results()` BEFORE calling `decide()`.
"""

from typing import Dict, Iterable, List, Optional

from ctr.model import DEFAULTS, FAILURE_TRANSPORT, NO_TOKEN_ERROR, Decision, Usage

#: A token at or above this utilisation has no headroom left and can never be
#: a switch target, however low every other token is.
EXHAUSTED_PCT = 100.0

#: state.json key: label -> number of CONSECUTIVE failed probes seen so far.
PROBE_FAILURES_KEY = "probe_failures"

#: How many consecutive failed probes the active token must accumulate before a
#: probe FAILURE is allowed to trigger a switch.
#:
#: A single failure proves nothing. lane-context.md measured that an
#: *unauthenticated* request to the usage endpoint also answers 429, so a 429 is
#: not evidence that the token is rate-limited — and a transient network blip,
#: a curl timeout or a 500 is not evidence of anything either. Switching on one
#: of those abandons a perfectly healthy active token (and parks it, so it takes
#: a full cooldown to come back). Two in a row is a pattern; one is noise.
#:
#: A MEASURED `status == "rejected"` is different: that is the API stating the
#: token is exhausted, so it still triggers immediately (see `_evaluate_trigger`).
MIN_CONSECUTIVE_FAILURES = 2

#: state.json key: label -> unix time of the failure that last incremented the
#: counter above.
LAST_FAILURE_KEY = "probe_failed_at"

#: "Consecutive" has to mean consecutive IN TIME as well as in ticks.
#:
#: Measured 2026-09-16: the two failures that drove `probe_failures.social` to 2
#: were 01:26 and 09:00 — SEVEN AND A HALF HOURS apart, because the Mac was
#: asleep in between and the monitor simply did not run. Calling those two ticks
#: "consecutive" is meaningless: nothing was observed in the gap, and the second
#: failure is evidence about a freshly-woken network, not about a pattern.
#:
#: Three monitor intervals at the 300 s default. Long enough that a couple of
#: genuinely back-to-back failures still accumulate (they are 300 s apart),
#: short enough that a sleep, a lid close or a commute restarts the count.
FAILURE_GAP_RESET_S = 900

#: Never let a probe error string grow a log line without bound.
_MAX_REASON_DETAIL = 120


# ---------------------------------------------------------------------------
# small helpers (pure)
# ---------------------------------------------------------------------------


def _cfg(config: Optional[Dict], key: str) -> float:
    """Config value as a float, falling back to the frozen default."""
    default = DEFAULTS[key]
    raw = (config or {}).get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _seven_key(value: Optional[float]) -> float:
    """Sort key for the 7d tie-break; an unknown 7d sorts last."""
    if value is None:
        return float("inf")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _by_label(usages: Optional[List[Usage]]) -> Dict[str, Usage]:
    out = {}  # type: Dict[str, Usage]
    for usage in usages or []:
        if usage is not None and usage.label:
            out[usage.label] = usage
    return out


def _copy_state(state: Optional[Dict]) -> Dict:
    """A copy safe to mutate: top level, `parked` and the failure counters are new."""
    new = dict(state or {})
    parked = new.get("parked") or {}
    fresh = {}  # type: Dict[str, Dict]
    for label, entry in parked.items():
        fresh[label] = dict(entry) if isinstance(entry, dict) else {}
    new["parked"] = fresh
    failures = new.get(PROBE_FAILURES_KEY)
    new[PROBE_FAILURES_KEY] = dict(failures) if isinstance(failures, dict) else {}
    stamps = new.get(LAST_FAILURE_KEY)
    new[LAST_FAILURE_KEY] = dict(stamps) if isinstance(stamps, dict) else {}
    return new


def _fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "?"
    return "%.0f%%" % value


def _short(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > _MAX_REASON_DETAIL:
        return text[:_MAX_REASON_DETAIL] + "..."
    return text


# ---------------------------------------------------------------------------
# parking (hysteresis)
# ---------------------------------------------------------------------------


def is_parked(
    label: str,
    state: Optional[Dict],
    usages_by_label: Optional[Dict[str, Usage]],
    config: Optional[Dict],
) -> bool:
    """True while `label` is parked and has NOT yet cooled down.

    A token is parked when it triggered a switch. It stays parked until its own
    reading proves it recovered: 5h below `recover_below_5h` AND 7d below
    `recover_below_7d`. Without a fresh, successful reading we cannot prove
    recovery, so the token stays parked (fail closed).
    """
    entry = (state or {}).get("parked") or {}
    record = entry.get(label)
    if record is None:
        return False

    usage = (usages_by_label or {}).get(label)
    if usage is None or not usage.ok or usage.five_h is None:
        return True
    if usage.status == "rejected":
        return True

    # A recovery threshold recorded at park time wins over the live config so a
    # mid-flight config edit cannot un-park a still-hot token; park() itself
    # writes no threshold, so in practice these come from config.
    if not isinstance(record, dict):
        record = {}
    below_5h = record.get("until_5h")
    below_7d = record.get("until_7d")
    below_5h = _cfg(config, "recover_below_5h") if below_5h is None else float(below_5h)
    below_7d = _cfg(config, "recover_below_7d") if below_7d is None else float(below_7d)

    if usage.five_h >= below_5h:
        return True
    # An unknown 7d reading must not trap a token forever; the 5h window is the
    # binding constraint in practice.
    if usage.seven_d is not None and usage.seven_d >= below_7d:
        return True
    return False


def park(state: Optional[Dict], label: str, now: int) -> Dict:
    """Return a NEW state dict with `label` parked as of `now`."""
    new = _copy_state(state)
    new["parked"][label] = {"parked_at": _as_int(now)}
    return new


def unpark_recovered(
    state: Optional[Dict], usages: Optional[List[Usage]], config: Optional[Dict]
) -> Dict:
    """Return a NEW state dict with every recovered token removed from `parked`."""
    new = _copy_state(state)
    parked = new["parked"]
    if not parked:
        return new
    usages_by_label = _by_label(usages)
    for label in list(parked.keys()):
        if not is_parked(label, state, usages_by_label, config):
            del parked[label]
    return new


# ---------------------------------------------------------------------------
# consecutive probe failures (de-bounce)
# ---------------------------------------------------------------------------


def consecutive_failures(state: Optional[Dict], label: str) -> int:
    """How many consecutive failed probes `label` has accumulated (0 when clean)."""
    counters = (state or {}).get(PROBE_FAILURES_KEY)
    if not isinstance(counters, dict):
        return 0
    return max(0, _as_int(counters.get(label), 0))


def record_probe_results(
    state: Optional[Dict],
    usages: Optional[List[Usage]],
    now: Optional[int] = None,
) -> Dict:
    """Return a NEW state dict with the failure counters folded in.

    Per label seen in `usages`:

    * a SUCCESS resets the count to 0 and forgets the timestamp (including a
      success that reports `rejected` — that is a measurement, not a failure);
    * a TRANSPORT failure changes nothing at all. curl never got an HTTP status,
      so the API said nothing about this token and there is no evidence to
      record. It neither accumulates nor clears an existing count (v1.1);
    * any other failure increments, unless more than `FAILURE_GAP_RESET_S`
      passed since the last one, in which case the count restarts at 1.

    Labels absent from `usages` keep whatever they had, so a token that is not
    probed this tick neither accumulates nor forgets.

    Call this BEFORE `decide()` so the count includes the current tick.
    """
    new = _copy_state(state)
    counters = new[PROBE_FAILURES_KEY]
    stamps = new[LAST_FAILURE_KEY]
    moment = _as_int(now, 0)
    for usage in usages or []:
        if usage is None or not usage.label:
            continue
        label = usage.label
        if usage.ok:
            counters[label] = 0
            stamps.pop(label, None)
            continue
        if usage.failure_kind == FAILURE_TRANSPORT:
            continue  # not evidence about the token; see model.FAILURE_TRANSPORT
        previous = max(0, _as_int(counters.get(label), 0))
        last_at = _as_int(stamps.get(label), 0)
        stale = bool(moment and last_at and (moment - last_at) > FAILURE_GAP_RESET_S)
        counters[label] = 1 if (stale or previous <= 0) else previous + 1
        if moment:
            stamps[label] = moment
    return new


# ---------------------------------------------------------------------------
# candidate selection
# ---------------------------------------------------------------------------


def is_eligible_target(
    usage: Optional[Usage], config: Optional[Dict], relax: bool = False
) -> bool:
    """RULING 5 — may `usage` be switched TO at all?

    `relax=True` drops ONLY the `switch_at_*` thresholds, for the one case where
    they invert the ruling's own purpose: the active token is already spent, so
    there is no "stay put" option to protect. See `choose_best`.

    > A token is only an eligible target if `five_h < switch_at_5h` AND
    > `seven_d < switch_at_7d` (treat an unknown `seven_d` as eligible, an
    > unknown `five_h` as not). A token that is already in switch territory is
    > not a refuge.

    Measured motivation (Fable, verify:B-brain finding 1): active `a`
    (5h 3%, 7d 96%) with candidate `b` (5h 99%, 7d 10%) returned `switch -> b`
    — ctr pointed the shell at the token with 1% of its hourly window left and
    parked `a`, which could not unpark, so the next tick had nowhere to go at
    all. One bad switch stranded the fleet. Round 1 put this filter in
    `decide()` as a 7d-only gate, which left every other path — a failed probe,
    a `rejected` active, no active at all, and `ctr next` / `ctr status`, both
    of which call `choose_best` DIRECTLY — with no eligibility check. It
    belongs here, where every caller gets it.

    `EXHAUSTED_PCT` stays as a floor so a config that raises `switch_at_*`
    above 100 still cannot select a spent token.
    """
    if usage is None or not usage.usable:  # probe failed, or status "rejected"
        return False
    if usage.error:
        # A reading that is `ok` and still carries text is a SUSPECT one. Today
        # that is exactly RULING 8.2's clamp note, and clamping alone did not
        # close the hole the ruling described: clamping -5 to 0.0 leaves it the
        # LOWEST legal value, so the hostile reading still sorted first and ctr
        # still selected the most broken token (measured against the real
        # modules, in both list orders). A number we had to invent is not
        # evidence of headroom, so it cannot buy a switch. Note the asymmetry:
        # this only refuses a token as a TARGET. A suspect ACTIVE token is left
        # where it is rather than being chased off on a reading we distrust.
        return False
    if usage.five_h is None:  # unknown 5h: not eligible, per the ruling
        return False
    five_h = float(usage.five_h)
    if five_h >= EXHAUSTED_PCT:
        return False
    if not relax and five_h >= _cfg(config, "switch_at_5h"):
        return False
    if usage.seven_d is None:  # unknown 7d: eligible, per the ruling
        return True
    seven_d = float(usage.seven_d)
    if seven_d >= EXHAUSTED_PCT:
        return False
    return relax or seven_d < _cfg(config, "switch_at_7d")


def choose_best(
    usages: Optional[List[Usage]],
    exclude: Optional[Iterable[str]],
    config: Optional[Dict],
    state: Optional[Dict],
    now: int,
    relax: bool = False,
) -> Optional[str]:
    """Label of the token with the most headroom, or None.

    `now` is accepted for signature symmetry with the rest of the module;
    selection itself is time-independent (parking is decided by readings).

    `relax=True` keeps every safety check but drops the `switch_at_*` ceilings,
    so a token that is merely HOT still counts as a refuge. `decide()` uses it
    only as a second pass, and only when the active token is already spent.
    """
    excluded = set(exclude or ())
    usages_by_label = _by_label(usages)
    candidates = []  # type: List[Usage]
    for usage in usages or []:
        if usage is None or not usage.label or usage.label in excluded:
            continue
        if not is_eligible_target(usage, config, relax):
            continue
        if is_parked(usage.label, state, usages_by_label, config):
            continue
        candidates.append(usage)

    if not candidates:
        return None
    candidates.sort(key=lambda u: (float(u.five_h), _seven_key(u.seven_d), u.label))
    return candidates[0].label


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def _evaluate_trigger(
    active: str,
    usage: Optional[Usage],
    switch_at_5h: float,
    switch_at_7d: float,
    failures: int = 0,
):
    """(triggered, reason, window) for the active token.

    `window` is "5h" or "7d" when a utilisation threshold was crossed, and ""
    when the token is simply unusable (no reading / failed probe / rejected) —
    in which case no improvement margin is required to move off it.

    `failures` is how many CONSECUTIVE failed probes this token has, the
    current one included (see `record_probe_results`). A failed probe only
    counts as a trigger at `MIN_CONSECUTIVE_FAILURES`; below that we hold and
    let the next tick decide. A *measured* `rejected` status is not a probe
    failure and still triggers immediately.

    A missing reading (`usage is None`) is not a probe failure either: the
    token was never probed this tick — a dangling `active` pointer naming a
    label that is not in the registry at all — so waiting for a second one
    would mean waiting forever.

    A label that IS registered but has no keychain item is the same situation
    wearing a probe failure's clothes: `usage.probe_all` returns one Usage per
    RECORD, so it hands back `Usage.failed(label, NO_TOKEN_ERROR)` without
    having sent anything. Nothing was measured and a second tick cannot measure
    it either, so de-bouncing it only delays the move by a whole interval. It
    triggers immediately.
    """
    if usage is None:
        return True, "no usage reading for active '%s'" % active, ""
    if not usage.ok and usage.error == NO_TOKEN_ERROR:
        return True, "active '%s' has no token in the keychain" % active, ""
    if not usage.ok and usage.failure_kind == FAILURE_TRANSPORT:
        # We never reached the API, so we learned nothing about this token and
        # have no reason to move off it. Switching here would abandon a healthy
        # token, park it for a full cooldown, and land on a token the same dead
        # network would fail identically. `triggered` stays True only so
        # monitor.py writes the one log line; it never reaches notify() or a
        # switch. (v1.1, measured 2026-09-16.)
        return (
            False,
            "network: active '%s' unreachable, holding: %s" % (active, _short(usage.error)),
            "",
        )
    if not usage.ok:
        # max(1, ...) keeps the message honest even if a caller forgot to fold
        # this tick in: the probe in hand failed, so at least one has.
        seen = max(1, _as_int(failures, 1))
        detail = _short(usage.error)
        if seen < MIN_CONSECUTIVE_FAILURES:
            reason = "active '%s' probe failed (%d/%d consecutive), holding: %s" % (
                active,
                seen,
                MIN_CONSECUTIVE_FAILURES,
                detail,
            )
            return False, reason, ""
        reason = "active '%s' probe failed %d ticks in a row: %s" % (active, seen, detail)
        return True, reason, ""
    if usage.status == "rejected":
        return True, "active '%s' is rate-limited (rejected)" % active, ""
    if usage.five_h is not None and usage.five_h >= switch_at_5h:
        reason = "active '%s' 5h %s >= %s" % (
            active,
            _fmt_pct(usage.five_h),
            _fmt_pct(switch_at_5h),
        )
        return True, reason, "5h"
    if usage.seven_d is not None and usage.seven_d >= switch_at_7d:
        reason = "active '%s' 7d %s >= %s" % (
            active,
            _fmt_pct(usage.seven_d),
            _fmt_pct(switch_at_7d),
        )
        return True, reason, "7d"
    reason = "active '%s' 5h %s / 7d %s below thresholds" % (
        active,
        _fmt_pct(usage.five_h),
        _fmt_pct(usage.seven_d),
    )
    return False, reason, ""


def _improvement(active_usage: Usage, candidate: Usage, window: str):
    """(points gained, window actually compared) for the triggering window.

    CONTRACT NOTE. lane-context.md words the anti-flap margin as "the
    candidate's five_h is at least min_improvement points below the active's".
    Applied literally that makes `switch_at_7d` dead: a weekly-exhausted
    account usually has a COLD 5h window (5h resets every five hours, 7d
    weekly), so 7d can trigger while the 5h comparison can never clear the
    margin (measured: active 5h 3%/7d 96% vs candidate 5h 20%/7d 20% ->
    5h improvement -17, 7d improvement +76). ctr therefore measures the margin
    on the window that triggered. Every 5h-triggered switch — every case the
    contract enumerates — behaves exactly as written.
    """
    if window == "7d" and active_usage.seven_d is not None and candidate.seven_d is not None:
        return float(active_usage.seven_d) - float(candidate.seven_d), "7d"
    return float(active_usage.five_h) - float(candidate.five_h), "5h"


def _decide_no_active(
    usages: Optional[List[Usage]], config: Optional[Dict], state: Optional[Dict], now: int
) -> Decision:
    """No token is active yet: propose one, but never claim a threshold fired."""
    target = choose_best(usages, (), config, state, now)
    if target is None:
        return Decision("no_active", None, "no active token and no usable candidate", False)
    return Decision(
        "no_active", target, "no active token; '%s' has the most headroom" % target, False
    )


def _blocked_by_gates(
    active_usage: Optional[Usage],
    candidate: Usage,
    window: str,
    reason: str,
    state: Optional[Dict],
    config: Optional[Dict],
    now: int,
) -> Optional[Decision]:
    """The two anti-flap gates. Returns a hold Decision, or None when clear."""
    min_interval = int(_cfg(config, "min_switch_interval_s"))
    last_switch_at = _as_int((state or {}).get("last_switch_at"), 0)
    elapsed = now - last_switch_at
    if last_switch_at > 0 and elapsed < min_interval:
        return Decision(
            "hold",
            None,
            "%s; last switch %ds ago, waiting %ds (anti-flap)" % (reason, elapsed, min_interval),
            True,
        )

    # There is no 5h-headroom gate here any more: `choose_best` now applies
    # RULING 5's eligibility filter to EVERY candidate it returns, on every
    # path, so a candidate that reaches this point is already below both switch
    # thresholds. The old 7d-only gate could not fire without contradicting it.

    # An unusable active (failed probe / rejected) needs no margin: anything
    # that still works beats it.
    if active_usage is not None and active_usage.usable:
        min_improvement = _cfg(config, "min_improvement")
        improvement, compared = _improvement(active_usage, candidate, window)
        if improvement < min_improvement:
            return Decision(
                "hold",
                None,
                "%s; best candidate '%s' only %.0f %s points better, need %.0f (min_improvement)"
                % (reason, candidate.label, improvement, compared, min_improvement),
                True,
            )
    return None


def _active_is_spent(active_usage: Optional[Usage]) -> bool:
    """True when staying on the active token buys nothing at all.

    Deliberately narrow: a MEASURED exhaustion (`status == "rejected"`) or a
    label with no keychain item. A merely-failed probe does NOT qualify — a 429
    proves nothing (an unauthenticated request returns one too), so it must not
    unlock the relaxed pass.
    """
    if active_usage is None:
        return False
    if active_usage.status == "rejected":
        return True
    return active_usage.error == NO_TOKEN_ERROR


def decide(
    active: Optional[str],
    usages: Optional[List[Usage]],
    state: Optional[Dict],
    config: Optional[Dict],
    now: int,
) -> Decision:
    """What the monitor should do this tick. Pure."""
    now = _as_int(now)
    if not active:
        return _decide_no_active(usages, config, state, now)

    usages_by_label = _by_label(usages)
    active_usage = usages_by_label.get(active)
    triggered, reason, window = _evaluate_trigger(
        active,
        active_usage,
        _cfg(config, "switch_at_5h"),
        _cfg(config, "switch_at_7d"),
        consecutive_failures(state, active),
    )
    if not triggered:
        # A de-bounced probe failure is worth the one log line monitor.py emits
        # for a triggered hold; a healthy active token is not.
        debounced = active_usage is not None and not active_usage.ok
        return Decision("hold", None, reason, debounced)

    target = choose_best(usages, (active,), config, state, now)
    relaxed = False
    if target is None and _active_is_spent(active_usage):
        # RULING 5 REFINEMENT (measured by me against the real module after the
        # round-2 gate flagged it): the eligibility filter exists so we never
        # abandon a good active token for a nearly-spent one. When the ACTIVE
        # token is itself spent there is no good token to protect, and refusing
        # a refuge at 5h 90% leaves you on a DEAD token while one with 10% of
        # its hour left sits unused — strictly worse than switching. So when the
        # active is rejected, or has no keychain item at all, take a second pass
        # with the ceilings dropped. Every other guard still applies: parked,
        # excluded, suspect and >= 100% tokens stay refused.
        target = choose_best(usages, (active,), config, state, now, relax=True)
        relaxed = target is not None
    if target is None:
        return Decision(
            "no_candidate", None, "%s; no usable token with headroom left" % reason, True
        )

    candidate = usages_by_label[target]
    if relaxed:
        return Decision(
            "switch",
            target,
            "%s; no token is below the thresholds, so switching to the least-used "
            "one, '%s' (5h %s, 7d %s)"
            % (reason, target, _fmt_pct(candidate.five_h), _fmt_pct(candidate.seven_d)),
            True,
        )
    blocked = _blocked_by_gates(active_usage, candidate, window, reason, state, config, now)
    if blocked is not None:
        return blocked

    return Decision(
        "switch",
        target,
        "%s; switching to '%s' (5h %s, 7d %s)"
        % (reason, target, _fmt_pct(candidate.five_h), _fmt_pct(candidate.seven_d)),
        True,
    )


__all__ = [
    "EXHAUSTED_PCT",
    "MIN_CONSECUTIVE_FAILURES",
    "PROBE_FAILURES_KEY",
    "choose_best",
    "consecutive_failures",
    "decide",
    "is_eligible_target",
    "is_parked",
    "park",
    "record_probe_results",
    "unpark_recovered",
]
