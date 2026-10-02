"""Linux branches: Secret Service store, credentials file, systemd timer, shell.

Every test flips `ctr.host.SYSTEM` to "linux" and replaces the subprocess seams,
so the suite runs the same on the Mac and on the Linux box.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import claude_login, cli, host, keychain, notify, secretstore, shell, systemd  # noqa: E402
from ctr.launchd import Result  # noqa: E402

_REAL_RUN = secretstore._run


class FakeSecretTool(object):
    """In-memory `secret-tool`: store / lookup / clear, as measured on office."""

    def __init__(self):
        self.items = {}
        self.calls = []

    def run(self, cmd, stdin_data=None, timeout_s=None):
        self.calls.append(list(cmd))
        verb = cmd[1]
        service = cmd[-1]
        if verb == "store":
            self.items[service] = stdin_data or ""  # verbatim, newline included
            return 0, "", ""
        if verb == "lookup":
            if service not in self.items:
                return 1, "", ""
            return 0, self.items[service], ""
        if verb == "clear":
            return (0, "", "") if self.items.pop(service, None) is not None else (1, "", "")
        raise AssertionError("unexpected secret-tool call %r" % (cmd,))


class LinuxCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(host, "SYSTEM", "linux")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Safety net: a Linux branch reached without an explicit environ must
        # land in a temp dir, never in the real ~/.claude.
        env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)


class TestHost(unittest.TestCase):
    def test_platform_dispatch(self):
        with mock.patch.object(host, "SYSTEM", "darwin"):
            self.assertTrue(host.is_macos())
            self.assertEqual(cli._monitor_backend().__name__, "ctr.launchd")
        with mock.patch.object(host, "SYSTEM", "linux"):
            self.assertFalse(host.is_macos())
            self.assertIs(cli._monitor_backend(), systemd)


class TestSecretStore(LinuxCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeSecretTool()
        patcher = mock.patch.object(secretstore, "_run", self.fake.run)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_roundtrip_through_keychain_api(self):
        keychain.set("ctr:social", "acct", "sk-ant-oat01-secret")
        self.assertEqual(keychain.get("ctr:social"), "sk-ant-oat01-secret")
        self.assertTrue(keychain.exists("ctr:social"))
        self.assertEqual(keychain.token_for("social"), "sk-ant-oat01-secret")
        self.assertTrue(keychain.delete("ctr:social"))
        self.assertFalse(keychain.delete("ctr:social"))
        self.assertIsNone(keychain.get("ctr:social"))

    def test_secret_never_in_argv_and_stored_without_newline(self):
        keychain.set("ctr:a", "acct", "tok-123")
        for call in self.fake.calls:
            self.assertNotIn("tok-123", " ".join(call))
        self.assertEqual(self.fake.items["ctr:a"], "tok-123")

    def test_empty_secret_refused(self):
        with self.assertRaises(ValueError):
            keychain.set("ctr:a", "acct", "  ")

    def test_unverified_write_raises_keychain_error(self):
        def lossy(cmd, stdin_data=None, timeout_s=None):
            if cmd[1] == "store":
                return 0, "", ""
            return 1, "", ""

        with mock.patch.object(secretstore, "_run", lossy):
            with self.assertRaises(keychain.KeychainError):
                keychain.set("ctr:a", "acct", "tok")

    def test_failed_write_message_is_scrubbed(self):
        def failing(cmd, stdin_data=None, timeout_s=None):
            return 1, "", "boom tok-xyz"

        with mock.patch.object(secretstore, "_run", failing):
            with self.assertRaises(keychain.KeychainError) as ctx:
                keychain.set("ctr:a", "acct", "tok-xyz")
        self.assertNotIn("tok-xyz", str(ctx.exception))

    def test_missing_binary_reads_as_absent(self):
        with mock.patch.object(secretstore, "_run", _REAL_RUN), \
                mock.patch.object(secretstore, "SECRET_TOOL", "/nonexistent/secret-tool"):
            self.assertIsNone(secretstore.get("ctr:a"))
            self.assertFalse(secretstore.delete("ctr:a"))


class TestCredentialsFile(LinuxCase):
    def setUp(self):
        super().setUp()
        self.env = {"CLAUDE_CONFIG_DIR": self.tmp}
        self.path = os.path.join(self.tmp, ".credentials.json")
        self.login = {"accessToken": "login-at", "refreshToken": "login-rt",
                      "expiresAt": 1, "scopes": ["user:inference", "user:profile"]}
        with open(self.path, "w") as handle:
            json.dump({"claudeAiOauth": self.login, "mcpOAuth": {"x": {"k": 1}}}, handle)
        os.chmod(self.path, 0o600)

    def _read(self, path=None):
        with open(path or self.path) as handle:
            return json.load(handle)

    def test_service_name_is_the_credentials_path(self):
        self.assertEqual(claude_login.service_name(self.env), self.path)
        home = claude_login.service_name({})
        self.assertTrue(home.endswith(os.path.join(".claude", ".credentials.json")))

    def test_switch_writes_setup_token_keeps_mcp_backs_up_login(self):
        result = claude_login.switch_to("sk-ant-oat01-b", "max", self.env)
        self.assertTrue(result["backed_up_login"])
        data = self._read()
        self.assertEqual(data["claudeAiOauth"]["accessToken"], "sk-ant-oat01-b")
        self.assertIsNone(data["claudeAiOauth"]["refreshToken"])
        self.assertEqual(data["mcpOAuth"], {"x": {"k": 1}})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        backup = self.path + claude_login.LOGIN_BACKUP_SUFFIX
        self.assertEqual(self._read(backup), self.login)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        previous = self.path + claude_login.PREVIOUS_SUFFIX
        self.assertEqual(self._read(previous)["claudeAiOauth"], self.login)
        self.assertEqual(stat.S_IMODE(os.stat(previous).st_mode), 0o600)
        self.assertTrue(claude_login.has_backup(self.env))

    def test_previous_copy_is_never_world_readable(self):
        old = os.umask(0o022)
        try:
            claude_login.switch_to("tok-u", "", self.env)
        finally:
            os.umask(old)
        previous = self.path + claude_login.PREVIOUS_SUFFIX
        self.assertEqual(stat.S_IMODE(os.stat(previous).st_mode), 0o600)

    def test_holder_and_restore(self):
        claude_login.switch_to("tok-b", "", self.env)
        self.assertEqual(claude_login.holder({"b": "tok-b"}, self.env), "b")
        result = claude_login.restore_login(self.env)
        self.assertTrue(result["restored"])
        self.assertEqual(self._read()["claudeAiOauth"], self.login)
        self.assertEqual(claude_login.holder({"b": "tok-b"}, self.env), "login")

    def test_missing_file_is_an_empty_store(self):
        os.remove(self.path)
        self.assertEqual(claude_login.read_store(self.env), {})
        claude_login.switch_to("tok-c", "", self.env)
        self.assertEqual(self._read()["claudeAiOauth"]["accessToken"], "tok-c")

    def test_corrupt_file_is_never_overwritten(self):
        with open(self.path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(claude_login.LoginStoreError):
            claude_login.switch_to("tok-d", "", self.env)
        with open(self.path) as handle:
            self.assertEqual(handle.read(), "{not json")

    def test_no_leftover_temp_files(self):
        claude_login.switch_to("tok-e", "", self.env)
        leftovers = [n for n in os.listdir(self.tmp) if n.startswith(".ctr-")]
        self.assertEqual(leftovers, [])

    def test_keychain_login_helpers_read_the_file(self):
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(keychain.claude_login_token(), "login-at")
            claude_login.switch_to("tok-f", "", self.env)
            self.assertIsNone(keychain.claude_login_token())  # setup-token, not a login


class TestSystemd(LinuxCase):
    def test_unit_text(self):
        service = systemd.service_unit("/home/u/.local/bin/ctr")
        self.assertIn("Type=oneshot\n", service)
        self.assertIn('ExecStart="/home/u/.local/bin/ctr" monitor --once\n', service)
        self.assertIn('ExecStart="/a b/100%%/ctr"', systemd.service_unit("/a b/100%/ctr"))
        self.assertIn("Environment=PATH=", service)
        self.assertIn("mise/shims", service)
        timer = systemd.timer_unit(300)
        self.assertIn("OnUnitActiveSec=300s\n", timer)
        self.assertIn("Unit=ctr-monitor.service\n", timer)
        self.assertIn("WantedBy=timers.target\n", timer)
        self.assertIn("OnUnitActiveSec=1s", systemd.timer_unit(0))

    def test_install_writes_units_and_enables_timer(self):
        calls = []

        def run(cmd, timeout_s=None):
            calls.append(cmd)
            return Result(0, "", "")

        with mock.patch.object(systemd, "UNIT_DIR", self.tmp), \
                mock.patch.object(systemd, "_run", run):
            path = systemd.install("/x/ctr", 120)
            self.assertEqual(path, os.path.join(self.tmp, "ctr-monitor.timer"))
            with open(path) as handle:
                self.assertIn("OnUnitActiveSec=120s", handle.read())
            self.assertTrue(os.path.exists(os.path.join(self.tmp, "ctr-monitor.service")))
            self.assertIn(["systemctl", "--user", "daemon-reload"], calls)
            self.assertIn(["systemctl", "--user", "enable", "--now", "ctr-monitor.timer"], calls)
            self.assertTrue(systemd.uninstall())
            self.assertFalse(os.path.exists(path))
            self.assertFalse(systemd.uninstall())

    def test_enable_failure_raises(self):
        def run(cmd, timeout_s=None):
            return Result(1, "", "no bus") if "enable" in cmd else Result(0, "", "")

        with mock.patch.object(systemd, "UNIT_DIR", self.tmp), \
                mock.patch.object(systemd, "_run", run):
            with self.assertRaises(RuntimeError):
                systemd.install("/x/ctr")

    def test_status_reports_linger(self):
        def run(cmd, timeout_s=None):
            if cmd[0] == "loginctl":
                return Result(0, "Linger=no\n", "")
            return Result(0, "active\n", "")

        with mock.patch.object(systemd, "UNIT_DIR", self.tmp), \
                mock.patch.object(systemd, "_run", run):
            info = systemd.status()
        self.assertEqual(info["installed"], False)
        self.assertEqual(info["loaded"], True)
        self.assertEqual(info["linger"], False)

    def test_doctor_warns_without_linger(self):
        fake = mock.Mock()
        fake.status.return_value = {"installed": True, "loaded": True, "path": "/t", "linger": False}
        with mock.patch.object(cli, "_monitor_backend", return_value=fake):
            rows = cli._doctor_launchd()
        self.assertEqual(rows[0][0], "ok")
        self.assertEqual(rows[1][:2], ("warn", "linger"))


class TestShellLinux(LinuxCase):
    def test_active_sh_reads_secret_service_not_security(self):
        body = shell.active_sh_contents("social")
        self.assertNotIn("security", body)
        self.assertIn("secret-tool lookup service 'ctr:social'", body)

    def test_keychain_mode_reads_nothing(self):
        body = shell.active_sh_contents("social", "keychain")
        self.assertNotIn("secret-tool", body)
        self.assertIn("unset CLAUDE_CODE_OAUTH_TOKEN", body)

    def test_rc_targets(self):
        self.assertEqual(shell.rc_targets({"SHELL": "/usr/bin/bash"}), ["~/.bashrc"])
        self.assertEqual(shell.rc_targets({"SHELL": "/bin/zsh"}), ["~/.bashrc", "~/.zshrc"])
        with mock.patch.object(host, "SYSTEM", "darwin"):
            self.assertEqual(shell.rc_targets({"SHELL": "/usr/bin/bash"}), ["~/.zshrc"])

    def test_sourcing_exports_the_stubbed_secret(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        stub = os.path.join(bindir, "secret-tool")
        with open(stub, "w") as handle:
            handle.write('#!/bin/sh\nprintf "stub-linux-secret"\n')
        os.chmod(stub, 0o755)
        active = os.path.join(self.tmp, "active.sh")
        with open(active, "w") as handle:
            handle.write(shell.active_sh_contents("social"))
        env = {"PATH": bindir + ":/usr/bin:/bin", "HOME": self.tmp}
        out = subprocess.run(["bash", "-c", '. "%s"; printf "%%s" "$CLAUDE_CODE_OAUTH_TOKEN"'
                              % active], env=env, stdout=subprocess.PIPE).stdout.decode()
        self.assertEqual(out, "stub-linux-secret")

    def test_install_shell_writes_bashrc_block(self):
        rc = os.path.join(self.tmp, "bashrc")
        with open(rc, "w") as handle:
            handle.write("[[ $- != *i* ]] && return\nalias ll='ls -l'\n")
        shell.install_zshrc(rc, os.path.join(self.tmp, "active.sh"))
        with open(rc) as handle:
            text = handle.read()
        self.assertTrue(text.startswith("[[ $- != *i* ]] && return\nalias ll='ls -l'\n"))
        self.assertIn("# >>> ctr (claude-token-rotator) >>>", text)


class TestDoctorShadowedCtr(LinuxCase):
    def test_a_foreign_ctr_first_on_path_fails(self):
        link = os.path.join(self.tmp, "ctr")
        open(link, "w").close()
        with mock.patch("shutil.which", return_value="/usr/bin/ctr"):
            level, name, detail = cli._doctor_shadowed(link)
        self.assertEqual((level, name), ("fail", "ctr on PATH"))
        self.assertIn("/usr/bin/ctr", detail)
        with mock.patch("shutil.which", return_value=link):
            self.assertEqual(cli._doctor_shadowed(link)[0], "ok")


class TestSessionsLinux(LinuxCase):
    def test_ps_uses_the_procps_form_on_linux(self):
        from ctr import sessions

        self.assertEqual(sessions.ps_command()[:2], ["ps", "axeww"])
        with mock.patch.object(host, "SYSTEM", "darwin"):
            self.assertEqual(sessions.ps_command()[:2], ["/bin/ps", "-axEww"])


class TestNotifyLinux(LinuxCase):
    def test_uses_notify_send(self):
        seen = []
        with mock.patch.object(notify, "_run", lambda cmd: seen.append(cmd) or 0):
            notify.notify("ctr", "switched")
        self.assertEqual(seen[0][0], "notify-send")
        self.assertEqual(seen[0][-2:], ["ctr", "switched"])


if __name__ == "__main__":
    unittest.main()
