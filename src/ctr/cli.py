"""ctr command line (Lane D).

Exit codes: 0 ok, 1 runtime error, 2 usage error / needs an action from you.

Modules owned by other lanes (store, keychain, usage, selector, monitor,
launchd, rollover) are imported inside the handlers, so `import ctr.cli` and
`ctr --help` keep working even if one of them is missing or broken.

Python 3.8 compatible. stdlib only. A full token is never printed: every
display of a secret goes through model.redact().
"""

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import time
from typing import Dict, List, Optional

from ctr import render, shell
from ctr.model import (
    CLAUDE_KEYCHAIN_SERVICE,
    CONFIG_DIR,
    ENV_VAR,
    KEYCHAIN_PREFIX,
    LOG_FILE,
    TokenRecord,
    Usage,
    redact,
    valid_label,
)
from ctr.render import out as _out, warn as _warn

BIN_LINK = "~/.local/bin/ctr"


class CtrError(Exception):
    """Runtime failure -> exit 1."""


class CtrUsage(CtrError):
    """Bad usage, or something only the user can do -> exit 2."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


#: Anything token-shaped, whoever produced it. `redact()` is how ctr displays a
#: token it is holding on purpose; this is the backstop for one arriving inside
#: an exception message from a module that forgot to scrub.
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")


def _safe_message(exc: BaseException) -> str:
    """An exception's message with any token-shaped run redacted.

    Every sibling module scrubs today, so nothing reaches here with a secret in
    it — but that made "never print a full token" a property of everyone else's
    good behaviour rather than of this handler. Now it holds structurally.
    """
    return _SECRET_RE.sub(lambda m: redact(m.group(0)), str(exc))


def _config_dir(args) -> str:
    return os.path.expanduser(getattr(args, "config_dir", None) or CONFIG_DIR)


def _active_path(args) -> Optional[str]:
    return getattr(args, "active_file", None)


def _store(args):
    from ctr.store import Store

    return Store(getattr(args, "config_dir", None))


def _program_path() -> str:
    """Absolute path launchd should run."""
    link = os.path.expanduser(BIN_LINK)
    if os.path.exists(link):
        return link
    return os.path.realpath(sys.argv[0] or "ctr")


def _kind_of(token: str) -> str:
    return "oat" if token.startswith("sk-ant-oat") else "oauth"


def _prompt_token() -> str:
    """Ask for the token on the tty with echo off. The safest form, so it is
    the one `ctr add <alias>` uses and the one the help text recommends."""
    import getpass

    try:
        data = getpass.getpass("Paste the token for this alias (it will not echo): ")
    except (EOFError, KeyboardInterrupt):
        raise CtrUsage("no token entered")
    return _validate_token(data)


def _validate_token(data: str) -> str:
    """The one place a token is checked, whatever input it arrived on."""
    token = (data or "").strip()
    if not token:
        raise CtrUsage("no token on that input")
    if len(token.split()) != 1:
        raise CtrUsage("that input has whitespace in it — expected exactly one token")
    if not token.startswith("sk-"):
        raise CtrUsage("that does not look like a Claude token (%s)" % redact(token))
    return token


def _read_token(spec: str) -> str:
    """Read a token from stdin ('-') or an inherited file descriptor."""
    if spec == "-":
        data = sys.stdin.read()
    else:
        try:
            descriptor = int(spec)
        except ValueError:
            raise CtrUsage(
                "--token takes '-' (read stdin) or a file-descriptor number. "
                "Never pass the token itself: it would land in your shell "
                "history and in `ps` output."
            )
        try:
            with os.fdopen(os.dup(descriptor), "r") as stream:
                data = stream.read()
        except OSError:
            raise CtrUsage("cannot read from file descriptor %d" % descriptor)
    return _validate_token(data)


# ---------------------------------------------------------------------------
# Usage collection (cache-aware)
# ---------------------------------------------------------------------------


def _usages(store, records: List[TokenRecord], fresh: bool) -> List[Usage]:
    """One Usage per record, in order. Cached readings are reused unless fresh."""
    from ctr import keychain
    from ctr import usage as usage_module

    config = store.config()
    now = _now()
    ttl = int(config.get("cache_ttl_s", 60))
    timeout = int(config.get("http_timeout_s", 20))

    found = {}  # type: Dict[str, Usage]
    tokens = {}  # type: Dict[str, str]
    pending = []  # type: List[TokenRecord]
    for record in records:
        if not fresh:
            cached = store.cache_get(record.label, ttl, now)
            if cached is not None:
                found[record.label] = cached
                continue
        secret = keychain.token_for(record.label)
        if not secret:
            found[record.label] = Usage.failed(
                record.label, "no keychain item (re-add it: ctr add %s)" % record.label, now
            )
            continue
        tokens[record.label] = secret
        pending.append(record)

    if pending:
        # Start each token from the strategy that last worked for it: without
        # this every long-lived `oat` token pays a doomed /api/oauth/usage 403
        # before the header probe, i.e. 2 HTTP calls instead of 1.
        prefers = {}  # type: Dict[str, str]
        for record in pending:
            strategy = store.probe_strategy(record.label)
            if strategy:
                prefers[record.label] = strategy
        for reading in usage_module.probe_all(
            pending, tokens, timeout_s=timeout, prefers=prefers
        ):
            found[reading.label] = reading
            if reading.ok:
                store.cache_put(reading)

    return [
        found.get(r.label) or Usage.failed(r.label, "not probed", now) for r in records
    ]


def _activate(store, args, label: Optional[str]) -> str:
    """Write active.sh and record the active label. Returns the path written."""
    path = shell.write_active(label, _active_path(args))
    store.set_active(label)
    return path


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _resolve_token(args):
    """(token, kind, account, plan), or None when no token source was given."""
    account = args.account or ""
    plan = args.tier or ""
    if args.from_login:
        from ctr import keychain

        token = keychain.claude_login_token()
        if not token:
            raise CtrError(
                "no interactive Claude login found in the keychain item %r"
                % CLAUDE_KEYCHAIN_SERVICE
            )
        info = keychain.claude_login_info() or {}
        return (
            token,
            "oauth",
            account or str(info.get("account") or ""),
            plan or str(info.get("subscription") or ""),
        )
    if args.token:
        token = _read_token(args.token)
        return token, _kind_of(token), account, plan
    # RULING 4: `ctr add <alias> <token>` must work as written. It is the least
    # safe form — argv lands in `ps` and in ~/.zsh_history — so it warns after
    # storing, and the help text points at the bare prompting form instead.
    if args.secret:
        token = _validate_token(args.secret)
        return token, _kind_of(token), account, plan, True
    if sys.stdin.isatty():
        token = _prompt_token()
        return token, _kind_of(token), account, plan
    return None


def _probe_and_report(store, label: str, token: str) -> None:
    """Probe a freshly registered token once and say how it went."""
    from ctr import usage as usage_module

    timeout = int(store.config()["http_timeout_s"])
    reading = usage_module.probe(label, token, timeout_s=timeout)
    if reading.ok:
        store.cache_put(reading)
    render.probe_result(label, reading)


def cmd_add(args) -> int:
    label = args.label
    if not valid_label(label):
        raise CtrUsage(
            "invalid label %r — lowercase letters, digits, '-' and '_', starting "
            "with a letter or digit" % label
        )
    store = _store(args)
    if store.get(label) is not None:
        raise CtrUsage(
            "'%s' is already registered — remove it first: ctr remove %s"
            % (label, label)
        )

    resolved = _resolve_token(args)
    if resolved is None:
        render.print_setup_instructions(label)
        return 2
    token, kind, account, plan = resolved[:4]
    from_argv = len(resolved) > 4 and resolved[4]

    from ctr import keychain

    keychain.set(KEYCHAIN_PREFIX + label, account or label, token)
    record = TokenRecord(
        label=label,
        account=account,
        subscription=plan,
        added_at=_now_iso(),
        kind=kind,
        note=args.note or "",
    )
    try:
        store.add(record)
    except ValueError as exc:
        raise CtrUsage(str(exc))

    _report_stored(store, args, label, token, account, from_argv)
    return 0


def _report_stored(store, args, label, token, account, from_argv) -> None:
    """Everything `ctr add` says once the token is safely in the keychain."""
    render.stored_notice(redact(token), label, from_argv, account)
    _probe_and_report(store, label, token)
    if store.active() is None:
        render.first_token_notice(_activate(store, args, label))


def cmd_list(args) -> int:
    store = _store(args)
    records = store.tokens()
    if not records:
        _out("No tokens registered yet. Add one: ctr add <label>")
        return 0
    usages = _usages(store, records, args.fresh)
    render.print_tokens(store, records, usages, store.active(), _now())
    return 0


def cmd_status(args) -> int:
    store = _store(args)
    records = store.tokens()
    active = store.active()
    now = _now()
    usages = _usages(store, records, args.fresh) if records else []
    if args.json:
        _out(json.dumps(render.status_json(store, records, usages, active, now),
                        indent=2, sort_keys=True))
        return 0
    if not records:
        _out("No tokens registered yet. Add one: ctr add <label>")
        return 0
    render.print_tokens(store, records, usages, active, now)
    _out("")
    _out(render.headroom_line(store, records, usages, active, now))
    return 0


def cmd_remove(args) -> int:
    store = _store(args)
    label = args.label
    if store.get(label) is None:
        raise CtrUsage("no token labelled '%s' — see: ctr list" % label)

    from ctr import keychain

    # Ask BEFORE the removal: store.remove() clears the pointer itself, so a
    # check afterwards can never be true and active.sh was left dangling —
    # `ctr doctor` went red immediately after a clean `ctr remove`.
    was_active = store.active() == label
    removed_secret = keychain.delete(KEYCHAIN_PREFIX + label)
    store.remove(label)

    state = dict(store.state())
    state["parked"] = {
        k: v for k, v in (state.get("parked") or {}).items() if k != label
    }
    state["cache"] = {k: v for k, v in (state.get("cache") or {}).items() if k != label}
    store.save_state(state)

    _out(
        "Removed '%s' (keychain item %s)."
        % (label, "deleted" if removed_secret else "was already gone")
    )
    if was_active:
        path = _activate(store, args, None)
        _out("It was the active token — there is no active token now.\n"
             "Pick another one (`ctr use <label>`) and re-source %s." % path)
    return 0


def cmd_use(args) -> int:
    store = _store(args)
    label = args.label
    if store.get(label) is None:
        raise CtrUsage("no token labelled '%s' — see: ctr list" % label)
    path = _activate(store, args, label)
    render.switch_notice(label, path)
    return 0


def cmd_next(args) -> int:
    store = _store(args)
    records = store.tokens()
    if not records:
        raise CtrUsage("no tokens registered — add one: ctr add <label>")
    active = store.active()
    usages = _usages(store, records, args.fresh)

    from ctr import selector

    target = selector.choose_best(
        usages, [active] if active else [], store.config(), store.state(), _now()
    )
    if target is None:
        raise CtrError(
            "no other token has headroom right now — run `ctr status` to see why"
        )
    path = _activate(store, args, target)
    render.switch_notice(target, path)
    return 0


def cmd_active(args) -> int:
    store = _store(args)
    label = store.active()
    if label is None:
        _out("(none)")
        return 0
    record = store.get(label)
    detail = ""
    if record is not None and (record.account or record.subscription):
        detail = "  (%s %s)" % (record.account or "?", record.subscription or "?")
    _out(label + detail)
    return 0


def cmd_monitor(args) -> int:
    store = _store(args)
    from ctr import monitor

    if args.once:
        # A quiet tick logs nothing (5-min launchd would add ~288 "nothing
        # happened" lines a day) — but then --once printed NOTHING and read as
        # broken; and launchd aims this stdout AT that log, so printing always
        # recreates the noise. So a quiet tick speaks only to a human at a tty.
        result = monitor.tick(store, _now(), apply=True, auto_rollover=args.auto_rollover)
        if result.get("error") or result.get("switched") or sys.stdout.isatty():
            _out(render.tick_summary(result))
        return 1 if result.get("error") else 0

    return int(
        monitor.run(store, once=False, interval_s=args.interval, auto_rollover=args.auto_rollover)
    )


def cmd_install_monitor(args) -> int:
    from ctr import launchd

    program = _program_path()
    path = launchd.install(program, interval_s=args.interval)
    render.monitor_installed(program, path, args.interval, os.path.expanduser(LOG_FILE))
    return 0


def cmd_uninstall_monitor(args) -> int:
    from ctr import launchd

    if launchd.uninstall():
        _out("Monitor agent removed.")
    else:
        _out("No monitor agent was installed.")
    return 0


def cmd_rollover(args) -> int:
    from ctr import rollover

    dry_run = not args.apply
    only = [p.strip() for p in args.only.split(",") if p.strip()] if args.only else None
    if dry_run:
        _out("DRY RUN — nothing will be touched. Add --apply to do it for real.")
    result = rollover.run(dry_run=dry_run, only=only)
    render.print_rollover(result, dry_run)
    # A pane that failed to come back is a runtime error, not a clean run: the
    # exit code is all `monitor --auto-rollover` and any wrapper script sees.
    failed = [item for item in (result.get("acted") or []) if not item.get("ok")]
    return 1 if (result.get("error") or failed) else 0


def cmd_install_shell(args) -> int:
    store = _store(args)
    directory = _config_dir(args)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    os.chmod(directory, 0o700)
    active = shell.write_active(store.active(), _active_path(args))
    rc_file = shell.install_zshrc(getattr(args, "rc_file", None), _active_path(args))
    render.shell_installed(rc_file, active)
    return 0


def cmd_doctor(args) -> int:
    checks = []  # type: List[List[str]]
    store = _store(args)
    checks.extend(_doctor_paths(args, store))
    checks.extend(_doctor_shell(args, store))
    checks.extend(_doctor_env())
    checks.extend(_doctor_launchd())
    checks.extend(_doctor_probe_files())
    checks.extend(_doctor_tokens(store, args))
    return render.doctor_report(checks)


def _mode_of(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


def _doctor_paths(args, store) -> List:
    directory = _config_dir(args)
    out = []
    mode = _mode_of(directory)
    if mode is None:
        out.append(("fail", "config dir", "%s is missing — run: ctr install-shell" % directory))
    elif mode != 0o700:
        out.append(("warn", "config dir", "%s is %04o, expected 0700" % (directory, mode)))
    else:
        out.append(("ok", "config dir", "%s 0700" % directory))

    tokens_file = os.path.join(directory, "tokens.json")
    token_mode = _mode_of(tokens_file)
    if token_mode is None:
        out.append(("warn", "tokens.json", "not created yet (no tokens registered)"))
    elif token_mode != 0o600:
        out.append(("warn", "tokens.json", "is %04o, expected 0600" % token_mode))
    else:
        out.append(("ok", "tokens.json", "0600, no secrets by design"))

    link = os.path.expanduser(BIN_LINK)
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    if not os.path.exists(link):
        out.append(("warn", "ctr on PATH", "%s is missing — run ./install.sh" % BIN_LINK))
    elif os.path.dirname(link) not in path_dirs:
        out.append(("fail", "ctr on PATH", "%s exists but its directory is not in $PATH" % BIN_LINK))
    else:
        out.append(("ok", "ctr on PATH", link))
    return out


def _doctor_shell(args, store) -> List:
    info = shell.status(getattr(args, "rc_file", None), _active_path(args))
    out = []
    if info["rc_installed"]:
        out.append(("ok", "zshrc block", info["rc_path"]))
    else:
        out.append(("fail", "zshrc block", "not in %s — run: ctr install-shell" % info["rc_path"]))

    if not info["active_exists"]:
        out.append(("fail", "active.sh", "%s is missing — run: ctr install-shell" % info["active_path"]))
        return out
    if info["active_mode"] != 0o600:
        out.append(("warn", "active.sh", "is %04o, expected 0600" % (info["active_mode"] or 0)))
    recorded = store.active()
    if info["active_label"] == recorded:
        out.append(("ok", "active.sh", "label '%s' matches tokens.json" % (recorded or "(none)")))
    else:
        out.append((
            "fail",
            "active.sh",
            "says '%s' but tokens.json says '%s' — run: ctr use <label>"
            % (info["active_label"] or "(none)", recorded or "(none)"),
        ))
    if os.environ.get(ENV_VAR) is None:
        out.append(("warn", ENV_VAR, "not set in this shell — source %s" % info["active_path"]))
    else:
        out.append(("ok", ENV_VAR, "set in this shell (%s)" % redact(os.environ.get(ENV_VAR))))
    return out


#: MEASURED (lane-addendum RULING 2): each of these defeats ctr, differently.
CONFLICTING_ENV = [
    (
        "ANTHROPIC_API_KEY",
        "`claude -p` HANGS and never answers (measured: killed at 90s). "
        "`claude auth status` still says oauth_token, so it looks fine.",
    ),
    (
        "ANTHROPIC_AUTH_TOKEN",
        '`claude` says "Not logged in - Please run /login", even with a valid '
        "CLAUDE_CODE_OAUTH_TOKEN.",
    ),
]


def _doctor_env() -> List:
    """The two variables that silently defeat ctr.

    This is the single most likely way a user concludes "ctr is broken": every
    other check passes, `ctr status` is green, and `claude` still will not work.
    """
    out = []
    for name, symptom in CONFLICTING_ENV:
        if os.environ.get(name):
            out.append((
                "fail", name,
                "set in this environment -> %s Unset it: unset %s" % (symptom, name),
            ))
        else:
            out.append(("ok", name, "not set (it would override %s)" % ENV_VAR))
    return out


def _doctor_launchd() -> List:
    from ctr import launchd

    info = launchd.status()
    if not info.get("installed"):
        return [("warn", "monitor agent", "not installed — run: ctr install-monitor")]
    if not info.get("loaded"):
        return [("fail", "monitor agent", "plist present but not loaded — re-run: ctr install-monitor")]
    return [("ok", "monitor agent", str(info.get("plist") or ""))]


#: `usage._request` makes one 0600 work directory per probe and removes it in a
#: `finally`. A SIGKILL landing between the two leaves it behind — holding a
#: curl config file with a bearer token in it. The contract sanctions that hole
#: rather than complicating the probe path, so doctor sweeps for the wreckage.
PROBE_TMP_PREFIX = "ctr-probe-"


def _probe_leftovers(bases: Optional[List[str]] = None) -> List[str]:
    """Stale per-probe work directories, sorted. Never raises.

    `tempfile.gettempdir()` honours $TMPDIR (per-user, under /var/folders on
    macOS) while a launchd job may land in /tmp, so sweep both.
    """
    if bases is None:
        bases = [os.path.realpath(tempfile.gettempdir()), os.path.realpath("/tmp")]
    found = []  # type: List[str]
    for base in set(bases):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        found.extend(os.path.join(base, n) for n in names
                     if n.startswith(PROBE_TMP_PREFIX))
    return sorted(found)


def _doctor_probe_files() -> List:
    stale = _probe_leftovers()
    detail = render.probe_leftover_detail(PROBE_TMP_PREFIX, stale)
    return [("warn" if stale else "ok", "probe temp files", detail)]


def _doctor_tokens(store, args) -> List:
    records = store.tokens()
    if not records:
        return [("warn", "tokens", "none registered — run: ctr add <label>")]
    usages = _usages(store, records, True)
    return [(render.token_check_level(r), "token %s" % rec.label,
             render.token_check_detail(r)) for rec, r in zip(records, usages)]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _register_token_commands(subparsers) -> None:
    add = subparsers.add_parser(
        "add",
        help="register a token (label + keychain)",
        description=render.ADD_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add.add_argument("label")
    add.add_argument(
        "secret",
        nargs="?",
        metavar="TOKEN",
        help="the token itself (works, but prefer the bare form: it prompts "
             "without echo and keeps the token out of your shell history)",
    )
    add.add_argument(
        "--token",
        metavar="SOURCE",
        help="'-' to read the token from stdin, or a file-descriptor number. "
        "Never the token itself.",
    )
    add.add_argument(
        "--from-login",
        action="store_true",
        help="register the token from the interactive Claude login keychain item",
    )
    add.add_argument("--account", help="account email, for display only")
    add.add_argument("--tier", help="subscription tier, e.g. max — display only")
    add.add_argument("--note", help="free-text note")
    add.set_defaults(handler=cmd_add)

    listing = subparsers.add_parser("list", help="list registered tokens with live usage")
    listing.add_argument("--fresh", action="store_true", help="bypass the usage cache")
    listing.set_defaults(handler=cmd_list)

    status = subparsers.add_parser("status", help="usage per token; marks the active one")
    status.add_argument("--json", action="store_true", help="machine-readable, no secrets")
    status.add_argument("--fresh", action="store_true", help="bypass the usage cache")
    status.set_defaults(handler=cmd_status)

    remove = subparsers.add_parser(
        "remove", help="forget a token and delete its keychain item"
    )
    remove.add_argument("label")
    remove.set_defaults(handler=cmd_remove)


def _register_switch_commands(subparsers) -> None:
    use = subparsers.add_parser("use", help="make a token the active one")
    use.add_argument("label")
    use.set_defaults(handler=cmd_use)

    nxt = subparsers.add_parser("next", help="switch to the token with the most headroom")
    nxt.add_argument("--fresh", action="store_true", help="bypass the usage cache")
    nxt.set_defaults(handler=cmd_next)

    active = subparsers.add_parser("active", help="print the active label")
    active.set_defaults(handler=cmd_active)

    rollover = subparsers.add_parser(
        "rollover", help="exit + --resume parked claude sessions (dry run by default)"
    )
    rollover.add_argument(
        "--apply", action="store_true", help="actually do it (without this it is a dry run)"
    )
    rollover.add_argument(
        "--dry-run", action="store_true", help="explicit dry run (the default anyway)"
    )
    rollover.add_argument("--only", help="comma-separated pane ids to consider")
    rollover.set_defaults(handler=cmd_rollover)


def _register_service_commands(subparsers) -> None:
    monitor = subparsers.add_parser("monitor", help="watch usage and switch automatically")
    monitor.add_argument("--once", action="store_true", help="one pass, then exit")
    monitor.add_argument("--interval", type=int, default=300, help="seconds between passes")
    monitor.add_argument(
        "--auto-rollover",
        action="store_true",
        help="run rollover --apply after a switch (off by default)",
    )
    monitor.set_defaults(handler=cmd_monitor)

    install_monitor = subparsers.add_parser(
        "install-monitor", help="install the launchd agent"
    )
    install_monitor.add_argument(
        "--interval", type=int, default=300, help="seconds between passes"
    )
    install_monitor.set_defaults(handler=cmd_install_monitor)

    uninstall_monitor = subparsers.add_parser(
        "uninstall-monitor", help="remove the launchd agent"
    )
    uninstall_monitor.set_defaults(handler=cmd_uninstall_monitor)

    install_shell = subparsers.add_parser(
        "install-shell", help="install the ~/.zshrc block"
    )
    install_shell.set_defaults(handler=cmd_install_shell)

    doctor = subparsers.add_parser("doctor", help="check the installation end to end")
    doctor.set_defaults(handler=cmd_doctor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctr",
        description="claude-token-rotator — rotate long-lived Claude Code tokens.",
    )
    parser.add_argument("--config-dir", help=argparse.SUPPRESS)
    parser.add_argument("--active-file", help=argparse.SUPPRESS)
    parser.add_argument("--rc-file", help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True
    _register_token_commands(subparsers)
    _register_switch_commands(subparsers)
    _register_service_commands(subparsers)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return handler(args)
    except CtrUsage as exc:
        _warn(_safe_message(exc))
        return 2
    except CtrError as exc:
        _warn(_safe_message(exc))
        return 1
    except KeyboardInterrupt:
        _warn("interrupted")
        return 1
    except Exception as exc:  # friendly one-liner; CTR_DEBUG=1 for the traceback
        if os.environ.get("CTR_DEBUG"):
            raise
        _warn("%s: %s" % (exc.__class__.__name__, _safe_message(exc)))
        return 1


if __name__ == "__main__":
    sys.exit(main())
