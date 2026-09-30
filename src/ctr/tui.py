"""`ctr ui` / `ctr top` — full-screen account dashboard (Textual, optional).

Keys: ↑/↓ or j/k select · enter switch · r refresh now · f probe Fable · q quit.

Every probe, keychain read and switch runs in a worker thread, so the screen
never freezes on a slow request. Usage refreshes every `refresh_s` seconds
(default 60) through the same cache `ctr status` uses; Fable availability is
probed lazily — one tiny request per account, cached for fable.FABLE_TTL_S.

Only tui_model decides what is shown; this module lays it out. Nothing here
holds a token longer than a worker call, and nothing prints one.
"""

from typing import Dict, List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

from ctr import fable, switcher, tui_model


class CtrApp(App):
    TITLE = "ctr — Claude accounts"
    CSS = """
    #context { padding: 0 1; color: $text-muted; height: 1; }
    DataTable { height: 1fr; }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("f", "fable", "probe Fable"),
        Binding("enter", "switch", "switch", priority=True),
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
    ]

    def __init__(self, store, refresh_s: int = 60, active_path: Optional[str] = None):
        super().__init__()
        self.store = store
        self.refresh_s = max(10, int(refresh_s))
        self.active_path = active_path
        self.records = []  # type: List
        self.usages = {}  # type: Dict
        self.live = {}  # type: Dict
        self.probing = ""
        self.fable_queue = []  # type: List[str]

    # -- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("loading…", id="context")
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*tui_model.COLUMNS)
        self.load(fresh=False)
        self.set_interval(self.refresh_s, lambda: self.load(fresh=False))
        self.set_interval(15, self.render_table)  # reset countdowns tick down

    # -- data (worker threads) ---------------------------------------------

    def load(self, fresh: bool) -> None:
        self.run_worker(lambda: self._load(fresh), thread=True, group="load", exclusive=True)

    def _load(self, fresh: bool) -> None:
        from ctr import cli

        records = self.store.tokens()
        readings = cli._usages(self.store, records, fresh) if records else []
        try:
            live = switcher.live_view(self.store)
        except Exception as exc:
            live = {"claude_store_holds": "unreadable (%s)" % exc.__class__.__name__}
        self.call_from_thread(self._loaded, records, {u.label: u for u in readings}, live)

    def _loaded(self, records, usages, live) -> None:
        self.records, self.live = records, live
        self.usages = tui_model.merge_readings(self.usages, usages)
        self.render_table()
        self.queue_stale_fable()

    def queue_stale_fable(self) -> None:
        now = self._now()
        state = self.store.state()
        for record in self.records:
            if fable.cached(state, record.label, now) is None and record.label not in self.fable_queue:
                self.fable_queue.append(record.label)
        self._next_fable()

    def _next_fable(self) -> None:
        if self.probing or not self.fable_queue:
            return
        self.probing = self.fable_queue.pop(0)
        self.render_table()
        label = self.probing
        self.run_worker(lambda: self._probe_fable(label), thread=True, group="fable")

    def _probe_fable(self, label: str) -> None:
        from ctr import keychain

        token = keychain.token_for(label)
        verdict, detail = fable.probe(token) if token else (fable.UNKNOWN, "no keychain item")
        fable.remember(self.store, label, verdict, detail, self._now())
        self.call_from_thread(self._fable_done)

    def _fable_done(self) -> None:
        self.probing = ""
        self.render_table()
        self._next_fable()

    # -- rendering ----------------------------------------------------------

    def render_table(self) -> None:
        table = self.query_one(DataTable)
        keep = table.cursor_row
        now = self._now()
        state = self.store.state()
        fable_map = {r.label: fable.cached(state, r.label, now) for r in self.records}
        active = self.store.active()
        table.clear()
        for record, cells in zip(self.records, tui_model.rows(
                self.records, self.usages, active, fable_map, self.probing, now)):
            table.add_row(*[Text(c.text, style=c.style) for c in cells], key=record.label)
        if self.records:
            table.move_cursor(row=min(keep, len(self.records) - 1))
        self.query_one("#context", Static).update(
            tui_model.header_line(self.store.switch_mode(), active, self.live))

    # -- actions ------------------------------------------------------------

    def selected_label(self) -> Optional[str]:
        table = self.query_one(DataTable)
        if not self.records or table.cursor_row < 0:
            return None
        return self.records[min(table.cursor_row, len(self.records) - 1)].label

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_refresh(self) -> None:
        self.notify("refreshing usage…", timeout=2)
        self.load(fresh=True)

    def action_fable(self) -> None:
        label = self.selected_label()
        if label and label != self.probing and label not in self.fable_queue:
            self.fable_queue.insert(0, label)
            self._next_fable()

    def action_switch(self) -> None:
        label = self.selected_label()
        if label:
            self.run_worker(lambda: self._switch(label), thread=True, group="switch",
                            exclusive=True)

    def _switch(self, label: str) -> None:
        from ctr import cli_switch

        try:
            result = switcher.activate(self.store, label, "keychain", self.active_path)
            payload = cli_switch._switch_payload(result, cli_switch._safe_live(self.store))
            self.call_from_thread(self.notify, tui_model.switch_message(payload), timeout=8)
        except Exception as exc:
            from ctr.cli import _safe_message

            self.call_from_thread(self.notify, "switch failed: %s" % _safe_message(exc),
                                  severity="error", timeout=10)
        self.call_from_thread(self.load, False)

    @staticmethod
    def _now() -> int:
        import time

        return int(time.time())


def run(store, refresh_s: int = 60, active_path: Optional[str] = None) -> int:
    CtrApp(store, refresh_s, active_path).run()
    return 0


__all__ = ["CtrApp", "run"]
