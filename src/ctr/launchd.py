"""launchd agent for `ctr monitor --once`.

`plist_xml` is pure so the plist body is unit-testable without touching
~/Library/LaunchAgents. Everything that talks to launchctl goes through the
module-level `_run` indirection.
"""

import os
import subprocess
from typing import Dict, List, NamedTuple, Optional
from xml.sax.saxutils import escape

from ctr.model import LAUNCHD_LABEL, LAUNCHD_PLIST, LOG_FILE

#: launchd agents start with a minimal PATH; ctr shells out to security, curl
#: and herdr, so spell the search path out.
PATH_ENV = ":".join(
    [
        os.path.join(os.path.expanduser("~"), ".local", "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
)

DEFAULT_LOG_PATH = os.path.expanduser(LOG_FILE)
TIMEOUT_S = 30

_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>%(label)s</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>%(program)s</string>
\t\t<string>monitor</string>
\t\t<string>--once</string>
\t</array>
\t<key>StartInterval</key>
\t<integer>%(interval)d</integer>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>StandardOutPath</key>
\t<string>%(log)s</string>
\t<key>StandardErrorPath</key>
\t<string>%(log)s</string>
\t<key>EnvironmentVariables</key>
\t<dict>
\t\t<key>PATH</key>
\t\t<string>%(path)s</string>
\t</dict>
\t<key>ProcessType</key>
\t<string>Background</string>
</dict>
</plist>
"""


class Result(NamedTuple):
    """Minimal CompletedProcess stand-in so `_run` never raises."""

    returncode: int
    stdout: str
    stderr: str


def _run(cmd: List[str], timeout_s: int = TIMEOUT_S) -> Result:
    """Run a command; never raises. Indirection point for tests."""
    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            universal_newlines=True,
        )
        return Result(completed.returncode, completed.stdout or "", completed.stderr or "")
    except FileNotFoundError:
        return Result(127, "", "%s not found" % (cmd[0] if cmd else "command"))
    except subprocess.TimeoutExpired:
        return Result(124, "", "timed out after %ss" % timeout_s)
    except OSError as exc:
        return Result(126, "", str(exc))


def plist_xml(program: str, interval_s: int = 300, log_path: str = DEFAULT_LOG_PATH) -> str:
    """The launchd plist body. Pure."""
    interval = int(interval_s)
    if interval < 1:
        interval = 1
    return _PLIST_TEMPLATE % {
        "label": escape(LAUNCHD_LABEL),
        "program": escape(str(program)),
        "interval": interval,
        "log": escape(os.path.expanduser(str(log_path or DEFAULT_LOG_PATH))),
        "path": escape(PATH_ENV),
    }


def plist_path() -> str:
    return os.path.expanduser(LAUNCHD_PLIST)


def _domain_target() -> str:
    return "gui/%d" % os.getuid()


def _write_plist(path: str, body: str) -> None:
    """Atomic write, 0644 (launchd refuses group/other-writable plists)."""
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, 0o755)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def install(program: str, interval_s: int = 300) -> str:
    """Write the plist and (re)load the agent. Returns the plist path."""
    path = plist_path()
    _write_plist(path, plist_xml(program, interval_s))

    domain = _domain_target()
    _run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])  # may not be loaded
    booted = _run(["launchctl", "bootstrap", domain, path])
    if booted.returncode != 0:
        legacy = _run(["launchctl", "load", "-w", path])
        if legacy.returncode != 0:
            raise RuntimeError(
                "launchctl could not load %s (bootstrap: %s / load: %s)"
                % (path, booted.stderr.strip() or booted.returncode, legacy.stderr.strip())
            )
    return path


def uninstall() -> bool:
    """Unload the agent and delete the plist. True when a plist was removed."""
    path = plist_path()
    domain = _domain_target()
    booted = _run(["launchctl", "bootout", "%s/%s" % (domain, LAUNCHD_LABEL)])
    if booted.returncode != 0:
        _run(["launchctl", "unload", "-w", path])
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def status() -> Dict:
    """{"installed": bool, "loaded": bool, "plist": path}"""
    path = plist_path()
    listed = _run(["launchctl", "list", LAUNCHD_LABEL])
    return {
        "installed": os.path.exists(path),
        "loaded": listed.returncode == 0,
        "plist": path,
    }


__all__ = ["plist_xml", "plist_path", "install", "uninstall", "status", "PATH_ENV"]
