"""User-facing rendering for the ctr CLI: tables, notices and JSON payloads.

Split out of cli.py, which had grown to 1010 lines against a house limit of
800. This is a PURE MOVE — every function here behaves exactly as it did when
it lived in cli.py, down to the spacing of a table cell and the wording of a
notice. Nothing was renamed on the way except the leading underscore, which a
module boundary no longer needs.

Nothing in this module decides anything, and nothing touches the network, the
keychain or a subprocess. It formats what cli.py hands it and writes through
`out()` / `warn()`, which look `sys.stdout` / `sys.stderr` up at call time so a
test that redirects either stream still captures everything.

A full token is never rendered: a secret arrives here already passed through
model.redact().

Python 3.8 compatible. stdlib only.
"""

import sys
from typing import Dict, List, Optional, Sequence

from ctr.model import KEYCHAIN_PREFIX, TokenRecord, Usage, fmt_reset, pct

# ---------------------------------------------------------------------------
# Output primitives
# ---------------------------------------------------------------------------


def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def warn(text: str) -> None:
    sys.stderr.write("ctr: %s\n" % text)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append(
            "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(lines)


HEADERS = [
    " ",
    "LABEL",
    "ACCOUNT",
    "PLAN",
    "KIND",
    "ADDED",
    "5H",
    "RESETS",
    "7D",
    "RESETS",
    "NOTE",
]


def token_rows(
    records: List[TokenRecord], usages: List[Usage], active: Optional[str], now: int
) -> List[List[str]]:
    by_label = {u.label: u for u in usages}
    rows = []
    for record in records:
        reading = by_label.get(record.label)
        note = ""
        if reading is None:
            note = "no reading"
        elif not reading.ok:
            note = reading.error or "probe failed"
        elif reading.status == "rejected":
            note = "REJECTED (limit reached)"
        elif reading.error:
            # A reading can be `ok` and still carry a warning — RULING 8.2's
            # clamp note is the live case: the numbers are usable but were
            # outside 0..100 before we forced them in. Without this branch the
            # only place that note appeared was `ctr status --json`, so the
            # human table showed a hostile 0% with no warning at all.
            note = reading.error
        rows.append(
            [
                "*" if record.label == active else " ",
                record.label,
                record.account or "-",
                record.subscription or "-",
                record.kind,
                (record.added_at or "")[:10] or "-",
                pct(reading.five_h) if reading else "  ?",
                fmt_reset(reading.five_h_reset, now) if reading else "-",
                pct(reading.seven_d) if reading else "  ?",
                fmt_reset(reading.seven_d_reset, now) if reading else "-",
                note or record.note,
            ]
        )
    return rows


def print_tokens(store, records, usages, active, now) -> None:
    out(table(HEADERS, token_rows(records, usages, active, now)))
    if active is None:
        out("")
        out("No active token. Pick one: ctr use <label>")


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


HISTORY_WARNING = (
    "note: the token was on your command line, so it is now in `ps` history and in "
    "~/.zsh_history. Remove it with:  history -d <n>   (find it with: history | tail)\n"
    "      next time use:  ctr add %s    — it prompts without echoing."
)

FIRST_TOKEN_NOTICE = """
This is your first token, so it is now the active one.
    source %s"""

NO_ACCOUNT_NOTICE = (
    "Account/plan unknown for this token — annotate it next time with:\n"
    "    ctr add ... --account you@example.com --tier max"
)


def stored_notice(redacted: str, label: str, from_argv: bool, account: str) -> None:
    """What `ctr add` says once the token is safely in the keychain."""
    out("Stored %s in the keychain as '%s%s'." % (redacted, KEYCHAIN_PREFIX, label))
    if from_argv:
        warn(HISTORY_WARNING % label)
    if not account:
        out(NO_ACCOUNT_NOTICE)


def first_token_notice(path: str) -> None:
    out(FIRST_TOKEN_NOTICE % path)


def probe_result(label: str, reading: Usage) -> None:
    """How the one probe `ctr add` runs went. A failure is not fatal."""
    if reading.ok:
        out(
            "Probe ok (%s): 5h %s, 7d %s."
            % (reading.probe, pct(reading.five_h), pct(reading.seven_d))
        )
        return
    warn("probe failed for '%s': %s" % (label, reading.error))
    warn("the token is registered anyway — check it later with: ctr status")


SWITCH_NOTICE = """
Active token is now '%(label)s'.

NEW `claude` processes pick it up. Open a new shell, or run:
    source %(path)s

ALREADY-RUNNING claude sessions keep the OLD token in memory — nothing you do
to the keychain or the environment reaches them. Move them over with:
    ctr rollover           # preview only (this is the default)
    ctr rollover --apply   # exit + --resume each parked session"""


def switch_notice(label: str, path: str) -> None:
    out(SWITCH_NOTICE % {"label": label, "path": path})


SETUP_HELP = """ctr never runs `claude setup-token` for you — it opens a browser login
only you can complete. Run it yourself:

    claude setup-token

Then register the token it prints, without putting it in argv:

    ctr add %(label)s --token -          # paste it, then Ctrl-D
    pbpaste | ctr add %(label)s --token -

Or register the interactive login already in your keychain:

    ctr add %(label)s --from-login"""


def print_setup_instructions(label: str) -> None:
    out(SETUP_HELP % {"label": label})


ADD_HELP = (
    "ctr add <alias>            prompts for the token without echoing "
    "(recommended)\n"
    "ctr add <alias> <token>    works, but the token lands in `ps` and "
    "in ~/.zsh_history\n"
    "ctr add <alias> --token -  reads it from stdin"
)

PROBE_LEFTOVER_CLEAN = "no %s* left behind"
#: Wording note: a leftover is usually a work DIRECTORY, but a probe killed
#: between mkdtemp and the first write can leave a bare file too, so the text
#: has to cover both. `rm -rf` handles either.
PROBE_LEFTOVER_STALE = (
    "%d stale %s* leftover(s) from a killed probe — a work dir may still hold "
    "a 0600 curl config with a bearer token in it. Clear them with:  rm -rf %s"
)


def token_check_level(reading: Usage) -> str:
    """The doctor verdict for one token's probe: ok / warn / fail.

    An `ok` reading that still carries an error text is a SUSPECT one — the
    RULING 8.2 clamp note is the live case — so it is a warn, never a clean ok.
    """
    if not reading.ok:
        return "fail"
    return "warn" if reading.error else "ok"


def token_check_detail(reading: Usage) -> str:
    """The doctor detail cell for one token's probe."""
    if not reading.ok:
        return reading.error or "probe failed"
    detail = "5h %s, 7d %s (%s)" % (
        pct(reading.five_h), pct(reading.seven_d), reading.probe
    )
    # An `ok` reading that still carries an error is a suspect one (the clamp
    # note). Doctor reported those as a clean `ok` with no hint at all.
    return detail if not reading.error else "%s — %s" % (detail, reading.error)


def probe_leftover_detail(prefix: str, stale: List[str]) -> str:
    """The doctor detail cell for the stale-probe-directory sweep."""
    if not stale:
        return PROBE_LEFTOVER_CLEAN % prefix
    shown = " ".join(stale[:3]) + (" ..." if len(stale) > 3 else "")
    return PROBE_LEFTOVER_STALE % (len(stale), prefix, shown)


MONITOR_INSTALLED = (
    "Installed the monitor agent:\n"
    "    program  %s\n    plist    %s\n    every    %ds\n    log      %s\n\n"
    "Remove it with: ctr uninstall-monitor"
)


def monitor_installed(program: str, path: str, interval_s: int, log_path: str) -> None:
    out(MONITOR_INSTALLED % (program, path, interval_s, log_path))


SHELL_INSTALLED = (
    "Shell activation installed:\n"
    "    %s   (guarded block, safe to re-run)\n"
    "    %s   (no secret: it reads the keychain at source time)\n\n"
    "Load it in this shell now:\n    source %s"
)


def shell_installed(rc_file: str, active_path: str) -> None:
    out(SHELL_INSTALLED % (rc_file, active_path, active_path))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status_json(store, records, usages, active, now) -> Dict:
    by_label = {u.label: u for u in usages}
    return {
        "version": 1,
        "now": now,
        "active": active,
        "config": store.config(),
        "tokens": [
            {
                "record": record.to_json(),
                "active": record.label == active,
                "usage": (by_label.get(record.label) or Usage.failed(record.label, "not probed", now)).to_json(),
            }
            for record in records
        ],
    }


def headroom_line(store, records, usages, active, now) -> str:
    if active is None:
        return "No active token. Pick one: ctr use <label>"
    by_label = {u.label: u for u in usages}
    current = by_label.get(active)
    if current is None or not current.ok:
        return "Active '%s' has no usable reading — try: ctr status --fresh" % active
    config = store.config()
    over = (current.five_h or 0) >= config["switch_at_5h"] or (
        current.seven_d or 0
    ) >= config["switch_at_7d"]
    if not over:
        return "Active '%s' at %s (5h) / %s (7d) — below the switch thresholds." % (
            active,
            pct(current.five_h),
            pct(current.seven_d),
        )
    from ctr import selector

    target = selector.choose_best(usages, [active], config, store.state(), now)
    if target is None:
        return "Active '%s' at %s (5h) — and no other token has headroom." % (
            active,
            pct(current.five_h),
        )
    return "Active '%s' at %s (5h) — `ctr next` would switch to '%s'." % (
        active,
        pct(current.five_h),
        target,
    )


# ---------------------------------------------------------------------------
# rollover
# ---------------------------------------------------------------------------


def print_rollover(result: Dict, dry_run: bool) -> None:
    """Render rollover.run() output.

    `acted` holds one entry per pane we act on — in a dry run those carry the
    commands and ran none of them. `planned` is the full census (acted plus
    skipped), so it is only counted, never listed twice.
    """
    error = result.get("error")
    if error:
        warn(error)
    acted = result.get("acted") or []
    skipped = result.get("skipped") or []
    planned = result.get("planned") or []
    # "No parked claude session needs a rollover." is a CLAIM about the fleet,
    # so it may only be made about panes we actually examined. Suppressing it
    # on ANY error also silenced it for the partial-failure case (one pane with
    # an unresolvable tab label, nothing to do), where it is both true and the
    # plainest thing to say. It stays suppressed only when nothing was examined
    # AND an error explains why — `--only` matching no pane, or `herdr agent
    # list` failing outright, where asserting it would be a guess.
    if not acted and (planned or not error):
        out("No parked claude session needs a rollover.")
    for item in acted:
        pane = str(item.get("pane") or "?")
        detail = str(item.get("detail") or "")
        if dry_run:
            out("would roll over %s — %s" % (pane, detail))
            for command in item.get("commands") or []:
                out("    %s" % command)
        else:
            out("%s %s — %s" % ("ok  " if item.get("ok") else "FAIL", pane, detail))
    for item in skipped:
        out(
            "skip %s — %s"
            % (item.get("pane") or "?", item.get("reason") or item.get("detail") or "")
        )
    census = len(planned)
    if census:
        out("")
        out("%d claude pane(s) examined, %d to roll over." % (census, len(acted)))
    if dry_run and acted:
        out("Run it for real with: ctr rollover --apply")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def tick_summary(result: Dict) -> str:
    """One line describing what a single monitor tick did. Pure.

    `ctr monitor --once` is the first thing a human runs to check the monitor
    works, and an uneventful tick writes nothing to the log by design. Without
    this the command printed absolutely nothing and looked broken.
    """
    if result.get("error"):
        return "monitor: %s" % result["error"]
    decision = result.get("decision")
    action = getattr(decision, "action", "hold")
    reason = getattr(decision, "reason", "") or "nothing to report"
    readings = [u for u in (result.get("usages") or []) if u is not None]
    ok = [u for u in readings if u.ok]
    seen = "%d token(s) probed" % len(readings)
    if len(ok) != len(readings):
        seen += ", %d unreadable" % (len(readings) - len(ok))
    if result.get("switched"):
        return "monitor: switched to '%s' — %s. Run `ctr rollover` to resume parked sessions." % (
            getattr(decision, "target", "?"),
            reason,
        )
    if action == "hold":
        return "monitor: no change (%s). %s" % (seen, reason)
    return "monitor: %s (%s). %s" % (action, seen, reason)


def doctor_report(checks: List) -> int:
    """Print the doctor table and its verdict. Returns the process exit code."""
    failed = 0
    rows = []
    for level, name, detail in checks:
        if level == "fail":
            failed += 1
        rows.append([level.upper() if level != "ok" else "ok", name, detail])
    out(table(["", "CHECK", "DETAIL"], rows))
    out("")
    if failed:
        out("%d check(s) failed. Fix those first." % failed)
    else:
        out("All good.")
    return 1 if failed else 0


__all__ = [
    "tick_summary",
    "out", "warn", "table", "HEADERS", "token_rows", "print_tokens",
    "HISTORY_WARNING", "stored_notice", "first_token_notice", "probe_result",
    "SWITCH_NOTICE", "switch_notice",
    "SETUP_HELP", "print_setup_instructions", "ADD_HELP",
    "PROBE_LEFTOVER_CLEAN", "PROBE_LEFTOVER_STALE", "probe_leftover_detail",
    "token_check_level", "token_check_detail",
    "MONITOR_INSTALLED", "monitor_installed", "SHELL_INSTALLED", "shell_installed",
    "status_json", "headroom_line", "print_rollover", "doctor_report",
]
