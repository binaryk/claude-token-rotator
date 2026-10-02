"""systemd user timer for `ctr monitor --once` — the Linux twin of launchd.py.

Same interface (install / uninstall / status). `service_unit` and `timer_unit`
are pure so the unit text is unit-testable without touching
~/.config/systemd/user. Everything that talks to systemctl/loginctl goes
through the module-level `_run` indirection.

A user timer only runs while the user's systemd instance is alive. Without
lingering that instance stops at the last logout, so a headless box needs
`loginctl enable-linger <user>` once; `status()` reports it.
"""

import getpass
import os
from typing import Dict, List

from ctr.launchd import Result, _run as _launchd_run

UNIT = "ctr-monitor"
UNIT_DIR = "~/.config/systemd/user"
TIMEOUT_S = 30

#: User units start with systemd's minimal PATH; ctr shells out to
#: secret-tool, curl and herdr, and mise shims live under ~/.local/share.
PATH_ENV = ":".join(
    [
        os.path.join(os.path.expanduser("~"), ".local", "bin"),
        os.path.join(os.path.expanduser("~"), ".local", "share", "mise", "shims"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
)


def _run(cmd: List[str], timeout_s: int = TIMEOUT_S) -> Result:
    """Run a command; never raises. Indirection point for tests."""
    return _launchd_run(cmd, timeout_s)


def service_unit(program: str) -> str:
    """The oneshot service: one monitor pass. Pure."""
    return (
        "[Unit]\n"
        "Description=ctr (claude-token-rotator) monitor pass\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=\"%s\" monitor --once\n"
        "Environment=PATH=%s\n"
    ) % (program.replace("%", "%%"), PATH_ENV)


def timer_unit(interval_s: int = 300) -> str:
    """The timer that runs the service every `interval_s` seconds. Pure."""
    interval = max(1, int(interval_s))
    return (
        "[Unit]\n"
        "Description=Run the ctr monitor every %ds\n"
        "\n"
        "[Timer]\n"
        "OnActiveSec=10\n"
        "OnUnitActiveSec=%ds\n"
        "AccuracySec=15\n"
        "Unit=%s.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    ) % (interval, interval, UNIT)


def unit_dir() -> str:
    return os.path.expanduser(UNIT_DIR)


def timer_path() -> str:
    return os.path.join(unit_dir(), UNIT + ".timer")


def service_path() -> str:
    return os.path.join(unit_dir(), UNIT + ".service")


def _write(path: str, body: str) -> None:
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, 0o755)
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        handle.write(body)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _systemctl(*args: str) -> Result:
    return _run(["systemctl", "--user"] + list(args))


def install(program: str, interval_s: int = 300) -> str:
    """Write both units, enable and start the timer. Returns the timer path."""
    _write(service_path(), service_unit(program))
    _write(timer_path(), timer_unit(interval_s))
    reloaded = _systemctl("daemon-reload")
    if reloaded.returncode != 0:
        raise RuntimeError("systemctl --user daemon-reload failed: %s" % reloaded.stderr.strip())
    enabled = _systemctl("enable", "--now", UNIT + ".timer")
    if enabled.returncode != 0:
        raise RuntimeError(
            "systemctl --user could not enable %s.timer: %s" % (UNIT, enabled.stderr.strip())
        )
    _systemctl("restart", UNIT + ".timer")  # pick up a changed interval
    return timer_path()


def uninstall() -> bool:
    """Stop and disable the timer, delete both units. True when a unit was removed."""
    _systemctl("disable", "--now", UNIT + ".timer")
    removed = False
    for path in (timer_path(), service_path()):
        if os.path.exists(path):
            os.remove(path)
            removed = True
    _systemctl("daemon-reload")
    return removed


def lingering(user: str = "") -> bool:
    """True when the user's systemd instance outlives their sessions."""
    name = user or os.environ.get("USER") or getpass.getuser()
    shown = _run(["loginctl", "show-user", name, "-p", "Linger"])
    return shown.returncode == 0 and shown.stdout.strip() == "Linger=yes"


def status() -> Dict:
    """{"installed": bool, "loaded": bool, "path": timer path, "linger": bool}"""
    active = _systemctl("is-active", UNIT + ".timer")
    return {
        "installed": os.path.exists(timer_path()),
        "loaded": active.returncode == 0,
        "path": timer_path(),
        "linger": lingering(),
    }


__all__ = ["service_unit", "timer_unit", "install", "uninstall", "status", "lingering", "PATH_ENV"]
