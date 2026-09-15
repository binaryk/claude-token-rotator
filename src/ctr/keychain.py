"""macOS keychain wrapper for ctr.

Every secret ctr manages lives here and nowhere else. The rules this module
enforces:

* a secret is NEVER passed as an argv element (it would be visible in `ps`);
  `security add-generic-password` reads it from stdin instead;
* a secret NEVER reaches an exception message, stdout or a log line — anything
  we surface is scrubbed first and tokens are shown via `model.redact()`;
* an empty secret is refused, because `security` happily stores one.

MEASURED 2026-09-15 on this Mac (security 55.x, macOS 25.3):

    printf '%s\\n%s\\n' "$secret" "$secret" |
        security add-generic-password -U -s ctr:label -a acct -w

  stores the secret with zero argv exposure. `-w` given as the LAST option
  prompts on stdin and asks for the value TWICE. Writing it only ONCE makes
  `security` store an EMPTY password and still exit 0 — hence the mandatory
  read-back verification in `set()`.

Python 3.8 compatible. stdlib only.
"""

import json
import os
import subprocess
from typing import Dict, List, Optional, Tuple

from ctr.model import CLAUDE_KEYCHAIN_SERVICE, KEYCHAIN_PREFIX

SECURITY = "/usr/bin/security"
CLAUDE_CONFIG_JSON = "~/.claude.json"
DEFAULT_TIMEOUT_S = 15
#: Account recorded on a keychain item when the caller has no better name.
DEFAULT_ACCOUNT = "ctr"
#: `security -D` kind, so the items are recognisable in Keychain Access.
ITEM_KIND = "ctr long-lived Claude token"


class KeychainError(RuntimeError):
    """A keychain operation failed. The message never contains a secret."""


def _run(
    cmd: List[str],
    stdin_data: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> Tuple[int, str, str]:
    """Run `security`. The single subprocess seam — tests replace this.

    `stdin_data` may contain a secret; it is never echoed back by `security`
    and never lands in argv.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        return 127, "", "cannot run %s: %s" % (cmd[0], exc.strerror or "os error")
    try:
        out, err = proc.communicate(input=stdin_data or "", timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 124, "", "timed out after %ds" % timeout_s
    return proc.returncode, out, err


def _scrub(text: str, secret: Optional[str]) -> str:
    """Remove any accidental occurrence of `secret` from text we may surface."""
    cleaned = (text or "").strip()
    if secret:
        cleaned = cleaned.replace(secret, "***")
    return cleaned.replace("\n", " ")[:200]


def get(service: str) -> Optional[str]:
    """Return the stored secret, or None when the item is absent or empty."""
    if not service:
        return None
    rc, out, _err = _run([SECURITY, "find-generic-password", "-s", service, "-w"])
    if rc != 0:
        return None
    secret = out.rstrip("\r\n")
    return secret or None


def exists(service: str) -> bool:
    """True when the item is present, WITHOUT retrieving its secret."""
    if not service:
        return False
    rc, _out, _err = _run([SECURITY, "find-generic-password", "-s", service])
    return rc == 0


def set(service: str, account: str, secret: str) -> None:  # noqa: A001 (frozen API)
    """Upsert a keychain item. Raises KeychainError; never logs the secret."""
    if not service:
        raise ValueError("service is required")
    if not secret or not secret.strip():
        raise ValueError("refusing to store an empty secret")
    cmd = [
        SECURITY,
        "add-generic-password",
        "-U",
        "-a",
        account or DEFAULT_ACCOUNT,
        "-s",
        service,
        "-D",
        ITEM_KIND,
        "-w",  # MUST stay last: makes `security` read the value from stdin
    ]
    rc, _out, err = _run(cmd, stdin_data="%s\n%s\n" % (secret, secret))
    if rc != 0:
        raise KeychainError(
            "keychain write failed for %s (rc=%d): %s" % (service, rc, _scrub(err, secret))
        )
    if get(service) != secret:
        raise KeychainError(
            "keychain write for %s could not be verified — the item is missing "
            "or empty; nothing was stored" % service
        )


def delete(service: str) -> bool:
    """Delete the item. False when it was not there."""
    if not service:
        return False
    rc, _out, _err = _run([SECURITY, "delete-generic-password", "-s", service])
    return rc == 0


def list_ctr_services(labels: Optional[List[str]] = None) -> List[str]:
    """Return the `ctr:<label>` services that really exist in the keychain.

    `security` offers no prefix search and `dump-keychain` would expose every
    item on the Mac, so the candidate labels come from the ctr registry
    (tokens.json) and each one is confirmed with a single attribute lookup
    (no secret is read). Pass `labels` to avoid touching the registry.
    """
    if labels is None:
        labels = _registered_labels()
    services = []
    for label in labels:
        service = KEYCHAIN_PREFIX + label
        if exists(service):
            services.append(service)
    return services


def _registered_labels() -> List[str]:
    from ctr import store as store_module  # lazy: keeps this module I/O-light

    try:
        return [record.label for record in store_module.Store().tokens()]
    except Exception:  # a broken/absent registry must not break `ctr list`
        return []


def token_for(label: str) -> Optional[str]:
    """The ctr-managed token for `label`, or None."""
    if not label:
        return None
    return get(KEYCHAIN_PREFIX + label)


def claude_login_token() -> Optional[str]:
    """The interactive-login access token Claude Code stores for itself."""
    blob = get(CLAUDE_KEYCHAIN_SERVICE)
    if not blob:
        return None
    oauth = _claude_oauth_blob(blob)
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


def claude_login_info() -> Dict:
    """Best-effort, secret-free description of the current interactive login.

    Returns {"account": str, "subscription": str, "expires_at": int}; missing
    pieces come back as "" / 0 rather than raising.
    """
    info = {"account": "", "subscription": "", "expires_at": 0}
    blob = get(CLAUDE_KEYCHAIN_SERVICE)
    if blob:
        oauth = _claude_oauth_blob(blob)
        subscription = oauth.get("subscriptionType")
        if isinstance(subscription, str):
            info["subscription"] = subscription
        info["expires_at"] = _epoch_seconds(oauth.get("expiresAt"))
    account = _claude_account_email()
    if account:
        info["account"] = account
    return info


def _claude_oauth_blob(blob: str) -> Dict:
    """Parse the `Claude Code-credentials` JSON. Never raises, never logs it."""
    try:
        data = json.loads(blob)
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else {}


def _claude_account_email() -> str:
    """`oauthAccount.emailAddress` from ~/.claude.json, or ""."""
    path = os.path.expanduser(CLAUDE_CONFIG_JSON)
    try:
        with open(path, "r") as handle:
            data = json.load(handle)
    except (IOError, OSError, ValueError):
        return ""
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return ""
    email = account.get("emailAddress")
    return email if isinstance(email, str) else ""


def _epoch_seconds(value) -> int:
    """Claude Code stores expiry in MILLISECONDS; normalise to seconds."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    if number > 100000000000:  # > year 5138 in seconds => it is milliseconds
        number //= 1000
    return number if number > 0 else 0


__all__ = [
    "KeychainError",
    "get",
    "exists",
    "set",
    "delete",
    "list_ctr_services",
    "token_for",
    "claude_login_token",
    "claude_login_info",
]
