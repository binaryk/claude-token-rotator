"""Unit tests for ctr.rollover.

Nothing here touches herdr, the network, the keychain or ~/.config/ctr:
rollover._run, rollover._now and rollover._sleep are replaced with fakes that
record every command. The herdr payloads below are captured from the live
herdr 0.8.2 on this Mac (2026-09-15), trimmed to the fields ctr reads.
"""

import json
import os
import sys
import unittest
from typing import NamedTuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from ctr import rollover  # noqa: E402
from ctr.model import Pane  # noqa: E402

# ---------------------------------------------------------------------------
# Captured herdr payloads
# ---------------------------------------------------------------------------

#: An agent WITHOUT a `name` key, on the BOSS tab, working.
AGENT_BOSS = {
    "agent": "claude", "agent_status": "working", "pane_id": "w6:pP", "tab_id": "w6:tH",
    "cwd": "/Users/x/ai-strategy",
    "agent_session": {"agent": "claude", "kind": "id", "value": "1e2abf24-boss"},
}
#: An agent WITHOUT a `name` key (12 of 20 live panes look like this).
AGENT_UNNAMED = {
    "agent": "claude", "agent_status": "idle", "pane_id": "w6:pC", "tab_id": "w6:t9",
    "cwd": "/Users/x/ai-strategy",
    "agent_session": {"agent": "claude", "kind": "id", "value": "3d3aef84-9e7f-4e45"},
}
#: An agent WITH a `name` key.
AGENT_NAMED = {
    "agent": "claude", "agent_status": "idle", "name": "context-bus",
    "pane_id": "w6:pR", "tab_id": "w6:tK", "cwd": "/Users/x/context-bus",
    "agent_session": {"agent": "claude", "kind": "id", "value": "c9e103f2-a126"},
}
#: The caller's own pane.
AGENT_SELF = {
    "agent": "claude", "agent_status": "working", "name": "token-rotator",
    "pane_id": "w6:pY", "tab_id": "w6:tS", "cwd": "/Users/x/claude-token-rotator",
    "agent_session": {"agent": "claude", "kind": "id", "value": "979199c5-7fe8"},
}
#: A codex agent -- must never be planned.
AGENT_CODEX = {
    "agent": "codex", "agent_status": "done", "pane_id": "w7:p15", "tab_id": "w7:t14",
    "cwd": "/Users/x/routines",
    "agent_session": {"agent": "codex", "kind": "id", "value": "01a0829d-b83d"},
}
#: An agent whose agent_session is missing entirely.
AGENT_NO_SESSION = {
    "agent": "claude", "agent_status": "idle", "name": "mcpsecverify",
    "pane_id": "w9:pB", "tab_id": "w9:tB", "cwd": "/Users/x/growee",
}
#: A second BOSS tab, parked on a limit -- BOSS must win over the marker.
AGENT_BOSS2 = {
    "agent": "claude", "agent_status": "idle", "pane_id": "w7:p1", "tab_id": "w7:t1",
    "cwd": "/Users/x/ai-strategy",
    "agent_session": {"agent": "claude", "kind": "id", "value": "aaaaaaaa-bbbb"},
}

FLEET = [AGENT_BOSS, AGENT_UNNAMED, AGENT_NAMED, AGENT_SELF, AGENT_CODEX,
         AGENT_NO_SESSION, AGENT_BOSS2]

AGENT_LIST_JSON = json.dumps({"id": "cli:agent:list", "result": {"agents": FLEET}})

TABS = [
    {"tab_id": "w6:tH", "label": "BOSS BOSS", "workspace_id": "w6"},
    {"tab_id": "w6:t9", "label": "events", "workspace_id": "w6"},
    {"tab_id": "w6:tK", "label": "context-bus", "workspace_id": "w6"},
    {"tab_id": "w6:tS", "label": "token-rotator", "workspace_id": "w6"},
    {"tab_id": "w7:t14", "label": "4", "workspace_id": "w7"},
    {"tab_id": "w9:tB", "label": "mcpsecverify", "workspace_id": "w9"},
    {"tab_id": "w7:t1", "label": "BOSS", "workspace_id": "w7"},
]
#: `herdr tab list` as a BARE ARRAY (the shape the contract records).
TAB_LIST_BARE_JSON = json.dumps(TABS)
#: `herdr tab list` as measured on herdr 0.8.2 today: an object.
TAB_LIST_OBJECT_JSON = json.dumps({"id": "cli:tab:list", "result": {"tabs": TABS}})

PROC_CLAUDE = {"id": "cli:pane:process_info", "result": {"process_info": {
    "foreground_processes": [
        {"argv0": "node", "cmdline": "node /p/mcp-server.cjs", "name": "node", "pid": 5262},
        {"argv0": "claude", "cmdline": "claude --resume 3d3aef84", "name": "2.1.267", "pid": 3944},
    ], "pane_id": "w6:pC", "shell_pid": 52121}}}
PROC_SHELL = {"id": "cli:pane:process_info", "result": {"process_info": {
    "foreground_processes": [], "pane_id": "w6:pC", "shell_pid": 52121}}}
PROC_CLAUDE_JSON = json.dumps(PROC_CLAUDE)
PROC_SHELL_JSON = json.dumps(PROC_SHELL)

AGENT_GET_JSON = json.dumps({"id": "cli:agent:get", "result": {
    "agent": AGENT_NAMED, "type": "agent_info"}})

PARKED_VIEWPORT = (
    "  I'll keep going once the window resets.\n"
    "Usage limit reached · continuing automatically at 4pm\n"
    "⚠ /low-priority to continue now\n")
ORDINARY_VIEWPORT = (
    "  Wrote the rate limit design doc and pushed it.\n"
    "❯\n  [OMC#4.10.1] | 5h:50%(1h40m) wk:18%(6d16h) | thinking\n")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner(object):
    """Stands in for rollover._run. Records every command it is handed."""

    def __init__(self, rules=None, default=(0, "", "")):
        self.calls = []
        self.rules = list(rules or [])
        self.default = default

    def __call__(self, cmd, timeout_s=rollover.DEFAULT_TIMEOUT_S):
        argv = [str(part) for part in cmd]
        self.calls.append(argv)
        joined = " ".join(argv)
        for needle, response in self.rules:
            if needle in joined:
                return response
        return self.default

    @property
    def commands(self):
        return [" ".join(argv) for argv in self.calls]

    def mutating(self):
        return [" ".join(argv) for argv in self.calls
                if any(part in rollover.MUTATING_HERDR_VERBS for part in argv[1:3])]


class FakeClock(object):
    """Monotonic clock that advances `step` seconds on every call."""

    def __init__(self, step=1.0, start=0.0):
        self.now = start
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


FLEET_RULES = [
    ("agent list", (0, AGENT_LIST_JSON, "")),
    ("tab list", (0, TAB_LIST_OBJECT_JSON, "")),
    ("agent read w6:pC", (0, PARKED_VIEWPORT, "")),
    ("agent read w7:p1", (0, PARKED_VIEWPORT, "")),
    ("agent read", (0, ORDINARY_VIEWPORT, "")),
    ("pane process-info", (0, PROC_SHELL_JSON, "")),
    ("agent get", (0, AGENT_GET_JSON, "")),
]


def pane(pane_id="w6:pC", tab_id="w6:t9", name="sess", status="idle",
         session_id="3d3aef84-9e7f-4e45", cwd="/Users/x", tab_label="events"):
    return Pane(pane_id=pane_id, tab_id=tab_id, name=name, status=status,
                session_id=session_id, cwd=cwd, tab_label=tab_label)


class AgentPane(NamedTuple):
    """Pane-like object that also carries the herdr agent kind."""

    pane_id: str
    tab_id: str
    name: str
    status: str
    session_id: str
    cwd: str
    tab_label: str
    agent: str


class RolloverTestCase(unittest.TestCase):
    """Replaces every side-effecting indirection in rollover."""

    def setUp(self):
        self._originals = (rollover._run, rollover._now, rollover._sleep)
        self.slept = []
        rollover._sleep = self.slept.append
        self.addCleanup(self._restore)

    def _restore(self):
        rollover._run, rollover._now, rollover._sleep = self._originals

    def use_runner(self, rules=None, default=(0, "", "")):
        runner = FakeRunner(rules, default)
        rollover._run = runner
        return runner

    def use_clock(self, step=1.0):
        clock = FakeClock(step)
        rollover._now = clock
        return clock

    def use_env(self, pane_id):
        previous = os.environ.get(rollover.HERDR_PANE_ENV)
        if pane_id is None:
            os.environ.pop(rollover.HERDR_PANE_ENV, None)
        else:
            os.environ[rollover.HERDR_PANE_ENV] = pane_id
        self.addCleanup(self._restore_env, previous)

    def _restore_env(self, previous):
        if previous is None:
            os.environ.pop(rollover.HERDR_PANE_ENV, None)
        else:
            os.environ[rollover.HERDR_PANE_ENV] = previous


# ---------------------------------------------------------------------------
# has_limit_marker (pure)
# ---------------------------------------------------------------------------


class TestHasLimitMarker(unittest.TestCase):
    def test_usage_limit_reached_line(self):
        self.assertTrue(rollover.has_limit_marker(
            "Usage limit reached · continuing automatically at 4pm"))

    def test_low_priority_line(self):
        self.assertTrue(rollover.has_limit_marker("⚠ /low-priority to continue now"))

    def test_marker_anywhere_in_a_viewport(self):
        self.assertTrue(rollover.has_limit_marker(PARKED_VIEWPORT))

    def test_case_insensitive(self):
        self.assertTrue(rollover.has_limit_marker("USAGE LIMIT REACHED, sorry"))

    def test_ordinary_output_is_not_a_marker(self):
        self.assertFalse(rollover.has_limit_marker(ORDINARY_VIEWPORT))

    def test_text_merely_mentioning_limit_is_not_a_marker(self):
        self.assertFalse(rollover.has_limit_marker("the rate limit design doc"))
        self.assertFalse(rollover.has_limit_marker("limit"))
        self.assertFalse(rollover.has_limit_marker("usage limits reached their peak"))

    def test_empty_is_not_a_marker(self):
        self.assertFalse(rollover.has_limit_marker(""))
        self.assertFalse(rollover.has_limit_marker(None))


# ---------------------------------------------------------------------------
# plan() -- the safety predicate. One test per refusal.
# ---------------------------------------------------------------------------


class TestPlanRefusals(unittest.TestCase):
    PARKED = {"w6:pC": PARKED_VIEWPORT, "w7:p1": PARKED_VIEWPORT, "w6:pY": PARKED_VIEWPORT}

    def only(self, target, self_pane=None, viewports=None):
        if viewports is None:
            viewports = self.PARKED
        plans = rollover.plan([target], self_pane, viewports)
        self.assertEqual(1, len(plans))
        return plans[0]

    def test_acts_on_a_parked_idle_claude_pane(self):
        item = self.only(pane())
        self.assertTrue(item.act)
        self.assertIn(rollover.REASON_ACT, item.reason)
        self.assertIn("3d3aef84", item.reason)

    def test_refuses_the_callers_own_pane(self):
        item = self.only(pane(pane_id="w6:pY", tab_label="token-rotator"), self_pane="w6:pY")
        self.assertFalse(item.act)
        self.assertTrue(item.reason.startswith(rollover.REASON_OWN_PANE))

    def test_refuses_a_boss_tab(self):
        item = self.only(pane(pane_id="w7:p1", tab_label="BOSS BOSS"))
        self.assertFalse(item.act)
        self.assertTrue(item.reason.startswith(rollover.REASON_BOSS_TAB))

    def test_boss_match_is_case_insensitive_and_substring(self):
        for label in ("BOSS BOSS", "boss", "Boss", "the-boss-tab"):
            item = self.only(pane(pane_id="w7:p1", tab_label=label))
            self.assertFalse(item.act, label)
            self.assertTrue(item.reason.startswith(rollover.REASON_BOSS_TAB), label)

    def test_refuses_a_working_agent(self):
        item = self.only(pane(status="working"))
        self.assertFalse(item.act)
        self.assertTrue(item.reason.startswith(rollover.REASON_WORKING))

    def test_refuses_a_non_claude_agent(self):
        codex = AgentPane(pane_id="w6:pC", tab_id="w6:t9", name="x", status="idle",
                          session_id="3d3aef84-9e7f-4e45", cwd="/Users/x",
                          tab_label="events", agent="codex")
        item = self.only(codex)
        self.assertFalse(item.act)
        self.assertTrue(item.reason.startswith(rollover.REASON_NOT_CLAUDE))

    def test_accepts_a_pane_like_object_that_says_claude(self):
        claude = AgentPane(pane_id="w6:pC", tab_id="w6:t9", name="x", status="idle",
                           session_id="3d3aef84-9e7f-4e45", cwd="/Users/x",
                           tab_label="events", agent="claude")
        self.assertTrue(self.only(claude).act)

    def test_refuses_a_pane_without_a_session_id(self):
        item = self.only(pane(session_id=""))
        self.assertFalse(item.act)
        self.assertEqual(rollover.REASON_NO_SESSION, item.reason)

    def test_refuses_a_pane_with_no_limit_marker(self):
        item = self.only(pane(), viewports={"w6:pC": ORDINARY_VIEWPORT})
        self.assertFalse(item.act)
        self.assertEqual(rollover.REASON_NO_MARKER, item.reason)

    def test_refuses_a_pane_whose_viewport_was_never_read(self):
        item = self.only(pane(), viewports={})
        self.assertFalse(item.act)
        self.assertEqual(rollover.REASON_NO_MARKER, item.reason)

    def test_refuses_a_pane_that_trips_several_rules_at_once(self):
        # own pane + BOSS tab + working + no session + no marker, all together.
        doomed = pane(pane_id="w6:pY", tab_label="BOSS BOSS", status="working",
                      session_id="")
        item = self.only(doomed, self_pane="w6:pY", viewports={})
        self.assertFalse(item.act)
        self.assertTrue(
            any(item.reason.startswith(reason) for reason in rollover.REFUSAL_REASONS),
            item.reason)

    def test_plan_is_pure(self):
        panes = [pane(), pane(pane_id="w6:pR", tab_label="ctx")]
        before = list(panes)
        viewports = dict(self.PARKED)
        plans = rollover.plan(panes, None, viewports)
        self.assertEqual(before, panes)
        self.assertEqual(dict(self.PARKED), viewports)
        self.assertEqual([p.pane_id for p in panes], [item.pane.pane_id for item in plans])

    def test_plan_tolerates_a_missing_viewports_mapping(self):
        plans = rollover.plan([pane()], None, None)
        self.assertFalse(plans[0].act)


# ---------------------------------------------------------------------------
# herdr JSON parsing
# ---------------------------------------------------------------------------


class TestHerdrJson(RolloverTestCase):
    def test_parses_an_object(self):
        self.use_runner([("agent list", (0, AGENT_LIST_JSON, ""))])
        self.assertEqual(7, len(rollover.herdr_json(["agent", "list"])["result"]["agents"]))

    def test_returns_none_on_unparseable_output(self):
        self.use_runner(default=(0, "herdr: not json", ""))
        self.assertIsNone(rollover.herdr_json(["agent", "list"]))

    def test_returns_none_when_the_command_fails_with_no_output(self):
        self.use_runner(default=(127, "", "command not found: herdr"))
        self.assertIsNone(rollover.herdr_json(["agent", "list"]))

    def test_parses_a_json_error_body_on_a_non_zero_exit(self):
        self.use_runner(default=(1, '{"error":"no such pane"}', ""))
        self.assertEqual({"error": "no such pane"}, rollover.herdr_json(["agent", "get", "x"]))

    def test_refuses_a_mutating_verb_from_the_read_only_path(self):
        runner = self.use_runner()
        with self.assertRaises(rollover.RolloverSafetyError):
            rollover.herdr_json(["agent", "prompt", "w6:pC", "/exit"])
        self.assertEqual([], runner.calls)


class TestParsers(unittest.TestCase):
    def test_parse_agent_list_object_shape(self):
        agents = rollover._parse_agent_list(json.loads(AGENT_LIST_JSON))
        self.assertEqual(7, len(agents))

    def test_parse_agent_list_bare_array(self):
        self.assertEqual(7, len(rollover._parse_agent_list(FLEET)))

    def test_parse_agent_list_garbage(self):
        for payload in (None, {}, [], {"result": {}}, "nope", {"result": None}):
            self.assertEqual([], rollover._parse_agent_list(payload))

    def test_parse_tab_labels_bare_array(self):
        labels = rollover._parse_tab_labels(json.loads(TAB_LIST_BARE_JSON))
        self.assertEqual("BOSS BOSS", labels["w6:tH"])
        self.assertEqual("events", labels["w6:t9"])

    def test_parse_tab_labels_object_shape(self):
        labels = rollover._parse_tab_labels(json.loads(TAB_LIST_OBJECT_JSON))
        self.assertEqual("BOSS BOSS", labels["w6:tH"])
        self.assertEqual(len(TABS), len(labels))

    def test_parse_tab_labels_garbage(self):
        for payload in (None, {}, "nope", {"result": 3}):
            self.assertEqual({}, rollover._parse_tab_labels(payload))

    def test_build_panes_drops_codex_and_fills_labels(self):
        panes = rollover._build_panes(
            rollover._parse_agent_list(json.loads(AGENT_LIST_JSON)),
            rollover._parse_tab_labels(json.loads(TAB_LIST_BARE_JSON)))
        self.assertEqual(6, len(panes))
        self.assertNotIn("w7:p15", [p.pane_id for p in panes])
        by_id = dict((p.pane_id, p) for p in panes)
        self.assertEqual("BOSS BOSS", by_id["w6:pP"].tab_label)
        self.assertEqual("BOSS", by_id["w7:p1"].tab_label)

    def test_build_panes_handles_a_missing_name_key(self):
        panes = rollover._build_panes([AGENT_UNNAMED], {"w6:t9": "events"})
        self.assertEqual("", panes[0].name)
        self.assertEqual("3d3aef84-9e7f-4e45", panes[0].session_id)

    def test_build_panes_handles_a_missing_agent_session(self):
        panes = rollover._build_panes([AGENT_NO_SESSION], {})
        self.assertEqual("", panes[0].session_id)

    def test_build_panes_marks_an_unresolvable_tab_label_unknown(self):
        """A tab we cannot name must NOT look like a tab with an empty label.

        An empty label matches no BOSS pattern, so the old `labels.get(tab_id,
        "")` made an unrecognised `herdr tab list` payload indistinguishable
        from "this tab has no name" and ctr would act on the BOSS pane.
        """
        panes = rollover._build_panes([AGENT_NO_SESSION], {})
        self.assertEqual(rollover.UNKNOWN_TAB_LABEL, panes[0].tab_label)

    def test_plan_refuses_a_pane_whose_tab_label_is_unknown(self):
        unknown = Pane(pane_id="w6:pP", tab_id="w6:tH", name="boss-ish", status="idle",
                       session_id="sid-1234567890", cwd="/Users/x",
                       tab_label=rollover.UNKNOWN_TAB_LABEL)
        plans = rollover.plan([unknown], None, {"w6:pP": "Usage limit reached"})
        self.assertFalse(plans[0].act)
        self.assertIn(rollover.REASON_UNKNOWN_TAB, plans[0].reason)

    def test_unrecognised_tab_list_shape_refuses_every_pane(self):
        """The whole-stack version: well-formed JSON in an unknown shape."""
        payload = json.dumps({"id": "cli:tab:list", "result": {"items": [
            {"tab_id": "w6:tH", "label": "BOSS BOSS"}]}})
        labels = rollover._parse_tab_labels(json.loads(payload))
        self.assertEqual({}, labels, "precondition: the shape is unrecognised")
        panes = rollover._build_panes(
            rollover._parse_agent_list(json.loads(AGENT_LIST_JSON)), labels)
        plans = rollover.plan(panes, None, dict(
            (p.pane_id, "Usage limit reached") for p in panes))
        self.assertEqual([], [p.pane.pane_id for p in plans if p.act])

    def test_agent_get_fields(self):
        self.assertEqual(("c9e103f2-a126", "context-bus"),
                         rollover._agent_get_fields(json.loads(AGENT_GET_JSON)))

    def test_agent_get_status(self):
        self.assertEqual("", rollover._agent_get_status(None))
        self.assertEqual("", rollover._agent_get_status({"result": {"agent": None}}))
        self.assertEqual("working", rollover._agent_get_status(
            {"result": {"agent": {"agent_status": "working"}}}))

    def test_agent_get_fields_when_absent(self):
        for payload in (None, {}, {"result": {}}, {"result": {"agent": None}},
                        {"result": {"agent": {"agent": "claude"}}}):
            self.assertEqual(("", ""), rollover._agent_get_fields(payload))


class TestListPanes(RolloverTestCase):
    def test_lists_claude_panes_only(self):
        self.use_runner(FLEET_RULES)
        panes = rollover.list_panes()
        self.assertEqual(6, len(panes))
        self.assertTrue(all(p.pane_id != "w7:p15" for p in panes))

    def test_bare_array_tab_list_is_accepted(self):
        rules = [("tab list", (0, TAB_LIST_BARE_JSON, ""))] + FLEET_RULES
        self.use_runner(rules)
        by_id = dict((p.pane_id, p) for p in rollover.list_panes())
        self.assertEqual("BOSS BOSS", by_id["w6:pP"].tab_label)

    def test_refuses_to_list_when_tab_labels_are_unavailable(self):
        self.use_runner([("agent list", (0, AGENT_LIST_JSON, "")),
                         ("tab list", (1, "", "boom"))])
        panes, error = rollover._list_panes_raw()
        self.assertEqual([], panes)
        self.assertIn("tab list", error)

    def test_reports_an_agent_list_failure(self):
        self.use_runner(default=(127, "", "command not found: herdr"))
        panes, error = rollover._list_panes_raw()
        self.assertEqual([], panes)
        self.assertIn("agent list", error)


# ---------------------------------------------------------------------------
# process-info parsing and wait_for_shell
# ---------------------------------------------------------------------------


class TestClaudeProcessDetection(unittest.TestCase):
    def test_native_build_reports_a_version_string_as_name(self):
        proc = {"argv0": "claude", "cmdline": "claude --resume abc", "name": "2.1.267"}
        self.assertTrue(rollover._is_claude_process(proc))

    def test_never_matches_on_the_name_field_alone(self):
        proc = {"argv0": "node", "cmdline": "node /p/mcp-server.cjs", "name": "claude"}
        self.assertFalse(rollover._is_claude_process(proc))

    def test_mcp_child_is_not_claude(self):
        proc = {"argv0": "node", "cmdline": "node /p/mcp-server.cjs", "name": "node"}
        self.assertFalse(rollover._is_claude_process(proc))

    def test_matches_an_absolute_path_in_cmdline(self):
        proc = {"argv0": "", "cmdline": "/usr/local/bin/claude --resume abc", "name": "2.1.267"}
        self.assertTrue(rollover._is_claude_process(proc))

    def test_unrelated_processes(self):
        for proc in ({"argv0": "caffeinate", "cmdline": "caffeinate -i -t 300", "name": "caffeinate"},
                     {"argv0": "zsh", "cmdline": "-zsh", "name": "zsh"},
                     {}):
            self.assertFalse(rollover._is_claude_process(proc))

    def test_shell_is_back_only_on_a_recognised_payload(self):
        self.assertFalse(rollover._shell_is_back(PROC_CLAUDE))
        self.assertTrue(rollover._shell_is_back(PROC_SHELL))
        self.assertFalse(rollover._shell_is_back(None))
        self.assertFalse(rollover._shell_is_back({"result": {}}))


class TestWaitForShell(RolloverTestCase):
    def test_returns_true_when_the_shell_is_already_back(self):
        runner = self.use_runner([("process-info", (0, PROC_SHELL_JSON, ""))])
        self.use_clock(1.0)
        self.assertTrue(rollover.wait_for_shell("w6:pC"))
        self.assertEqual([], runner.mutating())
        self.assertEqual([], self.slept)

    def test_polls_until_claude_is_gone(self):
        calls = []

        def runner(cmd, timeout_s=30):
            calls.append([str(part) for part in cmd])
            if "process-info" in " ".join(str(part) for part in cmd):
                body = PROC_CLAUDE_JSON if len(calls) < 3 else PROC_SHELL_JSON
                return (0, body, "")
            return (0, "", "")

        rollover._run = runner
        self.use_clock(1.0)
        self.assertTrue(rollover.wait_for_shell("w6:pC"))
        self.assertEqual(3, len(calls))
        self.assertEqual([rollover.WAIT_POLL_S, rollover.WAIT_POLL_S], self.slept)

    def test_times_out_and_sends_exactly_one_ctrl_c_fallback(self):
        runner = self.use_runner([("process-info", (0, PROC_CLAUDE_JSON, ""))])
        self.use_clock(20.0)
        self.assertFalse(rollover.wait_for_shell("w6:pC"))
        fallback = [cmd for cmd in runner.commands if "ctrl+c" in cmd]
        self.assertEqual(["herdr agent send-keys w6:pC ctrl+c ctrl+c"], fallback)

    def test_an_unreadable_pane_never_counts_as_a_free_shell(self):
        self.use_runner(default=(1, "", "no such pane"))
        self.use_clock(20.0)
        self.assertFalse(rollover.wait_for_shell("w6:pZ"))


# ---------------------------------------------------------------------------
# agent start outcome
# ---------------------------------------------------------------------------


class TestStartOutcome(unittest.TestCase):
    def test_timeout_is_success_pending(self):
        ok, detail = rollover._start_outcome(1, '{"result":{"status":"timeout"}}', "")
        self.assertTrue(ok)
        self.assertIn("success-pending", detail)

    def test_timeout_on_stderr_is_success_pending(self):
        ok, _detail = rollover._start_outcome(2, "", "Error: agent start timed out")
        self.assertTrue(ok)

    def test_clean_exit_is_success(self):
        ok, detail = rollover._start_outcome(0, '{"result":{"status":"ready"}}', "")
        self.assertTrue(ok)
        self.assertEqual("started and resumed", detail)

    def test_our_own_subprocess_kill_is_never_success_pending(self):
        """rc 124 is `_run` killing herdr, not herdr reporting a timeout.

        `_run` synthesises the text "timed out after 180s" itself, so a plain
        substring search reported a hung `agent start` as a completed rollover
        while the pane sat /exit-ed and dead.
        """
        returncode, out, err = rollover._run(["sleep", "3"], timeout_s=1)
        self.assertEqual(rollover.TRANSPORT_TIMEOUT_RC, returncode)
        ok, detail = rollover._start_outcome(returncode, out, err)
        self.assertFalse(ok)
        self.assertIn("probably down", detail)

    def test_our_own_timeout_text_alone_is_not_success_pending(self):
        ok, _detail = rollover._start_outcome(124, "", "timed out after 180s")
        self.assertFalse(ok)

    def test_an_echoed_timeout_flag_is_not_success_pending(self):
        for out, err in (
            ("", "error: unexpected argument '--timeout' found\nusage: herdr agent start"),
            ('{"error":"pane w6:pZ is busy","request":{"timeout":150000}}', ""),
        ):
            ok, _detail = rollover._start_outcome(1, out, err)
            self.assertFalse(ok, "%r must not read as a herdr timeout" % (err or out))

    def test_other_failures_are_failures(self):
        ok, detail = rollover._start_outcome(1, "", "pane w6:pZ not found\nmore")
        self.assertFalse(ok)
        self.assertIn("rc=1", detail)
        self.assertIn("pane w6:pZ not found", detail)


# ---------------------------------------------------------------------------
# rollover_pane
# ---------------------------------------------------------------------------


class TestRolloverPaneDryRun(RolloverTestCase):
    def test_runs_nothing_and_lists_the_exact_commands(self):
        runner = self.use_runner(FLEET_RULES)
        result = rollover.rollover_pane(pane(name="events"), dry_run=True)
        self.assertEqual([], runner.calls)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual([
            "herdr agent get w6:pC",
            "herdr agent read w6:pC --source visible --lines 12",
            "herdr agent send-keys w6:pC esc",
            "herdr agent prompt w6:pC /exit",
            "herdr pane process-info --pane w6:pC",
            "herdr agent start events --kind claude --pane w6:pC "
            "--timeout 150000 -- --resume 3d3aef84-9e7f-4e45",
            "herdr agent rename w6:pC events",
        ], result["commands"])

    def test_an_unnamed_pane_gets_a_fallback_name_and_no_rename(self):
        self.use_runner(FLEET_RULES)
        result = rollover.rollover_pane(pane(name=""), dry_run=True)
        self.assertEqual(6, len(result["commands"]))
        self.assertIn("herdr agent start ctr-w6-pC --kind claude", result["commands"][5])
        self.assertEqual([], [cmd for cmd in result["commands"] if "rename" in cmd])


class TestRolloverPaneLive(RolloverTestCase):
    def live_rules(self, extra=None):
        rules = list(extra or [])
        rules += [("agent get", (0, AGENT_GET_JSON, "")),
                  ("agent read", (0, PARKED_VIEWPORT, "")),
                  ("pane process-info", (0, PROC_SHELL_JSON, ""))]
        return rules

    def test_happy_path_order_and_rename_back(self):
        runner = self.use_runner(self.live_rules())
        self.use_clock(1.0)
        target = pane(pane_id="w6:pR", name="context-bus", tab_label="context-bus",
                      session_id="c9e103f2-a126")
        result = rollover.rollover_pane(target, dry_run=False)
        self.assertTrue(result["ok"], result["detail"])
        self.assertEqual([
            "herdr agent get w6:pR",
            "herdr agent read w6:pR --source visible --lines 12",
            "herdr agent send-keys w6:pR esc",
            "herdr agent prompt w6:pR /exit",
            "herdr pane process-info --pane w6:pR",
            "herdr agent start context-bus --kind claude --pane w6:pR "
            "--timeout 150000 -- --resume c9e103f2-a126",
            "herdr agent rename w6:pR context-bus",
        ], runner.commands)
        self.assertEqual(runner.commands, result["commands"])

    def test_a_start_timeout_is_treated_as_success(self):
        self.use_runner(self.live_rules(
            [("agent start", (1, '{"result":{"status":"timeout"}}', ""))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(pane(pane_id="w6:pR", name="context-bus"),
                                        dry_run=False)
        self.assertTrue(result["ok"])
        self.assertIn("success-pending", result["detail"])

    def test_refuses_a_pane_that_started_working_since_the_plan(self):
        """The plan is a snapshot; panes are rolled over sequentially.

        A parked session resumes itself ("continuing automatically at <time>"),
        so pane #3 of a multi-pane park is acted on minutes after its status
        was read. `agent get` is step 1 of the frozen recipe and already
        carries the fresh status - refusing on it costs no extra herdr call.
        """
        working = json.dumps({"result": {"agent": {
            "agent_status": "working", "name": "context-bus",
            "agent_session": {"value": "c9e103f2-a126"}}}})
        runner = self.use_runner(self.live_rules([("agent get", (0, working, ""))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(
            pane(pane_id="w6:pR", name="context-bus", status="idle"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertIn(rollover.REASON_WORKING, result["detail"])
        self.assertEqual([], runner.mutating(), "a working pane must not be touched")

    def test_refuses_a_pane_whose_marker_vanished_since_the_plan(self):
        """The second abort path, and the one `agent get` cannot see.

        A parked session resumes BY ITSELF - the marker literally says
        "continuing automatically at <time>" - and herdr can still report the
        pane `idle` while the model is already answering, because the viewport
        changes before `agent_status` does. RULING 8.4 authorises the extra
        read-only `agent read` this costs: /exit-ing a session that is mid-turn
        is far more expensive than one wasted herdr call.
        """
        runner = self.use_runner(self.live_rules(
            [("agent read", (0, ORDINARY_VIEWPORT, ""))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(
            pane(pane_id="w6:pR", name="context-bus"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(rollover.REASON_MARKER_GONE, result["detail"])
        self.assertEqual([], runner.mutating(),
                         "a session that resumed itself must not be touched")
        self.assertIn("herdr agent read w6:pR --source visible --lines 12",
                      runner.commands)

    def test_the_marker_is_re_read_after_agent_get_and_before_anything_mutates(self):
        runner = self.use_runner(self.live_rules(
            [("agent read", (0, ORDINARY_VIEWPORT, ""))]))
        self.use_clock(1.0)
        rollover.rollover_pane(pane(pane_id="w6:pR", name="context-bus"), dry_run=False)
        self.assertEqual(["herdr agent get w6:pR",
                          "herdr agent read w6:pR --source visible --lines 12"],
                         runner.commands)

    def test_an_unreadable_viewport_refuses_the_pane(self):
        """Failing closed costs one missed rollover; failing open costs a turn.

        It must NOT be reported as REASON_MARKER_GONE. `read_viewport` returns
        None for any non-zero rc - a transport timeout, herdr restarting, "no
        such pane" - and "the session resumed itself" is a causal claim none of
        those establish. The operator would read it as "that stuck session
        recovered" when the pane is in fact still parked and untouched.
        """
        runner = self.use_runner(self.live_rules(
            [("agent read", (1, "", "no such pane"))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(
            pane(pane_id="w6:pR", name="context-bus"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(rollover.REASON_VIEWPORT_UNREADABLE, result["detail"])
        self.assertNotEqual(rollover.REASON_MARKER_GONE, result["detail"])
        self.assertNotIn("resumed itself", result["detail"])
        self.assertEqual([], runner.mutating())

    def test_a_readable_but_empty_viewport_is_the_marker_being_gone(self):
        """rc 0 with no text IS evidence: we read the pane and the marker is
        not there. That is the one case REASON_MARKER_GONE may be claimed for
        without a viewport full of text."""
        runner = self.use_runner(self.live_rules(
            [("agent read", (0, "", ""))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(
            pane(pane_id="w6:pR", name="context-bus"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(rollover.REASON_MARKER_GONE, result["detail"])
        self.assertEqual([], runner.mutating())

    def test_the_viewport_text_never_reaches_the_refusal_detail(self):
        self.use_runner(self.live_rules([("agent read", (0, ORDINARY_VIEWPORT, ""))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(pane(pane_id="w6:pR"), dry_run=False)
        self.assertNotIn("OMC#4.10.1", json.dumps(result))

    def test_refuses_a_pane_with_no_session_id_without_touching_it(self):
        runner = self.use_runner([("agent get", (0, json.dumps({"result": {}}), ""))])
        result = rollover.rollover_pane(pane(session_id=""), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(rollover.REASON_NO_SESSION, result["detail"])
        self.assertEqual([], runner.mutating())

    def test_falls_back_to_the_session_id_we_already_had(self):
        runner = self.use_runner(self.live_rules(
            [("agent get", (1, "", "herdr: pane busy"))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(pane(name="events"), dry_run=False)
        self.assertTrue(result["ok"], result["detail"])
        self.assertIn("--resume 3d3aef84-9e7f-4e45", " ".join(runner.commands))

    def test_stops_when_the_session_will_not_exit(self):
        runner = self.use_runner(self.live_rules(
            [("pane process-info", (0, PROC_CLAUDE_JSON, ""))]))
        self.use_clock(20.0)
        result = rollover.rollover_pane(pane(name="events"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertIn("still in the foreground", result["detail"])
        self.assertEqual([], [cmd for cmd in runner.commands if "agent start" in cmd])

    def test_stops_when_esc_fails(self):
        runner = self.use_runner(self.live_rules(
            [("send-keys", (1, "", "pane gone"))]))
        result = rollover.rollover_pane(pane(name="events"), dry_run=False)
        self.assertFalse(result["ok"])
        self.assertIn("esc failed", result["detail"])
        self.assertEqual([], [cmd for cmd in runner.commands if "prompt" in cmd])

    def test_reports_a_failed_rename_without_failing_the_rollover(self):
        self.use_runner(self.live_rules([("agent rename", (1, "", "name taken"))]))
        self.use_clock(1.0)
        result = rollover.rollover_pane(pane(name="events"), dry_run=False)
        self.assertTrue(result["ok"])
        self.assertIn("rename back", result["detail"])


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun(RolloverTestCase):
    def test_dry_run_performs_zero_mutating_herdr_calls(self):
        runner = self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True)
        self.assertEqual([], runner.mutating(), "dry run must never mutate a pane")
        self.assertTrue(result["dry_run"])
        self.assertEqual("", result["error"])

    def test_dry_run_acts_only_on_the_parked_non_boss_pane(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True)
        self.assertEqual(["w6:pC"], [item["pane"] for item in result["acted"]])
        self.assertEqual(6, len(result["planned"]))
        self.assertEqual(5, len(result["skipped"]))

    def test_dry_run_reports_a_refusal_reason_for_every_skipped_pane(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        skipped = dict((item["pane"], item["reason"]) for item in rollover.run()["skipped"])
        self.assertTrue(skipped["w6:pY"].startswith(rollover.REASON_OWN_PANE))
        self.assertTrue(skipped["w6:pP"].startswith(rollover.REASON_BOSS_TAB))
        self.assertTrue(skipped["w7:p1"].startswith(rollover.REASON_BOSS_TAB))
        self.assertEqual(rollover.REASON_NO_SESSION, skipped["w9:pB"])
        self.assertEqual(rollover.REASON_NO_MARKER, skipped["w6:pR"])

    def test_a_boss_pane_viewport_is_never_even_read(self):
        runner = self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        rollover.run(dry_run=True)
        reads = [cmd for cmd in runner.commands if "agent read" in cmd]
        self.assertEqual([], [cmd for cmd in reads if "w7:p1" in cmd or "w6:pP" in cmd])
        self.assertEqual([], [cmd for cmd in reads if "w6:pY" in cmd])

    def test_only_restricts_the_fleet(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True, only=["w6:pR"])
        self.assertEqual(["w6:pR"], [item["pane"] for item in result["planned"]])
        self.assertEqual([], result["acted"])

    def test_only_still_refuses_an_unsafe_pane(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True, only=["w6:pY", "w7:p1"])
        self.assertEqual([], result["acted"])
        self.assertEqual(2, len(result["skipped"]))

    def test_only_that_matches_nothing_says_so_instead_of_nothing_to_do(self):
        """An empty plan renders as "No parked claude session needs a
        rollover", which is the opposite of what a typo'd pane id means."""
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True, only=["w9:pZZ"])
        self.assertIn("no pane matched", result["error"])
        self.assertIn("w9:pZZ", result["error"])
        self.assertEqual([], result["planned"])
        self.assertEqual([], result["acted"])
        self.assertEqual([], result["skipped"])

    def test_only_that_matches_nothing_names_every_filter_it_was_given(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        error = rollover.run(dry_run=True, only=["w9:pZZ", "w9:pQQ"])["error"]
        self.assertIn("w9:pZZ", error)
        self.assertIn("w9:pQQ", error)

    def test_one_good_id_does_not_swallow_a_typo_beside_it(self):
        """Regression. `run` only set the error when NO pane matched, so
        `--only w6:pC,w9:pZZ` rolled over w6:pC and said nothing whatever about
        w9:pZZ — the operator saw a successful run and no hint that half their
        filter was a typo. Every id that matched nothing is now named."""
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True, only=["w6:pC", "w9:pZZ"])
        self.assertIn("no pane matched", result["error"])
        self.assertIn("w9:pZZ", result["error"])
        # ...and only the id that missed, not the one that matched.
        self.assertNotIn("w6:pC", result["error"])
        # The pane that DID match is still planned: the message is a warning,
        # not a refusal to do the work that was asked for unambiguously.
        self.assertEqual(["w6:pC"], [item["pane"] for item in result["planned"]])

    def test_a_filter_that_matches_nothing_never_masks_a_herdr_failure(self):
        self.use_runner(default=(127, "", "command not found: herdr"))
        self.use_env("w6:pY")
        error = rollover.run(dry_run=True, only=["w9:pZZ"])["error"]
        self.assertIn("agent list", error)
        self.assertNotIn("no pane matched", error)

    def test_a_filter_that_does_match_reports_no_error(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        self.assertEqual("", rollover.run(dry_run=True, only=["w6:pC"])["error"])

    def test_defaults_to_dry_run(self):
        runner = self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        result = rollover.run()
        self.assertTrue(result["dry_run"])
        self.assertEqual([], runner.mutating())

    def test_without_herdr_pane_id_nothing_is_assumed_to_be_ours(self):
        self.use_runner(FLEET_RULES)
        self.use_env(None)
        panes = [item["pane"] for item in rollover.run(dry_run=True)["acted"]]
        self.assertIn("w6:pC", panes)

    def test_a_herdr_failure_is_reported_not_swallowed(self):
        self.use_runner(default=(127, "", "command not found: herdr"))
        self.use_env("w6:pY")
        result = rollover.run(dry_run=True)
        self.assertIn("agent list", result["error"])
        self.assertEqual([], result["planned"])
        self.assertEqual([], result["acted"])

    def test_viewport_text_never_reaches_the_result(self):
        self.use_runner(FLEET_RULES)
        self.use_env("w6:pY")
        blob = json.dumps(rollover.run(dry_run=True))
        self.assertNotIn("continuing automatically", blob)
        self.assertNotIn("OMC#4.10.1", blob)


if __name__ == "__main__":
    unittest.main()
