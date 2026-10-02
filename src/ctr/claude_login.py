"""Seamless switching: point Claude Code's own credentials store at a ctr token.

A running `claude` re-reads its keychain item before every token-refresh check
(30 s read cache) and immediately after any 401, so writing another account's
token there moves every RUNNING session that was started WITHOUT
CLAUDE_CODE_OAUTH_TOKEN onto that account, no restart. A session started with
that env var ignores the store entirely and stays pinned to its token.

MEASURED 2026-09-30 (Claude Code 2.1.285, isolated CLAUDE_CONFIG_DIR, logging
proxy fingerprinting the bearer of every request):

* a setup-token stored as {accessToken, refreshToken: null, expiresAt: null,
  scopes: ["user:inference"]} is accepted (no refresh is attempted: a null
  expiry is "not expired");
* switching account A -> B under a running session: the next prompt 35 s
  later authenticated as B, with B's own rate-limit headers;
* switching with no wait: one request with the cached old token got 401, the
  session re-read the store at once and the retry authenticated as the new
  account — the user saw nothing;
* a session started with CLAUDE_CODE_OAUTH_TOKEN kept its token regardless.

The item also carries other data (`mcpOAuth`: MCP server logins). A switch
replaces ONLY `claudeAiOauth` and writes everything else back untouched. The
interactive /login it replaces (the one with a refresh token) is saved first
to the ctr item backup_service(), re-captured on every switch so a rotated
refresh token is never lost, and put back by restore_login().

Writing: `security -i` reads commands from stdin, but only lines up to
SECURITY_I_LIMIT bytes; the `-w` prompt silently truncates at 128 bytes. The
item is ~18 KB on a Mac with several MCP logins, so larger payloads go through
argv as hex — exactly what Claude Code itself does for this same item on every
refresh ("Keychain payload exceeds security -i stdin limit; using argv"). It is
the one place ctr passes secret material in argv, and only because the store's
owner already does.

Python 3.8 compatible. stdlib only. Never prints or logs a token.
"""

import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from typing import Dict, Optional

from ctr import host
from ctr.model import CLAUDE_KEYCHAIN_SERVICE

SECURITY = "/usr/bin/security"
TIMEOUT_S = 15
#: Claude Code's own bound for a `security -i` command line (bytes).
SECURITY_I_LIMIT = 4032
#: ctr's copy of the interactive /login a switch displaced is kept under
#: BACKUP_PREFIX + <Claude's service name>, so each config dir has its own.
BACKUP_PREFIX = "ctr-login:"
BACKUP_ACCOUNT = "ctr"
#: Scopes a long-lived `claude setup-token` token carries.
SETUP_TOKEN_SCOPES = ["user:inference"]

_USER_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

#: Linux: Claude Code keeps the credentials document in this file inside its
#: config dir (~/.claude, or $CLAUDE_CONFIG_DIR), mode 0600.
CREDENTIALS_FILE = ".credentials.json"
#: Linux: ctr's copy of a displaced /login sits next to it with this suffix.
LOGIN_BACKUP_SUFFIX = ".ctr-login"
#: Linux: the raw previous file, copied before every write ctr makes.
PREVIOUS_SUFFIX = ".ctr-prev"


class LoginStoreError(RuntimeError):
    """A credentials-store operation failed. The message never holds a secret."""


def _run(cmd, stdin_data: Optional[str] = None):
    """The single subprocess seam — tests replace this. Returns (rc, out, err)."""
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data or "",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=TIMEOUT_S,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", exc.strerror or "os error"
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Where Claude Code keeps it
# ---------------------------------------------------------------------------


def service_name(environ: Optional[Dict[str, str]] = None) -> str:
    """The keychain service Claude Code uses, mirroring its own rule.

    Default: "Claude Code-credentials". With CLAUDE_CONFIG_DIR set (or
    CLAUDE_SECURESTORAGE_CONFIG_DIR non-empty), Claude appends
    "-" + sha256(dir)[:8], so a separate config dir has a separate login.
    """
    env = os.environ if environ is None else environ
    if not host.is_macos():
        return credentials_path(env)
    secure = env.get("CLAUDE_SECURESTORAGE_CONFIG_DIR")
    if secure is not None:
        directory = secure
        plain = not secure
    else:
        directory = env.get("CLAUDE_CONFIG_DIR") or ""
        plain = not directory
    if plain:
        return CLAUDE_KEYCHAIN_SERVICE
    digest = hashlib.sha256(unicodedata.normalize("NFC", directory).encode("utf-8"))
    return "%s-%s" % (CLAUDE_KEYCHAIN_SERVICE, digest.hexdigest()[:8])


def credentials_path(environ: Optional[Dict[str, str]] = None) -> str:
    """Linux: the file Claude Code keeps its credentials in.

    On Linux the "service" ctr passes around IS this absolute path, so the
    rest of the module (backup naming, summaries) works unchanged.
    """
    env = os.environ if environ is None else environ
    directory = env.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(directory, CREDENTIALS_FILE)


def keychain_account() -> str:
    """The account attribute Claude Code files its item under ($USER)."""
    try:
        name = os.environ.get("USER") or getpass.getuser()
    except Exception:
        name = ""
    return name if name and _USER_RE.match(name) else "claude-code-user"


# ---------------------------------------------------------------------------
# Raw read / write
# ---------------------------------------------------------------------------


#: `security` exit status for "no such item". Any OTHER failure (36 locked,
#: 124 timeout, an ACL refusal) is NOT an empty store: treating it as one would
#: write back a document without mcpOAuth and without backing up the /login.
#: Claude Code's own mutate() refuses to write after a failed read, too.
ITEM_NOT_FOUND = 44


def _read_text(service: str, account: str) -> Optional[str]:
    """The item's text, None when it does not exist. Raises on any other failure."""
    if not host.is_macos():
        return _read_file(_linux_path(service))
    rc, out, err = _run([SECURITY, "find-generic-password", "-a", account, "-s", service, "-w"])
    if rc == ITEM_NOT_FOUND:
        return None
    if rc != 0:
        raise LoginStoreError(
            "could not read keychain item %r (rc=%d: %s); nothing was changed"
            % (service, rc, (err or "").strip()[:80])
        )
    return out.rstrip("\r\n") or None


def _write_text(service: str, account: str, text: str) -> None:
    """Upsert (service, account) with `text`. Never deletes: the ACL survives."""
    if not host.is_macos():
        _write_file(_linux_path(service), text)
        return
    hexed = text.encode("utf-8").hex()
    line = 'add-generic-password -U -a "%s" -s "%s" -X "%s"\n' % (account, service, hexed)
    if len(line) <= SECURITY_I_LIMIT:
        rc, _out, err = _run([SECURITY, "-i"], stdin_data=line)
    else:
        rc, _out, err = _run(
            [SECURITY, "add-generic-password", "-U", "-a", account, "-s", service, "-X", hexed]
        )
    if rc != 0:
        raise LoginStoreError(
            "keychain write to %r failed (rc=%d): %s"
            % (service, rc, (err or "").strip().replace(hexed, "***").replace(text, "***")[:120])
        )
    try:
        stored = _read_text(service, account)
    except LoginStoreError:
        stored = None
    if stored != text:
        raise LoginStoreError("keychain write to %r could not be verified" % service)


def _linux_path(service: str) -> str:
    """Linux: the file behind a "service" (the credentials path or its backup)."""
    if service.startswith(BACKUP_PREFIX):
        return service[len(BACKUP_PREFIX):] + LOGIN_BACKUP_SUFFIX
    return service


def _read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except (IOError, OSError) as exc:
        raise LoginStoreError(
            "could not read %r (%s); nothing was changed" % (path, exc.strerror or "os error")
        )
    return text.rstrip("\r\n") or None


def _write_file(path: str, text: str) -> None:
    """Back up the current file, then replace it atomically at mode 0600."""
    directory = os.path.dirname(path) or "."
    try:
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        if os.path.exists(path):
            # Created 0600 up front: copyfile alone would create it under the
            # umask (0644) and expose the refresh token until the chmod.
            previous = path + PREVIOUS_SUFFIX
            os.close(os.open(previous, os.O_WRONLY | os.O_CREAT, 0o600))
            os.chmod(previous, 0o600)
            shutil.copyfile(path, previous)
        handle, tmp = tempfile.mkstemp(dir=directory, prefix=".ctr-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (IOError, OSError) as exc:
        raise LoginStoreError("write to %r failed (%s)" % (path, exc.strerror or "os error"))
    if _read_file(path) != text:
        raise LoginStoreError("write to %r could not be verified" % path)


def _parse(text: Optional[str], what: str) -> Dict:
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise LoginStoreError("%s is not valid JSON; refusing to overwrite it" % what)
    if not isinstance(data, dict):
        raise LoginStoreError("%s is not a JSON object; refusing to overwrite it" % what)
    return data


def read_store(environ: Optional[Dict[str, str]] = None) -> Dict:
    """Claude Code's credentials document ({} when there is none)."""
    service = service_name(environ)
    return _parse(_read_text(service, keychain_account()), repr(service))


def _oauth(data: Dict) -> Dict:
    value = data.get("claudeAiOauth")
    return value if isinstance(value, dict) else {}


def is_interactive_login(oauth: Dict) -> bool:
    """A real /login: it can refresh itself. A setup-token cannot."""
    refresh = oauth.get("refreshToken")
    return isinstance(refresh, str) and bool(refresh)


def setup_token_oauth(token: str, subscription: str = "") -> Dict:
    """The claudeAiOauth record for a long-lived setup-token (measured shape)."""
    return {
        "accessToken": token,
        "refreshToken": None,
        "expiresAt": None,
        "scopes": list(SETUP_TOKEN_SCOPES),
        "subscriptionType": subscription or None,
    }


# ---------------------------------------------------------------------------
# What the store holds
# ---------------------------------------------------------------------------


def fingerprint(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:12]


def holder(tokens: Dict[str, str], environ: Optional[Dict[str, str]] = None) -> str:
    """Who the store currently authenticates as.

    Returns a ctr label, "login" (an interactive /login), "none", or "other".
    Tokens are compared in memory only.
    """
    try:
        oauth = _oauth(read_store(environ))
    except LoginStoreError:
        return "unreadable"
    access = oauth.get("accessToken")
    if not isinstance(access, str) or not access:
        return "none"
    for label, token in tokens.items():
        if token and token == access:
            return label
    return "login" if is_interactive_login(oauth) else "other"


def backup_service(environ: Optional[Dict[str, str]] = None) -> str:
    return BACKUP_PREFIX + service_name(environ)


def has_backup(environ: Optional[Dict[str, str]] = None) -> bool:
    try:
        return _read_text(backup_service(environ), BACKUP_ACCOUNT) is not None
    except LoginStoreError:
        return False


# ---------------------------------------------------------------------------
# Switch / restore
# ---------------------------------------------------------------------------


def switch_to(token: str, subscription: str = "", environ: Optional[Dict[str, str]] = None) -> Dict:
    """Make Claude Code's store hold `token`. Returns a secret-free summary.

    {"service": str, "backed_up_login": bool, "unchanged": bool}
    """
    if not token or not token.strip():
        raise ValueError("refusing to switch to an empty token")
    service = service_name(environ)
    account = keychain_account()
    data = _parse(_read_text(service, account), repr(service))
    current = _oauth(data)
    if current.get("accessToken") == token:
        return {"service": service, "backed_up_login": False, "unchanged": True}
    backed_up = False
    if is_interactive_login(current):
        # Re-captured on EVERY switch away: a running session may have rotated
        # the refresh token since the last one, and only the newest is valid.
        _write_text(backup_service(environ), BACKUP_ACCOUNT, json.dumps(current))
        backed_up = True
    updated = dict(data)
    updated["claudeAiOauth"] = setup_token_oauth(token, subscription)
    _write_text(service, account, json.dumps(updated))
    return {"service": service, "backed_up_login": backed_up, "unchanged": False}


def restore_login(environ: Optional[Dict[str, str]] = None) -> Dict:
    """Put the saved interactive /login back into Claude Code's store.

    When the store ALREADY holds an interactive login (you ran `claude /login`
    since the switch), that one is newer than the backup: it is kept, and
    becomes the backup, instead of being overwritten by an older copy.
    """
    service = service_name(environ)
    account = keychain_account()
    data = _parse(_read_text(service, account), repr(service))
    current = _oauth(data)
    if is_interactive_login(current):
        _write_text(backup_service(environ), BACKUP_ACCOUNT, json.dumps(current))
        return {"service": service, "restored": False, "already_login": True}
    saved = _parse(_read_text(backup_service(environ), BACKUP_ACCOUNT), "the saved login")
    if not is_interactive_login(saved):
        raise LoginStoreError(
            "no saved interactive login to restore (ctr saves one the first time "
            "`ctr switch` replaces a /login)"
        )
    updated = dict(data)
    updated["claudeAiOauth"] = saved
    _write_text(service, account, json.dumps(updated))
    return {"service": service, "restored": True, "already_login": False}


__all__ = [
    "BACKUP_PREFIX",
    "backup_service",
    "LoginStoreError",
    "fingerprint",
    "has_backup",
    "holder",
    "is_interactive_login",
    "keychain_account",
    "read_store",
    "restore_login",
    "service_name",
    "setup_token_oauth",
    "switch_to",
]
