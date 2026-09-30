"""The one place a switch happens — `ctr use/switch/next`, the TUI and the monitor.

Two modes (Store.switch_mode):

* "env" (v1, `ctr use`): active.sh exports CLAUDE_CODE_OAUTH_TOKEN for NEW
  shells. Running sessions keep their token until `ctr rollover`.
* "keychain" (`ctr switch`): the token goes into Claude Code's own credentials
  store (claude_login.switch_to), active.sh exports nothing, and every running
  session started without the env var follows within one request.

Python 3.8 compatible. stdlib only.
"""

from typing import Dict, Optional

from ctr import claude_login, keychain, sessions, shell


class SwitchError(RuntimeError):
    """The switch did not happen. The message never contains a secret."""


def activate(store, label: Optional[str], mode: Optional[str] = None,
             active_path: Optional[str] = None) -> Dict:
    """Make `label` active in `mode` (default: the store's current mode).

    Returns a secret-free summary: {"label", "mode", "active_sh",
    "claude_store": {...} | None}. The credentials store is written FIRST, so
    a failed keychain write leaves the registry and active.sh untouched.
    """
    previous = store.switch_mode()
    mode = mode or previous
    if mode not in shell.SWITCH_MODES:
        raise SwitchError("unknown switch mode: %r" % (mode,))
    written = None
    if mode == shell.MODE_KEYCHAIN:
        written = _apply_to_claude_store(store, label)
        if label is None:
            mode = shell.MODE_ENV  # nothing active: Claude has its own login back
    elif previous == shell.MODE_KEYCHAIN:
        # Leaving keychain mode (`ctr use`): Claude's store still holds a ctr
        # token that nothing will move any more. Hand the /login back.
        written = _leave_keychain_mode()
    path = shell.write_active(label, active_path, mode)
    store.set_active(label)
    store.set_switch_mode(mode)
    return {"label": label, "mode": mode, "active_sh": path, "claude_store": written}


def _apply_to_claude_store(store, label: Optional[str]) -> Optional[Dict]:
    if label is None:
        # Nothing active any more: hand Claude its own /login back if we hold it.
        return _leave_keychain_mode()
    record = store.get(label)
    if record is None:
        raise SwitchError("no token labelled '%s' — see: ctr list" % label)
    token = keychain.token_for(label)
    if not token:
        raise SwitchError("no keychain item for '%s' (re-add it: ctr add %s)" % (label, label))
    try:
        return claude_login.switch_to(token, record.subscription)
    except claude_login.LoginStoreError as exc:
        raise SwitchError(str(exc))


def _leave_keychain_mode() -> Optional[Dict]:
    if not claude_login.has_backup():
        return None
    try:
        return claude_login.restore_login()
    except claude_login.LoginStoreError as exc:
        raise SwitchError(str(exc))


def restore_login(store, active_path: Optional[str] = None) -> Dict:
    """Undo keychain mode: Claude's /login back in its store, ctr back to env
    mode with no active token (new shells export nothing)."""
    try:
        result = claude_login.restore_login()
    except claude_login.LoginStoreError as exc:
        raise SwitchError(str(exc))
    path = shell.write_active(None, active_path, shell.MODE_ENV)
    store.set_active(None)
    store.set_switch_mode(shell.MODE_ENV)
    return dict(result, active_sh=path)


def registered_tokens(store) -> Dict[str, str]:
    """label -> token, in memory only. Missing items are skipped."""
    out = {}
    for record in store.tokens():
        token = keychain.token_for(record.label)
        if token:
            out[record.label] = token
    return out


def live_view(store, tokens: Optional[Dict[str, str]] = None) -> Dict:
    """Secret-free: who Claude's store holds, and which sessions follow it."""
    tokens = registered_tokens(store) if tokens is None else tokens
    return {
        "claude_store_holds": claude_login.holder(tokens),
        "sessions": sessions.summary(sessions.scan(tokens)),
    }


__all__ = ["SwitchError", "activate", "live_view", "registered_tokens", "restore_login"]
