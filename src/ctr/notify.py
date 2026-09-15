"""macOS notifications for ctr.

One public function. It must never raise: a failed notification can never be a
reason for the monitor loop to die. Messages passed in here are already
redacted by their callers — this module additionally strips control characters
so nothing can break out of the AppleScript string literal.
"""

import subprocess
from typing import List, Optional

#: AppleScript notifications silently truncate long bodies; keep them short.
MAX_TITLE = 80
MAX_MESSAGE = 240
#: osascript is local and instant; anything slower is a hang.
TIMEOUT_S = 10


def _run(cmd: List[str], timeout_s: int = TIMEOUT_S) -> int:
    """Run a command, return its exit status. Indirection point for tests."""
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_s,
    )
    return completed.returncode


def _sanitize(text: Optional[str], limit: int) -> str:
    """One safe line: no control characters, bounded length."""
    raw = "" if text is None else str(text)
    cleaned = "".join(ch if ch >= " " and ch != "\x7f" else " " for ch in raw)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3] + "..."
    return cleaned


def _quote(text: str) -> str:
    """AppleScript string literal for already-sanitised text."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applescript(title: str, message: str) -> str:
    """The exact AppleScript ctr runs. Pure, so it can be unit-tested."""
    return "display notification %s with title %s" % (
        _quote(_sanitize(message, MAX_MESSAGE)),
        _quote(_sanitize(title, MAX_TITLE)),
    )


def notify(title: str, message: str) -> None:
    """Post a macOS notification. Never raises; failures go to the ctr log."""
    try:
        status = _run(["osascript", "-e", applescript(title, message)])
        if status != 0:
            _log("notification failed (osascript exit %d)" % status)
    except Exception as exc:  # osascript missing, sandboxed, timed out...
        _log("notification failed: %s" % exc.__class__.__name__)


def _log(message: str) -> None:
    """Best-effort logging; imported lazily to avoid an import cycle."""
    try:
        from ctr.monitor import log_line

        log_line(message)
    except Exception:
        pass  # nowhere left to report to


__all__ = ["notify", "applescript", "MAX_TITLE", "MAX_MESSAGE"]
