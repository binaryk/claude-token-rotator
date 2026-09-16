"""The keychain write must work from a REAL TERMINAL, not just from a pipe.

This file exists because every other test in this suite, and every agent shell
call that built ctr, runs without a controlling TTY — and that is precisely the
condition under which the bug could not appear.

`security add-generic-password ... -w` with no value reads the secret from the
controlling terminal when it has one. Run from Warp, `ctr add` printed
"password data for new item:" at the user, waited, and stored an EMPTY
password; the read-back check caught it and refused to register the token, so
nothing silently half-worked — but no token could be added at all.

These tests fork a pty so the child really has a terminal, then exercise the
module's own `_run`. They touch a throwaway keychain service and delete it.
"""

import os
import pty
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import keychain  # noqa: E402

PROBE_SERVICE = "ctr:__pytest_tty_probe__"
PROBE_SECRET = "sk-ant-oat01-TTYPROBE-do-not-use-0123456789"
CHILD_TIMEOUT_S = 10


def _forget():
    subprocess.run(
        ["security", "delete-generic-password", "-s", PROBE_SERVICE],
        capture_output=True,
    )


def _write_under_a_tty():
    """Run keychain.set() in a child that owns a real terminal.

    Returns the child's exit status, or None when it hung on a tty prompt.
    """
    pid, fd = pty.fork()
    if pid == 0:  # child: has a controlling terminal
        try:
            keychain.set(PROBE_SERVICE, "probe", PROBE_SECRET)
            os._exit(0)
        except BaseException:
            os._exit(1)
    os.close(fd)
    deadline = time.time() + CHILD_TIMEOUT_S
    while time.time() < deadline:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.WEXITSTATUS(status)
        time.sleep(0.05)
    # Kill the whole process group: without the fix the `security` GRANDCHILD
    # is the thing blocked on /dev/tty, and killing only our child leaves it
    # running forever, holding the terminal.
    try:
        os.killpg(os.getpgid(pid), 9)
    except OSError:
        os.kill(pid, 9)
    os.waitpid(pid, 0)
    subprocess.run(["pkill", "-f", "add-generic-password.*%s" % PROBE_SERVICE],
                   capture_output=True)
    return None


class KeychainWriteUnderATtyTests(unittest.TestCase):
    """Guards the 2026-09-16 defect: `ctr add` was broken in every real shell."""

    def setUp(self):
        _forget()
        self.addCleanup(_forget)

    def test_the_write_does_not_prompt_on_the_terminal(self):
        status = _write_under_a_tty()
        self.assertIsNotNone(
            status,
            "keychain.set() hung waiting on a terminal prompt — `security` is "
            "reading /dev/tty instead of stdin; _run needs start_new_session",
        )
        self.assertEqual(0, status, "keychain.set() failed under a real tty")

    def test_the_secret_actually_lands_under_a_tty(self):
        self.assertIsNotNone(_write_under_a_tty(), "hung on a tty prompt")
        stored = keychain.get(PROBE_SERVICE)
        self.assertEqual(PROBE_SECRET, stored, "an empty or wrong secret was stored")

    def test_the_child_is_detached_from_the_terminal(self):
        """The mechanism itself, so a refactor cannot quietly drop it."""
        seen = {}

        original = subprocess.Popen

        def spy(cmd, **kwargs):
            seen.update(kwargs)
            return original(cmd, **kwargs)

        subprocess.Popen = spy
        try:
            keychain._run([keychain.SECURITY, "help"])
        finally:
            subprocess.Popen = original
        self.assertTrue(
            seen.get("start_new_session"),
            "`security` must run in its own session or it will read /dev/tty",
        )


if __name__ == "__main__":
    unittest.main()
