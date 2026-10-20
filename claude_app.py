"""Driver for Claude Code sessions in the Claude desktop app ("Claude EXE").

It follows the same driver protocol as codex_app / codex_cli, so the shared
CodexWatcher runs it: terminal_handles, window_from_handle, window_exists,
read_text, inspect_screen, session_key, prepare, send_prompt.

What is read where:
  * the window, through UI Automation: which session is showing (the page is
    titled "<session title> - Claude Code"), whether a turn is running (the
    Stop button next to the prompt box), whether the prompt box holds a
    draft, and how many session panes are open;
  * that session's own transcript (~/.claude/projects/*/<id>.jsonl): which
    session the title belongs to, the latest API error (a usage limit with
    its reset time, a network error, a safeguard block), and when the last
    turn finished. Claude Code marks those errors itself, so nothing here
    guesses from rendered text.

Input is never simulated with keys. The prompt is written into the
ProseMirror prompt box through ValuePattern and the Send button is invoked,
the way the Codex desktop driver does it, and only when the window shows the
verified session, in one pane, with an empty prompt box and no running turn.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

import pytz
import uiautomation as auto

from codex_cli import Screen

DOC_SUFFIX = " - Claude Code"
STOP_NAMES = {"停止", "Stop", "Stop response", "Stop generating"}
SEND_NAMES = {"发送", "发送消息", "Send", "Send message", "Submit", "提交"}
MODEL_PREFIXES = ("模型：", "模型:", "Model:", "Model：")
_BLANK = re.compile(r"[\s​‌‍﻿ ]+")

# Transcript classification. Every pattern runs only on records Claude Code
# itself flagged as API errors (isApiErrorMessage), never on conversation text,
# so a session that merely discusses an error is not mistaken for one stuck on
# it. Written with \s+ so this source cannot match itself on a watched screen.
_LIMIT = re.compile(r"hit\s+your\s+(?:[\w-]+\s+){0,3}limit\b", re.I)
_MODEL_LIMIT = re.compile(r"reached\s+your\s+[\w .\-]{1,40}?\s+limit\b", re.I)
_SAFEGUARD = re.compile(r"safeguards\s+flagged", re.I)
_AUTH = re.compile(r"/login\b|OAuth|authentication|\b401\b|invalid\s+api\s+key", re.I)
_API_ERROR = re.compile(r"^\s*API\s+Error\b", re.I)
_RESET = re.compile(
    r"\bresets\s+(?:(?P<mon>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),?\s+)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b\s*(?:\((?P<tz>[^)]+)\))?", re.I)
TAIL_BYTES = 512 * 1024
INDEX_MAX_AGE_DAYS = 21

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.restype = wintypes.HANDLE


# ---------------------------------------------------------------- windows

def executable(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def is_desktop_executable(path):
    """The Claude desktop app, not the Claude Code CLI (also claude.exe)."""
    value = str(path).replace("/", "\\").casefold()
    return PureWindowsPath(value).name == "claude.exe" and (
        "\\windowsapps\\claude_" in value or "\\anthropicclaude\\" in value
        or "\\programs\\claude\\" in value)


def terminal_handles():
    """Visible Claude desktop windows (same name as the other drivers)."""
    handles = []
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, name, 256)
        if (name.value.startswith("Chrome_WidgetWin") and user32.IsWindowVisible(hwnd)
                and is_desktop_executable(executable(hwnd))):
            handles.append(int(hwnd))
        return True

    if not user32.EnumWindows(callback(visit), 0):
        raise OSError("Claude desktop window enumeration failed")
    return handles


def window_exists(hwnd):
    return bool(user32.IsWindow(wintypes.HWND(hwnd)))


class Window:
    """One Claude desktop window. Name is the session it is showing."""

    def __init__(self, hwnd, control):
        self.hwnd = int(hwnd)
        self.NativeWindowHandle = self.hwnd
        self.control = control
        self.Name = "Claude"
        self.expected_identity = ""


def window_from_handle(hwnd):
    if not window_exists(hwnd):
        return None
    try:
        return Window(hwnd, auto.ControlFromHandle(int(hwnd)))
    except Exception:
        return None


# ---------------------------------------------------------------- transcripts

def projects_root():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def _records_from_tail(path, max_bytes=TAIL_BYTES):
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]          # the first line was cut in half
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def _text_of(record):
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _is_prompt(record):
    """A user record that is a typed prompt, not a tool result or meta note."""
    if record.get("type") != "user" or record.get("isSidechain") or record.get("isMeta"):
        return False
    msg = record.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(isinstance(p, dict) and p.get("type") == "text" for p in content) and not any(
            isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
    return False


@dataclass
class TranscriptState:
    session_id: str = ""
    title: str = ""
    cwd: str = ""
    desktop: bool = False
    last_prompt_uuid: str = ""
    last_prompt_text: str = ""
    error_uuid: str = ""
    error_text: str = ""
    completion_uuid: str = ""


def is_desktop_transcript(path):
    """True if Claude Code wrote this transcript for the desktop app."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 80:
                    return False
                compact = line.replace(" ", "")
                if '"entrypoint":"' in compact:
                    return '"entrypoint":"claude-desktop"' in compact
    except OSError:
        return False
    return False


def _last_title(path):
    """The latest custom-title in the whole file; only used when the tail had none."""
    title = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"custom-title"' in line:
                    try:
                        title = str(json.loads(line).get("customTitle") or title)
                    except ValueError:
                        continue
    except OSError:
        pass
    return title


def read_transcript(path):
    state = TranscriptState(session_id=Path(path).stem, desktop=True)
    records = _records_from_tail(path)
    seen_prompt = False
    for record in reversed(records):
        kind = record.get("type")
        if not state.title and kind == "custom-title" and record.get("customTitle"):
            state.title = str(record["customTitle"])
        if not state.cwd and record.get("cwd"):
            state.cwd = str(record["cwd"])
        if record.get("isSidechain"):
            continue
        if kind == "assistant" and not seen_prompt:
            if record.get("isApiErrorMessage") and not state.error_uuid and not state.completion_uuid:
                state.error_uuid = str(record.get("uuid", ""))
                state.error_text = _text_of(record)
            msg = record.get("message") if isinstance(record.get("message"), dict) else {}
            if (not state.completion_uuid and not record.get("isApiErrorMessage")
                    and msg.get("stop_reason") == "end_turn"):
                state.completion_uuid = str(record.get("uuid", ""))
        if not seen_prompt and _is_prompt(record):
            seen_prompt = True
            state.last_prompt_uuid = str(record.get("uuid", ""))
            state.last_prompt_text = _text_of(record).strip()
        if state.title and state.cwd and seen_prompt:
            break
    if not state.title:
        state.title = _last_title(path)
    return state


class TranscriptIndex:
    """Maps a visible session title to its transcript, cached by mtime/size."""

    def __init__(self, root=None):
        self.root = Path(root) if root else None
        self._cache = {}
        self._desktop = {}        # path -> bool; a transcript's origin never changes

    def _files(self):
        root = self.root or projects_root()
        cutoff = time.time() - INDEX_MAX_AGE_DAYS * 86400
        try:
            for project in root.iterdir():
                if not project.is_dir():
                    continue
                for path in project.glob("*.jsonl"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_mtime < cutoff:
                        continue
                    desktop = self._desktop.get(path)
                    if desktop is None:
                        desktop = self._desktop[path] = is_desktop_transcript(path)
                    if desktop:
                        yield path, st
        except OSError:
            return

    def state(self, path):
        try:
            st = path.stat()
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(path)
        if cached and cached[0] == key:
            return cached[1]
        state = read_transcript(path)
        self._cache[path] = (key, state)
        return state

    def find(self, title, cwd=""):
        """Desktop transcripts currently titled `title` (optionally in `cwd`)."""
        out = []
        for path, _ in self._files():
            state = self.state(path)
            if state and state.title == title and (
                    not cwd or _same_dir(state.cwd, cwd)):
                out.append((path, state))
        return out


def _same_dir(a, b):
    return os.path.normcase(os.path.normpath(a or "")) == os.path.normcase(os.path.normpath(b or ""))


INDEX = TranscriptIndex()


# ---------------------------------------------------------------- reading

def limit_reset(text, now):
    """UTC reset time named in a usage-limit message, or None."""
    m = _RESET.search(text or "")
    if not m:
        return None
    hour, minute = int(m.group("h")), int(m.group("m") or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    hour24 = hour % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    try:
        tz = pytz.timezone((m.group("tz") or "").strip())
    except pytz.UnknownTimeZoneError:
        tz = None
    local_now = now.astimezone(tz) if tz else now.astimezone()
    if m.group("mon"):
        try:
            month = datetime.strptime(m.group("mon")[:3], "%b").month
            day = int(m.group("day"))
            naive = datetime(local_now.year, month, day, hour24, minute)
        except ValueError:
            return None
        if naive < local_now.replace(tzinfo=None) - timedelta(days=180):
            naive = naive.replace(year=naive.year + 1)
    else:
        naive = local_now.replace(tzinfo=None, hour=hour24, minute=minute, second=0, microsecond=0)
        if naive <= local_now.replace(tzinfo=None):
            naive += timedelta(days=1)
    aware = tz.localize(naive, is_dst=True) if tz else naive.replace(tzinfo=local_now.tzinfo)
    return aware.astimezone(timezone.utc)


def classify_error(text):
    """'quota' / 'model_limit' / 'blocked' / 'auth' / 'network' / ''."""
    text = text or ""
    if _SAFEGUARD.search(text):
        return "blocked"
    if _MODEL_LIMIT.search(text):
        return "model_limit"
    if _LIMIT.search(text):
        return "quota"
    if _AUTH.search(text):
        return "auth"
    if _API_ERROR.search(text):
        return "network"
    return ""


@dataclass
class View:
    title: str = ""
    panes: int = 0
    composer: object = None
    draft_text: str = ""
    readable: bool = False
    running: bool = False
    send_button: object = None
    model: str = ""
    cwd_hint: str = ""
    identity: str = ""
    transcript: TranscriptState | None = None
    candidates: int = 0
    hold: str = ""
    button_names: list = field(default_factory=list)
    screen: Screen = field(default_factory=Screen)

    def __bool__(self):
        return bool(self.title)


def _walk(control, limit=20000):
    nodes = []
    stack = [(control, 0)]
    while stack and len(nodes) < limit:
        node, depth = stack.pop()
        try:
            kids = node.GetChildren()
        except Exception:
            continue
        for kid in reversed(kids):
            nodes.append(kid)
            if depth < 70:
                stack.append((kid, depth + 1))
    return nodes


def _composer_region(composer):
    """The smallest ancestor holding the prompt box's own buttons."""
    node = composer
    for _ in range(8):
        try:
            node = node.GetParentControl()
        except Exception:
            return None
        if node is None:
            return None
        buttons = []
        for kid in _walk(node, 600):
            try:
                if kid.ControlTypeName == "ButtonControl":
                    buttons.append(kid)
            except Exception:
                continue
        if any((b.Name or "").startswith(MODEL_PREFIXES) for b in buttons):
            return node, buttons
    return None


def _draft_of(composer):
    try:
        pattern = composer.GetValuePattern()
        value = pattern.Value or ""
    except Exception:
        return None
    return _BLANK.sub("", value)


def read_text(window, index=None):
    """Read one window: identity, state and the matching transcript."""
    index = index or INDEX
    view = View()
    try:
        window.control.GetChildren()        # wakes Chromium's UIA tree
        nodes = _walk(window.control)
    except Exception:
        return None
    composers = []
    for node in nodes:
        try:
            kind = node.ControlTypeName
            name = node.Name or ""
        except Exception:
            continue
        if kind == "DocumentControl" and name.endswith(DOC_SUFFIX):
            view.title = name[: -len(DOC_SUFFIX)].strip()
        elif kind == "EditControl" and "ProseMirror" in (node.ClassName or ""):
            composers.append(node)
    if not view.title:
        return None                          # not a Code session page
    window.Name = view.title
    view.panes = len(composers)
    if view.panes != 1:
        view.hold = "panes" if view.panes else "no_prompt_box"
        return _finish(view, index)
    view.composer = composers[0]
    draft = _draft_of(view.composer)
    view.readable = draft is not None
    view.draft_text = draft or ""
    region = _composer_region(view.composer)
    if region is None:
        view.hold = "no_toolbar"
        return _finish(view, index)
    _node, buttons = region
    view.button_names = []
    for button in buttons:
        name = button.Name or ""
        view.button_names.append(name)
        if name in STOP_NAMES:
            view.running = True
        elif name in SEND_NAMES:
            view.send_button = button
        elif name.startswith(MODEL_PREFIXES):
            view.model = name.split("：", 1)[-1].split(":", 1)[-1].strip()
    return _finish(view, index)


def _finish(view, index):
    matches = index.find(view.title) if view.title else []
    view.candidates = len(matches)
    if len(matches) == 1:
        path, state = matches[0]
        view.transcript = state
        view.identity = state.session_id
    elif not view.hold:
        view.hold = "no_transcript" if not matches else "same_title"
    return view


def inspect_screen(view, now=None):
    now = now or datetime.now(timezone.utc)
    if view is None or not view.title:
        return Screen()
    state = view.transcript
    ready = bool(view.identity and view.readable and not view.hold)
    error_kind = classify_error(state.error_text) if state and state.error_uuid else ""
    reset = limit_reset(state.error_text, now) if error_kind == "quota" else None
    # model_limit / blocked / auth: nothing typed can fix them, and the watcher
    # treats any kind it does not retry as "needs attention".
    if error_kind == "quota" and reset is None:
        error_kind = "quota_retry"
    screen = Screen(identified=True, ready=ready, running=view.running,
                    draft=bool(view.draft_text), blocked=False, model=view.model,
                    error_kind=error_kind, error_id=state.error_uuid if state else "",
                    reset_utc=reset, completion_id=state.completion_uuid if state else "")
    view.screen = screen
    return screen


def session_key(window, view):
    return view.identity if view else ""


def prepare(window, identity):
    window.expected_identity = identity


# ---------------------------------------------------------------- sending

@dataclass
class SendOutcome:
    status: str
    detail: str = ""

    def __bool__(self):
        return self.status == "sent"


def _sendable(window, identity):
    view = read_text(window)
    if view is None:
        return None, "unreadable"
    inspect_screen(view)
    if view.hold:
        return None, view.hold
    if not view.identity or view.identity != identity:
        return None, "changed_session"
    if view.running:
        return None, "running"
    if not view.readable:
        return None, "unreadable_prompt_box"
    if view.draft_text:
        return None, "draft"
    return view, ""


def _wait(predicate, seconds, step=0.25):
    end = time.time() + seconds
    while time.time() < end:
        result = predicate()
        if result:
            return result
        time.sleep(step)
    return predicate()


def send_prompt(window, prompt, dry_run=False):
    if not prompt.strip() or "\n" in prompt or "\r" in prompt:
        return SendOutcome("held", "invalid_text")
    identity = window.expected_identity
    view, why = _sendable(window, identity)
    if view is None:
        return SendOutcome("held", "preflight_" + why)
    if dry_run:
        return SendOutcome("sent", "dry_run")
    user32.GetForegroundWindow.restype = wintypes.HWND
    previous = user32.GetForegroundWindow()
    before_prompt = view.transcript.last_prompt_uuid if view.transcript else ""
    staged = None
    try:
        # ProseMirror turns a ValuePattern write into input only while its own
        # editor has focus. Focus that verified control alone: no window
        # activation by coordinates, no clipboard, no key events.
        view.composer.SetFocus()
        time.sleep(0.05)
        view, why = _sendable(window, identity)
        if view is None:
            return SendOutcome("held", "focused_" + why)
        before_buttons = set(view.button_names)
        pattern = view.composer.GetValuePattern()
        pattern.SetValue(prompt)
        staged = view.composer
        found = _wait(lambda: _staged_send_button(window, identity, prompt), 3)
        if not found:
            after = read_text(window)
            new = [n for n in (after.button_names if after else []) if n not in before_buttons]
            return SendOutcome("held", "no_send_button:" + ",".join(new)[:80])
        found.GetInvokePattern().Invoke()
        staged = None
        if _wait(lambda: _submitted(window, before_prompt, prompt), 15):
            return SendOutcome("sent", "confirmed")
        return SendOutcome("held", "unconfirmed")
    except Exception as error:
        return SendOutcome("held", "send_" + type(error).__name__)
    finally:
        if staged is not None:
            # Our own text must not be left behind as a draft the user then
            # sends by accident, or that blocks every later send.
            try:
                if _BLANK.sub("", staged.GetValuePattern().Value or "") == _BLANK.sub("", prompt):
                    staged.GetValuePattern().SetValue("")
            except Exception:
                pass
        if previous and previous != window.hwnd and user32.GetForegroundWindow() == window.hwnd:
            user32.SetForegroundWindow(wintypes.HWND(previous))


def _staged_send_button(window, identity, prompt):
    view = read_text(window)
    if view is None or view.identity != identity or view.running:
        return None
    if view.draft_text != _BLANK.sub("", prompt):
        return None
    button = view.send_button
    try:
        return button if button is not None and button.IsEnabled else None
    except Exception:
        return None


def _submitted(window, before_prompt, prompt):
    view = read_text(window)
    if view is None or not view.transcript:
        return False
    state = view.transcript
    return bool(state.last_prompt_uuid and state.last_prompt_uuid != before_prompt
                and _BLANK.sub("", state.last_prompt_text) == _BLANK.sub("", prompt))


def poll_submission(window, submission, retry_seconds=30, allow_click=True):
    # send_prompt confirms synchronously; nothing is ever left staged.
    return SendOutcome("held", "no_submission")


def apply_session_options(window, model, effort):
    # Model and effort are not switched in the desktop app by this tool.
    return not model and not effort
