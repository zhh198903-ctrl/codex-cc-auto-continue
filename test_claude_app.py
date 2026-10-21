"""Claude EXE (desktop app) driver: transcript reading, identity, and the
shared watcher's decisions on top of it.

Error texts are the real ones from desktop transcripts on this machine, split
so this file cannot look like a stuck session on a watched screen.
"""
import json
import os
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import claude_app as ca
from codex_watcher import CodexWatcher

APOS = "'"
LIMIT_3AM = "You" + APOS + "ve hit your session " + "limit · resets 3am (Asia/Shanghai)"
LIMIT_WEEKLY = "You" + APOS + "ve hit your weekly " + "limit · resets Oct 2, 4pm (Asia/Shanghai)"
LIMIT_MIN = "You" + APOS + "ve hit your session " + "limit · resets 2:20am (Asia/Shanghai)"
MODEL_LIMIT = "You" + APOS + "ve reached your Fable " + "limit. Run /usage-credits to continue or switch models with /model."
NET_RESET = "API " + "Error: Connection dropped (ECONN" + "RESET)"
NET_SLOW = "API " + "Error: No response from API (waited 4m, then 10m on the retry)."
NET_CUT = "API " + "Error: The response stopped arriving. The response above may be incomplete."
SAFEGUARD = "API " + "Error: Opus 5.5" + APOS + "s safeguards " + "flagged this session (https://www.anthropic.com/legal/aup)."
NOW = datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc)      # 12:00 in Shanghai


def rec(kind, **kw):
    base = {"type": kind, "uuid": kw.pop("uuid", kind + str(time.time_ns())), "sessionId": "s",
            "entrypoint": "claude-desktop", "cwd": r"D:\work"}
    base.update(kw)
    return base


def prompt(text, uuid):
    return rec("user", uuid=uuid, message={"role": "user", "content": text})


def reply(text, uuid, stop="end_turn", error=False):
    r = rec("assistant", uuid=uuid, message={"role": "assistant", "stop_reason": stop,
                                             "content": [{"type": "text", "text": text}]})
    if error:
        r["isApiErrorMessage"] = True
    return r


def title(text):
    return {"type": "custom-title", "customTitle": text, "sessionId": "s"}


class Tmp:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name) / "projects"
        (self.root / "D--work").mkdir(parents=True)

    def write(self, name, records, entry="claude-desktop"):
        path = self.root / "D--work" / (name + ".jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                r = dict(r)
                if "entrypoint" in r:
                    r["entrypoint"] = entry
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        return path


class Parsing(unittest.TestCase):
    def test_reset_times(self):
        self.assertEqual(ca.limit_reset(LIMIT_3AM, NOW), datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(ca.limit_reset(LIMIT_MIN, NOW), datetime(2026, 10, 20, 18, 20, tzinfo=timezone.utc))
        later = datetime(2026, 9, 30, 4, 0, tzinfo=timezone.utc)
        self.assertEqual(ca.limit_reset(LIMIT_WEEKLY, later), datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.assertIsNone(ca.limit_reset(MODEL_LIMIT, NOW))

    def test_error_kinds(self):
        for text, kind in ((LIMIT_3AM, "quota"), (LIMIT_WEEKLY, "quota"), (MODEL_LIMIT, "model_limit"),
                           (NET_RESET, "network"), (NET_SLOW, "network"), (NET_CUT, "network"),
                           (SAFEGUARD, "blocked"), ("All done, tests pass.", "")):
            self.assertEqual(ca.classify_error(text), kind, text)

    def test_desktop_executable_only(self):
        self.assertTrue(ca.is_desktop_executable(
            r"C:\Program Files\WindowsApps\Claude_2.42304.2.0_x64__pzs8sxrjxfjjc\app\claude.exe"))
        self.assertTrue(ca.is_desktop_executable(r"C:\Users\u\AppData\Local\AnthropicClaude\app-1.2\claude.exe"))
        self.assertFalse(ca.is_desktop_executable(r"C:\Users\u\.local\bin\claude.exe"))   # the CLI
        self.assertFalse(ca.is_desktop_executable(r"C:\Program Files\WindowsApps\OpenAI.Codex_1\app\Codex.exe"))


class Transcripts(unittest.TestCase):
    def setUp(self):
        self.tmp = Tmp()
        self.addCleanup(self.tmp.dir.cleanup)
        self.index = ca.TranscriptIndex(self.tmp.root)

    def test_state_after_last_prompt(self):
        path = self.tmp.write("a", [title("Fix build"), prompt("go", "p1"), reply(NET_RESET, "e1", error=True),
                                    prompt("continue", "p2"), reply("done", "a2")])
        state = ca.read_transcript(path)
        self.assertEqual((state.title, state.last_prompt_uuid, state.error_uuid, state.completion_uuid),
                         ("Fix build", "p2", "", "a2"))

    def test_error_is_the_latest_word(self):
        path = self.tmp.write("a", [title("Fix build"), prompt("go", "p1"), reply("half", "a1", stop="tool_use"),
                                    rec("user", uuid="t1", message={"content": [{"type": "tool_result", "content": "x"}]}),
                                    reply(LIMIT_3AM, "e1", error=True)])
        state = ca.read_transcript(path)
        self.assertEqual((state.last_prompt_uuid, state.error_uuid, state.completion_uuid), ("p1", "e1", ""))
        self.assertIn("resets 3am", state.error_text)

    def test_sidechains_and_old_errors_are_ignored(self):
        # A subagent's error lands after the main turn ended: it is not the
        # session's state, so it must not read as "stuck on an error".
        path = self.tmp.write("a", [title("T"), prompt("go", "p1"), reply(NET_RESET, "e0", error=True),
                                    prompt("again", "p2"), reply("ok", "a2"),
                                    dict(reply(NET_CUT, "side", error=True), isSidechain=True)])
        state = ca.read_transcript(path)
        self.assertEqual((state.error_uuid, state.completion_uuid), ("", "a2"))

    def test_identity_needs_a_unique_desktop_title(self):
        self.tmp.write("one", [title("Shared"), prompt("x", "p")])
        self.tmp.write("cli", [title("Solo"), prompt("x", "p")], entry="cli")
        self.assertEqual(len(self.index.find("Shared")), 1)
        self.assertEqual(self.index.find("Solo"), [])              # a CLI transcript is not a desktop session
        self.tmp.write("two", [title("Shared"), prompt("y", "q")])
        self.assertEqual(len(self.index.find("Shared")), 2)         # same title: ambiguous

    def test_title_beyond_the_tail_is_found(self):
        big = [title("Long one"), prompt("x", "p")] + [rec("attachment", attachment={"t": "z" * 4000}) for _ in range(200)]
        path = self.tmp.write("long", big)
        self.assertEqual(ca.read_transcript(path).title, "Long one")


class Node:
    """Just enough of a uiautomation control for read_text."""

    def __init__(self, kind, name="", cls="", kids=(), value=None):
        self.ControlTypeName, self.Name, self.ClassName = kind, name, cls
        self.kids, self.parent, self.value = list(kids), None, value
        for kid in self.kids:
            kid.parent = self

    def GetChildren(self):
        return list(self.kids)

    def GetParentControl(self):
        return self.parent

    def GetValuePattern(self):
        return types.SimpleNamespace(Value=self.value)


def page(doc_names, composers=1, toolbar=True):
    """A Claude window: documents named `doc_names`, the last holding the
    prompt box(es) and, with `toolbar`, the Code session's buttons."""
    buttons = [Node("ButtonControl", n) for n in
               (["添加", "模型：Opus 5.5 超高", "用量：12%", "跳过权限确认"] if toolbar else ["添加"])]
    boxes = [Node("EditControl", "", "ProseMirror", value="​") for _ in range(composers)]
    region = Node("GroupControl", kids=boxes + buttons)
    docs = [Node("DocumentControl", n) for n in doc_names[:-1]]
    docs.append(Node("DocumentControl", doc_names[-1], kids=[Node("GroupControl", kids=[region])]))
    return ca.Window(1, Node("PaneControl", "Claude", kids=docs))


class ReadWindow(unittest.TestCase):
    # A session opened in its own window titles its page with the bare
    # session title, without " - Claude Code" (Control session, 2026-10-21,
    # app 2.42304): it was read as "not a Code page" and left unwatched.
    def setUp(self):
        self.tmp = Tmp()
        self.addCleanup(self.tmp.dir.cleanup)
        self.index = ca.TranscriptIndex(self.tmp.root)
        self.tmp.write("w1", [title("Watch remote test"), prompt("x", "p")])

    def test_main_window_page(self):
        v = ca.read_text(page(["", "", "Watch remote test - Claude Code"]), self.index)
        self.assertEqual((v.title, v.identity, v.hold, v.panes), ("Watch remote test", "w1", "", 1))

    def test_popout_page_with_bare_title(self):
        w = page(["", "Watch remote test"])
        v = ca.read_text(w, self.index)
        self.assertEqual((v.title, v.identity, v.hold, v.readable, w.Name),
                         ("Watch remote test", "w1", "", True, "Watch remote test"))

    def test_bare_name_without_a_session_is_not_a_page(self):
        self.assertIsNone(ca.read_text(page(["", "Some chat"]), self.index))

    def test_bare_name_needs_the_code_toolbar(self):
        self.assertIsNone(ca.read_text(page(["Watch remote test"], toolbar=False), self.index))

    def test_bare_name_needs_one_prompt_box(self):
        self.assertIsNone(ca.read_text(page(["Watch remote test"], composers=2), self.index))

    def test_two_session_names_on_one_page_are_ambiguous(self):
        self.tmp.write("w2", [title("Other session"), prompt("x", "p")])
        self.assertIsNone(ca.read_text(page(["Other session", "Watch remote test"]), self.index))

    def test_same_title_twice_is_still_left_alone(self):
        self.tmp.write("w3", [title("Watch remote test"), prompt("y", "q")])
        v = ca.read_text(page(["Watch remote test"]), self.index)
        self.assertEqual((v.hold, v.identity), ("same_title", ""))


def view_for(state, running=False, draft="", hold="", identity=None):
    v = ca.View(title=state.title if state else "T", panes=1, composer=object(), readable=True,
                draft_text=draft, running=running, model="Opus", hold=hold)
    v.transcript = state
    v.identity = identity if identity is not None else (state.session_id if state else "")
    return v


class FakeDriver(types.SimpleNamespace):
    """The real classification (inspect_screen) over scripted views."""

    def __init__(self):
        super().__init__(auto=types.SimpleNamespace(InitializeUIAutomationInCurrentThread=lambda: None))
        self.views = {}
        self.sent = []
        self.result = "sent"

    def terminal_handles(self):
        return list(self.views)

    def window_from_handle(self, hwnd):
        return types.SimpleNamespace(hwnd=hwnd, Name="Claude", expected_identity="")

    def window_exists(self, hwnd):
        return hwnd in self.views

    def read_text(self, window):
        v = self.views.get(window.hwnd)
        if v is not None:
            window.Name = v.title
        return v

    inspect_screen = staticmethod(ca.inspect_screen)
    session_key = staticmethod(ca.session_key)
    prepare = staticmethod(ca.prepare)
    apply_session_options = staticmethod(ca.apply_session_options)

    def send_prompt(self, window, text, dry_run=False):
        self.sent.append((window.hwnd, window.expected_identity, text, dry_run))
        return ca.SendOutcome(self.result, "test")

    def poll_submission(self, *a, **k):
        return ca.SendOutcome("held")


def state(sid="s1", title="T", error="", error_uuid="", completion="c0", prompt_uuid="p0"):
    return ca.TranscriptState(session_id=sid, title=title, desktop=True, last_prompt_uuid=prompt_uuid,
                              error_text=error, error_uuid=error_uuid, completion_uuid=completion)


class Watcher(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.w = CodexWatcher(self.driver, "claude_app", "Claude EXE")
        self.logs = []
        self.w.log.connect(lambda level, msg: self.logs.append((level, msg)))
        self.clock = [NOW]
        self.w.now = lambda: self.clock[0]
        self.w.configure({"poll": 2, "buffer": 60, "retry": 30, "max_retries": 3})
        self.w.running = True

    def tick(self, secs=0):
        self.clock[0] += timedelta(seconds=secs)
        self.w.tick()

    def arm(self, sid, text, loops=1):
        cfg = dict(self.w.config)
        cfg["sessions"] = {sid: {"after_finish": text, "loops": loops}}
        self.w.configure(cfg)
        self.w.running = True

    def test_after_finish_once_per_completed_turn(self):
        self.arm("s1", "next task")
        self.driver.views[1] = view_for(state(completion="c0"), running=True)
        self.tick()
        self.tick(10)
        self.assertEqual(self.driver.sent, [])                       # running: nothing typed
        self.driver.views[1] = view_for(state(completion="c1"))
        self.tick(2)
        self.tick(6)
        self.assertEqual([s[2] for s in self.driver.sent], ["next task"])
        self.assertEqual(self.driver.sent[0][1], "s1")              # aimed at the verified session
        for _ in range(5):
            self.tick(10)
        self.assertEqual(len(self.driver.sent), 1)                   # loops spent: not repeated
        self.assertTrue(any("Claude EXE #1" in m for _, m in self.logs))

    def test_draft_and_unidentified_sessions_get_nothing(self):
        self.arm("s1", "next task")
        self.driver.views[1] = view_for(state(completion="c0"), running=True)
        self.tick()
        self.driver.views[1] = view_for(state(completion="c1"), draft="half a thought")
        self.tick(10)
        self.tick(10)
        self.assertEqual(self.driver.sent, [])
        self.driver.views[1] = view_for(state(completion="c2"), hold="same_title", identity="")
        self.tick(10)
        self.tick(10)
        self.assertEqual(self.driver.sent, [])
        self.assertTrue(any("same title" in m for _, m in self.logs))

    def test_usage_limit_waits_for_reset_plus_buffer(self):
        self.driver.views[1] = view_for(state(error=LIMIT_3AM, error_uuid="e1"))
        self.tick()
        self.assertEqual(self.w.states[1].status, "pending")
        self.clock[0] = datetime(2026, 10, 20, 19, 0, 30, tzinfo=timezone.utc)
        self.tick()
        self.assertEqual(self.driver.sent, [])                       # inside the buffer
        self.tick(40)
        self.assertEqual([s[2] for s in self.driver.sent], ["continue"])

    def test_network_error_retries_on_the_interval(self):
        self.driver.views[1] = view_for(state(error=NET_RESET, error_uuid="e1"))
        self.tick()
        self.tick(10)
        self.tick(25)
        self.assertEqual([s[2] for s in self.driver.sent], ["continue", "continue"])

    def test_blocks_and_model_limits_are_left_alone(self):
        for err in (SAFEGUARD, MODEL_LIMIT):
            self.driver.views[1] = view_for(state(error=err, error_uuid="e-" + err[:5]))
            self.tick(60)
            self.tick(60)
            self.assertEqual(self.driver.sent, [])
            self.assertEqual(self.w.states[1].status, "attention")

    def test_two_windows_on_one_session_act_once(self):
        # Unlimited runs and a retry path have no shared budget to hide behind:
        # only the one-window-per-session rule stops a double send here.
        self.arm("s1", "next task", loops=-1)
        for hwnd in (1, 2):
            self.driver.views[hwnd] = view_for(state(completion="c0"), running=True)
        self.tick()
        for hwnd in (1, 2):
            self.driver.views[hwnd] = view_for(state(completion="c1"))
        self.tick(2)
        self.tick(6)
        self.assertEqual(len(self.driver.sent), 1)
        self.driver.sent.clear()
        for hwnd in (1, 2):
            self.driver.views[hwnd] = view_for(state(error=NET_RESET, error_uuid="e9", completion="c1"))
        self.tick(40)
        self.assertEqual(len(self.driver.sent), 1)

    def test_model_or_effort_overrides_are_not_attempted(self):
        self.assertTrue(ca.apply_session_options(None, "", ""))
        self.assertFalse(ca.apply_session_options(None, "opus", ""))


if __name__ == "__main__":
    unittest.main()
