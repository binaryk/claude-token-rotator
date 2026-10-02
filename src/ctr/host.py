"""The one platform seam: which OS ctr is running on.

macOS keeps every secret in the login keychain and runs the monitor under
launchd. Anything else is treated as Linux: secrets go to the freedesktop
Secret Service through `secret-tool`, Claude Code's login lives in
~/.claude/.credentials.json, and the monitor is a systemd user timer.

Tests flip `SYSTEM` to exercise the other branch.
"""

import sys

SYSTEM = sys.platform


def is_macos() -> bool:
    return SYSTEM == "darwin"


__all__ = ["SYSTEM", "is_macos"]
