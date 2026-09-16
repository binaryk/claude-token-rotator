"""A stale item under a different account must never shadow a fresh write.

macOS keys a generic-password item by (service, ACCOUNT), and
`security find-generic-password -s SERVICE -w` (no account) returns SOME
matching item — in practice the older one. `keychain.set()` writes under a
specific account but verifies via the service-only `get()`, so a leftover item
under a different account made a perfectly good write look failed.

Measured live 2026-09-16: a failed `ctr add work --from-login` stored an
EMPTY item under account "alice@example.com" (the TTY bug), then
`ctr add work <token>` stored the real token under account "work" and still
reported "keychain write for ctr:work could not be verified".

The fake below models the (service, account) keying and, like macOS, resolves a
service-only lookup to the OLDEST item — so without the purge-before-write fix
these tests reproduce the failure, and with it they pass.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import keychain  # noqa: E402

REAL = "sk-ant-oat01-REALTOKEN-do-not-use-0123456789abcdefghij"
SERVICE = "ctr:work"


class FakeSecurity(object):
    """A minimal `security` that keys items by (service, account), insertion-ordered."""

    def __init__(self):
        self.items = []  # list of [service, account, secret], oldest first

    def _find(self, service, account):
        for item in self.items:
            if item[0] == service and (account is None or item[1] == account):
                return item
        return None

    def __call__(self, cmd, stdin_data=None, timeout_s=15):
        verb = cmd[1]
        service = cmd[cmd.index("-s") + 1] if "-s" in cmd else None
        account = cmd[cmd.index("-a") + 1] if "-a" in cmd else None

        if verb == "find-generic-password":
            item = self._find(service, account)  # service-only -> OLDEST match
            if item is None:
                return 44, "", "SecKeychainSearchCopyNext: item not found"
            if "-w" in cmd:
                return 0, item[2] + "\n", ""
            return 0, "attributes", ""

        if verb == "add-generic-password":
            secret = (stdin_data or "").split("\n", 1)[0]  # first line, like -w
            existing = self._find(service, account)
            if existing is not None:
                if "-U" not in cmd:
                    return 45, "", "item already exists"
                existing[2] = secret
            else:
                self.items.append([service, account, secret])
            return 0, "", ""

        if verb == "delete-generic-password":
            item = self._find(service, account)  # removes ONE, oldest first
            if item is None:
                return 44, "", "item not found"
            self.items.remove(item)
            return 0, "", ""

        return 1, "", "unknown verb %r" % verb


class DuplicateShadowTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSecurity()
        original = keychain._run
        keychain._run = self.fake
        self.addCleanup(lambda: setattr(keychain, "_run", original))

    def test_a_stale_empty_ghost_does_not_fail_the_write(self):
        # the pre-fix wreckage: an empty item under the login email
        self.fake.items.append([SERVICE, "alice@example.com", ""])
        # the retry, under a different account
        keychain.set(SERVICE, "work", REAL)  # must NOT raise
        self.assertEqual(REAL, keychain.get(SERVICE))

    def test_only_one_item_survives_the_write(self):
        self.fake.items.append([SERVICE, "alice@example.com", ""])
        self.fake.items.append([SERVICE, "stale2", ""])
        keychain.set(SERVICE, "work", REAL)
        survivors = [i for i in self.fake.items if i[0] == SERVICE]
        self.assertEqual(1, len(survivors), "duplicates must be purged before writing")
        self.assertEqual(REAL, survivors[0][2])

    def test_delete_purges_every_account(self):
        self.fake.items.append([SERVICE, "a@example.com", ""])
        self.fake.items.append([SERVICE, "b", REAL])
        self.fake.items.append(["ctr:other", "c", "keep-me"])
        self.assertTrue(keychain.delete(SERVICE))
        self.assertEqual([], [i for i in self.fake.items if i[0] == SERVICE])
        self.assertEqual(1, len([i for i in self.fake.items if i[0] == "ctr:other"]),
                         "delete must not touch other services")

    def test_get_returns_the_secret_when_only_the_good_item_exists(self):
        keychain.set(SERVICE, "work", REAL)
        self.assertEqual(REAL, keychain.get(SERVICE))

    def test_write_then_read_round_trips_without_any_ghost(self):
        keychain.set(SERVICE, "work", REAL)
        keychain.set(SERVICE, "work", REAL + "-rotated")
        self.assertEqual(REAL + "-rotated", keychain.get(SERVICE))
        self.assertEqual(1, len([i for i in self.fake.items if i[0] == SERVICE]))


if __name__ == "__main__":
    unittest.main()
