"""The ctr monitor loop: probe every token, decide, switch, log, notify.

`tick()` is the whole pass and it must never raise out of the loop — a monitor
that dies on a transient error is worse than useless. Every collaborating
module (keychain, usage, shell, rollover) is imported lazily INSIDE the
functions so this module still imports on a partially built tree.

No token value ever reaches this module's log lines: only labels and
percentages are logged.
"""

import os
import stat
import time
from datetime import datetime
from typing import Dict, List, Optional

from ctr import selector
from ctr.model import LOG_FILE, Decision, Usage, merged_config, pct
from ctr.notify import notify

#: Cap a single log line so a pathological error string cannot flood the file.
MAX_LOG_CHARS = 500
#: Never spin faster than this in the foreground loop.
MIN_INTERVAL_S = 30


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _one_line(message: str) -> str:
    text = " ".join(str(message or "").split())
    if len(text) > MAX_LOG_CHARS:
        text = text[: MAX_LOG_CHARS - 3] + "..."
    return text


def _tighten(handle: int) -> None:
    """Force an ALREADY EXISTING log file to 0600.

    `os.open(..., 0o600)` only applies its mode when it creates the file, and
    launchd with `RunAtLoad` opens `StandardOutPath` itself at the default
    umask before ctr's own code ever runs — so the log can already exist as
    0644 (world readable) the first time `log_line` appends to it. It carries
    no secrets by design, but it does carry account labels and utilisation, so
    tighten it. `fchmod` on the open descriptor cannot race a swap of the path.
    """
    try:
        if stat.S_IMODE(os.fstat(handle).st_mode) != 0o600:
            os.fchmod(handle, 0o600)
    except OSError:
        # Not ours to chmod (a shared or foreign-owned file). The line is
        # already written; there is nowhere left to report this.
        pass


def log_line(message: str, log_path: Optional[str] = None) -> None:
    """Append an ISO timestamp + message to the ctr log (kept 0600)."""
    path = os.path.expanduser(log_path or LOG_FILE)
    try:
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        handle = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(handle, ("%s %s\n" % (_timestamp(), _one_line(message))).encode("utf-8"))
            _tighten(handle)
        finally:
            os.close(handle)
    except Exception:
        # Logging is the last resort; there is nowhere left to report a failure.
        pass


# ---------------------------------------------------------------------------
# one pass
# ---------------------------------------------------------------------------


def _safe(exc: BaseException) -> str:
    """The only form of an exception this module ever writes down.

    An exception raised deeper in the stack can carry arbitrary text, and a
    caller one mistake away from putting a bearer token in a message would put
    it straight into ~/Library/Logs/ctr.log (persistent, and read by a human
    who pastes it into a bug report). The class name is enough to diagnose with
    and cannot carry a secret.
    """
    return exc.__class__.__name__


def _read_config(store) -> Dict:
    try:
        return merged_config(store.config())
    except Exception as exc:
        log_line("config unreadable (%s); using defaults" % exc.__class__.__name__)
        return merged_config({})


def _collect_tokens(records) -> Dict[str, str]:
    """label -> secret, straight from the keychain. Secrets never leave here."""
    from ctr import keychain

    tokens = {}  # type: Dict[str, str]
    for record in records:
        try:
            secret = keychain.token_for(record.label)
        except Exception as exc:
            log_line("keychain read failed for '%s' (%s)" % (record.label, exc.__class__.__name__))
            continue
        if secret:
            tokens[record.label] = secret
        else:
            log_line("no keychain item for '%s' — run `ctr add %s`" % (record.label, record.label))
    return tokens


def _prefers(store, records) -> Dict[str, str]:
    """label -> the probe strategy that last worked for it.

    Without this every long-lived `oat` token pays two HTTP requests per tick:
    a 403 on /api/oauth/usage, then the header probe that actually works.
    """
    prefers = {}  # type: Dict[str, str]
    lookup = getattr(store, "probe_strategy", None)
    if lookup is None:
        return prefers
    for record in records:
        try:
            strategy = lookup(record.label)
        except Exception as exc:
            log_line("probe strategy lookup failed for '%s' (%s)" % (record.label, _safe(exc)))
            continue
        if strategy:
            prefers[record.label] = strategy
    return prefers


def _probe(records, tokens: Dict[str, str], config: Dict, prefers=None) -> List[Usage]:
    from ctr import usage as usage_module

    return usage_module.probe_all(
        records,
        tokens,
        timeout_s=int(config.get("http_timeout_s", 20)),
        prefers=prefers or {},
    )


def _cache(store, usages: List[Usage]) -> None:
    for usage in usages:
        if not usage.ok:
            continue
        try:
            store.cache_put(usage)
        except Exception as exc:
            log_line("cache write failed for '%s' (%s)" % (usage.label, exc.__class__.__name__))


def _apply_switch(store, state: Dict, active: Optional[str], target: str, now: int) -> bool:
    """Point the shell at `target` and record it. True when it took effect."""
    from ctr import shell

    try:
        shell.write_active(target)
    except Exception as exc:
        log_line("switch to '%s' aborted: could not write active.sh (%s)" % (target, _safe(exc)))
        return False
    try:
        store.set_active(target)
    except Exception as exc:
        # A switch that did not persist is NOT a switch. Reporting True here
        # logged and notified a rotation that never happened and stamped
        # last_switch_at, which then suppressed the real switch for a whole
        # min_switch_interval_s. active.sh is already pointing at `target`, so
        # the next tick simply rewrites it and retries the registry.
        log_line(
            "switch to '%s' NOT recorded: active.sh written but the registry "
            "update failed (%s) — will retry next tick" % (target, _safe(exc))
        )
        return False
    if active:
        state.update(selector.park(state, active, now))  # park() returns a new dict
    state["last_switch_at"] = int(now)
    return True


def _run_rollover() -> str:
    from ctr import rollover

    result = rollover.run(dry_run=False)
    return "rollover: %d acted, %d skipped" % (
        len(result.get("acted") or []),
        len(result.get("skipped") or []),
    )


def _tick_inner(store, now: int, apply: bool, auto_rollover: bool) -> Dict:
    config = _read_config(store)
    records = store.tokens()
    tokens = _collect_tokens(records)
    usages = _probe(records, tokens, config, _prefers(store, records))
    _cache(store, usages)

    try:
        active = store.active()
    except Exception:
        active = None
    state = selector.unpark_recovered(store.state(), usages, config)
    # Fold this tick's probe results into the consecutive-failure counters
    # BEFORE deciding: decide() reads them to de-bounce a failed probe, and
    # they are persisted by the save_state below.
    state = selector.record_probe_results(state, usages, now)
    decision = selector.decide(active, usages, state, config, now)

    switched = False
    # "no_active" carries a proposal too: without this the monitor logs the same
    # line every tick forever after `ctr remove` of the active token.
    if decision.action in ("switch", "no_active") and decision.target and apply:
        switched = _apply_switch(store, state, active, decision.target, now)

    try:
        store.save_state(state)
    except Exception as exc:
        log_line("state save failed (%s)" % exc.__class__.__name__)

    if switched:
        _announce_switch(active, decision, usages, auto_rollover)
    elif decision.action in ("switch", "no_active") and decision.target and not apply:
        log_line("would switch to '%s' (apply=False) — %s" % (decision.target, decision.reason))
    elif decision.triggered or decision.action != "hold":
        log_line("%s — %s" % (decision.action, decision.reason))

    return {"decision": decision, "usages": usages, "switched": switched}


def _announce_switch(
    previous: Optional[str], decision: Decision, usages: List[Usage], auto_rollover: bool
) -> None:
    log_line("switched %s -> %s — %s" % (previous or "(none)", decision.target, decision.reason))
    tail = "Run `ctr rollover` to resume parked sessions."
    if auto_rollover:
        try:
            tail = _run_rollover()
        except Exception as exc:
            tail = "auto-rollover failed (%s)" % exc.__class__.__name__
        log_line(tail)
    summary = ", ".join(
        "%s %s" % (usage.label, pct(usage.five_h)) for usage in usages if usage.ok
    )
    notify("ctr: now using %s" % decision.target, "%s. %s" % (summary or decision.reason, tail))


def tick(store, now: int, apply: bool = True, auto_rollover: bool = False) -> Dict:
    """One monitor pass. Never raises."""
    try:
        return _tick_inner(store, int(now), apply, auto_rollover)
    except Exception as exc:
        reason = "monitor tick failed: %s" % _safe(exc)
        log_line(reason)
        return {
            "decision": Decision("no_candidate", None, reason, False),
            "usages": [],
            "switched": False,
            "error": reason,
        }


def run(store, once: bool = False, interval_s: int = 300, auto_rollover: bool = False) -> int:
    """Foreground monitor. Returns a process exit code."""
    interval = max(MIN_INTERVAL_S, int(interval_s))
    while True:
        result = tick(store, int(time.time()), apply=True, auto_rollover=auto_rollover)
        if once:
            return 1 if result.get("error") else 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log_line("monitor stopped")
            return 0


__all__ = ["tick", "run", "log_line"]
