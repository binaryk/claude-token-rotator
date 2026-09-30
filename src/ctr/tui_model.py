"""What `ctr ui` shows — pure and stdlib-only, so it is testable without Textual.

tui.py only lays these rows out; every word and number on screen comes from
here. Nothing here performs I/O or holds a secret.
"""

from typing import Dict, List, NamedTuple, Optional

from ctr.model import TokenRecord, Usage, fmt_reset
from ctr.pretty import band, bar

COLUMNS = ("", "ACCOUNT", "STATUS", "5H", "5H RESET", "7D", "7D RESET", "OVERAGE", "FABLE")


class Cell(NamedTuple):
    text: str
    style: str = ""


def merge_readings(old: Dict[str, Usage], new: Dict[str, Usage]) -> Dict[str, Usage]:
    """New readings, except a failed probe keeps the last good one (marked stale)."""
    merged = dict(new)
    for label, reading in new.items():
        previous = old.get(label)
        if not reading.ok and previous is not None and previous.ok:
            note = "stale: " + (reading.error or "probe failed")
            merged[label] = previous._replace(error=note[:60])
    return merged


def status_cell(reading: Optional[Usage]) -> Cell:
    if reading is None:
        return Cell("…", "dim")
    if reading.ok and reading.error.startswith("stale:"):
        return Cell(reading.error[:40], "yellow")
    if not reading.ok:
        return Cell("error: %s" % (reading.error or "probe failed")[:40], "red")
    if reading.status == "rejected":
        return Cell("LIMIT REACHED", "bold red")
    return Cell(reading.status or "ok", "green")


def fable_cell(entry: Optional[Dict], probing: bool) -> Cell:
    if probing:
        return Cell("probing…", "yellow")
    if not entry:
        return Cell("?", "dim")
    verdict = entry.get("verdict")
    if verdict == "yes":
        return Cell("yes", "green")
    if verdict == "no":
        return Cell("NO (limit)", "bold red")
    return Cell("?", "yellow")


def overage_cell(reading: Optional[Usage]) -> Cell:
    value = (reading.overage if reading else "") or ""
    if not value:
        return Cell("-", "dim")
    return Cell(value, "green" if value.startswith("allowed") else "dim")


def rows(records: List[TokenRecord], usages: Dict[str, Usage], active: Optional[str],
         fable: Dict[str, Optional[Dict]], probing: str, now: int) -> List[List[Cell]]:
    """One row per account, in registry order."""
    out = []
    for record in records:
        reading = usages.get(record.label)
        five = reading.five_h if reading and reading.ok else None
        seven = reading.seven_d if reading and reading.ok else None
        is_active = record.label == active
        name = record.label + ("  " + record.account if record.account else "")
        out.append([
            Cell("●" if is_active else " ", "bold cyan"),
            Cell(name, "bold cyan" if is_active else ""),
            status_cell(reading),
            Cell(bar(five), band(five)),
            Cell(fmt_reset(reading.five_h_reset, now) if reading else "-"),
            Cell(bar(seven), band(seven)),
            Cell(fmt_reset(reading.seven_d_reset, now) if reading else "-"),
            overage_cell(reading),
            fable_cell(fable.get(record.label), probing == record.label),
        ])
    return out


def header_line(mode: str, active: Optional[str], live: Dict) -> str:
    """The one-line context under the title. Secret-free."""
    parts = ["mode: %s" % mode, "active: %s" % (active or "none")]
    holds = live.get("claude_store_holds")
    if holds:
        parts.append("Claude store holds: %s" % holds)
    sess = live.get("sessions") or {}
    if sess:
        parts.append("sessions: %d follow · %d pinned by env"
                     % (sess.get("following", 0), sess.get("pinned", 0)))
    return "   ".join(parts)


def switch_message(payload: Dict) -> str:
    """Toast after a switch, from cli_switch's payload. Secret-free."""
    sess = payload.get("sessions") or {}
    text = "Switched to %s — %d running session(s) follow" % (
        payload.get("label"), sess.get("following", 0))
    if sess.get("pinned"):
        text += "; %d pinned by env keep their token (ctr rollover)" % sess["pinned"]
    return text


__all__ = ["COLUMNS", "Cell", "merge_readings", "fable_cell", "header_line", "overage_cell", "rows",
           "status_cell", "switch_message"]
