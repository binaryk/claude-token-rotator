"""Linux secret store for ctr: the freedesktop Secret Service via `secret-tool`.

Same interface and rules as keychain.py (get / exists / set / delete, keyed by
the `ctr:<label>` service name): a secret never travels in argv, never reaches
a message, and an empty secret is refused.

MEASURED 2026-10-02 on the Omarchy box `office` (Arch, gnome-keyring with the
passwordless Default_keyring, socket-activated):

* `secret-tool store` and `lookup` work from an ssh session AND from a systemd
  user unit (`systemd-run --user --wait --pipe`), so the monitor can read it;
* `store` reads the secret from stdin VERBATIM — a trailing newline is stored —
  so ctr writes the bare value and verifies it by reading it back;
* `lookup` prints the secret with no newline, exit 1 when there is none;
* `clear` exits 0 when it removed something and 1 when there was nothing.

Python 3.8 compatible. stdlib only.
"""

import subprocess
from typing import List, Optional, Tuple

SECRET_TOOL = "secret-tool"
DEFAULT_TIMEOUT_S = 15
#: The attribute every ctr item is filed under (value: the service name).
ATTRIBUTE = "service"
ITEM_LABEL = "ctr long-lived Claude token"


class SecretStoreError(RuntimeError):
    """A Secret Service operation failed. The message never contains a secret."""


def _run(
    cmd: List[str], stdin_data: Optional[str] = None, timeout_s: int = DEFAULT_TIMEOUT_S
) -> Tuple[int, str, str]:
    """Run `secret-tool`. The single subprocess seam — tests replace this.

    `start_new_session=True` keeps a locked keyring's unlock prompt from ever
    grabbing a terminal; the timeout bounds a GUI prompt nobody answers.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            start_new_session=True,
        )
    except OSError as exc:
        return 127, "", "cannot run %s: %s" % (cmd[0], exc.strerror or "os error")
    try:
        out, err = proc.communicate(input=stdin_data or "", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 124, "", "timed out after %ds (is the keyring locked?)" % timeout_s
    return proc.returncode, out, err


def _scrub(text: str, secret: Optional[str]) -> str:
    cleaned = (text or "").strip()
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned.replace("\n", " ")[:200]


def get(service: str) -> Optional[str]:
    """The stored secret, or None when the item is absent or empty."""
    if not service:
        return None
    rc, out, _err = _run([SECRET_TOOL, "lookup", ATTRIBUTE, service])
    if rc != 0:
        return None
    return out or None


def exists(service: str) -> bool:
    return get(service) is not None


def set(service: str, account: str, secret: str) -> None:  # noqa: A001 (frozen API)
    """Upsert the item for `service`. Raises SecretStoreError; never logs the secret."""
    if not service:
        raise ValueError("service is required")
    if not secret or not secret.strip():
        raise ValueError("refusing to store an empty secret")
    label = "%s (%s)" % (ITEM_LABEL, service)
    del account  # the Secret Service item is keyed by service alone
    rc, _out, err = _run(
        [SECRET_TOOL, "store", "--label=%s" % label, ATTRIBUTE, service], stdin_data=secret
    )
    if rc != 0:
        raise SecretStoreError(
            "secret store write failed for %s (rc=%d): %s" % (service, rc, _scrub(err, secret))
        )
    if get(service) != secret:
        raise SecretStoreError(
            "secret store write for %s could not be verified; the item is missing "
            "or different" % service
        )


def delete(service: str) -> bool:
    """Delete the item for `service`. False when none existed."""
    if not service:
        return False
    rc, _out, _err = _run([SECRET_TOOL, "clear", ATTRIBUTE, service])
    return rc == 0


__all__ = ["SecretStoreError", "get", "exists", "set", "delete", "SECRET_TOOL"]
