"""Shell activation surface for ctr (Lane D).

Two jobs:

1. Generate ``~/.config/ctr/active.sh`` — the file a shell sources to export
   ``CLAUDE_CODE_OAUTH_TOKEN``. The generated file **contains no secret**: it
   calls ``security find-generic-password`` at source time, so the token only
   ever lives in the macOS keychain.
2. Install/remove one guarded block in ``~/.zshrc`` that sources that file.

``active_sh_contents``, ``zshrc_block`` and ``parse_active_label`` are pure and
unit-tested. Everything that touches the filesystem writes atomically.

Python 3.8 compatible. stdlib only.
"""

import os
import re
import shutil
import tempfile
from typing import Dict, List, Optional, Tuple

from ctr.model import (
    ACTIVE_FILE,
    ENV_VAR,
    KEYCHAIN_PREFIX,
    ZSHRC_BEGIN,
    ZSHRC_END,
    valid_label,
)

#: Where the guarded source line goes.
ZSHRC_PATH = "~/.zshrc"
#: Suffix of the one-time backup taken before ctr first edits the rc file.
BACKUP_SUFFIX = ".ctr-backup"

#: The lines ctr itself writes inside its block. Used ONLY to clean up a block
#: whose END marker has been lost: everything else found there is the user's.
_CTR_GENERATED_RE = re.compile(
    r"^[ \t]*(?:"
    r"#[ \t]*Managed by ctr\b.*"
    r"|#[ \t]*markers; delete the whole block\b.*"
    r"|if \[ -r \"[^\"]*\" \]; then \. \"[^\"]*\"; fi"
    r")[ \t]*$"
)

_ACTIVE_LABEL_RE = re.compile(
    r"^CTR_ACTIVE_LABEL=['\"]?([A-Za-z0-9_-]*)['\"]?[ \t]*$", re.MULTILINE
)

#: Variables that silently defeat CLAUDE_CODE_OAUTH_TOKEN (measured: with
#: ANTHROPIC_API_KEY set `claude -p` HANGS; with ANTHROPIC_AUTH_TOKEN set it
#: says "Not logged in"). active.sh warns about them but never unsets them:
#: they may be there deliberately for another tool.
CONFLICTING_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

_CONFLICT_WARNING = (
    "  if [ -n \"${ANTHROPIC_API_KEY:-}\" ] || [ -n \"${ANTHROPIC_AUTH_TOKEN:-}\" ]; then\n"
    "    printf '%s\\n' \\\n"
    "      'ctr: ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, so "
    "CLAUDE_CODE_OAUTH_TOKEN will not take effect.' \\\n"
    "      'ctr:   ANTHROPIC_API_KEY   -> claude hangs and never answers' \\\n"
    "      'ctr:   ANTHROPIC_AUTH_TOKEN -> claude says \"Not logged in\"' \\\n"
    "      'ctr: unset it for this shell if you want ctr to drive claude.' >&2\n"
    "  fi\n"
)

_HEADER = (
    "# ctr (claude-token-rotator) — generated file. It contains NO secret.\n"
    "# The token is read from the macOS keychain every time this file is sourced.\n"
)


# ---------------------------------------------------------------------------
# Pure generators
# ---------------------------------------------------------------------------


def active_sh_contents(label: Optional[str]) -> str:
    """Body of ``active.sh`` for ``label`` (or the inert body when None).

    Safe to source in every case: a missing keychain item leaves ``ENV_VAR``
    untouched and prints nothing, and ``label=None`` changes nothing at all.
    """
    if label is None:
        return (
            _HEADER
            + "# No active ctr token: sourcing this file changes nothing.\n"
            + "# Pick one with: ctr use <label>\n"
            + "CTR_ACTIVE_LABEL=''\n"
            + "export CTR_ACTIVE_LABEL\n"
        )
    if not valid_label(label):
        raise ValueError("invalid token label: %r" % (label,))
    service = KEYCHAIN_PREFIX + label
    return (
        _HEADER
        + "# Regenerate with: ctr use <label>\n"
        + "CTR_ACTIVE_LABEL='%s'\n" % label
        + "export CTR_ACTIVE_LABEL\n"
        + "_ctr_tok=\"$(security find-generic-password -s '%s' -w 2>/dev/null || true)\"\n"
        % service
        + 'if [ -n "$_ctr_tok" ]; then\n'
        + '  %s="$_ctr_tok"\n' % ENV_VAR
        + "  export %s\n" % ENV_VAR
        + _CONFLICT_WARNING
        + "fi\n"
        + "unset _ctr_tok\n"
    )


def zshrc_block(active_path: str) -> str:
    """The guarded block that sources ``active_path``. Pure."""
    return (
        ZSHRC_BEGIN
        + "\n"
        + "# Managed by ctr — rewritten by `ctr install-shell`. Do not edit inside the\n"
        + "# markers; delete the whole block (markers included) to remove ctr.\n"
        + 'if [ -r "%s" ]; then . "%s"; fi\n' % (active_path, active_path)
        + ZSHRC_END
        + "\n"
    )


def parse_active_label(text: str) -> Optional[str]:
    """Read ``CTR_ACTIVE_LABEL`` back out of an ``active.sh`` body. Pure."""
    match = _ACTIVE_LABEL_RE.search(text or "")
    if not match:
        return None
    return match.group(1) or None


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if not parent:
        return
    if not os.path.isdir(parent):
        os.makedirs(parent, 0o700)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass  # not ours to fix (e.g. the user's home); the write still works


def _atomic_write(path: str, text: str, mode: int) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file + os.replace."""
    directory = os.path.dirname(path) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".ctr-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _file_mode(path: str, default: int) -> int:
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return default


def _home_relative(path: str) -> str:
    """Rewrite a path under $HOME as ``$HOME/...`` so the rc block is portable."""
    home = os.path.expanduser("~")
    if path == home:
        return "$HOME"
    if home and path.startswith(home + os.sep):
        return "$HOME/" + path[len(home) + 1 :]
    return path


def _backup_once(path: str) -> Optional[str]:
    """Copy ``path`` to ``path + BACKUP_SUFFIX`` unless a backup already exists."""
    if not os.path.exists(path):
        return None
    backup = path + BACKUP_SUFFIX
    if os.path.exists(backup):
        return backup
    shutil.copy2(path, backup)
    return backup


def _resolve_rc(path: str) -> str:
    """Follow a symlinked rc file to the file it points at.

    ``os.replace`` replaces the LINK, not its target, so writing straight to a
    symlinked ~/.zshrc (the common dotfiles setup) detached it into a private
    copy: every later dotfiles edit stopped reaching the shell, and the ctr
    block never reached the dotfiles repo. Resolve first, then write.
    """
    if os.path.islink(path):
        return os.path.realpath(path)
    return path


def _read(path: str) -> str:
    try:
        with open(path, "r") as stream:
            return stream.read()
    except IOError:
        return ""


# ---------------------------------------------------------------------------
# active.sh
# ---------------------------------------------------------------------------


def write_active(label: Optional[str], path: Optional[str] = None) -> str:
    """Write ``active.sh`` for ``label``. Returns the absolute path written."""
    target = os.path.expanduser(path or ACTIVE_FILE)
    _ensure_parent(target)
    _atomic_write(target, active_sh_contents(label), 0o600)
    return target


# ---------------------------------------------------------------------------
# ~/.zshrc block
# ---------------------------------------------------------------------------


def _skip_block(lines: List[str], start: int) -> int:
    """Index just past the block that begins at ``start``.

    A block whose END marker is missing (a hand-edit, or a dotfiles merge
    conflict) used to run to EOF, so the next ``ctr install-shell`` — which
    ``install.sh`` runs on every install — deleted every line after it. That
    silently destroyed real ~/.zshrc content, and the one-time backup was by
    then a stale day-one copy, so it was unrecoverable.

    Without an END marker ctr removes only the marker line and the lines it
    wrote itself, and stops at the first line it does not recognise. A stray
    fragment left behind is a cosmetic problem; a deleted `export` is not.
    """
    index = start + 1
    while index < len(lines) and lines[index].strip() != ZSHRC_END:
        index += 1
    if index < len(lines):
        return index + 1
    index = start + 1
    while index < len(lines) and _CTR_GENERATED_RE.match(lines[index]):
        index += 1
    return index


def _remove_blocks(content: str) -> Tuple[List[str], Optional[int]]:
    """Strip every ctr block. Returns (remaining lines, index of the first one)."""
    lines = content.splitlines(True)
    kept = []  # type: List[str]
    first = None  # type: Optional[int]
    index = 0
    while index < len(lines):
        if lines[index].strip() == ZSHRC_BEGIN:
            if first is None:
                first = len(kept)
            index = _skip_block(lines, index)
            continue
        kept.append(lines[index])
        index += 1
    return kept, first


def install_zshrc(
    rc_path: Optional[str] = None, active_path: Optional[str] = None
) -> str:
    """Install (or refresh) the single guarded ctr block. Idempotent.

    Every byte outside the markers is preserved. The rc file is backed up once,
    before the first edit ctr ever makes to it.
    """
    rc_file = _resolve_rc(os.path.expanduser(rc_path or ZSHRC_PATH))
    active = os.path.expanduser(active_path or ACTIVE_FILE)
    block = zshrc_block(_home_relative(active))

    content = _read(rc_file)
    kept, first = _remove_blocks(content)
    if first is None:
        # Appending: the only byte we may add outside the markers is the
        # newline terminator an unterminated last line is missing.
        if kept and not kept[-1].endswith("\n"):
            kept[-1] = kept[-1] + "\n"
        kept.append(block)
    else:
        kept.insert(first, block)

    updated = "".join(kept)
    if updated != content:
        _backup_once(rc_file)
        _atomic_write(rc_file, updated, _file_mode(rc_file, 0o644))
    return rc_file


def uninstall_zshrc(rc_path: Optional[str] = None) -> bool:
    """Remove every ctr block. True when something was removed."""
    rc_file = _resolve_rc(os.path.expanduser(rc_path or ZSHRC_PATH))
    content = _read(rc_file)
    if not os.path.exists(rc_file):
        return False
    kept, first = _remove_blocks(content)
    if first is None:
        return False
    _backup_once(rc_file)
    _atomic_write(rc_file, "".join(kept), _file_mode(rc_file, 0o644))
    return True


def status(
    rc_path: Optional[str] = None, active_path: Optional[str] = None
) -> Dict:
    """Best-effort view of the shell installation, for ``ctr doctor``."""
    rc_file = _resolve_rc(os.path.expanduser(rc_path or ZSHRC_PATH))
    active = os.path.expanduser(active_path or ACTIVE_FILE)
    body = _read(active) if os.path.exists(active) else ""
    return {
        "rc_path": rc_file,
        "rc_installed": ZSHRC_BEGIN in _read(rc_file),
        "active_path": active,
        "active_exists": os.path.exists(active),
        "active_label": parse_active_label(body),
        "active_mode": _file_mode(active, 0) if os.path.exists(active) else None,
    }


__all__ = [
    "ZSHRC_PATH",
    "CONFLICTING_ENV_VARS",
    "BACKUP_SUFFIX",
    "active_sh_contents",
    "zshrc_block",
    "parse_active_label",
    "write_active",
    "install_zshrc",
    "uninstall_zshrc",
    "status",
]
