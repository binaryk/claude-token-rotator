"""Which running `claude` processes will (not) follow a seamless switch.

A session started with CLAUDE_CODE_OAUTH_TOKEN in its environment never looks
at Claude Code's credentials store again (measured 2026-09-30), so `ctr switch`
cannot move it; only a restart can (`ctr rollover`). Everything else re-reads
the store and follows.

`ps -E` appends each process's environment to its command line. It only works
for the caller's own processes, which is exactly the set that matters. The
token found there is fingerprinted in memory and never leaves this module.

Python 3.8 compatible. stdlib only.
"""

import os
import re
import subprocess
from typing import Dict, List, NamedTuple

from ctr.model import ENV_VAR

_ENV_RE = re.compile(r"(?:^|\s)%s=(\S+)" % ENV_VAR)


class Session(NamedTuple):
    pid: int
    pinned: bool
    #: ctr label of the pinned token, "unknown" when it is not a ctr token,
    #: "" when the session is not pinned.
    label: str


def _ps() -> str:
    """The single subprocess seam — tests replace this."""
    try:
        proc = subprocess.run(
            ["/bin/ps", "-axEww", "-o", "pid=,ppid=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _is_claude(argv0: str) -> bool:
    base = os.path.basename(argv0)
    return base == "claude" or "/claude/versions/" in argv0


def scan(tokens: Dict[str, str]) -> List[Session]:
    """Every running claude process, pinned or following. No secret is kept."""
    by_token = {token: label for label, token in tokens.items() if token}
    rows = []
    for line in _ps().splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not (parts[0].isdigit() and parts[1].isdigit()):
            continue
        if _is_claude(parts[2].split(" ", 1)[0]):
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    claude_pids = {pid for pid, _ppid, _cmd in rows}
    found = []  # type: List[Session]
    for pid, ppid, command in rows:
        if ppid in claude_pids:
            continue  # a helper claude spawned, not a session of its own
        match = _ENV_RE.search(command)
        if match is None:
            found.append(Session(pid, False, ""))
        else:
            found.append(Session(pid, True, by_token.get(match.group(1), "unknown")))
    return found


def summary(sessions: List[Session]) -> Dict:
    """Secret-free counts: {"following": n, "pinned": n, "pinned_by_label": {...}}."""
    pinned = {}  # type: Dict[str, int]
    for session in sessions:
        if session.pinned:
            pinned[session.label] = pinned.get(session.label, 0) + 1
    return {
        "following": sum(1 for s in sessions if not s.pinned),
        "pinned": sum(pinned.values()),
        "pinned_by_label": pinned,
    }


__all__ = ["Session", "scan", "summary"]
