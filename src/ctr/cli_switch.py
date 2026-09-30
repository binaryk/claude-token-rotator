"""`ctr switch` and `ctr ui` — the seamless-switch commands (v2).

Kept out of cli.py, which sits at the 800-line house limit. cli.py registers
these through register(). Exit codes follow cli.py: 0 ok, 1 runtime error,
2 usage error.

Python 3.8 compatible. The TUI needs Textual (optional); nothing else here
imports a third-party package.
"""

import json
from typing import Dict, List

from ctr import render
from ctr.render import out as _out

UI_INSTALL_HINT = (
    "ctr ui needs Textual (an optional dependency; the rest of ctr is stdlib-only).\n"
    "Install it for the python3 that runs ctr:\n"
    "    python3 -m pip install --user 'textual>=0.47'\n"
    "then run `ctr ui` again."
)

SWITCH_HELP = """Switch the account Claude Code itself is logged in as.

The token goes into Claude Code's own credentials store (the macOS keychain
item "Claude Code-credentials"), so every RUNNING claude session that was
started without CLAUDE_CODE_OAUTH_TOKEN moves to the new account on its next
request — no restart. Sessions started with that variable stay on their token
until restarted (`ctr rollover`); ctr counts them for you.

Your interactive /login is saved first and comes back with:
    ctr switch --restore-login"""


def _cli():
    from ctr import cli  # lazy: cli imports this module

    return cli


def live_extras(store) -> Dict:
    """Secret-free live fields for `status --json`. Never raises."""
    from ctr import fable, switcher

    extras = {"mode": store.switch_mode()}
    try:
        extras.update(switcher.live_view(store))
    except Exception as exc:  # the keychain or ps may be unavailable
        extras["live_error"] = exc.__class__.__name__
    state = store.state()
    now = _cli()._now()
    extras["fable"] = {
        record.label: fable.cached(state, record.label, now) for record in store.tokens()
    }
    return extras


def _switch_payload(result: Dict, live: Dict) -> Dict:
    written = result.get("claude_store") or {}
    return {
        "switched": True,
        "label": result.get("label"),
        "mode": result.get("mode"),
        "active_sh": result.get("active_sh"),
        "claude_store_service": written.get("service"),
        "login_backed_up": bool(written.get("backed_up_login")),
        "claude_store_holds": live.get("claude_store_holds"),
        "sessions": live.get("sessions"),
    }


def switch_lines(payload: Dict) -> List[str]:
    """Human summary of a switch. Pure; secret-free by construction."""
    label = payload.get("label")
    sessions = payload.get("sessions") or {}
    lines = ["Switched to '%s'. Claude Code's credentials store now holds it." % label]
    if payload.get("login_backed_up"):
        lines.append("Your interactive /login was saved; `ctr switch --restore-login` "
                     "brings it back.")
    following = sessions.get("following", 0)
    pinned = sessions.get("pinned", 0)
    lines.append("%d running claude session(s) follow it from their next request." % following)
    if pinned:
        detail = ", ".join("%d on %s" % (n, name) for name, n in
                           sorted((sessions.get("pinned_by_label") or {}).items()))
        lines.append("%d session(s) were started with CLAUDE_CODE_OAUTH_TOKEN (%s) and "
                     "keep that token until restarted: ctr rollover" % (pinned, detail))
    lines.append("New shells: source %s (it now exports no token)." % payload.get("active_sh"))
    return lines


def cmd_switch(args) -> int:
    cli = _cli()
    from ctr import switcher

    store = cli._store(args)
    try:
        if args.restore_login:
            result = switcher.restore_login(store, cli._active_path(args))
            payload = {"restored_login": True, "active_sh": result.get("active_sh"),
                       "claude_store_service": result.get("service")}
            first = ("Claude Code's store already holds an interactive /login (newer than "
                     "ctr's copy); kept it." if result.get("already_login") else
                     "Restored your interactive /login into Claude Code's credentials store.")
            text = [first, "ctr is back in env mode with no active token."]
        else:
            if not args.label:
                raise cli.CtrUsage("usage: ctr switch <label>  (or --restore-login)")
            if store.get(args.label) is None:
                raise cli.CtrUsage("no token labelled '%s' — see: ctr list" % args.label)
            result = switcher.activate(store, args.label, "keychain", cli._active_path(args))
            payload = _switch_payload(result, _safe_live(store))
            text = switch_lines(payload)
    except switcher.SwitchError as exc:
        raise cli.CtrError(str(exc))
    if args.json:
        _out(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for line in text:
            _out(line)
    return 0


def _safe_live(store) -> Dict:
    from ctr import switcher

    try:
        return switcher.live_view(store)
    except Exception:
        return {}


def cmd_ui(args) -> int:
    import importlib.util

    if importlib.util.find_spec("textual") is None:
        render.warn(UI_INSTALL_HINT)
        return 2
    from ctr import tui

    return tui.run(_cli()._store(args), refresh_s=args.interval,
                   active_path=_cli()._active_path(args))


def doctor_mode(store, active_path: str) -> List:
    """Doctor rows for the switch mode. (level, name, detail) tuples."""
    import os

    from ctr import switcher
    from ctr.model import ENV_VAR, redact

    in_env = os.environ.get(ENV_VAR)
    if store.switch_mode() != "keychain":
        if in_env is None:
            return [("warn", ENV_VAR, "not set in this shell — source %s" % active_path)]
        return [("ok", ENV_VAR, "set in this shell (%s)" % redact(in_env))]
    rows = [("ok", "switch mode", "keychain — running sessions follow `ctr switch`")]
    if in_env is not None:
        rows.append(("warn", ENV_VAR, "set in this shell, so claude started here is pinned "
                     "to it — open a new shell (active.sh unsets it)"))
    try:
        holds = switcher.live_view(store)["claude_store_holds"]
    except Exception as exc:
        return rows + [("warn", "claude store", "unreadable (%s)" % exc.__class__.__name__)]
    active = store.active()
    if holds == active:
        rows.append(("ok", "claude store", "holds the active token '%s'" % active))
    else:
        rows.append(("fail", "claude store", "holds '%s' but the active token is '%s' — "
                     "run: ctr switch %s" % (holds, active, active or "<label>")))
    return rows


def register(subparsers) -> None:
    import argparse

    switch = subparsers.add_parser(
        "switch",
        help="switch Claude Code's own login; running sessions follow",
        description=SWITCH_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    switch.add_argument("label", nargs="?")
    switch.add_argument("--json", action="store_true", help="machine-readable, no secrets")
    switch.add_argument("--restore-login", action="store_true",
                        help="put your saved interactive /login back")
    switch.set_defaults(handler=cmd_switch)

    ui = subparsers.add_parser("ui", aliases=["top"],
                               help="full-screen dashboard (needs Textual)")
    ui.add_argument("--interval", type=int, default=60,
                    help="seconds between usage refreshes (default 60)")
    ui.set_defaults(handler=cmd_ui)


__all__ = ["cmd_switch", "cmd_ui", "live_extras", "register", "switch_lines"]
