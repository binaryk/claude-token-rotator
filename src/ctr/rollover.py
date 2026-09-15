"""herdr live-session rollover: exit-and-resume panes parked on a usage limit.

A running `claude` keeps its OAuth token in memory, so switching ctr's active
token does nothing for sessions that are already up. The only proven recovery
(measured 2026-09-10) is exit-and-resume, which is what this module automates.

Everything defaults to ``dry_run=True``. ``run(dry_run=True)`` executes NO herdr
command against a pane at all: it reads state, plans, and reports the exact
commands it *would* run.

Viewport text is read only to look for a usage-limit marker and is then
discarded -- it never reaches a result dict or a log, because a pane's visible
text could contain anything, a secret someone echoed included.

Python 3.8 compatible. stdlib only.
"""

import json
import os
import re
import shlex
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from ctr.model import Pane, RolloverPlan

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HERDR_BIN = "herdr"
HERDR_PANE_ENV = "HERDR_PANE_ID"
CLAUDE_AGENT = "claude"
CLAUDE_PROCESS = "claude"
STATUS_WORKING = "working"
EXIT_PROMPT = "/exit"

#: Default subprocess timeout for a read-only herdr call.
DEFAULT_TIMEOUT_S = 30
#: `herdr agent start --timeout` value, milliseconds (from the proven recipe).
START_TIMEOUT_MS = 150000
#: Subprocess timeout for `agent start`; must exceed START_TIMEOUT_MS.
START_SUBPROCESS_TIMEOUT_S = 180
#: MCP children take up to ~20 s to die after /exit, so poll generously.
WAIT_TIMEOUT_S = 45
WAIT_POLL_S = 1.5
#: Send the one-shot ctrl+c fallback after this fraction of the wait budget.
FALLBACK_AFTER_FRACTION = 0.5
VIEWPORT_LINES = 12
#: Name given to a pane that had none, so `herdr agent start` has an argument.
FALLBACK_NAME_PREFIX = "ctr-"
#: A `herdr agent start` reporting a timeout still worked: a resumed session
#: that auto-continues goes straight to `working`. Success-pending, not failure.
#: herdr's exact wording is unmeasured, so accept both spellings.
START_TIMEOUT_MARKERS = ("timeout", "timed out")

#: `_run` returns this when IT killed the subprocess. That is OUR timeout, not
#: herdr's, and it means the opposite thing: claude was already /exit-ed and we
#: have no evidence it ever came back. Never read it as success-pending.
TRANSPORT_TIMEOUT_RC = 124

#: Places the word "timeout" appears without herdr reporting one: our own
#: `--timeout 150000` argument echoed back in a usage message or a request dump.
_TIMEOUT_NOISE_RE = re.compile(r'--timeout(?:[= ]\S+)?|"timeout"\s*:\s*\d+|\btimeout_ms\b', re.I)

#: Output that proves `agent start` never ran at all, so a "timeout" in it is
#: an echo of our own argv rather than a result.
START_REJECTED_MARKERS = (
    "usage:", "unexpected argument", "unrecognized", "unknown argument",
    "no such", "is busy",
)

#: herdr verbs that change a pane. A read-only path reaching one is a bug.
MUTATING_HERDR_VERBS = (
    "send-keys", "prompt", "start", "rename", "stop",
    "kill", "restart", "close", "new", "split", "delete",
)

#: The two markers a parked session shows (measured). Nothing else counts.
LIMIT_MARKERS = ("usage limit reached", "/low-priority to continue now")

BOSS_RE = re.compile(r"boss", re.IGNORECASE)

#: tab_label of a pane whose tab we could NOT find in `herdr tab list`. It is a
#: sentinel, never a real label: a pane carrying it is refused, because an
#: unidentifiable tab could be the BOSS tab. `Pane.tab_label` is a plain str in
#: the frozen model, so "unknown" has to travel as a value no tab can hold.
UNKNOWN_TAB_LABEL = "\x00ctr:unknown-tab"

REASON_OWN_PANE = "caller's own pane"
REASON_BOSS_TAB = "BOSS tab"
REASON_WORKING = "agent is working"
REASON_NOT_CLAUDE = "not a claude agent"
REASON_NO_SESSION = "no session id - cannot --resume"
REASON_UNKNOWN_TAB = "tab label unknown - cannot rule out a BOSS tab"
REASON_NO_MARKER = "no usage-limit marker in viewport"
#: Raised by the live path only, never by plan(): the pane WAS parked when the
#: plan was made and is not any more. A parked session resumes BY ITSELF - the
#: marker literally says "continuing automatically at <time>" - and run() acts
#: sequentially at up to 180 s per pane, so a pane's turn can come minutes
#: after its viewport was sampled. RULING 8.4 authorises the extra `agent read`
#: this costs: it is read-only and cheap, and /exit-ing a session that is now
#: mid-turn is not.
REASON_MARKER_GONE = ("usage-limit marker gone from the viewport - the session "
                      "resumed itself since the plan was made")
#: An unreadable viewport also refuses the pane - failing closed costs one
#: missed rollover, failing open costs someone's turn - but it is NOT evidence
#: that the session resumed. `viewport()` returns "" for any non-zero rc: a
#: transport timeout, herdr restarting, "no such pane". Reporting those as
#: REASON_MARKER_GONE told the operator a stuck session had recovered by itself
#: when it is in fact still parked and untouched, which is a causal claim
#: nothing established. Same refusal, honest reason.
REASON_VIEWPORT_UNREADABLE = ("could not read the viewport to confirm the pane is "
                              "still parked - left alone")
REASON_ACT = "usage limit reached"

#: Every prefix `plan()` may use for a refusal. Tests and callers match these.
REFUSAL_REASONS = (
    REASON_OWN_PANE, REASON_BOSS_TAB, REASON_UNKNOWN_TAB, REASON_WORKING,
    REASON_NOT_CLAUDE, REASON_NO_SESSION, REASON_NO_MARKER,
)


class RolloverSafetyError(RuntimeError):
    """Raised when a read-only code path tries to mutate a pane."""


# ---------------------------------------------------------------------------
# Subprocess / clock indirection (tests replace these three)
# ---------------------------------------------------------------------------


def _run(cmd: List[str], timeout_s: int = DEFAULT_TIMEOUT_S) -> Tuple[int, str, str]:
    """Run a command and capture its output. Never raises."""
    try:
        proc = subprocess.run(
            list(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s
        )
    except FileNotFoundError:
        return (127, "", "command not found: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        return (124, "", "timed out after %ss" % timeout_s)
    except OSError as exc:
        return (1, "", "%s: %s" % (type(exc).__name__, exc))
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return (proc.returncode, out, err)


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# herdr invocation
# ---------------------------------------------------------------------------


def _is_mutating(args: List[str]) -> bool:
    """True when the noun/verb pair names a command that changes a pane."""
    return any(str(part) in MUTATING_HERDR_VERBS for part in list(args)[:2])


def _fmt(args: List[str]) -> str:
    """The exact shell form of a herdr call, for dry-run output and logs."""
    return " ".join([HERDR_BIN] + [shlex.quote(str(part)) for part in args])


def _herdr(args, mutating=False, timeout_s=DEFAULT_TIMEOUT_S):
    # type: (List[str], bool, int) -> Tuple[int, str, str]
    """Run `herdr <args>`. Refuses a mutating verb unless explicitly allowed."""
    if _is_mutating(args) and not mutating:
        raise RolloverSafetyError(
            "refusing a mutating herdr command from a read-only path: %s" % _fmt(args)
        )
    return _run([HERDR_BIN] + [str(part) for part in args], timeout_s=timeout_s)


def herdr_json(args: List[str]) -> Optional[dict]:
    """Run a read-only herdr command and parse its JSON. None on any failure.

    herdr 0.8.2 answers most commands with one JSON object; `tab list` answered
    with a bare array in an earlier build, so callers tolerate a list too.
    """
    returncode, out, _err = _herdr(list(args))
    if returncode != 0 and not out.strip():
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def _brief(text: str, limit: int = 200) -> str:
    """First meaningful line of a command's output, truncated."""
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


# ---------------------------------------------------------------------------
# Pure parsers for herdr JSON
# ---------------------------------------------------------------------------


def _unwrap(data, key: str):
    """herdr payloads come as a bare array, {"result": [...]}, {"result":
    {"<key>": [...]}} or {"<key>": [...]}. Return the array, or None."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get(key)
    return data.get(key)


def _parse_agent_list(data) -> List[Dict]:
    """Extract the agents array from `herdr agent list` output. Pure."""
    agents = _unwrap(data, "agents")
    if not isinstance(agents, list):
        return []
    return [agent for agent in agents if isinstance(agent, dict)]


def _parse_tab_labels(data) -> Dict[str, str]:
    """tab_id -> label from `herdr tab list`. Pure. Handles both shapes."""
    tabs = _unwrap(data, "tabs")
    if not isinstance(tabs, list):
        return {}
    labels = {}
    for tab in tabs:
        if isinstance(tab, dict) and isinstance(tab.get("tab_id"), str) and tab["tab_id"]:
            labels[tab["tab_id"]] = tab.get("label") or ""
    return labels


def _session_value(agent: Dict) -> str:
    session = agent.get("agent_session")
    return session.get("value") or "" if isinstance(session, dict) else ""


def _build_panes(agents: List[Dict], labels: Dict[str, str]) -> List[Pane]:
    """claude panes only, tab_label filled in. Pure."""
    panes = []
    for agent in agents:
        if (agent.get("agent") or "") != CLAUDE_AGENT or not agent.get("pane_id"):
            continue
        tab_id = agent.get("tab_id") or ""
        # Fail CLOSED. `labels.get(tab_id, "")` made an unrecognised `tab list`
        # payload - or a tab that simply is not in it - indistinguishable from a
        # tab with an empty label, and an empty label matches no BOSS pattern,
        # so ctr would have /exit-ed the operator's own BOSS session.
        tab_label = labels[tab_id] if tab_id in labels else UNKNOWN_TAB_LABEL
        panes.append(Pane(
            pane_id=agent["pane_id"],
            tab_id=tab_id,
            name=agent.get("name") or "",
            status=agent.get("agent_status") or "unknown",
            session_id=_session_value(agent),
            cwd=agent.get("cwd") or "",
            tab_label=tab_label,
        ))
    return panes


def _agent_get_status(data) -> str:
    """`agent_status` from `herdr agent get`. Pure. "" when absent."""
    result = data.get("result") if isinstance(data, dict) else None
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return ""
    return agent.get("agent_status") or ""


def _agent_get_fields(data) -> Tuple[str, str]:
    """(session_id, name) from `herdr agent get`. Pure. ("","") when absent."""
    result = data.get("result") if isinstance(data, dict) else None
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return ("", "")
    return (_session_value(agent), agent.get("name") or "")


def _is_claude_process(proc: Dict) -> bool:
    """True when a foreground process entry is the claude CLI itself.

    MEASURED: the native build reports name="2.1.267" while argv0="claude", so
    matching on `name` is wrong in both directions. Match argv0, else the first
    word of cmdline.
    """
    argv0 = (proc.get("argv0") or "").strip()
    if argv0 and os.path.basename(argv0) == CLAUDE_PROCESS:
        return True
    cmdline = (proc.get("cmdline") or "").strip()
    if not cmdline:
        return False
    return os.path.basename(cmdline.split()[0]) == CLAUDE_PROCESS


def _foreground_processes(data) -> Optional[List[Dict]]:
    """Foreground process dicts, or None when the payload is unrecognisable."""
    result = data.get("result") if isinstance(data, dict) else None
    info = result.get("process_info") if isinstance(result, dict) else None
    procs = info.get("foreground_processes") if isinstance(info, dict) else None
    if not isinstance(procs, list):
        return None
    return [proc for proc in procs if isinstance(proc, dict)]


def _shell_is_back(data) -> bool:
    """True only when we positively saw a foreground with no claude in it."""
    procs = _foreground_processes(data)
    if procs is None:
        return False
    return not any(_is_claude_process(proc) for proc in procs)


# ---------------------------------------------------------------------------
# Reading herdr state
# ---------------------------------------------------------------------------


def _list_panes_raw() -> Tuple[List[Pane], str]:
    """(claude panes, error). Refuses to report panes without tab labels."""
    agents_data = herdr_json(["agent", "list"])
    if agents_data is None:
        return ([], "herdr agent list failed - is herdr running?")
    tabs_data = herdr_json(["tab", "list"])
    if tabs_data is None:
        return ([], "herdr tab list failed - refusing to plan without tab labels "
                    "(BOSS panes could not be identified)")
    agents = _parse_agent_list(agents_data)
    panes = _build_panes(agents, _parse_tab_labels(tabs_data))
    unknown = [pane.pane_id for pane in panes if pane.tab_label == UNKNOWN_TAB_LABEL]
    if unknown:
        # Well-formed JSON in a shape the parser does not know looks exactly
        # like "this fleet has no tabs". Say so loudly instead of planning on it.
        return (panes, "could not resolve a tab label for %d pane(s) (%s) - they are "
                       "skipped because a BOSS tab cannot be ruled out; check "
                       "`herdr tab list`" % (len(unknown), ", ".join(sorted(unknown))))
    return (panes, "")


def list_panes() -> List[Pane]:
    """Every herdr pane hosting a claude agent, with its tab label."""
    return _list_panes_raw()[0]


def _viewport_args(pane_id: str, lines: int = VIEWPORT_LINES) -> List[str]:
    """The `herdr agent read` argv, shared by viewport(), the dry-run listing
    and the re-read the live path performs immediately before it acts."""
    return ["agent", "read", str(pane_id), "--source", "visible", "--lines",
            str(int(lines))]


def read_viewport(pane_id: str, lines: int = VIEWPORT_LINES) -> Optional[str]:
    """Visible text of a pane, or None when the READ ITSELF failed.

    The distinction matters only to the live path: an empty-but-readable pane
    is evidence (the marker is gone), an unreadable one is the absence of
    evidence. Never logged.
    """
    returncode, out, _err = _herdr(_viewport_args(pane_id, lines))
    return None if returncode != 0 else out


def viewport(pane_id: str, lines: int = VIEWPORT_LINES) -> str:
    """Visible text of a pane. "" when it cannot be read. Never logged."""
    text = read_viewport(pane_id, lines)
    return "" if text is None else text


def has_limit_marker(text: str) -> bool:
    """True only for a real parked-session marker. Pure.

    "Usage limit reached - continuing automatically at 4pm" -> True
    "/low-priority to continue now"                         -> True
    "the rate limit design doc"                             -> False
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in LIMIT_MARKERS)


# ---------------------------------------------------------------------------
# The safety predicate (PURE)
# ---------------------------------------------------------------------------


def _refusal(pane, self_pane, viewports):
    # type: (Pane, Optional[str], Dict[str, str]) -> Optional[str]
    """Why this pane must not be touched, or None when it may be."""
    if self_pane and pane.pane_id == self_pane:
        return "%s (%s=%s)" % (REASON_OWN_PANE, HERDR_PANE_ENV, pane.pane_id)
    if pane.tab_label == UNKNOWN_TAB_LABEL:
        return "%s (tab %s)" % (REASON_UNKNOWN_TAB, pane.tab_id or "?")
    if BOSS_RE.search(pane.tab_label or ""):
        return "%s (tab label %r)" % (REASON_BOSS_TAB, pane.tab_label)
    if (pane.status or "") == STATUS_WORKING:
        return "%s (status=%s)" % (REASON_WORKING, pane.status)
    # Pane has no `agent` field; list_panes() already drops codex agents. Re-check
    # defensively so a Pane-like object that does carry one is still refused.
    agent = getattr(pane, "agent", CLAUDE_AGENT)
    if agent != CLAUDE_AGENT:
        return "%s (agent=%s)" % (REASON_NOT_CLAUDE, agent)
    if not pane.session_id:
        return REASON_NO_SESSION
    if not has_limit_marker(viewports.get(pane.pane_id, "")):
        return REASON_NO_MARKER
    return None


def plan(panes, self_pane, viewports):
    # type: (List[Pane], Optional[str], Dict[str, str]) -> List[RolloverPlan]
    """One RolloverPlan per pane, in input order. PURE: no I/O, no mutation."""
    seen = viewports or {}
    plans = []
    for pane in panes:
        reason = _refusal(pane, self_pane, seen)
        if reason is None:
            reason = "%s - exit and --resume %s" % (REASON_ACT, pane.session_id[:8])
            plans.append(RolloverPlan(pane=pane, act=True, reason=reason))
        else:
            plans.append(RolloverPlan(pane=pane, act=False, reason=reason))
    return plans


# ---------------------------------------------------------------------------
# Acting on one pane
# ---------------------------------------------------------------------------


def wait_for_shell(pane_id, timeout_s=WAIT_TIMEOUT_S, poll_s=WAIT_POLL_S):
    # type: (str, int, float) -> bool
    """Poll until no foreground process in the pane is claude.

    MUTATING: sends `send-keys ctrl+c ctrl+c` once, after half the budget, when
    the session has not exited. Only call this from a real rollover.
    """
    start = _now()
    deadline = start + timeout_s
    fallback_at = start + timeout_s * FALLBACK_AFTER_FRACTION
    fallback_sent = False
    while True:
        if _shell_is_back(herdr_json(["pane", "process-info", "--pane", pane_id])):
            return True
        now = _now()
        if now >= deadline:
            return False
        if not fallback_sent and now >= fallback_at:
            fallback_sent = True
            _herdr(["agent", "send-keys", pane_id, "ctrl+c", "ctrl+c"], mutating=True)
        _sleep(poll_s)


def _fallback_name(pane: Pane) -> str:
    return FALLBACK_NAME_PREFIX + pane.pane_id.replace(":", "-")


def _start_args(pane: Pane, start_name: str, session_id: str) -> List[str]:
    """The `herdr agent start` argv. Shared by the dry-run and the live path."""
    return ["agent", "start", start_name, "--kind", "claude", "--pane", pane.pane_id,
            "--timeout", str(START_TIMEOUT_MS), "--", "--resume", session_id]


def _planned_commands(pane: Pane, start_name: str, session_id: str) -> List[List[str]]:
    """The exact herdr calls a rollover of this pane performs, in order."""
    commands = [
        ["agent", "get", pane.pane_id],
        _viewport_args(pane.pane_id),
        ["agent", "send-keys", pane.pane_id, "esc"],
        ["agent", "prompt", pane.pane_id, EXIT_PROMPT],
        ["pane", "process-info", "--pane", pane.pane_id],
        _start_args(pane, start_name, session_id),
    ]
    if pane.name:
        commands.append(["agent", "rename", pane.pane_id, pane.name])
    return commands


def _result(pane, ok, detail, steps, dry):
    # type: (Pane, bool, str, List[List[str]], bool) -> Dict
    return {"pane": pane.pane_id, "ok": ok, "detail": detail,
            "commands": [_fmt(step) for step in steps], "dry_run": dry}


def _herdr_reported_timeout(out: str, err: str) -> bool:
    """True only when herdr ITSELF reported a timeout. Pure.

    The word also arrives two other ways, and both mean the command failed:
    our own `--timeout 150000` echoed in a usage message, and a request dump
    like {"error":"pane is busy","request":{"timeout":150000}}. Strip those,
    then refuse any output that proves the command never ran.
    """
    blob = _TIMEOUT_NOISE_RE.sub(" ", ("%s\n%s" % (out or "", err or "")).lower())
    if any(marker in blob for marker in START_REJECTED_MARKERS):
        return False
    return any(marker in blob for marker in START_TIMEOUT_MARKERS)


def _start_outcome(returncode, out, err, timeout_s=START_SUBPROCESS_TIMEOUT_S):
    # type: (int, str, str, int) -> Tuple[bool, str]
    """Read `herdr agent start`. A timeout herdr reports is success-pending. Pure.

    A timeout WE impose is not. `_run` synthesises rc 124 with the text
    "timed out after 180s" when it kills herdr, which a plain substring search
    read as herdr's own success-pending timeout: a hung `agent start` was
    reported as a completed rollover while the pane sat /exit-ed and dead.
    """
    if returncode == TRANSPORT_TIMEOUT_RC:
        return (False, "herdr agent start did not return within %ss and was killed - "
                       "claude was already /exit-ed, so this pane is probably down; "
                       "check it and resume it by hand" % timeout_s)
    if _herdr_reported_timeout(out, err):
        return (True, "started; herdr reported timeout - the resumed session went "
                      "straight to working, treated as success-pending")
    if returncode == 0:
        return (True, "started and resumed")
    return (False, "herdr agent start failed (rc=%d): %s"
                   % (returncode, _brief(err or out)))


def _stop_claude(pane: Pane, steps: List[List[str]]) -> Optional[str]:
    """esc, /exit, wait for the shell. Returns an error, or None on success."""
    esc = ["agent", "send-keys", pane.pane_id, "esc"]
    steps.append(esc)
    returncode, out, err = _herdr(esc, mutating=True)
    if returncode != 0:
        return "send-keys esc failed: %s" % _brief(err or out)

    quit_cmd = ["agent", "prompt", pane.pane_id, EXIT_PROMPT]
    steps.append(quit_cmd)
    returncode, out, err = _herdr(quit_cmd, mutating=True)
    if returncode != 0:
        return "%s failed: %s" % (EXIT_PROMPT, _brief(err or out))

    steps.append(["pane", "process-info", "--pane", pane.pane_id])
    if not wait_for_shell(pane.pane_id):
        return "claude still in the foreground after %ss - pane left as it is" % WAIT_TIMEOUT_S
    return None


def _restore_name(pane: Pane, start_name: str, steps: List[List[str]]) -> str:
    """Rename the pane back to what it was. Returns a detail suffix."""
    if not pane.name:
        return "; pane was unnamed, it now carries %r" % start_name
    rename = ["agent", "rename", pane.pane_id, pane.name]
    steps.append(rename)
    returncode, out, err = _herdr(rename, mutating=True)
    if returncode != 0:
        return "; rename back to %r failed: %s" % (pane.name, _brief(err or out))
    return "; renamed back to %r" % pane.name


def _rollover_pane_live(pane: Pane) -> Dict:
    steps = [["agent", "get", pane.pane_id]]  # type: List[List[str]]
    # Capture the session id BEFORE exiting - it is gone once claude is down.
    live = herdr_json(steps[0])
    session_id, live_name = _agent_get_fields(live)
    # The plan was made against a SNAPSHOT, and panes are rolled over strictly
    # sequentially at up to 180 s each. A parked session resumes itself - the
    # marker literally says "continuing automatically at <time>" - so by the
    # time its turn comes it may be mid-turn. `agent get` is step 1 of the
    # frozen recipe and already carries the fresh status; the viewport below
    # costs one extra read-only call and catches the same drift when herdr has
    # not noticed it yet (a resumed session shows the marker gone before its
    # agent_status flips). Both refusals are the difference between a no-op and
    # a killed working session.
    if _agent_get_status(live) == STATUS_WORKING:
        return _result(pane, False, "%s (status changed to %s since the plan was "
                                    "made) - left alone" % (REASON_WORKING, STATUS_WORKING),
                       steps, False)
    session_id = session_id or pane.session_id
    if not session_id:
        return _result(pane, False, REASON_NO_SESSION, steps, False)
    steps.append(_viewport_args(pane.pane_id))
    visible = read_viewport(pane.pane_id)
    if visible is None:
        return _result(pane, False, REASON_VIEWPORT_UNREADABLE, steps, False)
    if not has_limit_marker(visible):
        return _result(pane, False, REASON_MARKER_GONE, steps, False)
    start_name = live_name or pane.name or _fallback_name(pane)

    error = _stop_claude(pane, steps)
    if error:
        return _result(pane, False, error, steps, False)

    steps.append(_start_args(pane, start_name, session_id))
    ok, detail = _start_outcome(
        *_herdr(steps[-1], mutating=True, timeout_s=START_SUBPROCESS_TIMEOUT_S)
    )
    if not ok:
        return _result(pane, False, detail, steps, False)
    return _result(pane, True, detail + _restore_name(pane, start_name, steps), steps, False)


def rollover_pane(pane: Pane, dry_run: bool) -> Dict:
    """Exit-and-resume one pane. dry_run runs NOTHING and only lists commands."""
    if not dry_run:
        return _rollover_pane_live(pane)
    steps = _planned_commands(pane, pane.name or _fallback_name(pane), pane.session_id)
    detail = "dry-run - %d herdr commands listed, none executed" % len(steps)
    return _result(pane, True, detail, steps, True)


# ---------------------------------------------------------------------------
# The whole fleet
# ---------------------------------------------------------------------------


def _plan_json(item: RolloverPlan) -> Dict:
    return {"pane": item.pane.pane_id, "name": item.pane.name,
            "status": item.pane.status, "tab_label": item.pane.tab_label,
            "cwd": item.pane.cwd, "act": item.act, "reason": item.reason}


def _plan_with_viewports(panes, self_pane):
    # type: (List[Pane], Optional[str]) -> List[RolloverPlan]
    """Plan twice: the first pass tells us whose viewport is worth reading."""
    viewports = {}  # type: Dict[str, str]
    for item in plan(panes, self_pane, viewports):
        if item.reason == REASON_NO_MARKER:
            viewports[item.pane.pane_id] = viewport(item.pane.pane_id)
    return plan(panes, self_pane, viewports)


def run(dry_run: bool = True, only: Optional[List[str]] = None) -> Dict:
    """Plan, and when dry_run is False perform, a rollover of the fleet.

    Returns {"dry_run":bool, "error":str, "planned":[...], "acted":[...],
    "skipped":[...]}. `acted` holds one rollover_pane() result per pane we would
    act on; in a dry run each lists its commands and executed none of them.
    """
    panes, error = _list_panes_raw()
    if only:
        wanted = [str(pane_id) for pane_id in only]
        present = {pane.pane_id for pane in panes}
        panes = [pane for pane in panes if pane.pane_id in set(wanted)]
        # Report EVERY id that matched nothing, not just the all-miss case. An
        # empty plan renders as "nothing to do", which is the opposite of what
        # a filter that matched no pane means - and one good id used to mask
        # any number of typos beside it, silently rolling over a subset the
        # operator never asked for on its own.
        missed = [pane_id for pane_id in wanted if pane_id not in present]
        if missed and not error:
            error = "no pane matched --only %s" % ", ".join(missed)
    plans = _plan_with_viewports(panes, os.environ.get(HERDR_PANE_ENV) or None)

    acted = []
    skipped = []
    for item in plans:
        if item.act:
            acted.append(rollover_pane(item.pane, dry_run))
        else:
            skipped.append(_plan_json(item))
    return {"dry_run": bool(dry_run), "error": error,
            "planned": [_plan_json(item) for item in plans],
            "acted": acted, "skipped": skipped}


__all__ = [
    "RolloverSafetyError", "REFUSAL_REASONS", "UNKNOWN_TAB_LABEL",
    "REASON_MARKER_GONE", "REASON_VIEWPORT_UNREADABLE",
    "herdr_json", "list_panes",
    "read_viewport", "viewport", "has_limit_marker", "plan", "wait_for_shell",
    "rollover_pane", "run",
]
