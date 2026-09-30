"""Coloured `ctr status` / `ctr list` via Rich — optional.

Rich is used only when it is importable AND stdout is a terminal; otherwise
this falls through to render.print_tokens, byte-for-byte the v1 table, so
pipes, scripts and tests see exactly what they always saw. ctr itself stays
stdlib-only; Rich arrives with Textual (`pip install textual`) or on its own.

Nothing here decides anything or touches a secret.
"""

import sys
from typing import List, Optional

from ctr import render
from ctr.model import TokenRecord, Usage, fmt_reset

BAR_WIDTH = 12
#: (upper bound exclusive, Rich style) — green, then yellow, then red.
BANDS = ((60.0, "green"), (85.0, "yellow"), (1000.0, "red"))


def _rich_console():
    """A Rich Console when Rich is usable here, else None."""
    if not sys.stdout.isatty():
        return None
    try:
        from rich.console import Console
    except ImportError:
        return None
    return Console()


def band(value: Optional[float]) -> str:
    """Style for a utilisation percentage. Pure."""
    if value is None:
        return "dim"
    for bound, style in BANDS:
        if value < bound:
            return style
    return "red"


def bar(value: Optional[float], width: int = BAR_WIDTH) -> str:
    """A text bar, e.g. '█████░░░░░░░ 41%'. Pure; '?' when unknown."""
    if value is None:
        return "?".rjust(width + 5)
    clamped = max(0.0, min(100.0, value))
    filled = int(round(clamped / 100.0 * width))
    return "%s%s %3d%%" % ("█" * filled, "░" * (width - filled), int(round(value)))


def note_for(reading: Optional[Usage]) -> str:
    if reading is None:
        return "no reading"
    if not reading.ok:
        return reading.error or "probe failed"
    if reading.status == "rejected":
        return "REJECTED (limit reached)"
    return reading.error or ""


def print_tokens(store, records: List[TokenRecord], usages: List[Usage],
                 active: Optional[str], now: int) -> None:
    console = _rich_console()
    if console is None:
        render.print_tokens(store, records, usages, active, now)
        return
    from rich.table import Table
    from rich.text import Text

    by_label = {u.label: u for u in usages}
    table = Table(box=None, header_style="bold", pad_edge=False)
    for name in (" ", "LABEL", "ACCOUNT", "5H", "RESETS", "7D", "RESETS", "NOTE"):
        table.add_column(name)
    for record in records:
        reading = by_label.get(record.label)
        five = reading.five_h if reading else None
        seven = reading.seven_d if reading else None
        is_active = record.label == active
        table.add_row(
            Text("●" if is_active else " ", style="bold cyan"),
            Text(record.label, style="bold cyan" if is_active else ""),
            record.account or "-",
            Text(bar(five), style=band(five)),
            fmt_reset(reading.five_h_reset, now) if reading else "-",
            Text(bar(seven), style=band(seven)),
            fmt_reset(reading.seven_d_reset, now) if reading else "-",
            Text(note_for(reading) or record.note, style="red" if reading and
                 (not reading.ok or reading.status == "rejected") else "dim"),
        )
    console.print(table)
    if active is None:
        console.print("\nNo active token. Pick one: ctr switch <label>")


__all__ = ["band", "bar", "print_tokens"]
