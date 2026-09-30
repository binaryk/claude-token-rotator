"""Tests for `ctr ui`: the pure row model always, the Textual app when installed."""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

from ctr import pretty, tui_model  # noqa: E402
from ctr.model import TokenRecord, Usage  # noqa: E402

NOW = 1790000000


def record(label):
    return TokenRecord(label=label, account=label + "@x", subscription="max",
                       added_at="", kind="oat", note="")


def reading(label, five=41.0, ok=True, status="allowed", error="", overage=""):
    return Usage(label=label, five_h=five if ok else None, seven_d=7.0 if ok else None,
                 five_h_reset=NOW + 3600, seven_d_reset=NOW + 7200, status=status,
                 probe="ratelimit_headers", ok=ok, error=error, checked_at=NOW, overage=overage)


class TestRows(unittest.TestCase):
    def test_one_row_per_account_with_active_marker_bars_and_fable(self):
        rows = tui_model.rows(
            [record("a"), record("b")],
            {"a": reading("a", overage="allowed"), "b": reading("b", 97.0, status="rejected")},
            "b", {"a": {"verdict": "yes"}, "b": {"verdict": "no"}}, "", NOW)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(rows[0]), len(tui_model.COLUMNS))
        self.assertEqual(rows[1][0].text, "●")
        self.assertEqual(rows[0][0].text, " ")
        self.assertIn("41%", rows[0][3].text)
        self.assertEqual(rows[1][2].text, "LIMIT REACHED")
        self.assertEqual(rows[1][3].style, "red")
        self.assertEqual(rows[0][4].text, "in 1h00m")
        self.assertEqual(rows[0][7].text, "allowed")
        self.assertEqual(rows[0][8].text, "yes")
        self.assertEqual(rows[1][8].text, "NO (limit)")

    def test_probing_and_unknown_fable(self):
        rows = tui_model.rows([record("a"), record("b")], {}, None, {}, "a", NOW)
        self.assertEqual(rows[0][8].text, "probing…")
        self.assertEqual(rows[1][8].text, "?")
        self.assertEqual(rows[1][2].text, "…")

    def test_a_failed_refresh_keeps_the_last_good_reading(self):
        merged = tui_model.merge_readings(
            {"a": reading("a", 41.0)}, {"a": reading("a", ok=False, error="probe rate limited (429)")})
        self.assertTrue(merged["a"].ok)
        self.assertEqual(merged["a"].five_h, 41.0)
        cell = tui_model.status_cell(merged["a"])
        self.assertTrue(cell.text.startswith("stale:"))

    def test_header_and_toast_are_secret_free_summaries(self):
        line = tui_model.header_line("keychain", "a", {"claude_store_holds": "a",
                                                       "sessions": {"following": 3, "pinned": 2}})
        self.assertIn("mode: keychain", line)
        self.assertIn("3 follow · 2 pinned", line)
        toast = tui_model.switch_message({"label": "a", "sessions": {"following": 3, "pinned": 2}})
        self.assertIn("ctr rollover", toast)


class TestPretty(unittest.TestCase):
    def test_bar_and_band(self):
        self.assertEqual(pretty.bar(50.0, 10), "█████░░░░░  50%")
        self.assertEqual(pretty.bar(150.0, 4), "████ 150%")
        self.assertEqual(pretty.band(10), "green")
        self.assertEqual(pretty.band(70), "yellow")
        self.assertEqual(pretty.band(90), "red")
        self.assertEqual(pretty.band(None), "dim")

    def test_non_tty_falls_back_to_the_v1_table(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            pretty.print_tokens(None, [record("a")], [reading("a")], "a", NOW)
        self.assertIn("LABEL", out.getvalue())  # render.HEADERS, i.e. the plain table


try:
    import textual  # noqa: F401
    HAVE_TEXTUAL = True
except ImportError:
    HAVE_TEXTUAL = False


@unittest.skipUnless(HAVE_TEXTUAL, "textual not installed (optional dependency)")
class TestApp(unittest.TestCase):
    """Drives the real Textual app headless; the data layer is faked."""

    def setUp(self):
        from ctr import cli, fable, switcher
        from ctr.store import Store

        self.tmp = tempfile.mkdtemp(prefix="ctr-tui-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(os.path.join(self.tmp, "cfg"))
        for label in ("a", "b"):
            self.store.add(record(label))
        self.store.set_active("a")
        self.switched = []

        def fake_activate(store, label, mode=None, active_path=None):
            self.switched.append((label, mode))
            store.set_active(label)
            return {"label": label, "mode": mode, "active_sh": "x", "claude_store": {}}

        patches = [
            (cli, "_usages", lambda store, records, fresh: [reading(r.label) for r in records]),
            (switcher, "live_view", lambda store, tokens=None: {"claude_store_holds": "a"}),
            (switcher, "activate", fake_activate),
            (fable, "probe", lambda token: ("yes", "answered")),
        ]
        from ctr import claude_login, keychain, sessions

        def refuse(*_a, **_k):
            raise AssertionError("test tried to run the real security tool")

        patches += [(keychain, "token_for", lambda label: "sk-ant-oat01-FAKE"),
                    (keychain, "_run", refuse), (claude_login, "_run", refuse),
                    (sessions, "_ps", lambda: "")]
        for obj, name, value in patches:
            original = getattr(obj, name)
            setattr(obj, name, value)
            self.addCleanup(setattr, obj, name, original)

    def test_rows_render_and_enter_switches_the_selected_account(self):
        from ctr.tui import CtrApp

        async def scenario():
            app = CtrApp(self.store, refresh_s=60)
            async with app.run_test(size=(160, 20)) as pilot:
                await pilot.pause(0.5)
                table = app.query_one("DataTable")
                self.assertEqual(table.row_count, 2)
                await pilot.press("j")
                await pilot.press("enter")
                await pilot.pause(0.5)
                await pilot.press("q")

        asyncio.run(scenario())
        self.assertEqual(self.switched, [("b", "keychain")])
        self.assertEqual(self.store.active(), "b")


if __name__ == "__main__":
    unittest.main()
