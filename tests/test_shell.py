"""Unit tests for ctr.shell (Lane D).

Nothing here touches the network, the real keychain, the user's real ~/.zshrc
or the real ~/.config/ctr: every path is injected into a temp directory, and the
one test that actually executes the generated script shadows `security` with a
stub on PATH.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

from ctr import shell
from ctr.model import ENV_VAR, ZSHRC_BEGIN, ZSHRC_END

# Anything that looks like a real credential must never appear in a generated
# file: an Anthropic token prefix, or a long opaque run of token characters.
_TOKEN_PREFIX_RE = re.compile(r"sk-ant-|sk-[A-Za-z0-9]{8,}")
_LONG_OPAQUE_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


class PureGeneratorTests(unittest.TestCase):
    def test_active_contents_reads_keychain_at_source_time(self):
        body = shell.active_sh_contents("social")
        self.assertIn("security find-generic-password -s 'ctr:social' -w", body)
        self.assertIn("CTR_ACTIVE_LABEL='social'", body)
        self.assertIn("export %s" % ENV_VAR, body)
        self.assertIn("unset _ctr_tok", body)

    def test_active_contents_none_does_nothing(self):
        body = shell.active_sh_contents(None)
        self.assertNotIn("security", body)
        self.assertNotIn("unset %s" % ENV_VAR, body)
        self.assertNotIn("export %s=" % ENV_VAR, body)
        self.assertIn("CTR_ACTIVE_LABEL=''", body)

    def test_active_contents_never_contains_a_token(self):
        for label in (None, "social", "devops-2", "a"):
            body = shell.active_sh_contents(label)
            self.assertIsNone(
                _TOKEN_PREFIX_RE.search(body), "token-like prefix in %r" % label
            )
            self.assertIsNone(
                _LONG_OPAQUE_RE.search(body), "long opaque run in %r" % label
            )

    def test_active_contents_rejects_an_invalid_label(self):
        for bad in ("", "Social", "-x", "a b", "a'; rm -rf /", "x" * 41):
            with self.assertRaises(ValueError):
                shell.active_sh_contents(bad)

    def test_generated_script_is_valid_sh(self):
        for label in (None, "social"):
            body = shell.active_sh_contents(label)
            proc = subprocess.run(
                ["sh", "-n"], input=body.encode(), stderr=subprocess.PIPE
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_zshrc_block_is_guarded_and_quotes_the_path(self):
        block = shell.zshrc_block("$HOME/.config/ctr/active.sh")
        self.assertTrue(block.startswith(ZSHRC_BEGIN))
        self.assertIn(ZSHRC_END + "\n", block)
        self.assertIn('if [ -r "$HOME/.config/ctr/active.sh" ]; then', block)

    def test_parse_active_label_round_trips(self):
        self.assertEqual(
            shell.parse_active_label(shell.active_sh_contents("work-2")), "work-2"
        )
        self.assertIsNone(shell.parse_active_label(shell.active_sh_contents(None)))
        self.assertIsNone(shell.parse_active_label(""))
        self.assertIsNone(shell.parse_active_label("nothing here"))


class SourcingTests(unittest.TestCase):
    """Execute the generated active.sh against a stubbed `security` binary."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-src-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)

    def _stub_security(self, body):
        path = os.path.join(self.bin, "security")
        with open(path, "w") as stream:
            stream.write(body)
        os.chmod(path, 0o755)

    def _source(self, label, preset=None):
        script = os.path.join(self.tmp, "active.sh")
        shell.write_active(label, script)
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        if preset is None:
            env.pop(ENV_VAR, None)
        else:
            env[ENV_VAR] = preset
        command = '. "%s"; echo "[${%s-UNSET}]"' % (script, ENV_VAR)
        proc = subprocess.run(
            ["sh", "-c", command],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc

    def test_sourcing_exports_the_keychain_value(self):
        self._stub_security('#!/bin/sh\necho "stub-secret-value"\n')
        proc = self._source("social")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), "[stub-secret-value]")

    def test_missing_keychain_item_leaves_the_env_var_untouched(self):
        self._stub_security('#!/bin/sh\necho "not found" >&2\nexit 44\n')
        proc = self._source("social", preset="previous-value")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), "[previous-value]")
        self.assertEqual(proc.stderr.decode(), "", "keychain miss must not spam")

    def test_missing_keychain_item_does_not_create_the_env_var(self):
        self._stub_security('#!/bin/sh\nexit 44\n')
        proc = self._source("social")
        self.assertEqual(proc.stdout.decode().strip(), "[UNSET]")

    def test_no_active_label_unsets_nothing(self):
        self._stub_security('#!/bin/sh\necho "should-not-be-called"\n')
        proc = self._source(None, preset="previous-value")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.decode().strip(), "[previous-value]")


class WriteActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-wa-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_creates_parent_dir_0700_and_file_0600(self):
        target = os.path.join(self.tmp, "cfg", "active.sh")
        written = shell.write_active("social", target)
        self.assertEqual(written, target)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(target)).st_mode), 0o700
        )

    def test_rewrite_replaces_content_and_keeps_mode(self):
        target = os.path.join(self.tmp, "active.sh")
        shell.write_active("social", target)
        shell.write_active("devops", target)
        with open(target) as stream:
            body = stream.read()
        self.assertEqual(body, shell.active_sh_contents("devops"))
        self.assertNotIn("ctr:social", body)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)

    def test_no_leftover_temp_files(self):
        target = os.path.join(self.tmp, "active.sh")
        shell.write_active("social", target)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["active.sh"])


PREFIX = "# user's own rc\nexport EDITOR=vim\n"
SUFFIX = "alias ll='ls -la'\n# trailing comment\n"


class ZshrcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-rc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rc = os.path.join(self.tmp, "zshrc")
        self.active = os.path.join(self.tmp, "active.sh")

    def _write_rc(self, text):
        with open(self.rc, "w") as stream:
            stream.write(text)

    def _rc(self):
        with open(self.rc) as stream:
            return stream.read()

    def _install(self, active=None):
        return shell.install_zshrc(self.rc, active or self.active)

    def test_creates_rc_when_missing(self):
        self._install()
        body = self._rc()
        self.assertEqual(body.count(ZSHRC_BEGIN), 1)
        self.assertEqual(body.count(ZSHRC_END), 1)
        self.assertIn(self.active, body)

    def test_idempotent_second_install_is_a_no_op(self):
        self._write_rc(PREFIX + SUFFIX)
        self._install()
        once = self._rc()
        self._install()
        twice = self._rc()
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(ZSHRC_BEGIN), 1)

    def test_appends_without_touching_a_single_surrounding_byte(self):
        original = PREFIX + SUFFIX
        self._write_rc(original)
        self._install()
        self.assertEqual(self._rc(), original + shell.zshrc_block(self.active))

    def test_only_a_missing_final_newline_is_added(self):
        original = PREFIX + "no newline at EOF"
        self._write_rc(original)
        self._install()
        self.assertEqual(
            self._rc(), original + "\n" + shell.zshrc_block(self.active)
        )

    def test_block_is_replaced_when_the_content_changes(self):
        self._write_rc(PREFIX)
        self._install()
        other = os.path.join(self.tmp, "elsewhere", "active.sh")
        self._install(other)
        body = self._rc()
        self.assertEqual(body.count(ZSHRC_BEGIN), 1)
        self.assertIn(other, body)
        self.assertNotIn('"%s"' % self.active, body)
        self.assertTrue(body.startswith(PREFIX))

    def test_block_keeps_its_original_position(self):
        self._write_rc(PREFIX + shell.zshrc_block(self.active) + SUFFIX)
        self._install()
        body = self._rc()
        self.assertTrue(body.startswith(PREFIX))
        self.assertTrue(body.endswith(SUFFIX))

    def test_in_place_refresh_is_byte_exact_outside_the_markers(self):
        other = os.path.join(self.tmp, "elsewhere.sh")
        self._write_rc(PREFIX + shell.zshrc_block(self.active) + SUFFIX)
        self._install(other)
        self.assertEqual(
            self._rc(), PREFIX + shell.zshrc_block(other) + SUFFIX
        )

    def test_duplicate_blocks_collapse_to_one(self):
        duplicate = shell.zshrc_block(self.active)
        self._write_rc(PREFIX + duplicate + "\n" + duplicate + SUFFIX)
        self._install()
        body = self._rc()
        self.assertEqual(body.count(ZSHRC_BEGIN), 1)
        self.assertTrue(body.startswith(PREFIX))
        self.assertTrue(body.endswith(SUFFIX))

    def test_unterminated_block_is_repaired(self):
        """A lost END marker must clean up ctr's lines, not run to EOF.

        The previous test put NOTHING after the orphan marker, so it could only
        ever prove that a `_skip_block` running to EOF had run to EOF. With
        real content after the marker it showed the actual behaviour: every
        later line was deleted, and the one-time backup was by then a stale
        day-one copy, so `export AWS_PROFILE=prod` was gone for good.
        """
        orphan = shell.zshrc_block(self.active)
        orphan = orphan[: orphan.index(ZSHRC_END)]  # ctr's own lines, END lost
        self._write_rc(PREFIX + orphan + "export AWS_PROFILE=prod\n")
        self._install()
        body = self._rc()
        self.assertEqual(body.count(ZSHRC_BEGIN), 1)
        self.assertEqual(body.count(ZSHRC_END), 1)
        self.assertIn("export AWS_PROFILE=prod", body, "user content must survive")
        self.assertTrue(body.startswith(PREFIX))
        # ctr's own orphaned lines are cleaned up, not duplicated.
        self.assertEqual(body.count('if [ -r "%s" ]' % self.active), 1)

    def test_an_orphan_marker_never_deletes_what_follows_it(self):
        self._write_rc(PREFIX + ZSHRC_BEGIN + "\nexport AWS_PROFILE=prod\n"
                       "alias deploy='make ship'\n")
        self._install()
        body = self._rc()
        self.assertIn("export AWS_PROFILE=prod", body)
        self.assertIn("alias deploy='make ship'", body)

    def test_a_symlinked_rc_is_followed_not_replaced(self):
        """The common dotfiles setup: ~/.zshrc -> dotfiles/zshrc.

        ``os.replace`` swaps the LINK, so writing straight to it detached the
        rc into a private copy — every later dotfiles edit silently stopped
        reaching the shell and the ctr block never reached the repo.
        """
        real = os.path.join(self.tmp, "dotfiles_zshrc")
        link = os.path.join(self.tmp, "linked_zshrc")
        with open(real, "w") as stream:
            stream.write(PREFIX)
        os.symlink(real, link)

        written = shell.install_zshrc(link, self.active)

        self.assertTrue(os.path.islink(link), "the symlink must survive")
        # realpath on both sides: macOS resolves /var -> /private/var.
        self.assertEqual(os.path.realpath(real), os.path.realpath(written),
                         "it must report the file it really wrote")
        with open(real) as stream:
            self.assertIn(ZSHRC_BEGIN, stream.read())
        self.assertTrue(shell.uninstall_zshrc(link))
        self.assertTrue(os.path.islink(link))
        with open(real) as stream:
            self.assertEqual(PREFIX, stream.read())

    def test_active_sh_warns_about_the_two_variables_that_defeat_it(self):
        """MEASURED: ANTHROPIC_API_KEY makes `claude -p` hang; ANTHROPIC_AUTH_TOKEN
        makes it say "Not logged in". Warn, but never unset — they may be
        deliberate for another tool."""
        body = shell.active_sh_contents("social")
        for name in shell.CONFLICTING_ENV_VARS:
            self.assertIn(name, body)
        self.assertNotIn("unset ANTHROPIC", body)
        script = os.path.join(self.tmp, "active_warn.sh")
        with open(script, "w") as stream:
            stream.write(body)
        for shell_bin in ("sh", "zsh"):
            proc = subprocess.run([shell_bin, "-n", script], stderr=subprocess.PIPE)
            self.assertEqual(0, proc.returncode,
                             "%s: %s" % (shell_bin, proc.stderr.decode()))

    def test_backup_is_taken_once_with_the_original_content(self):
        original = PREFIX + SUFFIX
        self._write_rc(original)
        self._install()
        backup = self.rc + shell.BACKUP_SUFFIX
        self.assertTrue(os.path.exists(backup))
        with open(backup) as stream:
            self.assertEqual(stream.read(), original)
        self._install(os.path.join(self.tmp, "other.sh"))
        with open(backup) as stream:
            self.assertEqual(stream.read(), original, "backup must not be rewritten")

    def test_no_backup_when_the_rc_did_not_exist(self):
        self._install()
        self.assertFalse(os.path.exists(self.rc + shell.BACKUP_SUFFIX))

    def test_rc_mode_is_preserved(self):
        self._write_rc(PREFIX)
        os.chmod(self.rc, 0o644)
        self._install()
        self.assertEqual(stat.S_IMODE(os.stat(self.rc).st_mode), 0o644)

    def test_installed_block_is_valid_sh(self):
        self._install()
        proc = subprocess.run(
            ["sh", "-n", self.rc], stderr=subprocess.PIPE
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())

    def test_home_paths_become_portable(self):
        home_active = os.path.join(os.path.expanduser("~"), ".config/ctr/active.sh")
        shell.install_zshrc(self.rc, home_active)
        body = self._rc()
        self.assertIn('"$HOME/.config/ctr/active.sh"', body)
        self.assertNotIn(os.path.expanduser("~") + "/.config", body)

    def test_uninstall_exactly_reverses_install(self):
        original = PREFIX + SUFFIX
        self._write_rc(original)
        self._install()
        self.assertTrue(shell.uninstall_zshrc(self.rc))
        self.assertEqual(self._rc(), original)

    def test_uninstall_removes_an_embedded_block_only(self):
        self._write_rc(PREFIX + shell.zshrc_block(self.active) + SUFFIX)
        self.assertTrue(shell.uninstall_zshrc(self.rc))
        self.assertEqual(self._rc(), PREFIX + SUFFIX)

    def test_uninstall_is_false_when_absent(self):
        self._write_rc(PREFIX)
        self.assertFalse(shell.uninstall_zshrc(self.rc))
        self.assertEqual(self._rc(), PREFIX)

    def test_uninstall_missing_file_is_false(self):
        self.assertFalse(shell.uninstall_zshrc(os.path.join(self.tmp, "nope")))

    def test_rc_never_contains_a_token(self):
        self._write_rc(PREFIX)
        self._install()
        body = self._rc()
        self.assertIsNone(_TOKEN_PREFIX_RE.search(body))


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ctr-st-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rc = os.path.join(self.tmp, "zshrc")
        self.active = os.path.join(self.tmp, "active.sh")

    def test_reports_uninstalled_state(self):
        info = shell.status(self.rc, self.active)
        self.assertFalse(info["rc_installed"])
        self.assertFalse(info["active_exists"])
        self.assertIsNone(info["active_label"])

    def test_reports_installed_state(self):
        shell.install_zshrc(self.rc, self.active)
        shell.write_active("social", self.active)
        info = shell.status(self.rc, self.active)
        self.assertTrue(info["rc_installed"])
        self.assertTrue(info["active_exists"])
        self.assertEqual(info["active_label"], "social")
        self.assertEqual(info["active_mode"], 0o600)


if __name__ == "__main__":
    unittest.main()
