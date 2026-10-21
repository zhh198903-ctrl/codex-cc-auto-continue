"""
GUI for the independent Claude CLI, Codex EXE and Codex CLI watchers.

The header contains global actions. Three tabs switch the settings and
shared session table between monitor types. The activity log stays below.
Shared form construction lives in
watcher_ui; provider configuration and command routing remain independent.

The watching loop runs on a QThread so UIA reads (which can take 100ms+
per terminal) never block the UI. The worker emits a snapshot dict each
tick; the main thread rebuilds the table from that snapshot.

Three user actions per row:
  * Now      — fire `continue` immediately, skipping the timer.
  * Skip     — clear the pending limit for this row (e.g. user already
               handled it manually). Cooldown still applies.
  * Exclude  — never watch this window again (per-session, persisted).

Persisted settings (QSettings → registry on Windows): poll interval,
buffer seconds, dry-run flag, excluded window titles.
"""

from __future__ import annotations

import collections
import html
import os
import re as _re
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

# IMPORTANT: set per-monitor v2 DPI awareness BEFORE any DLL that might set
# its own awareness (uiautomation/UIAutomationCore.dll, Qt). Windows allows
# the awareness to be set exactly once per process — whichever DLL gets there
# first wins, and Qt's later attempt then fails with "access denied" and
# triggers the warning the user sees. We do it ourselves at the earliest
# possible moment so Qt's preferred mode actually sticks.
def _set_dpi_awareness() -> None:
    import ctypes
    # Per-Monitor v2 (Windows 10 1703+). Constant is -4 cast to a context.
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    try:
        ok = ctypes.windll.user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
        if ok:
            return
    except (AttributeError, OSError):
        pass
    # Fallback for older Windows: SetProcessDpiAwareness (Win 8.1+).
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR
    except (AttributeError, OSError):
        pass

_set_dpi_awareness()

import pytz
from PyQt6.QtCore import (
    QEvent, QObject, QSettings, QThread, QTimer, QUrl, Qt, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QStyle, QSystemTrayIcon, QToolButton,
    QTextBrowser,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from auto_continue import (
    APP_VERSION, MAX_POST_MATCH_TAIL, NETWORK_POST_MATCH_TAIL,
    PROMPT_POST_MATCH_TAIL, SCAN_TAIL_CHARS, SWITCH_POST_MATCH_TAIL,
    TRIGGER_SPECS, compile_trigger_patterns,
    init_uia_thread, list_tab_titles, next_reset_datetime,
    fable_refusal_distance,
    chooser_signature, composer_has_draft,
    parse_chooser_prompt, parse_econnreset_stuck,
    classify_prompt,
    parse_fable_picker, parse_permission_prompt,
    parse_limit_message,
    parse_limit_prompt, parse_oauth_expired, parse_retry_exhausted,
    parse_server_error_stuck, parse_switch_model_prompt, read_terminal_text,
    send_continue, send_keys, send_text_lines, session_running,
    summarize_keyspec, keyspec_problem,
    typing_hold_reason,
)
import updater
import local_api
from pathlib import Path
from cli_routing import claude_windows as find_terminal_windows
from cli_routing import claude_window as terminal_window_from_handle
from codex_gui import ACTIVITY_LABELS, CodexGuiMixin
from watcher_ui import AppSettingsDialog, timing_spin


# Effort levels offered in the per-window dropdown, matching Claude Code's
# own `/effort` slider (low → … → ultracode). The first sentinel means
# "don't send /effort, just continue as-is". `ultracode` (xhigh + workflows)
# is newer — sessions on Claude Code older than 4.7 won't have it, in which
# case `/effort ultracode` is simply ignored by that session.
EFFORT_NONE = ""
EFFORT_LEVELS = [EFFORT_NONE, "low", "medium", "high", "xhigh", "max",
                 "ultracode"]
EFFORT_LABEL = {
    EFFORT_NONE: "(none)",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
    "ultracode": "ultracode",
}

# Model levels offered in the per-window dropdown, matching Claude Code's
# `/model` picker (Default / Sonnet / Haiku). The first sentinel means
# "don't send /model, leave the session on whatever model it's already on".
MODEL_NONE = ""
MODEL_LEVELS = [MODEL_NONE, "default", "opus", "sonnet", "haiku", "fable"]
MODEL_LABEL = {
    MODEL_NONE: "(none)",
    "default": "default",
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "fable": "fable",
}


def title_key(title: str) -> str:
    """Strip the leading WT spinner glyph + whitespace so the per-window
    settings (effort, exclusion) survive title churn while a session is
    actively running."""
    return _re.sub(r"^[^\w]+", "", title or "").strip()


def coerce_after_finish_loops(stored) -> dict:
    """title_key -> runs remaining (0 spent, -1 unlimited), from any input.

    The map round-trips through the settings store as JSON and through a
    queued signal, so it can arrive as anything; a bad entry is dropped
    rather than raised, because this runs on the worker thread and at start-up.
    """
    out = {}
    if isinstance(stored, dict):
        for k, v in stored.items():
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            out[str(k)] = max(-1, n)
    return out


def reconcile_loops(chosen: int, snapshot: int, fired):
    """What the After finish dialog should store when OK is pressed.

    `snapshot` is what the spinner showed when the dialog opened, `chosen` is
    what it shows now, and `fired` is a value the worker wrote WHILE the
    dialog was up (None if it did not fire).

    A modal exec() keeps pumping this thread's queued signals, so a run can
    be spent mid-dialog. Writing the spinner's untouched pre-fire number back
    would re-arm a prompt that had just been used — the repeat-firing the
    budget exists to prevent. So the worker's number wins, unless the user
    actually moved the spinner, which is an explicit instruction.
    """
    if fired is None or chosen != snapshot:
        return chosen
    return fired


# Shipped defaults, in one place so first run and Reset agree.
#   poll   60s — a UIA read of every window costs 100ms+, and nothing
#                this tool reacts to needs sub-minute latency.
#   buffer 60s — margin past the reset hour; the limit clears a little
#                after the stated minute often enough to matter.
#   retry 600s — a stuck session gets ONE nudge every 10 minutes. The
#                old 30s default poked it ~180 times through a real
#                90-minute outage, which helps nothing and buries the
#                log; 600s is what a real deployment converged on.
DEFAULT_INTERVAL_S = 60
DEFAULT_BUFFER_S = 60
DEFAULT_RETRY_S = 600

# A limit banner that reports a clock time always lifts within the day,
# so a computed reset further out than this is not the future — it is a
# banner we read after its moment had already gone by, wrapped to
# tomorrow by the arithmetic. Six hours clears the 5-hour window with
# room to spare.
STALE_RESET_H = 6

# A chooser gets ONE Enter. Its text stays in the scrollback after it is
# answered, so "the pattern still matches" cannot be the exit condition —
# that mistake once fired eight Enters at a single dialog. The answered
# chooser's signature is remembered instead, and only a DIFFERENT one is
# answered again. This is the backstop for the case where our Enter did not
# take: after this long, the same chooser may be answered once more.
CHOOSER_RETRY_S = 300

# Fast follow-up tick after answering a chooser: questions come in runs.
RETICK_MS = 6000

# "After finish": how long a session must sit idle before its configured
# follow-up prompt is typed. Long enough that a redraw gap or the user
# pausing to read can't masquerade as "the run is over" (one full poll at
# the default interval, plus margin), short enough that the window isn't
# wasted for long. The prompt re-arms only after the session is seen
# RUNNING again, so it fires once per completed run — not once per tick.
AFTER_FINISH_SETTLE_S = 90

# Claude Code's own "you are scrolled up" hint, drawn just above the prompt
# rule while the view is away from the live bottom. Only the last rows are
# searched, so the phrase quoted in a conversation further up does not count.
SCROLLED_AWAY_RE = _re.compile(r"Jump\s+to\s+bottom\b")
SCROLLED_AWAY_TAIL = 1500

# A window scrolled away from its live bottom is skipped (it cannot be read),
# so left scrolled it would be skipped for good. After this long it is sent
# Claude Code's own "jump to bottom" key and is watched again. 0 = never.
DEFAULT_SCROLL_BACK_MIN = 60
SCROLL_TO_BOTTOM_KEYS = "{Ctrl}{End}"


# ===========================================================================
# Watcher (background worker)
# ===========================================================================

# UI-facing window status codes. Kept short because they go in a table cell.
ST_IDLE = "idle"          # Window seen, no limit detected.
ST_PENDING = "pending"    # Limit detected, waiting until reset time.
ST_FIRING = "firing"      # In the middle of sending keys.
ST_SENT = "sent"          # Just sent — short-lived display state.
ST_COOLDOWN = "cooldown"  # Inside post-send cooldown window.
ST_EXCLUDED = "excluded"  # User chose to ignore this window.
ST_RETRY = "retry"        # Network retries exhausted, resending continue.
ST_PROMPT = "prompt"      # Limit picker open, confirming with Enter.


def _wid(hwnd: int) -> str:
    """Four hex digits identifying a window in the log.

    Log lines name a window by its TITLE, which is neither unique nor stable:
    two sessions on the same task (or two on none, both "Claude Code") write
    identical lines, and a session renames itself as it works. That makes a
    question like "was this window poked twice inside the retry interval?"
    unanswerable after the fact -- reading the log, separate windows blur into
    one and one window splits into several. The handle is the identity the
    watcher actually keys on, so a short slice of it restores the link.
    Low digits: the high half of an HWND barely varies within a session.
    """
    return f"{int(hwnd) & 0xFFFF:04x}"


_REDACT_KEEP = _re.compile(r"^(?:/[\w-]+(?:\s+[\w.-]+)?|continue|\(none\)|)$")


def redact_log_line(line: str) -> str:
    """Strip a log line of everything that says WHAT the machine is working on.

    The activity log is safe to keep locally and unsafe to hand over as-is: a
    Claude Code window is named after its conversation, so the titles alone
    disclose the projects and tasks on this machine, and the after-finish
    prompts are the user's own words typed verbatim into the log.

    What survives is what a diagnosis actually needs — the timestamp, the
    level, the sentence, the window id, and literal slash commands, none of
    which describe the work. A title that already carries an id collapses to
    the id, because the id is the better identifier anyway: it is unique and
    it survives a rename.
    """
    # 'some title' #1a2b  ->  #1a2b   (the id alone identifies it)
    out = _re.sub(r"'[^']*'(\s+#[0-9a-f]{4})", lambda m: m.group(1), line)

    # Every other quoted run is free text unless it is a bare command.
    def _one(m):
        inner = m.group(1)
        return m.group(0) if _REDACT_KEEP.match(inner) else "'…'"

    return _re.sub(r"'([^']*)'", _one, out)


def _hwnd_alive(hwnd: int) -> bool:
    """True if the OS still knows this window handle.

    This is what tells "the window closed" apart from "UIA did not hand it to
    us this pass". Only the first justifies throwing away what the watcher
    knows about a window; the second is a transient that
    find_terminal_windows() is documented to produce.
    """
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [ctypes.c_void_p]
        user32.IsWindow.restype = ctypes.c_int
        return bool(user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:
        # Can't tell. A stale row costs a line in the table; dropping live
        # state loses a pending limit or a send cooldown, so the lossy option
        # is not the default.
        return True


def _prompt_ident(p):
    """Which prompt this is, for seen_at: kind plus its sig (or its text)."""
    if not p:
        return None
    return (p.get("kind"), p.get("sig") or p.get("text"))


def _api_row(row: dict) -> dict:
    """A window row as the local API sends it: every datetime as ISO text.

    Any datetime, not a list of known keys: a list is what let the rescan
    reply go out with raw datetimes in it, and what a new field would
    silently fall out of next time.
    """
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in dict(row).items()}


def _discovery_exe():
    """This copy's exe path for the discovery file, or None from source."""
    return updater.current_exe_path() if updater.is_frozen() else None


class _ApiTicket:
    """One external action on its way to the watcher thread.

    It lives through queued -> started -> done, or queued -> cancelled, and
    the step between those two is taken under a lock, so exactly one side
    wins it. That is what lets the HTTP side promise that "busy" means
    nothing was done: it answers busy only after it has cancelled a ticket
    that had not started, and the watcher drops a cancelled ticket unrun.

    Before this, the request timed out into "busy" while the signal it had
    already emitted stayed queued, and the watcher typed the text anyway
    once the pass ended (2026-09-27, found by a companion's test window).
    The caller retried, as busy invites, so the text went in twice, and the
    second Enter landed on a permission prompt the first send had raised,
    approving it.

    `put` keeps the queue.Queue spelling the api_* slots already use.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._done = threading.Event()
        self.state = "queued"
        self.result = None

    def start(self) -> bool:
        with self._lock:
            if self.state != "queued":
                return False
            self.state = "started"
            return True

    def cancel(self) -> bool:
        with self._lock:
            if self.state != "queued":
                return False
            self.state = "cancelled"
            return True

    def put(self, result) -> None:
        with self._lock:
            if self._done.is_set():
                return              # first answer wins
            self.result = result
            self.state = "done"
            self._done.set()

    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)


@dataclass
class _WState:
    """Internal per-window state, kept inside the watcher thread only."""
    hwnd: int
    title: str
    status: str = ST_IDLE
    reset_utc: Optional[datetime] = None
    # The (hour_12, minute, ampm, tz) tuple that produced reset_utc. We
    # compare against this when re-parsing the scrollback so a still-visible
    # message whose wall-clock target has just passed doesn't get "rolled
    # forward" by next_reset_datetime and push out a pending that's about
    # to fire.
    reset_key: Optional[tuple] = None
    last_sent_utc: Optional[datetime] = None
    sent_flash_until: Optional[datetime] = None  # show "sent" for ~5s
    # Key of the last limit message we already fired for (or the user
    # skipped). Prevents the same still-visible message from re-arming a
    # bogus "tomorrow" pending after the cooldown expires. Cleared once a
    # tick sees no limit message at all.
    fired_key: Optional[tuple] = None
    # Network-retry exhaustion bookkeeping. Independent of the rate-limit
    # fields above.
    retry_last_sent_utc: Optional[datetime] = None
    retry_active: bool = False
    # True while a due limit fire is being held back because the
    # session is mid-turn; keeps that news to one log line.
    fire_deferred: bool = False
    # True while the window is enumerated but its text cannot be read,
    # so the log says so once per episode instead of every tick.
    read_blind: bool = False
    # Whether the last readable pass saw a turn streaming. Kept on the
    # state so the snapshot can report it without re-reading a screen.
    running: bool = False
    # What this window is waiting on, for the local API: a dict with a
    # kind and the question, or None. Same rule as `running` -- only a
    # readable pass may change it, so an unreadable one leaves the last
    # known answer standing instead of reporting 'not waiting'.
    prompt: Optional[dict] = None
    prompt_at: Optional[datetime] = None
    # When this window's screen was last read, by a pass or a rescan.
    read_at: Optional[datetime] = None
    # The limit picker ("What do you want to do?") is on screen; its warning
    # has been logged for this episode.
    prompt_active: bool = False
    # One line per hold (see typing_hold_reason), not one per pass.
    hold_logged: bool = False
    # OAuth-expired warn-once flag ('continue' can't fix that state).
    oauth_logged: bool = False
    # Multi-tab bookkeeping: background tabs can't be watched (WT exposes
    # only the active tab's content in the UIA tree). Count shown in the
    # table; warn-once flag for the log.
    tab_count: int = 1
    tabs_warned: bool = False
    af_seen_running: bool = False                 # ran since the last fire
    af_idle_since: Optional[datetime] = None      # idle streak start
    af_spent_logged: bool = False                 # budget-spent said once
    chooser_sig: Optional[str] = None             # last chooser we answered
    chooser_at: Optional[datetime] = None         # when we answered it
    chooser_warned: bool = False                  # said "couldn't" once
    chooser_seen_sig: Optional[str] = None        # chooser that warning is for
    scrolled_away: bool = False                   # view not at the live bottom
    scrolled_since: Optional[datetime] = None     # when it was first seen so


class Watcher(QObject):
    """
    Background worker. Runs `tick()` every `interval` seconds in its own
    thread. Emits a full snapshot list each tick — the GUI just rerenders.
    """

    # snapshot is a list[dict] — see _make_snapshot for the schema.
    snapshot = pyqtSignal(list)
    # (title_key, runs left) after an after-finish prompt spent one. The GUI
    # owns persistence, so the remaining count survives a restart — which is
    # the whole point of counting down rather than up.
    loops_spent = pyqtSignal(str, int)
    # (level, message). level ∈ {info,warn,err,fire,fire-quiet}. "fire-quiet"
    # is a fire in every respect that records what happened — log file, log
    # view, API event — and differs only in not popping a tray balloon. It
    # exists because a long outage resends on a timer, and a balloon every
    # retry interval is noise; demoting the LEVEL instead would have the log
    # under-report how many times this tool typed.
    log = pyqtSignal(str, str)
    running_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        # External actions waiting for this thread: (job, _ApiTicket).
        # Drained between passes (woken by a signal) and between windows
        # inside one, so a caller waits for one window, not a whole pass.
        self._api_pending: collections.deque = collections.deque()
        self._api_draining = False
        # The rows last published (see _publish_snapshot); None until then.
        self.latest_rows: Optional[list] = None
        self._states: dict[int, _WState] = {}
        # Handles the OS still reports as open that a pass did not
        # enumerate; kept so the warning fires on change, not every tick.
        self._ghost_hwnds: set[int] = set()
        self._excluded_titles: set[str] = set()
        # Per-window effort override. Key is the *stable* part of the WT
        # title (leading spinner glyph stripped), value is one of
        # {max,xhigh,high,medium,low,ultracode}. Missing/empty = no override.
        self._effort_overrides: dict[str, str] = {}
        # Per-window model override, same keying. Value ∈ {default,sonnet,
        # haiku}. Missing/empty = leave the session's current model alone.
        self._model_overrides: dict[str, str] = {}

        self._interval = 30
        self._buffer = 20
        self._retry_interval = 30
        self._dry_run = False

        # title_key -> prompt typed when a watched session finishes a turn
        # and sits idle (the main table's "After finish" column). Empty =
        # feature off for that window.
        self._after_finish: dict[str, str] = {}
        # title_key -> times the prompt may be typed before it stops
        # re-arming (missing = 1, 0 = unlimited). Typing the same follow-up
        # after every run is an infinite loop unless the user asked for it.
        self._after_finish_loops: dict[str, int] = {}

        # Auto-answer switches (Advanced). They are NOT equally
        # consequential, so they do not ship the same way.
        #
        # A permission request asks whether to carry out work the session was
        # already told to do; answering it unblocks that work and decides
        # nothing. It ships ON.
        #
        # An ordinary chooser is where a DECISION sits — which design to
        # take, and whether a written plan starts executing, which also puts
        # the session back at the permission level it started with. Making
        # those calls for someone out of the box is the surprising kind of
        # autonomy, so it ships OFF and is theirs to turn on.
        self._auto_choose = False
        self._auto_permission = True
        self._scroll_back_s = DEFAULT_SCROLL_BACK_MIN * 60

        # User overrides for the detection regexes (Advanced → Triggers).
        # Only keys the user actually changed (and that validated) live here;
        # a missing key means "pass None" → the parser uses its built-in
        # default. Written on the worker thread via set_trigger_patterns.
        self._patterns: dict = {}

        # Earliest pending fast-retick, so N windows asking for one still
        # produce ONE timer instead of N (which compounded geometrically).
        self._retick_at: Optional[datetime] = None

        # User commands accumulated between ticks. Each maps hwnd → True.
        self._cmd_fire_now: set[int] = set()
        self._cmd_skip: set[int] = set()

        self._running = False
        # Use a QTimer that lives on this object's owning thread (the worker
        # thread) once moveToThread happens. Started/stopped via slots.
        self._timer: Optional[QTimer] = None

    # ---- thread entry ----------------------------------------------------

    @pyqtSlot()
    def thread_started(self) -> None:
        # Must run before the first find_terminal_windows() so this worker
        # thread builds the UIA client in a live (STA) apartment — otherwise
        # it only ever sees windows that existed when it started.
        init_uia_thread()
        self._timer = QTimer()
        self._timer.setSingleShot(False)
        self._timer.setInterval(self._interval * 1000)
        self._timer.timeout.connect(self._tick_safely)

    # ---- config from main thread ----------------------------------------

    @pyqtSlot(int)
    def set_interval(self, seconds: int) -> None:
        self._interval = max(5, int(seconds))
        if self._timer is not None:
            self._timer.setInterval(self._interval * 1000)

    @pyqtSlot(int)
    def set_buffer(self, seconds: int) -> None:
        self._buffer = max(0, int(seconds))

    @pyqtSlot(int)
    def set_retry_interval(self, seconds: int) -> None:
        self._retry_interval = max(5, int(seconds))

    @pyqtSlot(int)
    def set_scroll_back(self, minutes: int) -> None:
        try:
            self._scroll_back_s = max(0, int(minutes)) * 60
        except (TypeError, ValueError):
            self._scroll_back_s = DEFAULT_SCROLL_BACK_MIN * 60

    @pyqtSlot(bool)
    def set_dry_run(self, on: bool) -> None:
        self._dry_run = bool(on)

    @pyqtSlot(list)
    def set_excluded(self, titles: list) -> None:
        # Normalized via title_key so exclusion survives the WT spinner
        # glyph and other leading-junk title churn (same keying as the
        # effort/model overrides).
        self._excluded_titles = {title_key(str(t)) for t in titles}

    @pyqtSlot(dict)
    def set_effort_overrides(self, overrides: dict) -> None:
        # Filter out empty / "(none)" entries so the worker only stores
        # actionable overrides.
        self._effort_overrides = {
            str(k): str(v) for k, v in overrides.items() if v
        }

    @pyqtSlot(dict)
    def set_model_overrides(self, overrides: dict) -> None:
        self._model_overrides = {
            str(k): str(v) for k, v in overrides.items() if v
        }

    @pyqtSlot(dict)
    def set_after_finish(self, overrides: dict) -> None:
        new = {
            str(k): str(v).strip() for k, v in overrides.items()
            if str(v).strip()
        }
        changed = {k for k in new if new.get(k) != self._after_finish.get(k)}
        self._after_finish = new
        # A new prompt deserves a fresh "no runs left" line if it runs out
        # too; the remaining count itself lives in the loops map.
        for st in self._states.values():
            if title_key(st.title) in changed:
                st.af_spent_logged = False

    @pyqtSlot(dict)
    def set_after_finish_loops(self, overrides: dict) -> None:
        # title_key -> runs REMAINING for that window's after-finish prompt
        # (missing = 1, 0 = spent, negative = unlimited).
        loops = coerce_after_finish_loops(overrides)
        changed = {k for k in loops
                   if loops.get(k) != self._after_finish_loops.get(k)}
        changed |= {k for k in self._after_finish_loops if k not in loops}
        self._after_finish_loops = loops
        for st in self._states.values():
            if title_key(st.title) in changed:
                st.af_spent_logged = False

    @pyqtSlot(bool, bool)
    def set_auto_answer(self, choosers: bool, permissions: bool) -> None:
        self._auto_choose = bool(choosers)
        self._auto_permission = bool(permissions)

    @pyqtSlot(dict)
    def set_trigger_patterns(self, overrides: dict) -> None:
        """Install user regex overrides for the detection patterns. Invalid
        entries are rejected (the built-in default stays live) and reported —
        a trigger that silently stops matching is exactly the failure this
        feature exists to fix, so failing loud beats failing open."""
        try:
            patterns, errors = compile_trigger_patterns(overrides or {})
        except Exception as e:                    # never kill the worker
            self.log.emit("err", f"trigger patterns not applied: {e}")
            return
        self._patterns = patterns
        for msg in errors:
            self.log.emit("err", msg)
        if patterns:
            self.log.emit(
                "info",
                "custom trigger pattern(s) active: "
                + ", ".join(sorted(patterns)))

    # ---- user actions per row -------------------------------------------

    @pyqtSlot(int)
    def cmd_fire_now(self, hwnd: int) -> None:
        self._cmd_fire_now.add(int(hwnd))
        # Run a tick right away so the user sees the effect without waiting
        # for the next timer fire.
        self._tick_safely()

    @pyqtSlot(int)
    def cmd_skip(self, hwnd: int) -> None:
        self._cmd_skip.add(int(hwnd))
        self._tick_safely()

    @pyqtSlot(int, str)
    def cmd_exclude(self, hwnd: int, title: str) -> None:
        self._excluded_titles.add(title_key(title))
        self._tick_safely()

    @pyqtSlot(str)
    def cmd_unexclude(self, title: str) -> None:
        self._excluded_titles.discard(title_key(title))
        self._tick_safely()

    @pyqtSlot(int)
    def cmd_clear_cooldown(self, hwnd: int) -> None:
        st = self._states.get(int(hwnd))
        if st is not None:
            st.last_sent_utc = None
            st.sent_flash_until = None
            st.status = ST_IDLE
            self.log.emit("info", f"cooldown cleared for {st.title!r}")
        self._tick_safely()

    # ---- start / stop ---------------------------------------------------

    @pyqtSlot()
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.running_changed.emit(True)
        self.log.emit("info",
                      f"watcher started (interval {self._interval}s, "
                      f"buffer {self._buffer}s, "
                      f"{'DRY-RUN' if self._dry_run else 'live'})")
        if self._timer is not None:
            self._timer.start()
        # Run an immediate first tick so the table populates.
        self._tick_safely()

    @pyqtSlot()
    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._timer is not None:
            self._timer.stop()
        self._retick_at = None
        self.running_changed.emit(False)
        self.log.emit("info", "watcher stopped")

    # ---- core tick ------------------------------------------------------

    def _tick_safely(self) -> None:
        # Guard: per-row command slots (fire-now / skip / exclude / …) call
        # this unconditionally — a full detection+send tick must never run
        # while the watcher is Stopped.
        if not self._running:
            return
        try:
            self._tick()
        except Exception as e:
            self.log.emit("err", f"tick error: {type(e).__name__}: {e}")

    def _retick_soon(self, ms: int = RETICK_MS) -> None:
        """Schedule a quick follow-up tick, so a run of choosers is answered
        in seconds rather than one per poll interval. Runs on the worker
        thread's own event loop.

        COALESCED on purpose. It can be called for several windows in one
        tick, and each scheduled tick may schedule more: one timer per call
        would let one tick queue two, and two windows queue four. Keeping a
        single earliest-deadline timer makes the retick rate independent of
        how many windows asked for one.
        """
        try:
            now = datetime.now(pytz.UTC)
            target = now + timedelta(milliseconds=int(ms))
            pending = (self._retick_at is not None
                       and self._retick_at >= now)
            if pending and self._retick_at <= target:
                return                       # a sooner tick is already pending
            if pending:
                # A shorter delay than the pending one: record the earlier
                # deadline and let the already-scheduled timer fire first —
                # scheduling a second singleShot here is what let one tick
                # queue two, and two windows queue four.
                self._retick_at = target
                return
            self._retick_at = target
            QTimer.singleShot(int(ms), self._retick_fire)
        except Exception:
            pass

    def _retick_fire(self) -> None:
        """Target of the coalescing timer. Only a tick that the retick timer
        itself triggered may clear the pending marker — clearing it at the top
        of every _tick (including the periodic poll) would let each poll queue
        a fresh self-sustaining chain, which is the accumulation the
        coalescing exists to prevent."""
        self._retick_at = None
        self._tick_safely()

    def _tick(self) -> None:
        now = datetime.now(pytz.UTC)
        cooldown = timedelta(minutes=15)
        buffer = timedelta(seconds=self._buffer)
        retry_interval = timedelta(seconds=self._retry_interval)

        windows = find_terminal_windows()
        seen: set[int] = set()

        for w in windows:
            # A caller's action waits for the window in hand, not the pass:
            # six windows took 20-40 s to sweep, and a keystroke held that
            # long timed out at the caller (2026-09-27). Between two windows
            # this thread is not typing anything of its own, so an external
            # send cannot interleave with one.
            self.api_drain()
            # Every window is processed inside its own guard. Without it a
            # throw here abandoned the whole loop, so every window after
            # this one went unwatched for the pass.
            _label = "<window>"
            try:
                try:
                    hwnd = int(w.NativeWindowHandle or 0)
                except Exception:
                    continue
                if not hwnd:
                    continue
                seen.add(hwnd)

                try:
                    title = w.Name or f"<hwnd {hwnd}>"
                except Exception:
                    # A UIA COMError reading the title shouldn't drop the whole
                    # tick — fall back to an hwnd label and keep going.
                    title = f"<hwnd {hwnd}>"
                st = self._states.setdefault(hwnd, _WState(hwnd=hwnd, title=title))
                st.title = title
                _label = title
                # What log lines print for this window: the title people
                # recognise, plus the id that survives a rename and tells two
                # same-named sessions apart.
                _wt = f"{title!r} #{_wid(hwnd)}"

                # Excluded windows never get processed. Keyed via title_key so
                # exclusion survives the WT spinner glyph / title churn.
                if title_key(title) in self._excluded_titles:
                    st.status = ST_EXCLUDED
                    # Queued row commands are meaningless for an excluded row —
                    # drop them so they can't fire much later (or into an
                    # unrelated window after Windows recycles the HWND).
                    self._cmd_fire_now.discard(hwnd)
                    self._cmd_skip.discard(hwnd)
                    continue

                # --- per-row commands accumulated since last tick ---
                if hwnd in self._cmd_skip:
                    self._cmd_skip.discard(hwnd)
                    if st.reset_utc is not None:
                        self.log.emit("info",
                                      f"skipped pending continue for {_wt}")
                        # Remember the skipped key: the message is still visible
                        # in the scrollback and must not instantly re-arm in the
                        # detection step below — that would make Skip a no-op.
                        st.fired_key = st.reset_key or st.fired_key
                    st.reset_utc = None
                    st.fire_deferred = False
                    st.reset_key = None
                    st.status = ST_IDLE

                force_fire = hwnd in self._cmd_fire_now
                if force_fire:
                    self._cmd_fire_now.discard(hwnd)

                # 0. Fading "sent" flash.
                if st.sent_flash_until and now >= st.sent_flash_until:
                    st.sent_flash_until = None
                    st.status = (
                        ST_COOLDOWN
                        if st.last_sent_utc and now - st.last_sent_utc < cooldown
                        else ST_IDLE
                    )

                # 0.45. Multi-tab warning: WT exposes only the ACTIVE tab's
                # content, so Claude sessions in background tabs are invisible.
                tabs = list_tab_titles(w)
                st.tab_count = max(1, len(tabs))
                if st.tab_count > 1:
                    if not st.tabs_warned:
                        others = [t for t in tabs if t != title]
                        self.log.emit(
                            "warn",
                            f"{_wt} has {st.tab_count} tabs — only the "
                            f"ACTIVE tab is watched; invisible: {others!r}. "
                            f"Open each Claude session in its own window "
                            f"(drag the tab out of the tab bar)."
                        )
                        st.tabs_warned = True
                else:
                    st.tabs_warned = False

                # 0.5. Read scrollback once per tick so every detector below
                # shares the same view of the terminal.
                text = read_terminal_text(w)
                tail = text[-SCAN_TAIL_CHARS:] if text else ""
                # Say which of the two silences this is. Every detector below
                # reports "nothing on screen" for an unreadable window exactly
                # as it does for a window with nothing on it, so a log without
                # this line cannot answer the first question anyone asks after
                # the fact -- was the allowance really back, had the network
                # really recovered, or had the tool simply not looked? The
                # behaviour no longer depends on the difference; the record
                # should not either.
                if not tail:
                    if not st.read_blind:
                        st.read_blind = True
                        self.log.emit(
                            "warn",
                            f"could not read the screen of {_wt} this pass — "
                            f"treating it as unknown, not as 'nothing there'")
                elif st.read_blind:
                    st.read_blind = False
                    self.log.emit("info", f"screen of {_wt} readable again")
                # Only a readable pass can say anything about this; an
                # unreadable one leaves the previous answer standing rather
                # than reporting a streaming turn as finished.
                # Scrolled up inside Claude Code: newer builds draw their own
                # screen, and while the user is reading back through it the
                # live bottom — spinner, prompt box, any dialog — is simply not
                # on screen; a "Jump to bottom" hint sits where they were.
                # Captured 2026-09-25 on a session hard at work: the rows on
                # show held an OLD error and nothing after it, so it read as
                # stuck and would have had 'continue' typed into a turn in
                # flight. What cannot be seen cannot be decided on — skip the
                # window this pass, keep everything known about it, and say so
                # once. It is picked up again the moment the view returns.
                if tail and SCROLLED_AWAY_RE.search(tail[-SCROLLED_AWAY_TAIL:]):
                    if not st.scrolled_away:
                        st.scrolled_away = True
                        st.scrolled_since = now
                        self.log.emit(
                            "info",
                            f"{_wt} is scrolled up (Claude Code shows "
                            f"'Jump to bottom') — its current state is not on "
                            f"screen, so nothing is done there until the view "
                            f"is back at the bottom")
                    elif (self._scroll_back_s > 0
                            and st.scrolled_since is not None
                            and now - st.scrolled_since
                            >= timedelta(seconds=self._scroll_back_s)):
                        # Left scrolled, a window would be skipped for good —
                        # a limit coming due or a dropped connection in it
                        # would go unanswered all night. After the set time,
                        # send Claude Code's own jump-to-bottom key; the next
                        # pass reads the live bottom and handles whatever is
                        # there. Only the view moves — nothing is typed into
                        # the conversation. Refused (another window holds the
                        # foreground) means it is simply tried next pass.
                        _mins = self._scroll_back_s // 60
                        _dr = "[dry-run] " if self._dry_run else ""
                        if send_keys(w, SCROLL_TO_BOTTOM_KEYS,
                                     dry_run=self._dry_run):
                            st.scrolled_since = now
                            self.log.emit(
                                "fire-quiet",
                                f"{_dr}scrolled {_wt} back to the bottom "
                                f"(Ctrl+End) after {_mins} min scrolled up, "
                                f"so it is watched again")
                    continue
                if st.scrolled_away and tail:
                    st.scrolled_away = False
                    st.scrolled_since = None
                    self.log.emit("info", f"{_wt} is back at the bottom")
                if tail:
                    # Same routine the on-demand rescan runs, so the two
                    # can never disagree about what a screen means.
                    self._read_derived(st, tail, now)
                dr = "[dry-run] " if self._dry_run else ""

                # 0.52. Dead-session states 'continue' can't fix: warn once.
                if tail and parse_oauth_expired(
                        tail, self._patterns.get("oauth")):
                    if not st.oauth_logged:
                        self.log.emit(
                            "warn",
                            f"OAuth token expired on {_wt} — auto-continue "
                            f"can't fix this; run /login in that session"
                        )
                        st.oauth_logged = True
                else:
                    st.oauth_logged = False

                # 0.55. Interactive limit picker ("What do you want to do?").
                # Newer Claude Code builds show this modal under the limit
                # banner. It is marked and left for the user.
                if tail and parse_limit_prompt(
                        tail, self._patterns.get("limit_prompt"),
                        self._patterns.get("limit")):
                    # NOTHING is pressed here, deliberately. This chooser
                    # offers paid extra usage next to "stop and wait", and
                    # Enter takes whichever option is highlighted -- so a
                    # keystroke sent on a guess can buy an upgrade. It did:
                    # the old code checked only that the waiting option was
                    # somewhere in the list, pressed Enter, and logged that it
                    # had confirmed a choice it never actually read.
                    #
                    # Gating on "is the safe option selected?" was the first
                    # fix and still meant deciding about money from a screen
                    # scrape. The rule now is simpler and needs no reading at
                    # all: this one is the user's. The banner above the picker
                    # still carries the reset time, so the countdown is armed
                    # from that -- what waits for a human is only the choice
                    # that costs something.
                    if not st.prompt_active:
                        st.prompt_active = True
                        self.log.emit(
                            "warn",
                            f"limit picker open on {_wt} — it offers PAID "
                            f"extra usage, so nothing is pressed. Choose in "
                            f"that window; auto-continue will not."
                        )
                    st.status = ST_PROMPT
                    continue
                elif st.prompt_active:
                    st.prompt_active = False
                    if st.status == ST_PROMPT:
                        st.status = ST_IDLE

                # 0.56. Any other chooser the session is waiting on. Claude Code
                # pauses and waits for a selection; until someone picks, nothing
                # happens — the same overnight stall as a limit or a network
                # error, just with a different cause. Enter accepts the option
                # the session already pre-selected; we never type a number, so a
                # false positive is a bare Enter rather than a stray character.
                #
                # Three gates make that false positive harmless:
                #   * the turn must not be running (nothing to answer mid-stream)
                #   * the composer must be EMPTY — Enter with a draft in the box
                #     would submit the draft
                #   * the chooser's signature must differ from the last one we
                #     answered, so one chooser gets exactly one Enter
                # Permission prompts are split out under their own switch: those
                # authorise an edit or a command rather than merely unblocking.
                if tail and not session_running(tail):
                    _is_perm = parse_permission_prompt(
                        tail, self._patterns.get("chooser"),
                        self._patterns.get("permission"))
                    _is_chooser = parse_chooser_prompt(
                        tail, self._patterns.get("chooser"),
                        self._patterns.get("permission"))
                    # The two dialogs that change which model the session
                    # runs on are NOT ordinary choosers: Enter on either one
                    # switches the model, and that is the user's decision.
                    # They are left for a human, whatever the switches say.
                    _model_dlg = bool(
                        parse_fable_picker(
                            tail, self._patterns.get("fable_picker"))
                        or parse_switch_model_prompt(
                            tail, self._patterns.get("switch_model")))
                    _want = (not _model_dlg
                             and ((_is_perm and self._auto_permission)
                                  or (_is_chooser and self._auto_choose)))
                    if _want and not composer_has_draft(tail):
                        _sig = chooser_signature(
                            tail, self._patterns.get("chooser"))
                        _fresh = _sig and _sig != st.chooser_sig
                        # A different chooser deserves its own warning if it
                        # cannot be answered either.
                        if _sig and _sig != st.chooser_seen_sig:
                            st.chooser_seen_sig = _sig
                            st.chooser_warned = False
                        _stale = (st.chooser_at is not None
                                  and now - st.chooser_at
                                  >= timedelta(seconds=CHOOSER_RETRY_S))
                        if _sig and (_fresh or _stale):
                            _what = ("permission prompt" if _is_perm
                                     else "chooser")
                            # Reported AFTER the fact. The send refuses when
                            # the window will not come to the foreground, and
                            # announcing "answering" before finding that out
                            # claimed an act that never happened — then
                            # repeated the claim on every poll, because an
                            # unanswered chooser stays due.
                            if send_text_lines(w, [""],
                                               dry_run=self._dry_run):
                                st.chooser_sig = _sig
                                st.chooser_at = now
                                st.chooser_warned = False
                                self.log.emit(
                                    "fire",
                                    f"{dr}answered the {_what} "
                                    f"(Enter = option 1) → {_wt}")
                                # Questions come in runs: a form asks
                                # several and then offers Submit as one more
                                # chooser. At the poll interval that is a
                                # minute of idle per question — measured
                                # live, three minutes to walk a four-step
                                # form. Look again shortly instead.
                                self._retick_soon()
                            elif not st.chooser_warned:
                                # Once per chooser: it retries every poll,
                                # and a line each time would bury the log for
                                # as long as the user keeps typing elsewhere.
                                st.chooser_warned = True
                                self.log.emit(
                                    "warn",
                                    f"could not answer the {_what} on "
                                    f"{_wt} — the window wouldn't come "
                                    f"forward; retrying while it is up")
                            st.status = ST_PROMPT
                            continue

                # 0.6. Network-stuck path. Runs *before* the rate-limit logic and
                # ignores the cooldown — if the API is unreachable in the middle
                # of a 5h wait we still want to resend 'continue' every
                # retry_interval seconds until the connection comes back. Two
                # flavors are treated identically:
                #   a) retry banner at attempt N/N — retries exhausted
                #   b) bare `API Error: ... (E...)` / `fetch failed` — no banner
                #   c) a server-side truncation (the "Server-error"/"Response-
                #      stalled" mid-stream wordings) — 'continue' resumes the
                #      cut-off turn
                stuck_reason = None
                if tail:
                    if parse_retry_exhausted(
                            tail, self._patterns.get("retry")):
                        stuck_reason = "network retries exhausted"
                    elif parse_econnreset_stuck(
                            tail, self._patterns.get("econnreset")):
                        stuck_reason = "network API error"
                    elif parse_server_error_stuck(
                            tail, self._patterns.get("server_error")):
                        stuck_reason = "response truncated mid-stream"
                if stuck_reason:
                    if (force_fire
                            or st.retry_last_sent_utc is None
                            or now - st.retry_last_sent_utc >= retry_interval):
                        first = not st.retry_active
                        if first:
                            self.log.emit(
                                "warn",
                                f"{stuck_reason} on {_wt}; "
                                f"sending 'continue' every "
                                f"{self._retry_interval}s until recovery"
                            )
                            st.retry_active = True
                        # Every resend is a keystroke and is logged as one.
                        # Only the BALLOON is held back after the first: a
                        # long outage resends on a timer and would otherwise
                        # pop one every retry_interval seconds. Until
                        # 2026-09-19 the repeats were logged at 'info', which
                        # cost more than the noise it saved — the log
                        # under-counted what had been typed (the exact
                        # reading that found the 83-continue defect), and the
                        # API published them as plain log lines, so a
                        # companion program watching 'fire' never learned the
                        # window was about to change under it.
                        _hold = (None if force_fire else typing_hold_reason(
                            tail, self._patterns.get("chooser")))
                        if _hold:
                            if not st.hold_logged:
                                st.hold_logged = True
                                self.log.emit(
                                    "info",
                                    f"{stuck_reason} on {_wt}, but {_hold} — "
                                    f"not typing 'continue' over it; "
                                    f"looking again next pass")
                            st.status = ST_RETRY
                            continue
                        st.hold_logged = False
                        self.log.emit(
                            "fire" if first else "fire-quiet",
                            f"{dr}resending 'continue' (retry path) → {_wt}"
                        )
                        ok = send_continue(w, dry_run=self._dry_run)
                        if ok:
                            st.retry_last_sent_utc = now
                            st.status = ST_RETRY
                            # If a rate-limit pending elapsed during the outage,
                            # this 'continue' doubles as its fire — otherwise
                            # we'd send a second, redundant continue right
                            # after recovery.
                            if (st.reset_utc is not None
                                    and now >= st.reset_utc + buffer):
                                st.fired_key = st.reset_key
                                st.reset_utc = None
                                st.fire_deferred = False
                                st.reset_key = None
                                st.last_sent_utc = now
                        else:
                            self.log.emit(
                                "warn",
                                f"retry send failed for {_wt}; "
                                f"will try again in {self._interval}s"
                            )
                            st.status = ST_RETRY
                    else:
                        st.status = ST_RETRY
                    continue
                else:
                    # `stuck_reason` is None both when the screen was read and
                    # held no error AND when it could not be read at all --
                    # read_terminal_text() returns None on a UIA miss and the
                    # tail becomes "". Only the first is recovery. Treating the
                    # second as recovery also cleared retry_last_sent_utc, and
                    # the next pass that DID read the error then fired at once
                    # instead of waiting out the retry interval, so a flaky
                    # read turned a 10-minute poke into a per-minute one --
                    # queueing prompts that all land together when the session
                    # finally answers, which is what the interval exists to
                    # prevent.
                    if st.retry_active and tail:
                        self.log.emit(
                            "info",
                            f"network error cleared on {_wt}; "
                            f"recovered"
                        )
                        st.retry_active = False
                        # Deliberately NOT clearing retry_last_sent_utc. It is
                        # the floor on how often this window gets typed into,
                        # and wiping it on every recovery made that floor mean
                        # nothing for a window that keeps failing and coming
                        # back: each recovery re-armed an immediate poke.
                        # Measured on this machine overnight on 2026-09-14 —
                        # one window took 83 'continue's in 8.4 hours, 71 of
                        # the 82 gaps under the configured 600s, median 240s.
                        # Every single cycle was defensible on its own; the
                        # sum of them was not. Keeping the stamp costs nothing
                        # real: a window quiet for hours has an old stamp, so
                        # a genuinely new outage still fires at once.
                        if st.status == ST_RETRY:
                            st.status = ST_IDLE

                # 1. Detect rate-limit and update pending if it changed.
                # Runs *before* the fire decision so a NEW limit message with a
                # different reset time correctly supersedes an older pending
                # target. Skipped inside the post-send cooldown window because
                # the lingering old message would otherwise re-trigger right
                # after we sent continue.
                in_cooldown = (st.last_sent_utc is not None
                               and now - st.last_sent_utc < cooldown)
                if tail and not in_cooldown:
                    parsed = parse_limit_message(
                        tail, self._patterns.get("limit"))
                    if (parsed and parsed != st.reset_key
                            and parsed != st.fired_key):
                        # `fired_key` blocks the stale case: a message we
                        # already fired for (or the user skipped), still visible
                        # after the cooldown, must not re-arm a bogus "tomorrow
                        # at the same time" pending.
                        hour_12, minute, ampm, tz_name = parsed
                        new_reset = None
                        try:
                            new_reset = next_reset_datetime(
                                hour_12, minute, ampm, tz_name
                            )
                        except Exception as e:
                            self.log.emit(
                                "err", f"reset calc failed for {_wt}: {e}"
                            )
                        if new_reset is not None:
                            # A banner only gives a time of day, so "the next
                            # 9:30pm" is tomorrow once 9:30pm has gone by. When
                            # we read one LATE — the window was unwatched, or
                            # the pattern only started matching after an update
                            # — that arithmetic parks a session for a full day
                            # over a limit that lifted hours ago. Measured: a
                            # 21:30 reset, parsed at 21:51, scheduled for 21:31
                            # TOMORROW.
                            #
                            # A limit that reports a clock time always lifts
                            # within the day, so anything computed further out
                            # than STALE_RESET_H means we are looking at a
                            # banner whose moment has already passed. Resume
                            # now instead. If that guess is wrong the session
                            # simply reprints the banner and we reschedule from
                            # it — one wasted 'continue' against a day of a
                            # window sitting dead.
                            if (new_reset - now
                                    > timedelta(hours=STALE_RESET_H)):
                                self.log.emit(
                                    "warn",
                                    f"limit on {_wt} says it resets at "
                                    f"{hour_12}:{minute:02d}{ampm}, which "
                                    f"already passed — treating it as lifted "
                                    f"and resuming now instead of waiting for "
                                    f"tomorrow")
                                new_reset = now
                            old = st.reset_utc
                            st.reset_utc = new_reset
                            st.fire_deferred = False
                            st.reset_key = parsed
                            local = (new_reset + buffer).astimezone()
                            if old is None:
                                self.log.emit(
                                    "info",
                                    f"limit on {_wt} → resets "
                                    f"{hour_12}:{minute:02d}{ampm} ({tz_name}); "
                                    f"will fire at "
                                    f"{local:%Y-%m-%d %H:%M:%S %Z}"
                                )
                            else:
                                old_local = (old + buffer).astimezone()
                                self.log.emit(
                                    "info",
                                    f"limit on {_wt} reset shifted: "
                                    f"{old_local:%Y-%m-%d %H:%M:%S} → "
                                    f"{local:%Y-%m-%d %H:%M:%S}"
                                )
                    elif parsed is None:
                        # No limit message anywhere near the tail: the handled
                        # message is gone, so any future match is genuinely new.
                        st.fired_key = None

                # 2. Pending? Decide whether to fire.
                if st.reset_utc is not None or force_fire:
                    fire_at = (st.reset_utc or now) + buffer
                    if force_fire or now >= fire_at:
                        # Re-verify the message is still current — if the user
                        # already continued manually, the session has moved on
                        # and a redundant 'continue' would start an unwanted
                        # turn. (Forced fires skip this check by design.)
                        if (not force_fire and tail
                                and parse_limit_message(
                                    tail, self._patterns.get("limit")) is None):
                            self.log.emit(
                                "info",
                                f"limit message gone on {_wt} before fire; "
                                f"assuming handled manually"
                            )
                            st.fired_key = st.reset_key
                            st.reset_utc = None
                            st.fire_deferred = False
                            st.reset_key = None
                            st.status = ST_IDLE
                            continue
                        # Never type into a turn that is already streaming.
                        # The re-verify above only catches a manual resume
                        # once its output has pushed the banner more than
                        # MAX_POST_MATCH_TAIL from the tail; a session that
                        # resumed seconds ago still shows it, so without this
                        # the fire lands mid-stream and Claude Code queues the
                        # prompt to run when the turn ends — an unwanted extra
                        # turn, the very thing the network path and the chooser
                        # path each already refuse to cause. The pending is
                        # kept, not cancelled: a later pass fires it once the
                        # session is idle. A forced fire is the user's own
                        # instruction and is left alone.
                        if not force_fire and tail and session_running(tail):
                            if not st.fire_deferred:
                                st.fire_deferred = True
                                self.log.emit(
                                    "info",
                                    f"limit on {_wt} is due but the "
                                    f"session is mid-turn; holding the "
                                    f"'continue' until it goes quiet")
                            continue
                        _hold = (None if force_fire else typing_hold_reason(
                            tail, self._patterns.get("chooser")))
                        if _hold:
                            if not st.hold_logged:
                                st.hold_logged = True
                                self.log.emit(
                                    "info",
                                    f"limit on {_wt} is due, but {_hold} — "
                                    f"holding the 'continue' until it is "
                                    f"clear")
                            continue
                        st.hold_logged = False
                        st.fire_deferred = False
                        st.status = ST_FIRING
                        model = self._model_overrides.get(title_key(title), "")
                        effort = self._effort_overrides.get(title_key(title), "")
                        lines = []
                        if model:
                            lines.append(f"/model {model}")
                            # A direct `/model <name>` switches immediately. The
                            # trailing blank Enter confirms a dialog if one ever
                            # pops; on an empty input box it's a harmless no-op.
                            lines.append("")
                        if effort:
                            lines.append(f"/effort {effort}")
                            # Every effort level change pops a "Change effort
                            # level?" confirmation dialog with the default
                            # cursor on "Yes, switch to <level>". Sending a
                            # plain Enter selects it. (Verified for low/high/
                            # max — applies to all levels.)
                            lines.append("")
                        lines.append("continue")
                        self.log.emit(
                            "fire",
                            f"{dr}sending {lines} to {_wt}"
                            + (" (forced)" if force_fire else "")
                        )
                        _why = {}
                        ok = send_text_lines(w, lines, dry_run=self._dry_run,
                                             outcome=_why)
                        if ok:
                            st.last_sent_utc = now
                            st.fired_key = st.reset_key
                            st.reset_utc = None
                            st.fire_deferred = False
                            st.reset_key = None
                            st.status = ST_SENT
                            st.sent_flash_until = now + timedelta(seconds=5)
                            self.log.emit(
                                "info",
                                ("simulated (dry-run)" if self._dry_run else "sent")
                                + (f" /model {model} +" if model else "")
                                + (f" /effort {effort} +" if effort else "")
                                + f" continue → {_wt}"
                            )
                        else:
                            self.log.emit("warn",
                                          f"send failed for {_wt} "
                                          f"({_why.get('reason', 'foreground')}); "
                                          f"will retry in {self._interval}s")
                            st.reset_utc = now  # immediate retry next tick
                            st.fire_deferred = False
                            st.status = ST_PENDING
                    else:
                        st.status = ST_PENDING
                    continue

                # 3. No pending. Resolve status: cooldown vs idle.
                if in_cooldown and st.status != ST_SENT:
                    st.status = ST_COOLDOWN
                elif st.status not in (ST_SENT, ST_FIRING):
                    st.status = ST_IDLE

                # 4. "After finish": a watched session that RAN and has now sat
                # idle long enough gets its configured follow-up prompt, so the
                # window keeps producing instead of waiting for a human. This
                # point is only reached when nothing else owns the window — every
                # handler above (network, picker, oauth, pending limit, fire)
                # bails out with `continue` — and the guards below keep it away
                # from the states that merely LOOK idle:
                #   * never ran while watched  -> af_seen_running is still False
                #     (a fresh shell at a prompt is not a finished run)
                #   * post-fire cooldown       -> the fire's own follow-up
                #   * safeguard notice in view -> blocked, not finished; the
                #     same prompt would only be refused again, so the user
                #     owns that
                af_cmd = self._after_finish.get(title_key(title))
                if af_cmd and tail:
                    _af_key = title_key(title)
                    # Loops is a REMAINING count, not a cap: each firing spends
                    # one and the new value is persisted immediately. A cap plus
                    # an in-memory counter looked equivalent but wasn't — the
                    # counter died with the process, so every restart (including
                    # a self-update) re-armed every spent prompt and the next
                    # completed run typed it a second time, into whatever task
                    # the window had moved on to.
                    _af_left = self._after_finish_loops.get(_af_key, 1)
                    if _af_left == 0:
                        # Spent. Says so once, then stays quiet: the prompt text
                        # is still visible in the dialog, and raising Loops (or
                        # editing the prompt) is what re-arms it.
                        if not st.af_spent_logged:
                            st.af_spent_logged = True
                            self.log.emit(
                                "info",
                                f"after-finish on {_wt}: no runs left "
                                f"(Loops 0) — set Loops again to re-arm")
                    elif session_running(tail):
                        st.af_seen_running = True
                        st.af_idle_since = None
                    elif (st.af_seen_running
                            and not in_cooldown
                            and fable_refusal_distance(
                                tail, self._patterns.get("fable")) is None):
                        if st.af_idle_since is None:
                            st.af_idle_since = now
                        elif (now - st.af_idle_since
                                >= timedelta(seconds=AFTER_FINISH_SETTLE_S)
                              and typing_hold_reason(
                                  tail, self._patterns.get("chooser"))):
                            # Waiting on the user is not "finished": a
                            # question or permission prompt stops the
                            # spinner too, and the prompt's Enter would
                            # have answered it.
                            if not st.hold_logged:
                                st.hold_logged = True
                                self.log.emit(
                                    "info",
                                    f"after-finish on {_wt} is held: "
                                    f"{typing_hold_reason(tail, self._patterns.get('chooser'))}")
                        elif (now - st.af_idle_since
                                >= timedelta(seconds=AFTER_FINISH_SETTLE_S)):
                            st.hold_logged = False
                            self.log.emit(
                                "fire",
                                f"{dr}after-finish → {_wt}: {af_cmd!r}")
                            _why = {}
                            if send_text_lines(w, [af_cmd],
                                               dry_run=self._dry_run,
                                               outcome=_why):
                                # Re-arm only after it is seen running again:
                                # once per completed run, not once per tick.
                                st.af_seen_running = False
                                st.af_idle_since = None
                                # Spend one run — but only when one was really
                                # spent. In dry-run the send types nothing and
                                # still returns True, so counting here charged
                                # the budget for work that never happened, and
                                # persisting made it permanent: one dry-run pass
                                # left every configured prompt at 0 runs, which
                                # is the opposite of what dry-run promises.
                                # Negative means unlimited and is left alone.
                                if _af_left > 0 and not self._dry_run:
                                    _af_left -= 1
                                    self._after_finish_loops[_af_key] = _af_left
                                    self.loops_spent.emit(_af_key, _af_left)
                            else:
                                self.log.emit(
                                    "warn",
                                    f"after-finish send failed for {_wt} "
                                    f"({_why.get('reason', 'foreground')}); "
                                    f"will retry next tick")

            except Exception as _e:
                self.log.emit(
                    'err',
                    f'tick error on {_label!r}: {type(_e).__name__}: {_e};'
                    f' skipping this window for this pass')
                continue
        # Drop closed windows, and queued row commands aimed at them (a
        # stale hwnd could be recycled by Windows for an unrelated window).
        # "Missed this pass" is NOT "closed": find_terminal_windows() returns
        # a partial list when a child window's UIA read throws, and an EMPTY
        # one when the root enumeration does — both promised in its docstring.
        # Pruning on that alone let one COM hiccup delete every window's
        # state, and the expensive half of that cannot be re-read from the
        # screen: a pending limit's reset time, the answered chooser, and the
        # send cooldown. So ask the OS which handles are really gone.
        ghosts = set()
        for hwnd in list(self._states):
            if hwnd in seen:
                continue
            if _hwnd_alive(hwnd):
                ghosts.add(hwnd)
            else:
                del self._states[hwnd]
        if ghosts != self._ghost_hwnds:
            if ghosts:
                # Name them. A bare count cannot answer the one question the
                # line raises — is it always the SAME window? (2026-09-23 had
                # five of these by mid-morning, each "1 window", and no way to
                # tell whether one session was being skipped repeatedly.)
                _ids = " ".join(f"#{_wid(h)}" for h in sorted(ghosts))
                self.log.emit(
                    "warn",
                    f"{len(ghosts)} open window(s) missing from this pass "
                    f"({_ids}; not readable as a Claude session right now) "
                    f"— keeping what is known about them rather than "
                    f"starting them over")
            self._ghost_hwnds = set(ghosts)
        # A ghost's queued row command is kept for the same reason its state
        # is: the window is still there to receive it.
        keep = seen | ghosts
        self._cmd_fire_now &= keep
        self._cmd_skip &= keep

        # Snapshot for the GUI.
        self._publish_snapshot()

    def api_enqueue(self, job, ticket: _ApiTicket) -> None:
        """Leave an action for this thread. Called on an HTTP thread.

        deque.append is atomic, so no lock is needed to hand it over; the
        ticket's own lock is what settles whether it still runs.
        """
        self._api_pending.append((job, ticket))

    @pyqtSlot()
    def api_drain(self) -> None:
        """Run every waiting external action, on this thread, in order.

        Called by the wake signal between passes and by the pass itself
        between windows. A ticket the caller has already given up on is
        dropped here unrun, which is the other half of "busy means nothing
        was done" (see _ApiTicket).
        """
        if self._api_draining:
            return
        self._api_draining = True
        try:
            while True:
                try:
                    job, ticket = self._api_pending.popleft()
                except IndexError:
                    return
                if not ticket.start():
                    continue
                try:
                    job(ticket)
                except Exception as exc:
                    ticket.put({"ok": False,
                                "reason": f"error: {type(exc).__name__}"})
                if not ticket.done():
                    # Every api_* slot answers on every path; this is for
                    # the one that some day forgets, so the caller is not
                    # left waiting on an action that has finished.
                    ticket.put({"ok": False, "reason": "error: no_result"})
        finally:
            self._api_draining = False

    @pyqtSlot(int, object, object)
    def api_send(self, hwnd: int, lines: list, out) -> None:
        """Type on behalf of an external caller, ON THE WATCHER THREAD.

        UI Automation is only initialised here, so the HTTP thread cannot do
        this itself -- it hands the job over and waits. Running on this thread
        also makes the serialisation free: api_drain runs it between passes
        or between two windows of one, never in the middle of the tool's own
        typing.

        The result goes back through `out` rather than a return value, since
        the caller is on another thread.
        """
        try:
            if self._dry_run:
                # Dry-run is a promise that nothing gets typed. It has to hold
                # for callers too, or the checkbox means less than it says.
                self.log.emit("fire", f"[dry-run] api: send hwnd={hwnd:#x} "
                                      f"lines={len(lines)}")
                out.put({"ok": True, "dry_run": True})
                return
            target = self._api_find(hwnd)
            if target is None:
                out.put({"ok": False, "reason": "no_window"})
                return
            # Text typed after an unsent draft is submitted WITH it, as one
            # message: a companion's "summarise progress" went out as the
            # tail of a half-typed sentence (2026-09-27). The same rule every
            # auto-answer here follows: a draft in the box is the user's, so
            # nothing is typed and nothing is cleared. The caller decides.
            # An unreadable screen is not a draft, as elsewhere.
            try:
                _screen = read_terminal_text(target) or ""
            except Exception:
                _screen = ""
            if composer_has_draft(_screen[-SCAN_TAIL_CHARS:]):
                self.log.emit("info", f"api: send hwnd={hwnd:#x} not typed "
                                      f"— the input box holds unsent text")
                out.put({"ok": False, "reason": "draft"})
                return
            # send_text_lines verifies the foreground actually landed on the
            # target and refuses to type otherwise ('foreground': nothing was
            # typed). It also waits to see each line arrive in the box and
            # leave it before handing the foreground back, and says which
            # part failed ('focus_lost', 'not_confirmed') — reported as such,
            # since then something may be sitting in the box.
            why = {}
            ok = send_text_lines(target, list(lines), outcome=why)
            reason = why.get("reason", "foreground")
            self.log.emit("fire", f"api: send hwnd={hwnd:#x} "
                                  f"lines={len(lines)}"
                                  + ("" if ok else f" (refused: {reason})"))
            out.put({"ok": True} if ok
                    else {"ok": False, "reason": reason})
        except Exception as exc:
            # Never let a caller take the watcher down with a bad request.
            try:
                out.put({"ok": False, "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    def _api_find(self, hwnd: int):
        """The one window with this handle, or None. On the watcher thread.

        Asked of that window directly rather than by listing the desktop:
        the listing took up to 9 s on 2026-09-27, and every API call paid it.
        """
        return terminal_window_from_handle(hwnd)

    @pyqtSlot(int, str, object)
    def api_keys(self, hwnd: int, keyspec: str, out) -> None:
        """Send a raw keystroke on behalf of a caller, on the watcher thread.

        Same promises as api_send: dry-run types nothing, a missing window is
        reported rather than guessed at, and the foreground check inside
        send_keys is what decides whether anything is typed at all.
        """
        try:
            bad = keyspec_problem(keyspec)
            if bad:
                # Before the window is touched: no foreground grab for a
                # request that cannot be typed, and not reported as
                # 'foreground', which reads as "try again".
                out.put({"ok": False, "reason": "bad_keyspec", "key": bad})
                return
            if self._dry_run:
                self.log.emit("fire", f"[dry-run] api: keys hwnd={hwnd:#x} "
                                      f"{summarize_keyspec(keyspec)}")
                out.put({"ok": True, "dry_run": True})
                return
            target = self._api_find(hwnd)
            if target is None:
                out.put({"ok": False, "reason": "no_window"})
                return
            ok = send_keys(target, keyspec)
            # Summarised, not quoted: a caller may type a whole sentence
            # through this route, and the log is not where someone's prompt
            # belongs. /send already logs a count for the same reason.
            self.log.emit("fire", f"api: keys hwnd={hwnd:#x} "
                                  f"{summarize_keyspec(keyspec)}"
                                  + ("" if ok else " (refused)"))
            out.put({"ok": True} if ok
                    else {"ok": False, "reason": "foreground"})
        except Exception as exc:
            try:
                out.put({"ok": False, "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    @pyqtSlot(int, object)
    def api_focus(self, hwnd: int, out) -> None:
        """Bring a window forward without typing anything into it.

        Unlike send/keys this changes nothing inside the session, so dry-run
        does not suppress it: dry-run is a promise about keystrokes, and
        pretending a window was not raised would be a different lie.
        """
        try:
            target = self._api_find(hwnd)
            if target is None:
                out.put({"ok": False, "reason": "no_window"})
                return
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(int(hwnd), 9)     # SW_RESTORE
            user32.SetForegroundWindow(int(hwnd))
            user32.BringWindowToTop(int(hwnd))
            # Windows refuses a foreground grab from a background process in
            # some states and says nothing; report what actually happened
            # rather than what was asked for.
            ok = (int(user32.GetForegroundWindow() or 0) == int(hwnd))
            self.log.emit("fire", f"api: focus hwnd={hwnd:#x}"
                                  + ("" if ok else " (refused)"))
            out.put({"ok": True} if ok
                    else {"ok": False, "reason": "foreground"})
        except Exception as exc:
            try:
                out.put({"ok": False, "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    @pyqtSlot(int, int, object)
    def api_text(self, hwnd: int, tail: int, out) -> None:
        """Hand back what a session has on screen.

        Gated by its own setting in the GUI — this is the session's contents,
        not its status. Reading is done here because UI Automation only works
        on this thread, and an unreadable window says so rather than coming
        back as an empty screen, which is a different thing entirely.
        """
        try:
            target = self._api_find(hwnd)
            if target is None:
                out.put({"ok": False, "reason": "no_window"})
                return
            text = read_terminal_text(target)
            if not text:
                out.put({"ok": False, "reason": "read_blind"})
                return
            out.put({"hwnd": hwnd, "text": text[-int(tail):]})
        except Exception as exc:
            try:
                out.put({"ok": False, "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    def _snapshot_row(self, st) -> dict:
        """One window's row. Shared with the on-demand rescan, deliberately:
        two places building "the same" row is how they stop being the same."""
        return {
            "hwnd": st.hwnd,
            "title": st.title,
            "status": st.status,
            "reset_utc": st.reset_utc,
            "last_sent_utc": st.last_sent_utc,
            "retry_last_sent_utc": st.retry_last_sent_utc,
            "tabs": st.tab_count,
            "excluded": title_key(st.title) in self._excluded_titles,
            "model": self._model_overrides.get(title_key(st.title), ""),
            "effort": self._effort_overrides.get(title_key(st.title), ""),
            # Added for the local API. title_key is what every per-window
            # setting is keyed by, so a caller showing a window list needs the
            # same identity the tool uses; running says whether typing now
            # would land mid-turn; prompt says what it is waiting on.
            "title_key": title_key(st.title),
            "running": bool(st.running),
            "prompt": (dict(st.prompt,
                            seen_at=st.prompt_at.isoformat()
                            if st.prompt_at else None)
                       if st.prompt else None),
            # Which of two readings is newer, for a caller mixing the list
            # with rescans: the list's own "ts" is when it was SERVED.
            "read_utc": st.read_at,
        }

    def _read_derived(self, st, tail: str, now) -> None:
        """Re-derive the read-only facts about a window from its screen.

        Shared by the tick and the on-demand rescan so the two cannot disagree
        about what a screen means. Only the things that are READ live here —
        deciding to type is the tick's business alone.
        """
        st.read_at = now
        st.running = bool(session_running(tail))
        _p = classify_prompt(
            tail,
            self._patterns.get('chooser'),
            self._patterns.get('permission'),
            self._patterns.get('limit_prompt'),
            self._patterns.get('limit'),
            self._patterns.get('switch_model'))
        # seen_at marks when THIS prompt appeared, not when it was last seen:
        # a watch showing "waiting 4 min" needs the former, and re-stamping it
        # every pass would make every prompt look brand new forever.
        # Compared by identity, not the whole dict: the dict now carries the
        # cursor and the check boxes, and moving the cursor is the same prompt.
        if _prompt_ident(_p) != _prompt_ident(st.prompt):
            st.prompt_at = now if _p else None
        st.prompt = _p

    @pyqtSlot(int, object)
    def api_scan(self, hwnd: int, out) -> None:
        """Read one window NOW, instead of waiting for the next pass.

        A 60-second pass is right for the job this tool exists to do; it is
        useless for "a prompt appeared, tell me within seconds". Dropping the
        global interval to match would spend UIA reads on every window all day
        to serve the few seconds when one of them is actually waiting.

        It READS and never acts. A caller polling this every few seconds must
        not be able to make the tool type anything — deciding to send is the
        tick's alone, so `status` is left exactly as the tick last set it.
        """
        try:
            target = self._api_find(hwnd)
            if target is None:
                out.put({"ok": False, "reason": "no_window"})
                return
            st = self._states.get(int(hwnd))
            if st is None:
                # Known to the OS but not yet to the watcher: the first pass
                # after it opened has not run. Nothing to update or return.
                out.put({"ok": False, "reason": "no_window"})
                return
            # The title too, not just the screen. Claude Code renames its
            # window to a summary of the conversation as soon as the first
            # message lands, and a caller matching sessions to windows by name
            # would otherwise be told the old one until the next full pass —
            # up to a minute of looking at the wrong window. Reading it costs
            # nothing here: the window object is already in hand.
            try:
                _t = target.Name
                if _t:
                    st.title = _t
            except Exception:
                pass        # a UIA hiccup on the title is not worth failing for
            text = read_terminal_text(target)
            tail = text[-SCAN_TAIL_CHARS:] if text else ""
            if not tail:
                # The previous values stand — an unreadable screen is not a
                # screen with nothing on it.
                out.put({"ok": False, "reason": "read_blind"})
                return
            self._read_derived(st, tail, datetime.now(pytz.UTC))
            # Into the list too, BEFORE answering. /windows served the last
            # pass's rows, so a caller that rescanned and then listed was
            # told the prompt was there and then that it was not, for up to
            # a pass (2026-09-27, Claude Control's approval page reopening
            # every few seconds). Published first, a list read after this
            # answer can never be older than it.
            self._publish_snapshot()
            out.put({"ok": True, "window": self._snapshot_row(st)})
        except Exception as exc:
            try:
                out.put({"ok": False, "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    def _publish_snapshot(self) -> None:
        """Hand the current rows to the GUI and to the local API.

        `latest_rows` is read by the API from its own thread. Rebinding one
        attribute is atomic, and a published list is never changed after,
        so no lock is needed; it is set before the signal so that nothing
        which happens after this call can see older rows than it.
        """
        rows = self._make_snapshot()
        self.latest_rows = rows
        self.snapshot.emit(rows)

    def _make_snapshot(self) -> list:
        out = []
        for st in self._states.values():
            out.append(self._snapshot_row(st))
        # Stable ordering: retry (network down) / limit picker are the most
        # urgent, then rate-limit pending, then idle, then excluded.
        order = {ST_RETRY: 0, ST_PROMPT: 1, ST_FIRING: 2,
                 ST_PENDING: 3, ST_SENT: 4, ST_IDLE: 5, ST_COOLDOWN: 6,
                 ST_EXCLUDED: 7}
        out.sort(key=lambda r: (order.get(r["status"], 9), r["title"].lower()))
        return out


# ===========================================================================
# Main window
# ===========================================================================

STATUS_LABEL = {
    ST_IDLE:     "Idle",
    ST_PENDING:  "⏳ Waiting",
    ST_FIRING:   "▶ Sending…",
    ST_SENT:     "✓ Sent",
    ST_COOLDOWN: "Cooldown",
    ST_EXCLUDED: "Excluded",
    ST_RETRY:    "⚠ Net retry",
    ST_PROMPT:   "⏎ Limit prompt",
    "held":      "Input held",
    "unknown":   "Unreadable",
    "attention": "Needs attention",
}


def _current_color_scheme() -> "Qt.ColorScheme":
    """Return the OS-level color scheme (Light/Dark/Unknown).

    Falls back to Unknown when running under a Qt build that predates
    QStyleHints.colorScheme() (added in Qt 6.5). Unknown is treated as
    Light by `_make_palette`.
    """
    app = QApplication.instance()
    if app is None:
        return Qt.ColorScheme.Unknown
    try:
        return app.styleHints().colorScheme()
    except (AttributeError, RuntimeError):
        return Qt.ColorScheme.Unknown


def _make_palette(scheme: "Qt.ColorScheme") -> dict:
    """Color set tuned to the active OS color scheme.

    Cell backgrounds and log text need explicit colors because Qt's auto
    palette only adjusts widget chrome (base / window / button) — our
    hard-coded status bg + log text were pale-on-light, which collapses
    on a dark widget base. Dark mode uses deeper saturated bgs with
    near-white text so cells stay readable.
    """
    if scheme == Qt.ColorScheme.Dark:
        return {
            "status_bg": {
                ST_PENDING:  QColor("#5c4a00"),
                ST_FIRING:   QColor("#5c3500"),
                ST_SENT:     QColor("#1f4a2b"),
                ST_COOLDOWN: QColor("#3a3f44"),
                ST_EXCLUDED: QColor("#2a2d31"),
                ST_RETRY:    QColor("#5c1e1e"),
                ST_PROMPT:   QColor("#3d3160"),
            },
            "status_fg": QColor("#f1f3f5"),
            "log_fg": {
                "info": QColor("#dee2e6"),
                "warn": QColor("#ffd43b"),
                "err":  QColor("#ff8787"),
                "fire": QColor("#74c0fc"),
            },
            "dot_running": "#51cf66",
            "dot_stopped": "#868e96",
        }
    # Light or Unknown → original light palette.
    return {
        "status_bg": {
            ST_PENDING:  QColor("#fff3bf"),
            ST_FIRING:   QColor("#ffd8a8"),
            ST_SENT:     QColor("#b2f2bb"),
            ST_COOLDOWN: QColor("#dee2e6"),
            ST_EXCLUDED: QColor("#f8f9fa"),
            ST_RETRY:    QColor("#ffc9c9"),
            ST_PROMPT:   QColor("#e5dbff"),
        },
        "status_fg": QColor("#212529"),
        "log_fg": {
            "info": QColor("#212529"),
            "warn": QColor("#b07a00"),
            "err":  QColor("#c92a2a"),
            "fire": QColor("#1864ab"),
        },
        "dot_running": "#2f9e44",
        "dot_stopped": "#adb5bd",
    }


def _fmt_local(dt_utc: Optional[datetime]) -> str:
    if dt_utc is None:
        return "—"
    return dt_utc.astimezone().strftime("%H:%M %Z")


def _fmt_countdown(target_utc: Optional[datetime], now_utc: datetime) -> str:
    if target_utc is None:
        return "—"
    delta = target_utc - now_utc
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "now"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


class Updater(QObject):
    """Background GitHub self-update worker, on its OWN QThread (separate from
    Watcher so a multi-minute download never stalls watchdog ticks).

    All network/disk work happens in the two slots; results come back to the
    main thread via signals. It never quits the app or swaps the exe — that's
    the main thread's job in MainWindow._on_update_ready.
    """

    update_available = pyqtSignal(dict)    # normalized release (carries _manual)
    no_update = pyqtSignal(str, bool)      # (current_version, manual?)
    check_failed = pyqtSignal(str, bool)   # (reason, manual?)
    progress = pyqtSignal(int)             # 0..100, or -1 = indeterminate
    download_failed = pyqtSignal(str)
    ready_to_install = pyqtSignal(str)     # path to the staged new exe
    note = pyqtSignal(str)                 # source choice / fallback, for the log

    @pyqtSlot(bool)
    def check(self, manual: bool) -> None:
        rel = updater.fetch_latest_release()
        if rel is None:
            self.check_failed.emit(
                "Could not reach GitHub (network / rate limit / no asset)",
                manual)
            return
        if updater.is_newer(rel.get("version", ""), APP_VERSION):
            rel["_manual"] = manual
            self.update_available.emit(rel)
        else:
            self.no_update.emit(APP_VERSION, manual)

    @pyqtSlot(dict)
    def download_and_stage(self, rel: dict) -> None:
        if (Path(sys.executable).parent / "Auto-Continue.version").is_file():
            self.download_failed.emit("Control integration: update the matching Control package; independent binary replacement is disabled")
            return
        try:
            if not _re.fullmatch(r'[0-9a-fA-F]{64}', rel.get('sha256') or ''):
                raise ValueError('Release SHA-256 is missing; retry after release metadata is available')
            dest = updater.staged_exe_path()
            size = int(rel.get("asset_size") or 0)

            def cb(done: int, total: int) -> None:
                tot = total or size
                self.progress.emit(int(done * 100 / tot) if tot else -1)

            # keep_partial: the staged path sits beside the exe, so what
            # survives a failed attempt is still there next launch. On a
            # link where this asset takes tens of minutes, that is the
            # difference between eventually finishing and never finishing.
            # Two sources carry the same build: the GitHub release, and the
            # download-site mirror. Whichever answers faster gets used, and a
            # failure on one falls through to the other — this machine's link
            # to GitHub drops often enough that a single source is a single
            # point of failure. The choice and both measurements are logged.
            updater.download_update(
                rel.get("version") or "", rel["asset_url"], dest,
                progress_cb=cb, total_hint=size,
                log_cb=self.note.emit, expected_sha256=rel['sha256'])
            if not updater.verify_sha256(dest, rel.get("sha256")):
                try:
                    os.remove(dest)
                except OSError:
                    pass
                self.download_failed.emit("Integrity check failed (SHA256 mismatch)")
                return
            self.ready_to_install.emit(dest)
        except Exception as e:
            self.download_failed.emit(f"Download failed: {type(e).__name__}: {e}")


# Shared named objects for single-instance coordination (session-scoped).
_MUTEX_NAME = "Local\\AutoContinueGuiSingleton" + os.environ.get("AUTO_CONTINUE_INSTANCE_SUFFIX", "")
_SHOW_EVENT_NAME = "Local\\AutoContinueShowEvent" + os.environ.get("AUTO_CONTINUE_INSTANCE_SUFFIX", "")
_ACK_EVENT_NAME = "Local\\AutoContinueAckEvent" + os.environ.get("AUTO_CONTINUE_INSTANCE_SUFFIX", "")


class _ShowListener(QThread):
    """Waits on a shared named Win32 auto-reset event and emits
    `show_requested` when a *second* launch signals it — so double-clicking
    the exe (or re-running it) surfaces the already-running window instead
    of a dead-end 'already running' popup. It also sets the ACK event so the
    second launch knows a LIVE instance handled the request (a stale/leaked
    mutex with no live owner never acks, letting the new launch take over).
    Pure ctypes; no QtNetwork (which the frozen build excludes)."""

    show_requested = pyqtSignal()

    def __init__(self, show_handle: int, ack_handle: int, parent=None):
        super().__init__(parent)
        self._h = show_handle
        self._ack = ack_handle
        self._stop = False

    def run(self) -> None:
        import ctypes
        k = ctypes.windll.kernel32
        while not self._stop:
            # 400 ms timeout so stop() is honored promptly; auto-reset event
            # clears itself when the wait succeeds (rc == WAIT_OBJECT_0 == 0).
            rc = k.WaitForSingleObject(self._h, 400)
            if rc == 0 and not self._stop:
                self.show_requested.emit()
                # Tell the waiting second instance a live primary exists.
                if self._ack:
                    k.SetEvent(self._ack)

    def stop(self) -> None:
        self._stop = True


# ===========================================================================
# "Start with Windows" — per-user boot autostart (HKCU Run key)
# ===========================================================================
# One checkbox toggles a per-user HKCU\...\Run entry that relaunches us at
# login with --minimized (straight to the tray). No admin rights needed. The
# registry entry — not QSettings — is the source of truth, so the checkbox
# stays honest even if the user removes it from Task Manager's Startup tab.

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE_NAME = "Auto-Continue"
_MINIMIZED_FLAG = "--minimized"


def _autostart_command() -> str:
    """Command line Windows runs at login to bring us up minimized in tray."""
    if getattr(sys, "frozen", False):
        # PyInstaller onefile: sys.executable IS our exe.
        return f'"{sys.executable}" {_MINIMIZED_FLAG}'
    # Source checkout: relaunch the .pyw entry via pythonw (no console window).
    entry = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "Auto-Continue.pyw")
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return f'"{pyw}" "{entry}" {_MINIMIZED_FLAG}'


def autostart_enabled() -> bool:
    """True when our HKCU Run entry currently exists."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
            winreg.QueryValueEx(key, _RUN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Add or remove the HKCU Run entry. Raises OSError on a registry failure
    (the caller reverts the checkbox to autostart_enabled())."""
    import winreg
    if enabled:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _RUN_VALUE_NAME, 0, winreg.REG_SZ,
                              _autostart_command())
    else:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, _RUN_VALUE_NAME)
        except FileNotFoundError:
            pass  # nothing to remove


class MainWindow(CodexGuiMixin, QMainWindow):
    # Signals into the watcher (auto-connected via Qt::QueuedConnection
    # because the watcher lives on a different thread).
    sig_start = pyqtSignal()
    sig_stop = pyqtSignal()
    sig_claude_start = pyqtSignal()
    sig_claude_stop = pyqtSignal()
    sig_codex_start = pyqtSignal()
    sig_codex_stop = pyqtSignal()
    sig_codex_config = pyqtSignal(dict)
    sig_codex_fire = pyqtSignal(int)
    sig_codex_skip = pyqtSignal(int)
    sig_codex_cooldown = pyqtSignal(int)
    sig_codex_app_start = pyqtSignal()
    sig_codex_app_stop = pyqtSignal()
    sig_codex_app_config = pyqtSignal(dict)
    sig_codex_app_fire = pyqtSignal(int)
    sig_codex_app_skip = pyqtSignal(int)
    sig_codex_app_cooldown = pyqtSignal(int)
    sig_codex_app_bind = pyqtSignal(dict)
    sig_claude_app_start = pyqtSignal()
    sig_claude_app_stop = pyqtSignal()
    sig_claude_app_config = pyqtSignal(dict)
    sig_claude_app_fire = pyqtSignal(int)
    sig_claude_app_skip = pyqtSignal(int)
    sig_claude_app_cooldown = pyqtSignal(int)
    sig_set_interval = pyqtSignal(int)
    sig_set_buffer = pyqtSignal(int)
    sig_set_retry_interval = pyqtSignal(int)
    sig_set_dry_run = pyqtSignal(bool)
    sig_set_excluded = pyqtSignal(list)
    sig_fire_now = pyqtSignal(int)
    sig_skip = pyqtSignal(int)
    sig_exclude = pyqtSignal(int, str)
    sig_unexclude = pyqtSignal(str)
    sig_clear_cooldown = pyqtSignal(int)
    sig_set_effort_overrides = pyqtSignal(dict)
    # The local API leaves its actions in the watcher's queue (UIA is only
    # initialised on that thread) and this wakes the watcher to run them
    # when it is between passes. Mid-pass it gets to them on its own.
    sig_api_wake = pyqtSignal()
    # Delivered to the GUI thread (settings touch widgets). Emitted from
    # an HTTP thread, so Qt queues it — see _api_do_settings for why a
    # timer cannot do this job.
    sig_api_settings = pyqtSignal(object, object)
    sig_set_model_overrides = pyqtSignal(dict)
    sig_set_after_finish = pyqtSignal(dict)
    sig_set_after_finish_loops = pyqtSignal(dict)
    sig_set_auto_answer = pyqtSignal(bool, bool)
    sig_set_scroll_back = pyqtSignal(int)
    sig_set_trigger_patterns = pyqtSignal(dict)
    # Into the updater (its own thread).
    sig_update_check = pyqtSignal(bool)      # manual?
    sig_update_download = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._init_cli_gui()
        self._dry_run_enabled = False  # Debug preference; no GUI control.
        self.setWindowTitle(f"Auto-Continue v{APP_VERSION} · Claude / Codex")
        self.resize(1060, 760)

        profile = os.environ.get("AUTO_CONTINUE_SETTINGS_FILE")
        self.settings = QSettings(profile, QSettings.Format.IniFormat) if profile else QSettings("auto_continue", "gui")
        self._latest_snapshot: list = []
        # Local API: the object exists even when the listener is switched off,
        # so _api_apply() can be called unconditionally from either direction.
        self._local_api = local_api.LocalApi({
            "version": APP_VERSION,
            "token": "",                    # filled in by _api_apply
            "enabled": self._api_enabled,
            "text_enabled": self._api_text_enabled,
            "snapshot": self._api_snapshot,
            "send": self._api_do_send,
            "keys": self._api_do_keys,
            "focus": self._api_do_focus,
            "text": self._api_do_text,
            "scan": self._api_do_scan,
            # Reported so a companion program can tell its user that this
            # tool will answer permission prompts before they can.
            "auto_permission": lambda: bool(self._auto_permission),
            "auto_choose": lambda: bool(self._auto_choose),
            "settings": self._api_do_settings,
        })
        self._api_last_send: dict = {}
        self._api_last_scan: dict = {}
        # Must exist before _load_settings runs, because _load_settings
        # toggles widgets that fire valueChanged → _save_settings, which
        # reads this attribute. Empty list is the right default.
        self._excluded_titles: list = []
        # Per-window effort overrides keyed by stable title (see title_key).
        self._effort_overrides: dict = {}
        # Per-window model overrides, same keying.
        self._model_overrides: dict = {}
        self._after_finish: dict = {}
        self._after_finish_loops: dict = {}
        self._auto_choose = False
        self._auto_permission = True
        self._scroll_back_min = DEFAULT_SCROLL_BACK_MIN
        # User regex overrides, keyed by TRIGGER_SPECS key. Only patterns the
        # user actually changed are stored, so a future build's improved
        # default still wins for every trigger they never touched.
        self._trigger_patterns: dict = {}
        # title_key -> runs left, for after-finish budgets spent WHILE the
        # After finish dialog is open. Its spinner snapshots the count when
        # it opens, and a modal exec() keeps pumping this thread's queued
        # signals, so OK would write the pre-fire number back and re-arm a
        # prompt that had just been used.
        self._pending_loops: dict = {}
        # Set by _load_settings when the stored settings show the model
        # recovery removed in 3.2.0 had been switched on, and its owner has
        # not been told yet.
        self._recovery_removed_note = False
        # The release dict from the latest "update available" result, if any.
        self._pending_release: Optional[dict] = None
        # Single-instance "show me" listener thread (attached in main()).
        self._show_listener: Optional[_ShowListener] = None
        # Theme-aware color set, plus a ring buffer of recent log entries
        # so they can be re-rendered when the user flips Win11 dark mode.
        # Must exist before _load_settings (which may emit log lines via
        # _apply_keep_awake) and before _build_ui (which doesn't read them
        # directly but is co-located for clarity).
        self._palette = _make_palette(_current_color_scheme())
        self._log_buffer: list = []  # list[tuple[ts, level, msg]]
        # Persistent activity log for overnight postmortems (the in-window
        # view is a 500-line ring buffer that dies with the process).
        # %LOCALAPPDATA%\auto_continue\activity.log, rotated at ~1 MB.
        self._log_path = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "auto_continue", "activity.log",
        )
        try:
            os.makedirs(os.path.dirname(self._log_path), exist_ok=True)
        except OSError:
            self._log_path = None
        # Signature of the last rendered table so unchanged snapshots skip
        # the full rebuild (which would close an open dropdown every tick).
        self._last_render_sig: Optional[list] = None

        self._build_ui()
        self._build_worker()
        self._build_updater()
        self._load_settings()
        controller_pid = os.environ.get('AUTO_CONTINUE_CONTROL_OWNER_PID')
        if controller_pid:
            from control_service import ProcessLifetime
            self._control_parent = ProcessLifetime(int(controller_pid))
            self._control_watchdog = QTimer(self)
            self._control_watchdog.setInterval(1000)
            self._control_watchdog.timeout.connect(
                lambda: self.close() if not self._control_parent.alive() else None)
            self._control_watchdog.start()

        # React to OS-level theme flips. Safe to skip on older Qt builds.
        try:
            QApplication.instance().styleHints().colorSchemeChanged.connect(
                self._on_color_scheme_changed
            )
        except (AttributeError, RuntimeError):
            pass

        # Periodic 1s repaint just for the live countdown column.
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._refresh_countdowns)
        self._tick_timer.start()

        # The listener gets knocked on once a minute. Cheap (a loopback
        # connect), and it is the only thing that can tell a serving API from
        # one that merely thinks it is serving.
        self._api_dead_since = None
        self._api_health_timer = QTimer(self)
        self._api_health_timer.setInterval(60000)
        self._api_health_timer.timeout.connect(self._api_health)
        self._api_health_timer.start()

        # Auto-check for updates once per launch (silent unless a newer
        # version exists). Delayed ~5s so startup/worker init isn't blocked;
        # the check itself runs on the updater thread regardless.
        QTimer.singleShot(5000, lambda: self.sig_update_check.emit(False))

    # ---- UI construction -------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        # Header bar
        header = QHBoxLayout()

        self.status_dot = QLabel("●")
        # Initial color comes from the active palette; will be repainted
        # by _refresh_running_indicator() (called from __init__ tail and
        # whenever Start/Stop toggles or the theme flips).
        self.status_dot.setStyleSheet(
            f"color: {self._palette['dot_stopped']}; font-size: 16px;"
        )
        self.status_text = QLabel("Stopped")
        self.status_text.setStyleSheet("font-weight: bold;")

        self.start_btn = QPushButton("Start")
        self.start_btn.setMinimumWidth(96)
        self.start_btn.setMinimumHeight(40)
        _sbf = self.start_btn.font()
        _sbf.setBold(True)
        _sbf.setPointSize(_sbf.pointSize() + 1)
        self.start_btn.setFont(_sbf)
        self.start_btn.clicked.connect(self._toggle_running)

        self.keep_awake_check = QCheckBox("Keep awake")
        self.keep_awake_check.setToolTip(
            "Prevent the system from going to sleep / Modern Standby. "
            "Required if you want auto-continue to fire while you're away "
            "from the keyboard for hours — Modern Standby kills background "
            "Python processes."
        )
        self.keep_awake_check.toggled.connect(
            lambda v: (self._apply_keep_awake(v), self._save_settings())
        )

        self.autostart_check = QCheckBox("Start on launch")
        self.autostart_check.setToolTip(
            "Begin watching automatically when the app opens — including "
            "the automatic relaunch after a self-update. Without this, "
            "protection lapses until you click Start."
        )
        self.autostart_check.toggled.connect(lambda _v: self._save_settings())

        # Boot autostart (stacked under "Start on launch" in the header).
        # Backed by the HKCU Run entry rather than QSettings.
        self.boot_check = QCheckBox("Start with Windows")
        self.boot_check.setToolTip(
            "Launch Auto-Continue automatically when you sign in to "
            "Windows, minimized to the system tray. Pair with 'Start on "
            "launch' above for hands-off protection from boot. Adds a "
            "per-user registry Run entry (no admin needed); uncheck to "
            "remove it."
        )
        self.boot_check.toggled.connect(self._on_boot_toggled)

        self.interval_spin = timing_spin(DEFAULT_INTERVAL_S, 5, 600)
        self.interval_spin.setToolTip("Polling interval")
        self.interval_spin.valueChanged.connect(
            lambda v: (self.sig_set_interval.emit(v), self._save_settings())
        )

        self.buffer_spin = timing_spin(DEFAULT_BUFFER_S, 0, 600)
        self.buffer_spin.setToolTip("Extra delay past the reset hour")
        self.buffer_spin.valueChanged.connect(
            lambda v: (self.sig_set_buffer.emit(v), self._save_settings())
        )

        self.retry_spin = timing_spin(DEFAULT_RETRY_S, 5, 3600)
        self.retry_spin.setToolTip(
            "When Claude shows 'attempt 10/10' (network retries exhausted), "
            "resend 'continue' every N seconds until the connection comes "
            "back and Claude responds."
        )
        self.retry_spin.valueChanged.connect(
            lambda v: (self.sig_set_retry_interval.emit(v),
                       self._save_settings())
        )

        header.addWidget(self.status_dot)
        header.addWidget(self.status_text)
        header.addSpacing(6)
        header.addWidget(self.start_btn)
        header.addStretch()
        self.app_settings_btn = QPushButton("App settings…")
        self.app_settings_btn.clicked.connect(self._open_app_settings)
        header.addWidget(self.app_settings_btn)
        self.more_btn = QToolButton()
        self.more_btn.setText("More")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_btn)
        for label, callback in (("Help", self._open_help),
                                ("Export log…", self._export_log_for_feedback),
                                ("Check updates", lambda: self.sig_update_check.emit(True))):
            action = self.more_menu.addAction(label)
            action.triggered.connect(callback)
        self.more_btn.setMenu(self.more_menu)
        header.addWidget(self.more_btn)

        # Driver settings live only in their corresponding visible card.
        self.advanced_btn = QPushButton("More settings…")
        self.advanced_btn.setToolTip("Claude CLI: answering, triggers and local API")
        self.advanced_btn.clicked.connect(self._open_advanced)
        root.addLayout(header)
        self._build_cli_controls(root)

        # Update banner (hidden until an update is found).
        self.update_banner = QWidget()
        bl = QHBoxLayout(self.update_banner)
        bl.setContentsMargins(8, 4, 8, 4)
        self.update_label = QLabel("")
        self.update_now_btn = QPushButton("Update now")
        self.update_now_btn.clicked.connect(self._start_update_download)
        self.update_notes_btn = QPushButton("Release notes")
        self.update_notes_btn.clicked.connect(self._open_release_notes)
        self.update_close_btn = QPushButton("✕")
        self.update_close_btn.setFixedWidth(28)
        self.update_close_btn.setToolTip("Dismiss")
        self.update_close_btn.clicked.connect(
            lambda: self.update_banner.setVisible(False))
        bl.addWidget(self.update_label)
        bl.addStretch()
        bl.addWidget(self.update_now_btn)
        bl.addWidget(self.update_notes_btn)
        bl.addWidget(self.update_close_btn)
        self.update_banner.setVisible(False)
        self._apply_banner_theme()
        root.addWidget(self.update_banner)

        # Resizable monitoring areas, with no duplicated configuration fields.
        self.monitor_splitter = QSplitter(Qt.Orientation.Vertical)
        self.monitor_splitter.setChildrenCollapsible(False)
        self.windows_panel = QGroupBox("Monitored sessions")
        window_layout = QVBoxLayout(self.windows_panel)
        self._build_monitor_status(window_layout)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["Window", "Status", "Reset", "Countdown", "Last sent",
             "Model", "Effort", "Action"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 8):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        for i, width in ((5, 135), (6, 90), (7, 150)):
            h.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.table.setColumnWidth(i, width)
        self.table.setMinimumHeight(70)
        window_layout.addWidget(self.table)
        self.monitor_splitter.addWidget(self.windows_panel)

        # Log view
        log_panel = QGroupBox("Activity log")
        self.log_panel = log_panel
        log_layout = QVBoxLayout(log_panel)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.setMinimumHeight(55)
        log_layout.addWidget(self.log_view)
        self.monitor_splitter.addWidget(log_panel)
        self.monitor_splitter.setStretchFactor(0, 3)
        self.monitor_splitter.setStretchFactor(1, 1)
        self.monitor_splitter.setSizes([360, 120])
        root.addWidget(self.monitor_splitter, 1)

        self.setCentralWidget(central)

        # Initial prominent Start/Stop styling + status dot.
        self._refresh_running_indicator()

        # System tray (optional minimize-to-tray)
        self._build_tray()

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Auto-Continue — Claude CLI / Codex EXE / Codex CLI")
        self.tray.activated.connect(self._on_tray_activated)

        # Right-click context menu so the user can show the window or
        # quit the app without having to restore it first.
        menu = QMenu()
        show_act = QAction("Show window", self)
        show_act.triggered.connect(self._show_from_tray)
        check_act = QAction("Check for updates…", self)
        check_act.triggered.connect(lambda: self.sig_update_check.emit(True))
        quit_act = QAction("Quit", self)
        quit_act.triggered.connect(self._quit_from_tray)
        menu.addAction(show_act)
        menu.addAction(check_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        self.tray.setContextMenu(menu)

        self.tray.show()
        # First-time tip so the user understands the app is still running
        # after they minimize it.
        self._notified_minimize_to_tray = False

    def _on_tray_activated(self, reason) -> None:
        # Single click and double click both restore. Context menu handled
        # separately by the menu itself.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_from_tray()

    def _show_from_tray(self) -> None:
        # Clear the minimized bit so showNormal doesn't restore-as-minimized,
        # then show + force to the foreground. Used both by the tray click
        # and by a second launch signalling us to surface (single-instance).
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        # Belt-and-braces native show + foreground grab. Qt's show() is a
        # no-op if its cached state already says "visible" (which desyncs
        # from the real window if anything hid it out from under Qt), and
        # activateWindow() won't steal foreground from another process on
        # Windows. A direct ShowWindow + SetForegroundWindow guarantees the
        # window actually appears and comes forward. (The signalling second
        # instance is exiting, so the foreground grab is allowed.)
        try:
            import ctypes
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)   # SW_RESTORE (un-minimize)
            user32.ShowWindow(hwnd, 5)   # SW_SHOW    (force visible)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        except Exception:
            pass

    @pyqtSlot()
    def _on_show_requested(self) -> None:
        self._show_from_tray()
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", "Already running — window restored.",
                QSystemTrayIcon.MessageIcon.Information, 2500)

    def notify_started_in_tray(self) -> None:
        """Boot-launched straight into the tray (--minimized): keep the window
        hidden and drop a one-shot balloon so the user knows we're alive. Also
        suppresses the later first-minimize tip (this already served it)."""
        self._notified_minimize_to_tray = True
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue",
                "Running in the system tray. Right-click the tray icon to "
                "open the window or quit.",
                QSystemTrayIcon.MessageIcon.Information, 3500)

    def attach_show_listener(self, show_handle: int, ack_handle: int) -> None:
        """Start the single-instance 'show me' listener on the given named
        event handles (created in main())."""
        if not show_handle:
            return
        self._show_listener = _ShowListener(show_handle, ack_handle, self)
        self._show_listener.show_requested.connect(self._on_show_requested)
        self._show_listener.start()

    def _quit_from_tray(self) -> None:
        # Mirrors the X-button path; closeEvent does the full shutdown.
        self.close()

    # Intercept Windows' minimize so it goes straight to the tray instead
    # of showing as a taskbar button.
    def changeEvent(self, event) -> None:
        if (event.type() == QEvent.Type.WindowStateChange
                and self.tray is not None
                and self.windowState() & Qt.WindowState.WindowMinimized):
            # Defer hide() until after Qt finishes processing the state
            # change, otherwise the window can flash visible briefly.
            QTimer.singleShot(0, self._hide_to_tray)
        super().changeEvent(event)

    def _hide_to_tray(self) -> None:
        # Restore normal state internally so the next show() doesn't pop
        # back as minimized, then hide the window entirely.
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.hide()
        if not self._notified_minimize_to_tray:
            self._notified_minimize_to_tray = True
            self.tray.showMessage(
                "Auto-Continue",
                "Still running in the system tray. "
                "Right-click the tray icon to show or quit.",
                QSystemTrayIcon.MessageIcon.Information, 3500,
            )

    # ---- Worker plumbing -------------------------------------------------

    def _build_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = Watcher()
        self.worker.moveToThread(self.worker_thread)

        self.worker_thread.started.connect(self.worker.thread_started)

        # Forward UI commands → worker.
        self.sig_start.connect(self._start_selected_watchers)
        self.sig_stop.connect(self._stop_watchers)
        self.sig_claude_start.connect(self.worker.start)
        self.sig_claude_stop.connect(self.worker.stop)
        self.sig_set_interval.connect(self.worker.set_interval)
        self.sig_set_buffer.connect(self.worker.set_buffer)
        self.sig_set_retry_interval.connect(self.worker.set_retry_interval)
        self.sig_set_dry_run.connect(self.worker.set_dry_run)
        self.sig_set_excluded.connect(self.worker.set_excluded)
        self.sig_fire_now.connect(self.worker.cmd_fire_now)
        self.sig_skip.connect(self.worker.cmd_skip)
        self.sig_exclude.connect(self.worker.cmd_exclude)
        self.sig_unexclude.connect(self.worker.cmd_unexclude)
        self.sig_clear_cooldown.connect(self.worker.cmd_clear_cooldown)
        self.sig_api_wake.connect(self.worker.api_drain)
        self.sig_api_settings.connect(self._api_apply_settings)
        self.sig_set_effort_overrides.connect(self.worker.set_effort_overrides)
        self.sig_set_model_overrides.connect(self.worker.set_model_overrides)
        self.sig_set_after_finish.connect(self.worker.set_after_finish)
        self.sig_set_after_finish_loops.connect(
            self.worker.set_after_finish_loops)
        self.sig_set_auto_answer.connect(self.worker.set_auto_answer)
        self.sig_set_scroll_back.connect(self.worker.set_scroll_back)
        self.sig_set_trigger_patterns.connect(
            self.worker.set_trigger_patterns)

        # Receive snapshots and logs.
        self.worker.snapshot.connect(self._on_claude_snapshot)
        self.worker.log.connect(self._append_log)
        self.worker.loops_spent.connect(self._on_loops_spent)
        self.worker.running_changed.connect(self._on_claude_running)
        self._build_codex_worker()

        from control_service import CodexService, CAPABILITIES
        self.codex_service = CodexService(self.codex_worker, self.codex_app_worker)
        self.codex_service.monitors['claude'] = self.worker
        self.codex_service.monitor_export = self._control_export_settings
        self.codex_service.moveToThread(self.worker_thread)
        for signal, monitor, slot in (
                (self.sig_claude_start, self.worker, self.codex_service.start_claude),
                (self.sig_codex_start, self.codex_worker, self.codex_service.start_codex),
                (self.sig_codex_app_start, self.codex_app_worker, self.codex_service.start_codex_app)):
            signal.disconnect(monitor.start)
            signal.connect(slot)
        self.codex_service.publish.connect(lambda event, body: self._api_publish(event, body))
        self.codex_service.spent.connect(self._on_codex_rpc_loops_spent)
        self.codex_service.rpc_snapshot.connect(self._on_codex_rpc_snapshot)
        self._local_api.ctx.update(capabilities=CAPABILITIES, codex_service=self.codex_service)
        if "--selftest" not in sys.argv:
            self.worker_thread.started.connect(self.codex_service.start)
        self.worker_thread.start()
        # Only now: the API hands sends to the watcher, so there has to be a
        # watcher to hand them to before anything can connect.
        self._api_apply()

    # ---- Updater plumbing -----------------------------------------------

    def _build_updater(self) -> None:
        self.updater_thread = QThread(self)
        self.updater = Updater()
        self.updater.moveToThread(self.updater_thread)
        # MainWindow -> updater
        self.sig_update_check.connect(self.updater.check)
        self.sig_update_download.connect(self.updater.download_and_stage)
        # updater -> MainWindow
        self.updater.update_available.connect(self._on_update_available)
        self.updater.no_update.connect(self._on_no_update)
        self.updater.check_failed.connect(self._on_update_check_failed)
        self.updater.progress.connect(self._on_update_progress)
        self.updater.note.connect(
            lambda m: self._append_log("info", m))
        self.updater.download_failed.connect(self._on_update_download_failed)
        self.updater.ready_to_install.connect(self._on_update_ready)
        self.updater_thread.start()

    def _apply_banner_theme(self) -> None:
        dark = _current_color_scheme() == Qt.ColorScheme.Dark
        bg, fg = ("#5c4a00", "#fff3bf") if dark else ("#fff3bf", "#5c4a00")
        self.update_banner.setStyleSheet(
            f"background:{bg}; border-radius:4px;")
        self.update_label.setStyleSheet(f"color:{fg}; font-weight:bold;")

    # ---- Update flow -----------------------------------------------------

    @pyqtSlot(dict)
    def _on_update_available(self, rel: dict) -> None:
        self._pending_release = rel
        ver = rel.get("version", "?")
        self.update_label.setText(f"🔄  New version v{ver} available")
        self.update_now_btn.setVisible(True)
        self.update_notes_btn.setVisible(True)
        frozen = updater.is_frozen()
        self.update_now_btn.setEnabled(frozen)
        self.update_now_btn.setToolTip(
            "Download, replace and restart" if frozen
            else "Run the packaged .exe to self-update (disabled in source mode)")
        self.update_banner.setVisible(True)
        self._append_log("info", f"update available: v{ver}")
        if self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue update",
                f"Version {ver} is available. Open the window to update.",
                QSystemTrayIcon.MessageIcon.Information, 6000)

    @pyqtSlot(str, bool)
    def _on_no_update(self, cur: str, manual: bool) -> None:
        self._append_log("info", f"up to date (v{cur})")
        if manual and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue",
                f"You're on the latest version (v{cur}).",
                QSystemTrayIcon.MessageIcon.Information, 4000)

    @pyqtSlot(str, bool)
    def _on_update_check_failed(self, reason: str, manual: bool) -> None:
        self._append_log("warn", f"update check failed: {reason}")
        if manual and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", f"Update check failed: {reason}",
                QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _start_update_download(self) -> None:
        if not self._pending_release:
            return
        ver = self._pending_release.get("version", "?")
        self.update_now_btn.setEnabled(False)
        self.update_notes_btn.setVisible(False)
        self.update_label.setText(f"Downloading v{ver}…")
        self._append_log("info", f"downloading v{ver}…")
        self.sig_update_download.emit(dict(self._pending_release))

    @pyqtSlot(int)
    def _on_update_progress(self, pct: int) -> None:
        ver = (self._pending_release or {}).get("version", "?")
        if pct < 0:
            self.update_label.setText(f"Downloading v{ver}…")
        else:
            self.update_label.setText(f"Downloading v{ver}…  {pct}%")

    @pyqtSlot(str)
    def _on_update_download_failed(self, reason: str) -> None:
        self._append_log("err", reason)
        # Revert the banner to the actionable state so the user can retry.
        if self._pending_release:
            self._on_update_available(self._pending_release)

    @pyqtSlot(str)
    def _on_update_ready(self, path: str) -> None:
        ver = (self._pending_release or {}).get("version", "?")
        if not updater.is_frozen():
            self._append_log(
                "info", f"dev mode — staged + verified at {path} (skip swap)")
            self.update_label.setText(f"v{ver} downloaded (dev mode, no swap)")
            return
        try:
            self._append_log("info", f"installing v{ver} and restarting…")
            # Launch the detached swapper FIRST — if writing/launching the
            # swap .bat fails we bail out here with the watcher threads
            # still alive, instead of a dead-but-"Running" app.
            state = self.codex_service.monitor_state()
            running = {name: bool(value and not state['paused']) for name, value in state['running'].items()}
            updater.stage_and_swap(path, resume=running)
            # Update now already authorizes the restart. Use the normal close
            # path to save settings and release the local API and UIA driver.
            self.close()
        except Exception as e:
            self._append_log("err", f"install failed: {type(e).__name__}: {e}")
            if self._pending_release:
                self._on_update_available(self._pending_release)

    def _open_release_notes(self) -> None:
        rel = self._pending_release or {}
        url = rel.get("html_url")
        if url:
            QDesktopServices.openUrl(QUrl(url))

    # The decorator's signature must match the SIGNAL's, not a subset of it:
    # PyQt truncates the emitted arguments to what the decorator declares, so
    # @pyqtSlot(str) here dropped `left`, the call raised TypeError, and an
    # unhandled exception inside a slot makes Qt call qFatal -- which is why
    # the packaged exe died with 0xc0000409 in Qt6Core seconds after every
    # after-finish send, taking the whole watchdog down with it.
    @pyqtSlot(str, int)
    def _on_loops_spent(self, key: str, left: int) -> None:
        """Persist the remaining after-finish runs the moment one is spent.

        The worker owns the countdown but not the registry, and the whole
        reason Loops counts DOWN is that the number has to survive a restart:
        an in-memory tally re-armed every spent prompt on every launch, and
        the window's next task got the previous task's follow-up typed into
        it a second time.
        """
        if not key:
            return
        self._after_finish_loops[key] = left
        self._pending_loops[key] = left
        self._save_settings()
        self._render_table()          # button label shows what is left
        self._append_log(
            "info",
            f"after-finish for {key!r}: "
            + (f"{left} run(s) left" if left else
               "no runs left — set Loops again to re-arm"))

    def _reset_settings(self) -> None:
        """Put Claude CLI settings back to their shipped defaults.

        Destructive and not obviously undoable, so it asks first and names
        what goes.
        """
        if QMessageBox.question(
                self, "Reset Claude CLI settings?",
                "This restores the Claude CLI shipped defaults:\n\n"
                f"  • poll {DEFAULT_INTERVAL_S}s · buffer {DEFAULT_BUFFER_S}s "
                f"· retry {DEFAULT_RETRY_S}s\n"
                "  • clears per-window model and effort overrides\n"
                "  • clears per-window after-finish prompts\n"
                "  • clears excluded windows\n"
                "  • restores the built-in trigger patterns\n\n"
                "Keep-awake and the start-up options are left as "
                "they are. Continue?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel
        ) != QMessageBox.StandardButton.Yes:
            return

        widgets = [self.interval_spin, self.buffer_spin, self.retry_spin]
        for wdg in widgets:
            wdg.blockSignals(True)
        try:
            self.interval_spin.setValue(DEFAULT_INTERVAL_S)
            self.buffer_spin.setValue(DEFAULT_BUFFER_S)
            self.retry_spin.setValue(DEFAULT_RETRY_S)
        finally:
            for wdg in widgets:
                wdg.blockSignals(False)

        self._effort_overrides = {}
        self._model_overrides = {}
        self._after_finish = {}
        self._after_finish_loops = {}
        self._pending_loops.clear()
        self._excluded_titles = set()
        self._trigger_patterns = {}

        # Push everything to the worker, then persist, so a crash between the
        # two cannot leave the running watcher and the saved state disagreeing.
        self.sig_set_interval.emit(DEFAULT_INTERVAL_S)
        self.sig_set_buffer.emit(DEFAULT_BUFFER_S)
        self.sig_set_retry_interval.emit(DEFAULT_RETRY_S)
        self.sig_set_excluded.emit([])
        self.sig_set_effort_overrides.emit({})
        self.sig_set_model_overrides.emit({})
        self.sig_set_after_finish.emit({})
        self.sig_set_after_finish_loops.emit({})
        self._auto_choose = False
        self._auto_permission = True
        self.sig_set_auto_answer.emit(False, True)
        self._scroll_back_min = DEFAULT_SCROLL_BACK_MIN
        self.sig_set_scroll_back.emit(DEFAULT_SCROLL_BACK_MIN)
        self.sig_set_trigger_patterns.emit({})
        self._save_settings()
        self._append_log(
            "info",
            f"settings reset to defaults (poll {DEFAULT_INTERVAL_S}s, buffer "
            f"{DEFAULT_BUFFER_S}s, retry {DEFAULT_RETRY_S}s)")

    def _collect_redacted_log(self) -> tuple:
        """(text, n_lines) — the whole activity log, oldest first, redacted.

        The rotated `.old` half is included: a report written the morning
        after an incident is often already on the far side of a rotation.
        """
        parts = []
        for path in ((self._log_path + ".old") if self._log_path else None,
                     self._log_path):
            if not path or not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    parts.extend(fh.read().splitlines())
            except OSError:
                continue
        lines = [redact_log_line(ln) for ln in parts if ln.strip()]
        header = [
            f"# Auto-Continue {APP_VERSION} activity log — redacted for sharing",
            "# Window titles and typed prompts are removed; windows appear as",
            "# #id only. Nothing here is terminal content: the log never held",
            "# any. Check it over before sending it anywhere.",
            "",
        ]
        return chr(10).join(header + lines), len(lines)

    def _export_log_for_feedback(self) -> None:
        """Show the redacted log, then save it only if the user says so.

        A log is safe where it lives and unsafe once it travels: a Claude Code
        window is named after its conversation, so the raw file names the
        projects and tasks on this machine. Redacting is not enough on its own
        — the user has to be able to SEE what is about to leave, which is why
        this previews rather than writing straight to a file.
        """
        text, n = self._collect_redacted_log()
        if not n:
            QMessageBox.information(self, "Export log",
                                    "The activity log is empty.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Export log for feedback — check before saving")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(
            f"{n} lines, titles and prompts removed. This is exactly what "
            f"will be written:"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setMinimumSize(760, 420)
        lay.addWidget(view)
        box = QDialogButtonBox()
        save = box.addButton("Save…", QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        box.rejected.connect(dlg.reject)
        save.clicked.connect(dlg.accept)
        lay.addWidget(box)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        default = os.path.join(
            os.path.expanduser("~"),
            f"auto-continue-log-{datetime.now():%Y%m%d-%H%M}.txt")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save redacted log", default, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            QMessageBox.warning(self, "Export log",
                                f"Could not write the file: {exc}")
            return
        self._append_log("info", f"redacted log exported ({n} lines)")
        QMessageBox.information(self, "Export log", f"Saved to: {path}")

    # ---- local API ------------------------------------------------------

    def _api_enabled(self) -> bool:
        return bool(self.settings.value("local_api/enabled", True, type=bool))

    def _control_export_settings(self):
        import json
        profile = os.environ.get('AUTO_CONTINUE_SETTINGS_FILE')
        store = QSettings(profile, QSettings.Format.IniFormat) if profile else QSettings('auto_continue', 'gui')
        store.sync()
        values = {}
        for key in store.allKeys():
            if key.startswith(('local_api/', 'updates/', 'update/', 'boot')) and key != 'local_api/text':
                continue
            value = store.value(key)
            try: json.dumps(value)
            except (TypeError, ValueError): continue
            values[key] = value
        return values

    def _api_port(self) -> int:
        try:
            return int(self.settings.value("local_api/port",
                                           local_api.DEFAULT_PORT))
        except (TypeError, ValueError):
            return local_api.DEFAULT_PORT

    def _api_token(self) -> str:
        """The token, minted once and kept. Rotating it on every start would
        break any client already holding one, for no gain — it never leaves
        this machine."""
        tok = self.settings.value("local_api/token", "", type=str)
        if not tok:
            tok = local_api.new_token()
            self.settings.setValue("local_api/token", tok)
        return tok

    def _api_snapshot(self) -> dict:
        """Whatever the last pass saw. Deliberately not a fresh UIA sweep:
        that would run on the HTTP thread, where UIA is not initialised, and
        it would let any caller slow the watcher down by polling."""
        src = getattr(self.worker, "latest_rows", None)
        if src is None:
            src = [r for r in self._latest_snapshot
                   if r.get("provider", "claude") == "claude"]
        return {"ts": int(time.time() * 1000),
                "windows": [_api_row(r) for r in src]}

    def _api_handoff(self, job, rate_limited: bool = True,
                     hwnd: int = 0) -> dict:
        """Put an action on the watcher thread and wait for its answer.

        Every external action goes through here so they all get the same
        refusals (no watcher, too soon, not reached in time) rather than each
        endpoint inventing its own idea of when it is safe to act.

        "busy" means nothing was done, and a caller may safely retry. It is
        only answered after the action has been withdrawn unstarted. Once
        the watcher has started it, typing is under way, and the only honest
        answer is the one it produces, so that is waited for.
        """
        worker = self.worker
        if worker is None:
            return {"ok": False, "reason": "disabled"}
        if getattr(getattr(self, 'codex_service', None), 'monitor_owner', ''):
            return {"ok": False, "reason": "monitor_paused"}
        now = time.time()
        key = int(hwnd)
        prev = self._api_last_send.get(key, 0.0)
        if rate_limited:
            if now - prev < local_api.MIN_SEND_GAP_S:
                return {"ok": False, "reason": "rate_limited"}
            self._api_last_send[key] = now
        ticket = _ApiTicket()
        worker.api_enqueue(job, ticket)
        self.sig_api_wake.emit()
        if ticket.wait(local_api.SEND_TIMEOUT_S):
            return ticket.result
        if ticket.cancel():
            if rate_limited:
                # Nothing happened, so an immediate retry is not the caller
                # repeating itself and must not be throttled as one.
                self._api_last_send[key] = prev
            return {"ok": False, "reason": "busy"}
        if ticket.wait(local_api.STARTED_WAIT_S):
            return ticket.result
        # Started and still not finished: it may well have typed. Saying
        # busy here would be the very lie this replaced.
        return {"ok": False, "reason": "unfinished"}

    def _api_do_send(self, hwnd: int, lines: list) -> dict:
        w = self.worker
        return self._api_handoff(
            lambda out: w.api_send(int(hwnd), list(lines), out),
            hwnd=hwnd)

    def _api_do_keys(self, hwnd: int, keyspec: str) -> dict:
        # Checked here too, before the rate limit: a spec that cannot be
        # typed did nothing, so it must not throttle the corrected retry.
        bad = keyspec_problem(keyspec)
        if bad:
            return {"ok": False, "reason": "bad_keyspec", "key": bad}
        w = self.worker
        return self._api_handoff(
            lambda out: w.api_keys(int(hwnd), str(keyspec), out),
            hwnd=hwnd)

    def _api_do_focus(self, hwnd: int) -> dict:
        # Not rate-limited: raising a window twice in a row is harmless and a
        # UI that follows the user's attention may legitimately ask often.
        w = self.worker
        return self._api_handoff(
            lambda out: w.api_focus(int(hwnd), out),
            rate_limited=False)

    def _api_do_scan(self, hwnd: int) -> dict:
        """On-demand rescan. Its own rate limit, and no log line.

        Not routed through _api_handoff's send throttle: that one exists to
        stop a caller typing twice, and this types nothing. It gets a limit of
        its own because each call is still a real UIA read — and no log line,
        because a client polling every few seconds would otherwise bury the
        log that everything else here depends on being readable.
        """
        now = time.time()
        if now - self._api_last_scan.get(int(hwnd), 0.0) < \
                local_api.MIN_SCAN_GAP_S:
            return {"ok": False, "reason": "rate_limited"}
        self._api_last_scan[int(hwnd)] = now
        w = self.worker
        r = self._api_handoff(
            lambda out: w.api_scan(int(hwnd), out),
            rate_limited=False)
        # The row carries datetimes, which JSON cannot hold: a rescan of any
        # window that had ever been sent a 'continue' failed to serialise
        # and the caller got no reply at all (found 2026-09-27).
        if isinstance(r, dict) and isinstance(r.get("window"), dict):
            r = dict(r, window=_api_row(r["window"]))
        return r

    def _api_do_text(self, hwnd: int, tail: int) -> dict:
        # Reading changes nothing, so no rate limit — but it still crosses to
        # the watcher, because UIA only works there.
        w = self.worker
        return self._api_handoff(
            lambda out: w.api_text(int(hwnd), int(tail), out),
            rate_limited=False)

    def _api_health(self) -> None:
        """Knock on the listener; if nobody answers, bring it back and say so.

        Added after the API stopped serving on 2026-09-13 while the app went
        on believing it was up: `running` is a flag this code sets, not a fact
        about the socket, so nothing noticed, nothing restarted it, and the
        discovery file stayed on disk telling clients to keep trying a port
        that answered nothing. Checking a flag you set yourself is not a check.
        """
        if not self._api_enabled():
            return
        if not self._local_api.running or self._local_api.alive():
            self._api_dead_since = None
            # A live socket nobody can find is a dead service as far as a
            # client is concerned: the port and token are discovered through
            # that file, so losing it is indistinguishable from the API being
            # gone. It went missing for real on 2026-09-13 — deleted by
            # something else entirely — while the port served on happily and
            # nothing here noticed, because the only thing being checked was
            # the socket.
            if self._local_api.running and not os.path.exists(
                    local_api.discovery_path()):
                try:
                    local_api.write_discovery(
                        self._api_port(), self._api_token(), APP_VERSION,
                        exe=_discovery_exe())
                    self._append_log(
                        "warn",
                        "local API token file had gone missing — written "
                        "again so clients can find the port")
                except OSError as exc:
                    self._append_log(
                        "warn", f"local API token file could not be "
                                f"rewritten: {exc}")
            return
        self._append_log(
            "warn",
            "local API stopped answering on port "
            f"{self._local_api.port} — restarting it")
        try:
            self._local_api.stop()
        except Exception:
            pass
        self._api_apply()
        if not (self._local_api.running and self._local_api.alive()):
            # Say it once, not every minute: a line a minute would bury the
            # log that the rest of this tool depends on being readable.
            if not self._api_dead_since:
                self._api_dead_since = datetime.now()
                self._append_log(
                    "err",
                    "local API could not be restarted; companion programs "
                    "cannot reach it until this is sorted out")

    def _api_publish(self, event: str, data) -> None:
        if event == 'monitor' and isinstance(data, dict):
            self.start_btn.setEnabled(not data.get('paused'))
            self.start_btn.setToolTip('Paused by Codex Claude Control' if data.get('paused') else '')
        """Fan an event out to SSE subscribers, never letting that fail here.

        This is called from the log and snapshot paths, which must keep
        working whether or not anyone is listening and whether or not the
        broker is in a good mood — a watchdog that stops watching because its
        event stream broke would have its priorities backwards.
        """
        try:
            if self._local_api.running:
                self._local_api.broker.publish(event, data)
        except Exception:
            pass

    def _api_do_settings(self, changes: dict) -> dict:
        """Change an answering switch on a caller's behalf.

        The only route that alters what the user configured. A caller holding
        the token can already type anything into any session, so this grants
        no new power — but a checkbox changing underneath the person who
        ticked it is a surprise, and the two things that stop it being one are
        here: a line in the log at the same level as a keystroke, and the GUI
        following along so the dialog never disagrees with reality.

        Marshalled onto the GUI thread because it touches widgets; the HTTP
        thread only waits for the answer.
        """
        # A signal, not QTimer.singleShot. singleShot creates its timer in the
        # CALLING thread, and this is called on an HTTP thread that has no Qt
        # event loop — so the timer never fired, the handler never ran, and
        # every request hung for the full timeout. Caught by driving a real
        # instance before release (2026-09-14): the unit tests called the
        # handler directly and stubbed the route, so the one hop that crosses
        # threads was never executed by any of them. A signal emitted from a
        # foreign thread is delivered queued to the receiver's own thread,
        # which is exactly what send/keys/focus already rely on.
        # A ticket, for the same reason as _api_handoff: a timed-out change
        # must not land later, after the caller was told it did not.
        ticket = _ApiTicket()
        self.sig_api_settings.emit(dict(changes), ticket)
        if ticket.wait(local_api.SEND_TIMEOUT_S):
            return ticket.result
        if ticket.cancel():
            return {"ok": False, "reason": "busy"}
        if ticket.wait(local_api.STARTED_WAIT_S):
            return ticket.result
        return {"ok": False, "reason": "unfinished"}

    def _api_apply_settings(self, changes: dict, out) -> None:
        if isinstance(out, _ApiTicket) and not out.start():
            return          # the caller already heard busy; change nothing
        try:
            for key, want in changes.items():
                want = bool(want)
                cur = (self._auto_permission if key == "auto_permission"
                       else self._auto_choose)
                if cur == want:
                    continue
                if key == "auto_permission":
                    self._auto_permission = want
                else:
                    self._auto_choose = want
                self.settings.setValue(key, want)
                self._append_log(
                    "fire",
                    f"api: settings {key}={'on' if want else 'off'}")
            self.sig_set_auto_answer.emit(self._auto_choose,
                                          self._auto_permission)
            out.put({"ok": True,
                     "permission_autoanswer": bool(self._auto_permission),
                     "chooser_autoanswer": bool(self._auto_choose)})
        except Exception as exc:
            try:
                out.put({"ok": False,
                         "reason": f"error: {type(exc).__name__}"})
            except Exception:
                pass

    def _api_text_enabled(self) -> bool:
        """Separate from the main switch, and off unless asked for.

        Window status is one thing; the contents of a session is another. The
        user turned the API on knowing it could type — that is not the same as
        agreeing to hand over what is on screen.
        """
        return bool(self.settings.value("local_api/text", False, type=bool))

    def _api_apply(self) -> None:
        """Bring the listener into line with the setting, in either direction."""
        want = self._api_enabled()
        if want and not self._local_api.running:
            # The handler reads the token straight out of this dict, so it has
            # to be there before the socket accepts anything.
            self._local_api.ctx["token"] = self._api_token()
            ok, err = self._local_api.start(self._api_port())
            if ok:
                path = local_api.write_discovery(
                    self._api_port(), self._api_token(), APP_VERSION,
                    exe=_discovery_exe())
                self._append_log(
                    "info",
                    f"local API listening on 127.0.0.1:{self._api_port()} — "
                    f"token in {path}")
            else:
                # A port already taken must not stop the watchdog: watching
                # windows is the job, the API is an extra.
                self._append_log(
                    "warn",
                    f"local API could not start on port {self._api_port()}: "
                    f"{err}; window watching is unaffected")
        elif not want and self._local_api.running:
            self._local_api.stop()
            local_api.clear_discovery()
            self._append_log(
                "info", "local API stopped and its token file removed")

    def _open_help(self) -> None:
        """Usage notes, kept inside the app so they are there when the session
        is stuck at 3am and nobody is going to go read a README."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Auto-Continue v{APP_VERSION} — Help")
        dlg.resize(760, 640)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setHtml(HELP_HTML.format(version=APP_VERSION))
        lay.addWidget(view)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    def _open_app_settings(self) -> None:
        controls = {"autostart": self.autostart_check, "boot": self.boot_check,
                    "keep_awake": self.keep_awake_check}
        dlg = AppSettingsDialog(self, controls)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            for key, value in dlg.values().items():
                controls[key].setChecked(value)

    def _open_advanced(self) -> None:
        dlg = AdvancedDialog(self, dict(self._trigger_patterns),
                             {"choose": self._auto_choose,
                              "permission": self._auto_permission,
                              "scroll_back_min": self._scroll_back_min})
        _accepted = dlg.exec()
        if dlg.reset_requested:
            # The dialog was rejected on purpose so nothing it holds is
            # written back; the reset (which asks first) runs on its own.
            self._reset_settings()
            return
        if _accepted:
            _auto = dlg.result_auto()
            _auto_changed = (
                _auto["choose"] != self._auto_choose
                or _auto["permission"] != self._auto_permission)
            self._auto_choose = _auto["choose"]
            self._auto_permission = _auto["permission"]
            if _auto["scroll_back_min"] != self._scroll_back_min:
                self._scroll_back_min = _auto["scroll_back_min"]
                self._append_log(
                    "info",
                    "scroll-back: "
                    + (f"a window left scrolled up is jumped back to the "
                       f"bottom after {self._scroll_back_min} min"
                       if self._scroll_back_min else "off"))
            self.sig_set_scroll_back.emit(self._scroll_back_min)
            self.sig_set_auto_answer.emit(self._auto_choose,
                                          self._auto_permission)
            if _auto_changed:
                self._append_log(
                    "info",
                    "auto-answer: choosers "
                    + ("ON" if self._auto_choose else "OFF")
                    + ", permission requests "
                    + ("ON" if self._auto_permission else "OFF"))
            self._trigger_patterns = dlg.result_patterns()
            # The API switch is applied here rather than live on the checkbox:
            # OK means OK, and a user who ticks then cancels should not have
            # opened a port in the meantime.
            _api = dlg.result_api()
            _api_changed = (_api["enabled"] != self._api_enabled()
                            or _api["port"] != self._api_port())
            if _api["text"] != self._api_text_enabled():
                # Worth its own line in the log: it changes what leaves this
                # machine's sessions, not just whether the port is open.
                self._append_log(
                    "info",
                    "local API: reading session contents is now "
                    + ("ALLOWED" if _api["text"] else "off"))
            self.settings.setValue("local_api/enabled", _api["enabled"])
            self.settings.setValue("local_api/text", _api["text"])
            self.settings.setValue("local_api/port", _api["port"])
            if _api_changed:
                # A port change has to go down before it can come back up on
                # the new number; _api_apply only ever moves one direction.
                self._stop_local_api()
                self._api_apply()
            self.sig_set_trigger_patterns.emit(
                dict(self._trigger_patterns))
            self._save_settings()
            if self._trigger_patterns:
                self._append_log(
                    "warn",
                    "custom trigger pattern(s): "
                    + ", ".join(sorted(self._trigger_patterns))
                    + " — built-in defaults are overridden for these")

    # ---- Settings persistence -------------------------------------------

    def _settings_int(self, key: str, default: int) -> int:
        """int(QSettings.value(...)) that survives a corrupt registry value
        instead of killing the (windowed, console-less) exe at startup."""
        try:
            return int(self.settings.value(key, default))
        except (TypeError, ValueError):
            return default

    def _load_settings(self) -> None:
        self._load_cli_settings()
        # Block widget signals so that programmatically populating the
        # controls doesn't trigger a chain of valueChanged → _save_settings
        # → sig_set_* before the worker is even ready.
        widgets = [self.interval_spin, self.buffer_spin, self.retry_spin,
                   self.keep_awake_check,
                   self.autostart_check, self.boot_check]
        for w in widgets:
            w.blockSignals(True)
        try:
            self.interval_spin.setValue(
                self._settings_int("interval", DEFAULT_INTERVAL_S))
            self.buffer_spin.setValue(
                self._settings_int("buffer", DEFAULT_BUFFER_S))
            self.retry_spin.setValue(
                self._settings_int("retry_interval", DEFAULT_RETRY_S)
            )
            self._dry_run_enabled = self.settings.value("dry_run", False, type=bool)
            self.keep_awake_check.setChecked(
                self.settings.value("keep_awake", False, type=bool)
            )
            self.autostart_check.setChecked(
                self.settings.value("autostart", True, type=bool)
            )
            # Boot-autostart is backed by the HKCU Run entry, not QSettings —
            # read the registry so the checkbox mirrors reality even if the
            # user toggled it from Task Manager's Startup tab.
            self.boot_check.setChecked(autostart_enabled())
            excl = self.settings.value("excluded", [], type=list) or []
            # Normalize through title_key — migrates raw titles persisted
            # by older versions (exclusion used to break when the WT
            # spinner glyph changed).
            self._excluded_titles = sorted(
                {title_key(str(t)) for t in excl if title_key(str(t))}
            )
            # QSettings can't roundtrip arbitrary dicts, so we serialize
            # effort overrides as JSON.
            import json
            raw = self.settings.value("effort_overrides", "", type=str) or ""
            # Both maps are consumed OUTSIDE this try (dict(...) further down),
            # so a stored value that parses but isn't a dict — `[1,2]`, `null`
            # from a hand-edited or older store — used to raise out of
            # __init__ and kill the windowed exe at startup with no console
            # and no message.
            try:
                loaded_e = json.loads(raw) if raw else {}
                self._effort_overrides = (
                    {str(k): str(v) for k, v in loaded_e.items()}
                    if isinstance(loaded_e, dict) else {})
            except Exception:
                self._effort_overrides = {}
            raw_m = self.settings.value("model_overrides", "", type=str) or ""
            try:
                loaded_m = json.loads(raw_m) if raw_m else {}
                self._model_overrides = (
                    {str(k): str(v) for k, v in loaded_m.items()}
                    if isinstance(loaded_m, dict) else {})
            except Exception:
                self._model_overrides = {}
            raw_af = self.settings.value("after_finish", "", type=str) or ""
            try:
                loaded_af = json.loads(raw_af) if raw_af else {}
                self._after_finish = (
                    {str(k): str(v) for k, v in loaded_af.items()
                     if str(v).strip()}
                    if isinstance(loaded_af, dict) else {})
            except Exception:
                self._after_finish = {}
            raw_al = self.settings.value(
                "after_finish_loops", "", type=str) or ""
            try:
                self._after_finish_loops = coerce_after_finish_loops(
                    json.loads(raw_al) if raw_al else {})
            except Exception:
                self._after_finish_loops = {}
            self._auto_choose = self.settings.value(
                "auto_choose", False, type=bool)
            self._auto_permission = self.settings.value(
                "auto_permission", True, type=bool)
            self._scroll_back_min = max(0, self._settings_int(
                "scroll_back_min", DEFAULT_SCROLL_BACK_MIN))
            raw_t = self.settings.value("trigger_patterns", "", type=str) or ""
            try:
                loaded_t = json.loads(raw_t) if raw_t else {}
                if isinstance(loaded_t, dict):
                    self._trigger_patterns = {
                        str(k): str(v) for k, v in loaded_t.items()
                        if isinstance(v, str) and v.strip()
                    }
            except Exception:
                self._trigger_patterns = {}
            # Model recovery was removed in 3.2.0. Its stored settings
            # ("fable_cfg") are left where they are and simply no longer
            # read: an older copy sharing this store, such as one bundled
            # with a companion app, may still use them. Someone who had it
            # switched on is told once, rather than finding out from a
            # session left sitting on a safeguard block.
            try:
                raw_f = self.settings.value("fable_cfg", "", type=str) or ""
                old_f = json.loads(raw_f) if raw_f else {}
                if (isinstance(old_f, dict) and old_f.get("enabled")
                        and not self.settings.value(
                            "recovery_removed_noted", False, type=bool)):
                    self._recovery_removed_note = True
            except Exception:
                pass
        finally:
            for w in widgets:
                w.blockSignals(False)

        # Now push the final config to the worker exactly once. Always emit
        # COPIES: a queued connection shares the same Python object across
        # threads, and the worker iterating a dict/list the GUI thread later
        # mutates would raise mid-slot.
        self.sig_set_interval.emit(self.interval_spin.value())
        self.sig_set_buffer.emit(self.buffer_spin.value())
        self.sig_set_retry_interval.emit(self.retry_spin.value())
        self.sig_set_dry_run.emit(self._dry_run_enabled)
        self.sig_set_excluded.emit(list(self._excluded_titles))
        self.sig_set_effort_overrides.emit(dict(self._effort_overrides))
        self.sig_set_model_overrides.emit(dict(self._model_overrides))
        self.sig_set_after_finish.emit(dict(self._after_finish))
        self.sig_set_after_finish_loops.emit(dict(self._after_finish_loops))
        self.sig_set_auto_answer.emit(self._auto_choose,
                                      self._auto_permission)
        self.sig_set_scroll_back.emit(self._scroll_back_min)
        self.sig_set_trigger_patterns.emit(dict(self._trigger_patterns))
        if self._recovery_removed_note:
            self._append_log(
                "warn",
                "model recovery was removed in 3.2.0 and no longer runs: a "
                "session blocked by safeguards, or out of quota on its model, "
                "now waits for you instead of being switched to another "
                "model (see Help)")
            self.settings.setValue("recovery_removed_noted", True)
        # Apply keep-awake state immediately if the user had it ON before.
        if self.keep_awake_check.isChecked():
            self._apply_keep_awake(True)
        # Resume watching automatically (also covers the relaunch after a
        # self-update — protection must not silently lapse).
        if self.autostart_check.isChecked() and 'AUTO_CONTINUE_UPDATE_RESUME' not in os.environ:
            self.sig_start.emit()

    def _save_settings(self) -> None:
        import json
        self._save_cli_settings()
        self.settings.setValue("interval", self.interval_spin.value())
        self.settings.setValue("buffer", self.buffer_spin.value())
        self.settings.setValue("retry_interval", self.retry_spin.value())
        self.settings.setValue("dry_run", self._dry_run_enabled)
        self.settings.setValue("keep_awake", self.keep_awake_check.isChecked())
        self.settings.setValue("autostart", self.autostart_check.isChecked())
        self.settings.setValue("excluded", list(self._excluded_titles))
        self.settings.setValue(
            "effort_overrides", json.dumps(self._effort_overrides)
        )
        self.settings.setValue(
            "model_overrides", json.dumps(self._model_overrides)
        )
        self.settings.setValue(
            "after_finish", json.dumps(self._after_finish)
        )
        self.settings.setValue(
            "after_finish_loops", json.dumps(self._after_finish_loops)
        )
        self.settings.setValue("auto_choose", self._auto_choose)
        self.settings.setValue("auto_permission", self._auto_permission)
        self.settings.setValue("scroll_back_min", self._scroll_back_min)
        self.settings.setValue(
            "trigger_patterns", json.dumps(self._trigger_patterns))

    # ---- Slots -----------------------------------------------------------

    @pyqtSlot()
    def _toggle_running(self) -> None:
        if self.start_btn.text() == "Start":
            self.sig_start.emit()
        else:
            self.sig_stop.emit()

    @pyqtSlot(bool)
    def _on_running_changed(self, running: bool) -> None:
        if running:
            self.status_text.setText("Running")
            self.start_btn.setText("Stop")
        else:
            self.status_text.setText("Stopped")
            self.start_btn.setText("Start")
        self._refresh_running_indicator()
        self._update_cli_controls()

    def _refresh_running_indicator(self) -> None:
        running = self.start_btn.text() == "Stop"
        color = (self._palette["dot_running"] if running
                 else self._palette["dot_stopped"])
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        # Prominent Start/Stop button — green to start, red to stop.
        bg = "#e03131" if running else "#2f9e44"
        hover = "#f03e3e" if running else "#37b24d"
        self.start_btn.setStyleSheet(
            "QPushButton {"
            f" background:{bg}; color:white; font-weight:bold;"
            " border:none; border-radius:5px; padding:6px 16px; }"
            f"QPushButton:hover {{ background:{hover}; }}"
            "QPushButton:disabled { background:palette(button); color:palette(mid); }")

    @pyqtSlot(list)
    def _on_snapshot(self, rows: list) -> None:
        self._latest_snapshot = rows
        # Subscribers hear about every pass, including the ones that change
        # nothing visible: a client rendering a live view wants the timestamp
        # moving, and "nothing changed" is itself the answer it is waiting for.
        self._api_publish("snapshot", self._api_snapshot())
        # Skip the full table rebuild when nothing user-visible changed —
        # rebuilding destroys the cell widgets, which closes any model/
        # effort dropdown the user has open and resets row selection.
        # (The countdown column is repainted by its own 1s timer.)
        sig = (self._view_provider, [(r.get("provider", "claude"), r["hwnd"], r["title"], r["status"], r["reset_utc"],
                r["last_sent_utc"], r["excluded"], r["model"], r["effort"],
                r.get("tabs", 1), r.get("thread_id"), r.get("cwd"), r.get("record_only", False),
                # Claude EXE shows Running/Idle from this flag; other types
                # leave it out so a turn boundary does not rebuild their rows.
                r.get("running") if r.get("provider") == "claude_app" else None)
               for r in rows])
        if sig == self._last_render_sig:
            self._refresh_activity_times()
            return
        if QApplication.activePopupWidget() is not None or any(
                combo.view().isVisible() or (combo.isEditable() and combo.lineEdit().hasFocus()
                                             and combo.lineEdit().isModified())
                for combo in self.table.findChildren(QComboBox)):
            return  # Preserve open menus and unfinished model-name edits.
        self._last_render_sig = sig
        self._render_table()

    @pyqtSlot(str, str)
    def _append_log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        log_file = os.environ.get('AUTO_CONTINUE_LOG_FILE')
        if log_file:
            try:
                with open(log_file, 'a', encoding='utf-8') as file:
                    file.write(f'{ts} [{level}] {message}\n')
            except OSError:
                pass
        # "fire-quiet" is a fire that skips the balloon; nothing downstream of
        # here should be able to tell the difference, so normalise it now and
        # keep the one difference in the one place it belongs.
        quiet = level == "fire-quiet"
        if quiet:
            level = "fire"
        # Keep a parallel buffer (capped to match the QPlainTextEdit's
        # 500-block limit) so a theme flip can re-render past lines in
        # the new color set.
        self._log_buffer.append((ts, level, message))
        if len(self._log_buffer) > 500:
            del self._log_buffer[: len(self._log_buffer) - 500]
        self._render_log_line(ts, level, message)
        self._write_log_file(ts, level, message)
        # `fire` is split out from `log` because it is the event a companion
        # program actually acts on — this tool just typed something, so the
        # window it was watching is about to change underneath it.
        self._api_publish("fire" if level == "fire" else "log",
                          {"level": level, "msg": message})
        # Tray notification on fire so the user notices even when minimized.
        if level == "fire" and not quiet and self.tray is not None:
            self.tray.showMessage(
                "Auto-Continue", message,
                QSystemTrayIcon.MessageIcon.Information, 4000,
            )

    def _write_log_file(self, ts: str, level: str, message: str) -> None:
        if not self._log_path:
            return
        try:
            try:
                if os.path.getsize(self._log_path) > 1_000_000:
                    old = self._log_path + ".old"
                    if os.path.exists(old):
                        os.remove(old)
                    os.replace(self._log_path, old)
            except OSError:
                pass
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now():%Y-%m-%d} {ts}  "
                         f"[{level}]  {message}\n")
        except OSError:
            pass

    def _render_log_line(self, ts: str, level: str, message: str) -> None:
        log_fg = self._palette["log_fg"]
        color = log_fg.get(level, log_fg["info"]).name()
        # Escape: window titles routinely contain '<'/'&' (the watcher's
        # own fallback title is literally '<hwnd N>'), which appendHtml
        # would otherwise parse as markup and silently strip.
        line = html.escape(f"{ts}  [{level}]  {message}")
        self.log_view.appendHtml(
            f'<span style="color:{color}">{line}</span>'
        )

    def _redraw_log_view(self) -> None:
        self.log_view.clear()
        for ts, level, msg in self._log_buffer:
            self._render_log_line(ts, level, msg)

    @pyqtSlot(Qt.ColorScheme)
    def _on_color_scheme_changed(self, scheme: "Qt.ColorScheme") -> None:
        self._palette = _make_palette(scheme)
        self._refresh_running_indicator()
        self._render_table()
        self._redraw_log_view()
        self._apply_banner_theme()

    # ---- Table rendering -------------------------------------------------

    def _render_table(self) -> None:
        rows = self._latest_snapshot
        selected_item = self.table.item(self.table.currentRow(), 0)
        selected_id = selected_item.data(Qt.ItemDataRole.UserRole) if selected_item else None
        # Qt defers deleting replaced cell widgets until the next event loop.
        # Hide them immediately so fast selection changes cannot leave ghosts.
        for old_row in range(self.table.rowCount()):
            for column in (5, 6, 7):
                widget = self.table.cellWidget(old_row, column)
                if widget:
                    widget.blockSignals(True)
                    if isinstance(widget, QComboBox) and widget.lineEdit():
                        widget.lineEdit().blockSignals(True)
                    widget.hide()
                    self.table.removeCellWidget(old_row, column)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        app_view = self._view_provider == "codex_app"
        self.table.setColumnHidden(2, app_view)
        self.table.setColumnHidden(3, app_view)
        self.table.horizontalHeaderItem(0).setText("Codex session / Window" if app_view else "Window")
        self.table.horizontalHeaderItem(4).setText("Last activity" if app_view else "Last sent")
        now = datetime.now(pytz.UTC)

        for r, row in enumerate(rows):
            self.table.setRowHeight(r, 30)
            tabs = row.get("tabs", 1)
            provider = row.get("provider", "claude")
            activity = bool(row.get("activity_row"))
            thread_id = row.get("thread_id", "")
            is_codex = provider in ("codex", "codex_app", "claude_app")
            if is_codex:
                codex_config = self._codex_row_config(row)
                options = self._codex_row_options(row)
                # Show settings edits immediately, without waiting for the
                # next Codex poll or changing the worker's snapshot object.
                row = dict(row,
                    model=options["model"], effort=options["effort"], excluded=options["excluded"])
            title_text = {"codex": "[Codex CLI] ", "codex_app": "[Codex EXE] ",
                          "claude_app": "[Claude EXE] "}.get(provider, "[Claude CLI] ") + row["title"]
            if activity:
                title_text = "[Codex] " + row["title"]
                if row.get("cwd"):
                    title_text += "\n" + row["cwd"]
                    self.table.setRowHeight(r, 46)
            if tabs > 1:
                title_text += f"   ⚠ {tabs} tabs"
            title_item = QTableWidgetItem(title_text)
            title_item.setData(Qt.ItemDataRole.UserRole,
                (provider, "session", thread_id) if activity else (provider, "window", row["hwnd"]))
            tip = f"hwnd={row['hwnd']}"
            if tabs > 1:
                tip += (f"\n⚠ This window has {tabs} tabs — only the ACTIVE"
                        f" tab is watched.\nWindows Terminal does not expose"
                        f" background tabs' content.\nDrag each Claude tab"
                        f" out into its own window for full coverage.")
            title_item.setToolTip(tip)
            if provider == "claude_app":
                title_item.setToolTip(tip + "\nThe session showing in this Claude window."
                    + f"\nSession ID: {thread_id or 'not matched yet'}"
                    + f"\nModel: {row.get('current_model', '') or 'unknown'}")
            elif is_codex:
                title_item.setToolTip(tip + "\nOnly the active tab is watched. Use a separate window per session."
                    + f"\nCurrent Codex: {row.get('current_model', '')} {row.get('current_effort', '')}")
            if activity:
                title_item.setToolTip(f"{row['title']}\n{row.get('cwd', '')}\nSession ID: {thread_id}"
                    + f"\nCurrent: {row.get('current_model', '')} {row.get('current_effort', '')}"
                    + (f"\nBound window: {row['hwnd']}" if row.get("hwnd") is not None else
                       "\nSettings saved per session. Open the session in Codex to apply them before its next continue."))
            self.table.setItem(r, 0, title_item)

            status_item = QTableWidgetItem(
                "Excluded" if is_codex and row["excluded"]
                else ACTIVITY_LABELS[row["status"]] if activity and row["status"] in ACTIVITY_LABELS
                else "Waiting for input" if is_codex and row["status"] == ST_PROMPT
                else "Usage limit · App retrying" if is_codex and row["status"] == "quota_busy"
                else "App retrying" if is_codex and row["status"] == "retry_busy"
                else "Waiting for Send" if is_codex and row["status"] == "waiting_send"
                else "Confirming submission" if is_codex and row["status"] == "confirming_send"
                else "Running" if provider == "claude_app" and row["status"] == ST_IDLE and row.get("running")
                else STATUS_LABEL.get(row["status"], row["status"])
            )
            bg = self._palette["status_bg"].get(row["status"])
            if bg is not None:
                status_item.setBackground(bg)
                # Pair fg with bg so text stays readable on whichever
                # theme we're tracking (Qt's default text color would
                # otherwise collide with our explicit cell bg).
                status_item.setForeground(self._palette["status_fg"])
            self.table.setItem(r, 1, status_item)

            self.table.setItem(r, 2, QTableWidgetItem(
                _fmt_local(row["reset_utc"])
            ))
            self.table.setItem(r, 3, QTableWidgetItem(
                _fmt_countdown(row["reset_utc"], now)
            ))
            self.table.setItem(r, 4, QTableWidgetItem(
                self._activity_time(row) if app_view else _fmt_local(row["last_sent_utc"])
            ))

            # Model dropdown — sent as `/model <name>` right before the
            # effort/continue sequence. "(none)" means skip /model and leave
            # the session on whatever model it's currently using.
            # EDITABLE: the listed names are conveniences, not a whitelist.
            # Anthropic ships new model families faster than this tool ships
            # builds — Opus 4.8 retired and Opus 5 arrived inside its lifetime
            # — so a fixed list would mean waiting for a release just to
            # select a family that already exists. Whatever is typed is passed
            # to /model verbatim, so a future alias works with no new build.
            model_combo = QComboBox()
            model_combo.setEditable(True)
            model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            model_levels = MODEL_LEVELS
            if is_codex:
                from codex_gui import MODEL_LEVELS as codex_models
                model_levels = codex_models
            for level in model_levels:
                model_combo.addItem((level or ("Keep current" if thread_id else "(none)")) if is_codex else MODEL_LABEL[level], userData=level)
            current_m = row.get("model", "")
            try:
                idx_m = model_levels.index(current_m)
                model_combo.setCurrentIndex(idx_m)
            except ValueError:
                # A custom name the user typed earlier — show it as-is.
                model_combo.setCurrentIndex(0)
                model_combo.setEditText(current_m)
            model_combo.setToolTip(
                "Auto-prefix the next continue with `/model <name>`. "
                "Pick from the list, or TYPE a name the list doesn't have yet "
                "(a newer model family). (none) leaves the session's model "
                "unchanged. Setting persists per window across restarts."
            )
            if is_codex:
                model_combo.setToolTip(f"Current: {row.get('current_model') or 'unknown'}. Select or type a model. Saved for this session; applied before continuing when its window is bound.")

            def _model_picked(_i=None, t=row["title"], cb=model_combo, cx=is_codex, cp=provider, sid=thread_id, saved=current_m):
                # Editable combos retain the old item data while typing.
                text = cb.currentText().strip()
                data = cb.currentData()
                value = data if data is not None and text == cb.itemText(cb.currentIndex()) else text
                # Opening the popup can finish line editing. Rebuilding an
                # unchanged row here would immediately destroy that popup.
                if value == saved:
                    return
                if cx:
                    self._on_codex_override(t, "model", value, cp, sid)
                else:
                    self._on_model_changed(t, value)

            model_combo.currentIndexChanged.connect(_model_picked)
            model_combo.lineEdit().editingFinished.connect(_model_picked)
            if provider == "claude_app":
                # Not switched in the desktop app by this tool; the cell shows
                # the session's own model instead.
                model_combo.blockSignals(True)
                model_combo.setEditText(row.get("current_model", "") or "(app)")
                model_combo.blockSignals(False)
                model_combo.setEnabled(False)
                model_combo.setToolTip("Claude EXE: the model is not changed by Auto-Continue. Shown: the session's current model.")
            self.table.setCellWidget(r, 5, model_combo)

            # Effort dropdown — sent as `/effort <level>` right before
            # `continue`. "(none)" means skip /effort and just send continue.
            effort_combo = QComboBox()
            effort_levels = EFFORT_LEVELS
            if is_codex:
                from codex_gui import EFFORT_LEVELS as codex_efforts
                effort_levels = codex_efforts
            for level in effort_levels:
                effort_combo.addItem((level or "(none)") if is_codex else EFFORT_LABEL[level], userData=level)
            current = row.get("effort", "")
            try:
                idx = effort_levels.index(current)
            except ValueError:
                idx = 0
            effort_combo.setCurrentIndex(idx)
            effort_combo.setToolTip(
                "Auto-prefix the next continue with `/effort <level>`. "
                "Setting persists per window across restarts."
            )
            if is_codex:
                effort_combo.setToolTip("Saved for this Codex session; applied before continuing when its window is bound. (none) keeps the current effort.")
            effort_combo.currentIndexChanged.connect(
                lambda _i, t=row["title"], cb=effort_combo, cx=is_codex, cp=provider, sid=thread_id:
                self._on_codex_override(t, "effort", cb.currentData(), cp, sid) if cx else
                self._on_effort_changed(t, cb.currentData())
            )
            if provider == "claude_app":
                effort_combo.setEnabled(False)
                effort_combo.setToolTip("Claude EXE: effort is not changed by Auto-Continue.")
            self.table.setCellWidget(r, 6, effort_combo)

            # Action buttons
            btn_widget = QWidget()
            hl = QHBoxLayout(btn_widget)
            hl.setContentsMargins(2, 0, 2, 0)
            hl.setSpacing(4)

            if row["excluded"]:
                un_btn = QPushButton("Include")
                un_btn.setToolTip("Resume watching this window")
                un_btn.clicked.connect(
                    lambda _, rr=row: self._cli_action(rr, "include")
                )
                hl.addWidget(un_btn)
            else:
                bound = row.get("hwnd") is not None and not row.get("record_only")
                now_btn = QPushButton("Now" if bound else "Open")
                now_btn.setToolTip("Send 'continue' immediately" if bound else "Open this session in Codex. Its settings are already saved.")
                now_btn.clicked.connect(
                    lambda _, rr=row, action="fire" if bound else "open": self._cli_action(rr, action)
                )
                hl.addWidget(now_btn)

                more = QToolButton()
                more.setText("Actions")
                more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                menu = QMenu(more)
                skip = menu.addAction("Skip pending continue")
                skip.setEnabled(bound and row["status"] == ST_PENDING)
                skip.triggered.connect(lambda _, rr=row: self._cli_action(rr, "skip"))
                exclude = menu.addAction("Exclude session" if thread_id else "Exclude window")
                exclude.triggered.connect(lambda _, rr=row: self._cli_action(rr, "exclude"))
                if thread_id and not bound and provider == "codex_app":
                    link = menu.addAction("Link visible Codex window")
                    link.setEnabled(self._cli_running["codex_app"] and len(self._codex_link_candidates(row)) == 1)
                    link.setToolTip("For identical titles: open this exact session first, then link its visible window to this session ID.")
                    link.triggered.connect(lambda _, rr=row: self._cli_action(rr, "link"))
                menu.addSeparator()

                # Keep the follow-up state visible without widening the table.
                _af_key = row["title"] if is_codex else title_key(row["title"])
                _af_cur = options["after_finish"] if is_codex else self._after_finish.get(_af_key, "")
                try:
                    _af_left = options["loops"] if is_codex else int(self._after_finish_loops.get(_af_key, 1))
                except (TypeError, ValueError):
                    _af_left = 1
                if not _af_cur:
                    _af_label = "After finish…"
                elif _af_left < 0:
                    _af_label = "After finish ∞"
                elif _af_left == 0:
                    _af_label = "After finish ✓ (0 left)"
                else:
                    _af_label = f"After finish ✓ ({_af_left} left)"
                af_action = menu.addAction(_af_label)
                af_tip = (
                    ("When this session finishes a run and sits idle, type "
                     "this prompt so it keeps working"
                     + (" (unlimited runs)" if _af_left < 0
                        else f" ({_af_left} run(s) left)")
                     + ":\n\n" + _af_cur)
                    if _af_cur else
                    "Set a prompt to type automatically when this session "
                    "finishes its current run and sits idle. Empty = off.")
                af_action.setToolTip(af_tip)
                more.setToolTip(af_tip)
                more.setText("Actions ✓" if _af_cur and _af_left else "Actions")
                af_action.triggered.connect(
                    lambda _, rr=row: self._cli_action(rr, "after_finish")
                )

                # Only relevant during cooldown — clears the suppression so
                # the next tick re-detects (useful for testing or when you
                # want to immediately catch a new limit hit after a fire).
                if row["status"] == ST_COOLDOWN:
                    cd_action = menu.addAction("Clear cooldown")
                    cd_action.setToolTip(
                        "Forget the recent send so detection resumes now."
                    )
                    cd_action.triggered.connect(
                        lambda _, rr=row: self._cli_action(rr, "cooldown")
                    )
                more.setMenu(menu)
                hl.addWidget(more)

            for button in btn_widget.findChildren(QPushButton):
                button.setMinimumWidth(0)
                button.setMaximumWidth(60)

            hl.addStretch()
            self.table.setCellWidget(r, 7, btn_widget)

        if selected_id is not None:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) == selected_id:
                    self.table.selectRow(r)
                    break

    @staticmethod
    def _activity_time(row) -> str:
        stamp = row.get("updated_utc") if row.get("activity_row") else row.get("read_utc") or row.get("last_sent_utc")
        return stamp.astimezone().strftime("%m-%d %H:%M:%S") if stamp else "—"

    def _refresh_activity_times(self) -> None:
        if self._view_provider != "codex_app":
            return
        for r, row in enumerate(self._latest_snapshot):
            item = self.table.item(r, 4)
            if item:
                item.setText(self._activity_time(row))

    def _refresh_countdowns(self) -> None:
        # Lightweight repaint of column 3 only — avoids rebuilding action
        # widgets every second.
        rows = self._latest_snapshot
        if not rows:
            return
        now = datetime.now(pytz.UTC)
        for r, row in enumerate(rows):
            if r >= self.table.rowCount():
                break
            self.table.setItem(r, 3, QTableWidgetItem(
                _fmt_countdown(row["reset_utc"], now)
            ))

    # ---- Actions ---------------------------------------------------------

    def _do_exclude(self, hwnd: int, title: str) -> None:
        key = title_key(title)
        if key and key not in self._excluded_titles:
            self._excluded_titles.append(key)
            self.sig_exclude.emit(hwnd, title)
            self.sig_set_excluded.emit(list(self._excluded_titles))
            self._save_settings()
            self._append_log("info", f"excluded {key!r}")

    def _on_effort_changed(self, title: str, level: str) -> None:
        key = title_key(title)
        # Empty/none → remove the override entirely so the dict stays small.
        if level:
            self._effort_overrides[key] = level
        else:
            self._effort_overrides.pop(key, None)
        self.sig_set_effort_overrides.emit(dict(self._effort_overrides))
        self._save_settings()
        self._append_log(
            "info",
            f"effort for {title!r} set to {level or '(none)'!r}"
        )

    def _on_model_changed(self, title: str, name: str) -> None:
        key = title_key(title)
        if name:
            self._model_overrides[key] = name
        else:
            self._model_overrides.pop(key, None)
        self.sig_set_model_overrides.emit(dict(self._model_overrides))
        self._save_settings()
        self._append_log(
            "info",
            f"model for {title!r} set to {name or '(none)'!r}"
        )

    def _on_after_finish_clicked(self, title: str) -> None:
        key = title_key(title)
        cur = self._after_finish.get(key, "")
        try:
            cur_loops = max(-1, int(self._after_finish_loops.get(key, 1)))
        except (TypeError, ValueError):
            cur_loops = 1
        dlg = QDialog(self)
        dlg.setWindowTitle("After finish")
        lay = QVBoxLayout(dlg)
        hint = QLabel(
            f"Prompt to type into {key!r} when it finishes a run and sits "
            f"idle. Empty = off.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        edit = QPlainTextEdit(cur)
        lay.addWidget(edit, 1)
        form = QFormLayout()
        spin = QSpinBox()
        # -1 is the minimum, so specialValueText labels it: QSpinBox only
        # substitutes text for the minimum value.
        spin.setRange(-1, 999)
        spin.setSpecialValueText("unlimited")
        spin.setValue(cur_loops)
        spin.setToolTip(
            "Runs REMAINING for this prompt. Each firing spends one, and "
            "the new value is saved immediately — so a restart cannot "
            "re-arm a prompt that is already used up, and the window's "
            "next task never inherits the previous task's follow-up. "
            "0 = spent (set it again to re-arm); unlimited = repeat after "
            "every completed run.")
        form.addRow("Loops (runs left)", spin)
        lay.addLayout(form)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        dlg.resize(480, 300)
        self._pending_loops.pop(key, None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = edit.toPlainText().strip()
        loops = spin.value()
        # A run spent while this dialog was open wins over the number the
        # spinner snapshotted before it opened — otherwise clicking OK after
        # only editing the prompt text silently re-arms a spent budget, which
        # is the repeat-firing this feature exists to prevent. Touching the
        # spinner is an explicit instruction and still wins.
        loops = reconcile_loops(loops, cur_loops,
                                self._pending_loops.pop(key, None))
        # A prompt that was edited but left at 0 runs would silently never
        # fire. Typing a new prompt is asking for it to run, so give it one.
        if text and text != cur and loops == 0:
            loops = 1
        if text:
            self._after_finish[key] = text
            self._after_finish_loops[key] = loops
        else:
            self._after_finish.pop(key, None)
            self._after_finish_loops.pop(key, None)
        self.sig_set_after_finish.emit(dict(self._after_finish))
        self.sig_set_after_finish_loops.emit(dict(self._after_finish_loops))
        self._save_settings()
        self._append_log(
            "info",
            f"after-finish for {key!r} "
            + (f"set to {text!r} ("
               + ("unlimited runs" if loops < 0 else f"{loops} run(s) left")
               + ")" if text else "cleared"))
        self._render_table()      # refresh the button's remaining count

    def _do_unexclude(self, title: str) -> None:
        key = title_key(title)
        if key in self._excluded_titles:
            self._excluded_titles.remove(key)
            self.sig_unexclude.emit(title)
            self.sig_set_excluded.emit(list(self._excluded_titles))
            self._save_settings()
            self._append_log("info", f"un-excluded {key!r}")

    @pyqtSlot(bool)
    def _on_boot_toggled(self, on: bool) -> None:
        # Registry write can fail (locked-down policy, AV); if it does, log it
        # and snap the checkbox back to the registry's real state so the UI
        # never claims a startup entry exists when it doesn't.
        try:
            set_autostart(on)
        except OSError as e:
            self._append_log(
                "err", f"couldn't update Windows startup entry: {e}")
            self.boot_check.blockSignals(True)
            self.boot_check.setChecked(autostart_enabled())
            self.boot_check.blockSignals(False)
            return
        self._append_log(
            "info",
            "will start with Windows, minimized to the tray" if on
            else "will no longer start with Windows")

    # ---- Keep-awake (SetThreadExecutionState) ---------------------------

    # When ON we register CONTINUOUS | SYSTEM_REQUIRED | AWAYMODE_REQUIRED.
    # AWAYMODE_REQUIRED is the key piece that asks Windows to keep the
    # system in S0 working state even when the user invokes sleep — without
    # it, Modern Standby still kicks in and kills our background python.
    # When OFF we re-register only CONTINUOUS (which clears the previous
    # request, returning the system to normal sleep behavior).
    def _apply_keep_awake(self, on: bool) -> None:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040
        try:
            if on:
                flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                         | ES_AWAYMODE_REQUIRED)
                rc = ctypes.windll.kernel32.SetThreadExecutionState(flags)
                if rc:
                    self._append_log(
                        "info",
                        "keep-awake ON — system will not enter sleep / "
                        "Modern Standby while this GUI runs"
                    )
                else:
                    self._append_log(
                        "warn",
                        "keep-awake request was rejected by the OS"
                    )
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
                self._append_log("info",
                                 "keep-awake OFF — normal sleep allowed")
        except Exception as e:
            self._append_log("err", f"keep-awake call failed: {e}")

    # ---- Lifecycle -------------------------------------------------------

    def _stop_local_api(self) -> None:
        """Close the socket and take the token file down with it.

        A discovery file outliving the process points a client at a port that
        is not listening — it would read as a broken server rather than a
        closed one, and it would leave a live token on disk for a service that
        is not there to honour it.
        """
        try:
            self._local_api.stop()
            local_api.clear_discovery()
        except Exception:
            pass

    def _shutdown_thread(self, thread: QThread) -> None:
        """quit() + generous wait; terminate as a last resort. Destroying a
        QThread object that is still running is qFatal (crash on quit) — a
        tick mid-UIA-scan or mid-send can easily outlive a short wait."""
        try:
            thread.quit()
            if not thread.wait(8000):
                thread.terminate()
                thread.wait(2000)
        except Exception:
            pass

    def closeEvent(self, event) -> None:
        # X button = full quit (per user preference). Tray-only background
        # mode is reached via the minimize button instead.
        self._save_settings()
        self._stop_local_api()
        if hasattr(self, "codex_service"):
            self.codex_service.close()
        # Release the wake-lock so the system can sleep normally after we
        # quit. SetThreadExecutionState requests are process-scoped — the
        # OS clears them on process exit too, but doing it explicitly here
        # is cheap insurance.
        if self.keep_awake_check.isChecked():
            self._apply_keep_awake(False)
        try:
            self.sig_stop.emit()
        except Exception:
            pass
        if self._show_listener is not None:
            self._show_listener.stop()
            self._show_listener.wait(1000)
        self._shutdown_thread(self.worker_thread)
        self._shutdown_thread(self.updater_thread)
        super().closeEvent(event)
        # setQuitOnLastWindowClosed is False (so the minimize-to-tray case
        # doesn't kill the app), so the X button path needs to ask the app
        # to quit explicitly.
        QApplication.instance().quit()


# ---------------------------------------------------------------------------
# In-app help
# ---------------------------------------------------------------------------
# Written in English at the user's request. Kept in the app rather than only
# in the README: the moment you need it is when a session is wedged and you
# are not about to go browsing GitHub.
HELP_HTML = """
<h2>Auto-Continue v{version}</h2>
<p>A watchdog for Claude CLI, Claude EXE (the Claude desktop app), Codex EXE
(the Codex desktop App), and Codex CLI. Select the types you want to monitor;
each type has its own settings and driver. It detects stalled sessions and sends the configured continuation
when the session is ready.</p>
<p>It never invents work. Every keystroke it sends is either something you
configured or the single word <code>continue</code>, and it only sends it
when the screen says the session is stuck.</p>

<h3>Getting started</h3>
<ol>
<li>Switch between the <b>Codex EXE</b>, <b>Codex CLI</b>, <b>Claude CLI</b> and
<b>Claude EXE</b> tabs to view each type's settings and sessions. Enable <b>Monitor this type</b>
on the types you want to watch. Switching tabs preserves background monitoring.
Poll, Buffer and Retry belong to each type separately. <b>App settings</b>
holds shared start-up and keep-awake options; <b>More</b> holds Help,
Export log and Check updates.</li>
<li>Leave your Claude Code sessions running in Windows Terminal. Each
<b>window</b> is watched separately. Background <i>tabs</i> are invisible to
Windows Terminal's accessibility layer, so a window with several tabs is
marked <b>&#9888; N tabs</b> and only its active tab is watched — drag tabs
out into their own windows for full coverage.</li>
<li>Press <b>Start</b>. The dot turns green and the table fills with windows
for the current tab. The Codex EXE tab shows recent local Codex sessions, including
background Codex sessions, in the same table as live Codex windows. The card's
<b>Recent activities</b> setting controls the count (default 5).
Only Codex sessions are monitored; ChatGPT and ChatGPT Work conversations are
excluded. Automatic continue acts on the visible Codex session in each App window.</li>
<li>That's it. Leave it running. Nothing is typed until a session actually
needs it.</li>
</ol>
<h3>What it reacts to</h3>
<ul>
<li><b>The 5-hour usage limit.</b> Reads the reset time from the banner,
waits until it passes (plus the <b>Buffer</b>, 60s by default, because the
limit often clears a little after the stated minute), then types
<code>continue</code> — but not into a turn that is already streaming. If you
resumed the session yourself just before the reset, the due
<code>continue</code> is held until the session goes quiet rather than being
queued behind the running turn and starting an extra one; the pending is kept,
not cancelled. If the newer <i>"What do you want to do?"</i> chooser
appears, <b>nothing is pressed</b> — that chooser's other option is
<i>paid extra usage</i>, and Enter takes whatever is highlighted, so
answering it is a decision about money that no screen scrape should make.
The window is marked <b>&#9166; Limit prompt</b> and waits for you. The
countdown is unaffected: the reset time is read from the banner, not from
the chooser. A banner found <i>after</i> its stated time has already gone by
— an unwatched window, or a version that could not yet read that wording —
is treated as a limit that has lifted, and resumed straight away. Plain
"next occurrence" arithmetic would otherwise read 9:30pm at 9:51pm as
<i>tomorrow</i> and park the session for a day.</li>
<li><b>Network stalls.</b> Retry-exhausted banners (<i>attempt N/N</i>) and
bare API connection errors get one <code>continue</code> every <b>Retry
interval</b> (10 minutes by default) until the connection comes back. A
connection error is recognised by the <b>error code</b> it carries — the
upper-case name in parentheses, such as <code>ECONNRESET</code> or
<code>ETIMEDOUT</code> — not by the sentence in front of it, because that
sentence keeps being reworded (three times in two weeks, and each rewording
left sessions sitting idle until the pattern caught up). Recovery is
recognised two ways: the session caught mid-turn, or a whole turn — your
prompt, then its <i>done</i> stamp — sitting below the error. The second
matters for a session that is only waiting on background work: it answers a
<code>continue</code> in seconds, between two checks, and used to be poked
again ten minutes later for nothing.
Crucially, only while the session is <i>actually</i> stuck: once it is
streaming again, the error still on screen is just scrollback, and poking it
would queue prompts that all fire at once when the turn ends. HTTP 500/529
errors are left alone — Claude Code retries those itself. Recovery has to be
<i>seen</i>, too: a pass that could not read the window at all looks the same
as a window with no error on it, and treating that as recovery used to throw
away the retry throttle, so the next pass that did read the error poked
immediately instead of waiting out the interval.</li>
<li><b>Truncated responses.</b> A reply cut off mid-stream is resumed with
<code>continue</code>.</li>
<li><b>Dead sessions.</b> An expired login cannot be fixed by typing, so it
is reported once in the log and never poked at.</li>
<li><b>Safeguard blocks</b> — left alone. When a model's safeguards block a
turn, nothing this tool could type would help: <code>continue</code> sends the
same conversation back to the same model. The session waits for you, and
<b>After finish</b> does not count it as finished. A model's own allowance
running out is yours to handle in the same way: it carries no reset time, so
there is nothing to wait for and nothing is typed.</li>
</ul>
<p><b>Model recovery was removed in 3.2.0.</b> Earlier versions had an opt-in
script for safeguard blocks: switch to a fallback model, let it finish,
<code>/compact</code>, switch back. It could also move a window to another
model when one model's allowance ran out, and steer a window back to its
target model on its own. It typed far more than <code>continue</code>,
depended on Claude Code's dialogs and status bar staying exactly as they were,
and needed a dozen timers and guards to stay safe — more machinery than the
problem it solved, in the one tool that must not misfire into your sessions.
It has been taken out, so a session that is merely waiting is resumed, and a
session that needs a decision is left for you. If you had it switched on, the
log says so once on the first start; its saved settings are left unused.</p>
<p>Everything above is matched with regexes you can edit
(<b>Claude CLI &#8594; More settings &#8594; Triggers</b>), because Anthropic re-words these banners
without notice. Those patterns track the wording of a <i>current</i> Claude
Code, and only that: spellings retired by Anthropic — the pre-rename
<code>/extra-usage</code>, the old "esc to interrupt" spinner — have been
dropped rather than carried forever, since Claude Code updates itself and a
pattern kept for builds nobody runs is dead weight that still has to be read
and reasoned about. If you deliberately pin an old build and a banner stops
being noticed, add its wording back under <b>Triggers</b>.</p>
<p><b>A network error quoted in the middle of a sentence is not one.</b>
Claude Code prints its error banners on a line of their own; the same words
inside a sentence are someone quoting them — usually Claude, explaining an
error you pasted. Those used to count, so a session that had simply
<i>discussed</i> a failure was poked with <code>continue</code> every retry
interval. They no longer do. The one gap left: prose that happens to wrap so
the quoted words start a fresh row still looks like the real thing.</p>
<p><b>It never types over a draft or into an open question.</b> Every line
this tool types ends with Enter. If the input box already holds text you have
not sent, the line would go after your text and both would be submitted as
one message. If a question or a permission prompt is open, the Enter would
pick whichever option is highlighted, which on a permission prompt is
"Yes". So a network <code>continue</code>, a limit resume and an After
finish prompt all wait while either is on screen, and go out once the box is
clear. The log says so once each time. This holds
wherever the prompt's cursor sits, even after the arrow keys have moved it
off the first option. <b>Fire now</b> is your own instruction and is not
held. Before 2.1.4, After finish in particular could answer a permission
prompt you had left for yourself.</p>
<p><b>A suggested prompt is not a draft.</b> After a turn Claude Code may show
a suggestion for your next message in the input box, in dim text (Tab takes
it; typing replaces it; Enter on its own sends nothing). It only looks like
text you typed, so until 3.3.2 it held every send exactly like a real draft.
Now the input box is read with its colours and cursor: text dimmer than the
box's prompt sign, with the cursor in front of it, is the suggestion and
counts as an empty box. Your own text is drawn in the normal colour, with the
cursor after it or wherever you moved it, and still holds everything. When
the colours or the cursor cannot be read, the text counts as a draft, as
before. The suggestion is also left out of the screen text a companion
program reads.</p>
<p><b>Each typed line is checked on the screen.</b> Windows delivers typed keys
to whichever window is in front when it gets to them, which on a busy machine
can be a moment after they were sent. Before 3.3.2 the window you were using
was put back in front straight after typing, and the last word of a long
line and its Enter could go to that window instead, while the send still
counted as done. Now each line is typed in short pieces, stopping if another
window takes the foreground. Enter is pressed only once the whole line shows
in the input box, and your window comes back only after Enter has taken the
line out of the box. When the screen does not show that, the send counts as
failed and nothing more is pressed. A line still sitting in the box then holds
the next attempt, as any draft does.</p>

<h3>Claude EXE (the Claude desktop app)</h3>
<p><b>Off by default.</b> Turn on <b>Monitor this type</b> on the Claude EXE tab.
It watches Claude Code sessions in the Claude desktop app &mdash; the session
showing in each Claude window, like Codex EXE does; sessions in the background
are not touched.</p>
<p><b>Which session it is looking at.</b> The window's title is always just
"Claude", but the page inside it carries the session's title, and Claude Code
records the same title in that session's own history file
(<code>%USERPROFILE%\\.claude\\projects</code>). Nothing is done until that
title matches exactly one desktop session. Two sessions with the same title,
more than one session pane open in one window (split view), or no matching
history: the window is left alone and the log says why, once. Renaming one of
two same-named sessions is enough.</p>
<p><b>What it reads where.</b> Whether a turn is running and whether the prompt
box holds anything come from the window. Usage limits, connection errors and
the end of a turn come from the session's history, where Claude Code marks its
own API errors &mdash; so a session that only <i>talks about</i> an error is not
mistaken for one stuck on it.</p>
<p><b>What it does.</b></p>
<ul>
<li>A usage limit with a reset time: waits until that time plus <b>Buffer</b>,
then sends <code>continue</code>. The desktop app may resume by itself at that
moment ("continuing automatically at &hellip;"); the Buffer lets it go first,
and a session that is already running again is left alone.</li>
<li>A connection error: one <code>continue</code> every <b>Retry</b> interval,
at most <b>Maximum retries</b> times (More settings).</li>
<li><b>After finish</b>: that session's prompt, once per completed turn, for as
many runs as its Loops allow.</li>
<li>A safeguard block, a model's own weekly limit, or a login problem: nothing
is typed &mdash; it cannot help &mdash; and the row shows <b>Needs
attention</b>.</li>
</ul>
<p><b>How it types.</b> It writes the line into the prompt box and presses the
app's own Send button: no simulated key presses, no clicking by screen
position. It only does so while the box is empty, no turn is running and the
window still shows the same session. A send counts once the session's history
records the new prompt; if the Send button cannot be found, the text it wrote
is removed again rather than left behind as a draft.</p>
<p><b>Not done in this version.</b> Permission prompts and questions are left
for you, and model and effort are not changed; the Model column shows the
session's current model.</p>

<h3>The table</h3>
<p>The shared table shows sessions for the current tab. Recent Codex EXE sessions
can each have their own Model, Effort, Exclude and After finish settings,
saved by session ID. A matched live window is merged into its session row.
<b>Open</b> opens a background session; <b>Now</b> continues a bound live window.
For identical titles, open the exact session and use <b>Actions → Link visible
Codex window</b> to bind it explicitly. A changed visible conversation clears
that binding.
<b>Now</b> is the primary window action;
<b>Actions</b> holds Skip, Exclude, After finish and Clear cooldown.
The activity log stays visible at the bottom; drag the divider to resize it.
The <b>Status</b> column is the quickest way
to see what the tool thinks is going on:</p>
<ul>
<li><b>Idle</b> — watched, nothing wrong.</li>
<li><b>Waiting</b> — a limit was detected; the countdown shows how long
until it fires.</li>
<li><b>Sending</b> / <b>Sent</b> — typing right now / just typed.</li>
<li><b>Cooldown</b> — sent recently; detection is suppressed for 15 minutes
so the message still in scrollback can't trigger a second fire.</li>
<li><b>Limit prompt</b> — confirming the limit chooser, or answering
another chooser the session was waiting on.</li>
<li><b>Excluded</b> — you told it to ignore this window.</li>
</ul>
<p>Per-row controls:</p>
<ul>
<li><b>Model</b> and <b>Effort</b> — optional. When set, the fire sequence
becomes <code>/model &#8230;</code> &#8594; <code>/effort &#8230;</code>
&#8594; <code>continue</code> instead of a bare <code>continue</code>, so a
session resumes on the model and effort you want rather than whatever it was
left on. The model box is <i>editable</i>: type a name the list doesn't have
yet and it is passed through verbatim, so a newly released model works
without waiting for a new build. <code>(none)</code> means "leave the session
on whatever it is using".</li>
<li><b>After finish&#8230;</b> — a prompt typed automatically when that
session finishes a run and sits idle, so the window keeps producing instead
of waiting for you. Details below.</li>
<li><b>Now</b> fires immediately (bypasses the countdown, still respects the
row's model/effort) &#183; <b>Skip</b> cancels a pending continue &#183;
<b>Exclude</b> stops watching that window for good &#183; <b>Clear
cooldown</b> lifts the 15-minute suppression so the next tick can
re-detect.</li>
</ul>

<h4>After finish — give a window its next task</h4>
<p>Click <b>After finish&#8230;</b> on a row and type what that session
should do when it runs out of work. When the session finishes a run and stays
idle for 90 seconds, the prompt is typed for you.</p>
<p><b>Loops (runs left)</b> in the same dialog is a countdown, default
<b>1</b>: each firing spends one and the new value is saved immediately. At
<b>0</b> the prompt is done and nothing more is typed — it stays visible so
you can see what ran, and setting Loops again (or editing the prompt, which
bumps a spent one back to 1) re-arms it. <b>unlimited</b> never counts down
and repeats after every completed run.</p>
<p>Counting down rather than up is what makes this survive a restart: an
in-memory tally reset on every launch — including a self-update — so a
prompt that had already done its one run got typed a second time, into
whatever task the window had moved on to. The saved number cannot do
that.</p>
<p>A run is only spent when the prompt is actually typed, and the window must
then be seen running <i>and</i> finishing again before anything else is
typed — the prompt is never repeated into a session that is merely
sitting idle.</p>
<p><b>A turn is recognised as running by its spinner line's shape</b> — the
spinner glyph, a verb, an ellipsis — whether or not a timer is showing yet.
Claude Code's timer comes and goes: a turn's first seconds show none, and a
turn still waiting for its first byte shows only the elapsed time, with none of
the usual detail after it. Reading the timer instead of the shape took both
for finished turns, and a session waiting on a slow API got
<code>continue</code> queued into it every retry interval.</p>
<p><b>Waiting on the API counts as running.</b> When a request gets no first
byte for a while, Claude Code replaces its spinner with <i>No response from the
API after 6m · retrying once, waiting up to 10m</i> and keeps waiting on its
own. That line has no timer on it, and earlier builds read it as a finished
turn — so after 90 seconds the next task was typed into a request that was
still in flight. It is now read as running, by this and by everything else
that waits for a quiet window. If the retry fails too and Claude Code gives
up with an <code>API Error</code> line saying no response came back, that is a
stuck session like any other network error, and gets its
<code>continue</code>.</p>
<p>The Actions menu shows the After finish prompt and remaining loops;
hovering Actions previews the prompt. Clearing the text turns it off for that
window.</p>
<p>Three idle-looking states deliberately never count as "finished":</p>
<ul>
<li>a session never seen running while watched — a fresh shell sitting at a
prompt is not a completed run;</li>
<li>a standing safeguard notice — that session is <i>blocked</i>, not
finished, and the prompt would only be refused again;</li>
<li>the post-send cooldown — the fire's own follow-up is still in
flight.</li>
</ul>

<h3>Claude CLI &#8594; More settings &#8594; Answering</h3>
<p><b>What it is for.</b> Claude Code sometimes stops and asks. A chooser
appears, the session waits, and nothing else happens until someone picks —
which, if you stepped away, means the window is idle for the rest of the
night for no better reason than an unanswered question. These two switches
answer it for you.</p>
<p>Both press a single <b>Enter</b>, which accepts the option the chooser
already has selected (its first one). Nothing types a number: if the
detection were ever wrong, a bare Enter on an empty prompt does nothing,
whereas a stray "1" would be sent to Claude as a message.</p>
<p>Three conditions must hold before either fires, and together they are what
make a false positive harmless:</p>
<ul>
<li>the turn is <b>not running</b> — there is nothing to answer mid-stream;</li>
<li>you have <b>nothing half-typed</b> — Enter with a draft in the input box
would submit that draft. A chooser that has replaced the input box entirely
(most of them do) counts as nothing typed, which is the normal case;</li>
<li>the chooser is one we have <b>not already answered</b> — its text stays
on screen afterwards, so "it still matches" is not evidence it is still
open. One chooser, one Enter.</li>
<li>the chooser is genuinely <b>open</b>. One that is waiting has taken the
input box away; if a box is showing beneath it, what you are looking at is
a chooser <i>quoted</i> in the conversation, or one that was answered a
while ago. Ask Claude to explain a dialog and it will print one — that must
not be answered, and is not.</li>
</ul>
<p>Detection deliberately errs towards matching. A wrong match presses Enter
on an idle session whose input box is empty, where Enter does nothing; a
missed one leaves the session sitting on the question all night, which is
the thing you turned this on to avoid.</p>
<p>Note the flip side of that: while this is on, <b>quitting Auto-Continue
leaves any session that is waiting on a chooser waiting</b> — unlike a rate
limit, which resumes by itself once the clock passes.</p>
<p><b>Answer choosers</b> (<b>off</b> by default) handles ordinary
questions. It ships off because this is the switch that DECIDES things —
which option the work takes, and whether a written plan starts running —
where the other one only says yes to work already asked for. Turn it on
when you want a session to keep going without you. The one this matters most for is the prompt that ends
<b>plan mode</b> — the plan is written and nothing runs until someone
approves it — so with this on, an unattended session carries straight on
into the work. Dialogs drawn inside a box are recognised the same as plain
ones.</p>
<p><b>Know what Enter takes there.</b> That prompt's first option does not
only approve the plan — it also sets the session's permission level, back
to whatever it was started with. Captured live: a session started plainly
offered <i>Yes, and use auto mode</i> and was in auto mode afterwards; a
session started with permissions bypassed offered <i>Yes, and bypass
permissions</i> and returned to bypass. So if you start your sessions
bypassed, approving a plan this way puts it straight back to bypassed and
begins the work — unattended, which is usually the point.</p>
<p>If you would rather approve plans yourself, turn <b>Answer choosers</b>
off — the plan prompt rides on that switch, not on the permission one,
because it is a chooser like any other until you read what its first option
says.</p>
<p><b>Answer tool-permission requests</b> (on by default) handles the
requests to run a command or edit a file that Claude Code shows when
bypass-permissions is off. It answers <i>yes</i> to work the session was
already told to do, which is why it is the one that ships on — but it does
authorise that work, so turn it off if you rely on reviewing those
requests yourself.</p>
<p>Two dialogs are deliberately left out of both switches: the safeguard
picker and the <i>Switch model?</i> confirmation. Answering either changes
which model the session runs on, and that is your decision, so both are left
for you.</p>
<p>Both are detected by patterns you can edit on the <b>Triggers</b> tab
(“Any chooser” and “Tool-permission prompt”); the permission pattern is what
keeps the two switches apart, so a chooser matching it is never answered by
the first switch.</p>

<h3>Claude CLI &#8594; More settings &#8594; Local API</h3>
<p>Another program on this machine can ask this one for the window table, and
ask it to type a line into a window. It exists because driving a Claude Code
window means enumerating it, checking the foreground really landed on it,
typing, and putting the previous window back — and two programs doing that at
once fight over the foreground and double-type into sessions. So this stays
the only driver and anything else goes through it.</p>
<p><b>It is on by default.</b> The listener is bound to <code>127.0.0.1</code>,
so nothing off this machine can reach it, and every request has to carry a
token. The token lives in
<code>%APPDATA%\\Auto-Continue\\local_api.json</code>, which is how a
companion program finds both it and the port.</p>
<p>What that means plainly: <b>while this is on, any program running as you
that can read that file can type into your sessions</b> — the same keystrokes
you could type yourself. That is the point of it, and it is also the reason
the switch is worth knowing about. Unticking it closes the port and deletes
the token file; a program that was using it simply stops being able to
connect. Every line typed on someone else's behalf is written to the log as
<code>api: send</code> at <b>fire</b> level, so the table and the log still
show who typed what.</p>
<p>The tool's own rules still apply to callers: <b>dry-run</b> types nothing
for them either, and a send whose foreground check fails is reported back as
refused instead of being typed somewhere else.</p>
<p><b><code>busy</code> always means nothing was done.</b> A caller's action
waits for this tool to be free, which during a pass is the gap between two
windows, not the end of the whole pass. If it is not reached within ten
seconds it is withdrawn and the caller hears <code>busy</code>, and a
withdrawn action is never run later. So retrying a busy is always safe. An
action that has already started is never answered busy; the caller waits for
what it really did. Before 2.1.4 a busy action could still be typed once the
pass ended, so a caller that retried typed it twice.</p>
<p>Most answers take a few hundredths of a second. The target window is
looked up by its handle rather than by listing every window on the desktop,
which took up to nine seconds whenever some other program was slow to
answer. A pass no longer lists the desktop that way either (see Timings), so
a request rarely waits more than a moment.</p>
<p><b>A line is not typed on top of a draft.</b> If the window's input box
already holds text that has not been sent, a caller's line would be typed
after it and submitted with it, as one message. So the send is refused as
<code>draft</code>: nothing is typed, the half-typed text is left exactly as
it was, and the caller can tell its user. Keystrokes are not checked this
way, because Enter and Esc act on whatever prompt is open.</p>
<p><b>A send is answered after it has been seen on the screen.</b> Each line
must show in the input box before its Enter, and leave the box after it (see
<i>A suggested prompt is not a draft</i> and <i>Each typed line is checked on
the screen</i> under Claude CLI). A failure says which part went wrong:
<code>foreground</code> means the window would not come forward and nothing
was typed; <code>focus_lost</code> means another window took the foreground
partway, so part of a line may be in the box; <code>not_confirmed</code>
means the line was typed but the screen never showed all of it arrive, or
never showed Enter take it, and nothing further was pressed. In the last two
cases read the screen before trying again. Keystroke specs are not checked
this way; a long one keeps the window in front a little longer before your
window comes back. Until 3.3.2 a send could answer <code>ok</code> while its
end had gone to another window.</p>
<p>A keystroke spec containing a key name this tool cannot press is refused
as <code>bad_keyspec</code>, naming the key, before the window is touched.
Backspace, for instance, is spelled <code>{{Back}}</code>. Anything else
would type the spec partway and then stop.</p>
<p>The token file also records where the running copy's exe lives. A
companion program that needs to start this tool again can then start your
copy, not an older one it carries.</p>
<p>A caller can ask for the window list, send a line, send a keystroke (an
ESC, say), bring a window forward, and subscribe to a live event stream that
carries every pass, every log line and every time this tool types something
of its own.</p>
<p>A caller can also ask for one window to be read again straight away,
rather than waiting for the next pass. The window list is updated with the
result before the answer goes back, so the list never says something older
than a fresh reading the caller has already seen. Every row also carries
<code>read_utc</code>, the time its screen was last read. Before 2.1.4 the
list kept the last pass's reading for up to a minute, so a prompt could
appear and disappear between two calls.</p>
<p><b>An open prompt is described in full</b>, so a companion can redraw it
somewhere else, on a watch for instance. When a window is waiting on a
question, a permission prompt or a plan confirmation, its row carries what
the screen shows:</p>
<ul>
<li>the question, and any title lines above it;</li>
<li>on a form with several questions, the tabs, and which tab is showing when
the text alone can tell;</li>
<li>every option, with its number, its description, whether the cursor is
on it, whether its box is ticked, whether it was already chosen, and whether
it is the row you type an answer into;</li>
<li>the rows under the rule, such as "Chat about this";</li>
<li>the hint line at the bottom.</li>
</ul>
<p>Anything the screen does not show is left empty rather than guessed. The
conversation itself (a diff, the text of a plan) is not included. A prompt is
reported wherever its cursor sits, including after the arrow keys have moved
it. That is only reporting: this tool's own Enter still answers a prompt
only when the cursor is on the first option, because Enter picks whatever
row the cursor is on.</p>
<p><b>What a caller types is counted, not quoted.</b> Every one of those
actions is written to the log as a keystroke — but the log records what was
done, not what was said, so a send appears as <code>lines=1</code> and a
keystroke spec as <code>&lt;24 chars&gt;{{Enter}}</code>: key names stay
readable, because "did it press Enter?" is the question a log answers, while
the words themselves are the session's business and not this log's. The
same key pressed three or more times in a row is written once with a count,
<code>{{Back}}&#215;80</code>, rather than eighty times.</p>
<p>A caller can also <b>turn the two answering switches on this tab off, or
back on</b>, and only those two. A companion program offering approval from a phone or a watch
is racing this tool: it answers a permission prompt within a pass, so a person
deciding remotely can find the prompt already gone. Rather than leave that to
be discovered, such a program can switch the automatic answer off. When it
does, the checkbox here changes with it and the log says
<code>api: settings auto_permission=off</code> at <b>fire</b> level — the same
level as a keystroke, because it is the same kind of event: something outside
this window changed what happens in your sessions.</p>
<p><b>Reading what is on screen is a separate switch, and it is off.</b> The
other capabilities are about a window's <i>status</i> — which one is waiting,
which is mid-turn. Session contents are the conversation itself: the code,
the file paths, whatever is up. Agreeing that a companion program may see the
table and press Enter is not the same as agreeing it may read the work, so
that one is asked for on its own and turning it on is written to the log.</p>

<h3>Claude CLI &#8594; More settings &#8594; Triggers</h3>
<p>Every detection pattern, editable and case-insensitive. Anthropic re-words
these banners without warning and a rename silently stops detection — this is
how you fix that yourself the same day instead of waiting for a new
build.</p>
<p>Each row names what it detects and has <b>Reset this</b> / <b>Reset
all</b>. The <b>test box</b> is the useful part: paste real terminal text and
it tells you whether the pattern matched, which capture groups it produced
(the limit pattern's groups are the reset time, so they must survive an
edit), and whether the match sits too far up the scrollback to count at
runtime — a stale banner thousands of characters back is deliberately
ignored.</p>
<p>A pattern also has to be quick. Claude Code pads every line with spaces
to the width of the window, so a screen is mostly blank space. A pattern that
lets two or three optional whitespace runs sit side by side can take seconds
to give up on such a screen, and that happens once per window on every pass.
The built-in safeguard pattern did exactly this until 2.1.4: several seconds a
pass on a nearly empty session. If you have a copy of the old one as your own
pattern, press <b>Reset this</b> on it.</p>
<p>Edits are validated on OK: the pattern must compile, must keep the capture
groups the parser indexes, and must not match empty text (which would fire on
every window). Only patterns you actually changed are stored, so future
builds' improved defaults still win for everything you left alone.</p>

<h3>Updating</h3>
<p><b>Check updates</b> looks at the GitHub release, and the download happens
from whichever of <b>two sources</b> answers faster: the GitHub release
itself, or the download-site mirror. Both are probed at once with a few
seconds' budget, the quicker one gets the transfer, and if it fails the other
is tried before the update is called off. A source that has not answered
within that budget counts as unreachable and the other one is used. Until
3.3.1 a source that stayed silent made the whole update fail with
<i>futures unfinished</i>, even with the other source ready, and only a
second click got it through.</p>
<p>This is not only about speed. Either source alone can carry an update, so
a link that cannot reach GitHub &mdash; which shows up in this tool's own log
on ordinary evenings &mdash; no longer means no updates. The mirror publishes
the distribution zip rather than a bare exe, so a download from it is
unpacked before staging; that is the only difference, and the same resume,
size check and hash apply either way.</p>
<p>Both measurements and the choice are written to the activity log, so when
an update is slow you can see which source was picked and what each one was
actually doing at the time.</p>

<h3>Timings</h3>
<ul>
<li><b>Poll interval</b> (60s) — how often every window is read. Reading a
window costs real time, and nothing here needs sub-minute latency. The list
of windows comes from Windows itself, and UI Automation is asked only about
those windows; the old desktop-wide UI Automation listing is used only if
Windows' own list fails. A window absent from one pass — UI Automation
refused it for a moment, or it is not showing a Claude session right now —
is not treated as closed: the handle is checked with the OS first, because
some of what is known about a window cannot be read off the screen a second
time: a pending limit's reset time, which chooser was already answered, and
how recently it was last typed into. The log says so once, naming the
window, and the window is simply looked at again on the next pass. A window
that was really closed is confirmed closed by the OS and dropped.
Until 3.2.0 a pass that came back short waited two seconds and listed the
windows again. That second look dated from the desktop-wide listing, whose
short lists the Windows listing no longer produces, and it had become a cost
of its own: a terminal where Claude Code had exited left a window that was
alive but no longer a Claude session, and every pass then waited two seconds
for it, holding up the local API with it.</li>
<li><b>Buffer</b> (60s) — extra margin past the stated reset time.</li>
<li><b>Retry interval</b> (600s) — the gap between <code>continue</code>s
while a session is network-stuck. Short values just bury the log: an outage
here once ran 90 minutes.</li>
</ul>

<h3>Starting over</h3>
<p><b>Reset Claude CLI settings&#8230;</b> (at the foot of Claude CLI's
More settings dialog) restores Claude timings, the
per-window model and effort overrides, After finish prompts, exclusions
and trigger patterns. It asks first and lists what it clears. Keep-awake and the start-up options are left as they
are. Each Codex type has its own reset in its More settings dialog.</p>
<p>Everything else you change is remembered across restarts, so the app comes
back exactly as you left it.</p>

<h3>Good to know</h3>
<ul>
<li>Before typing, the target window must actually reach the foreground. If
Windows refuses — because you are typing somewhere else — the send is retried
later rather than landing in the wrong app.</li>
<li><b>A session you have scrolled up in is left alone.</b> Newer Claude Code
draws its own screen, and while you read back through it the live bottom —
spinner, prompt box, any dialog — is not on screen at all; a <i>Jump to
bottom</i> hint takes its place. What is showing then is old: an error from an
hour ago with nothing after it looks exactly like a stuck session. So that
window is skipped until its view is back at the bottom, and the log says so
once. A due <code>continue</code> waits rather than going in blind.
Left scrolled, though, a window would be skipped for good — a limit coming
due or a dropped connection in it would go unanswered all night. So after
<b>60 minutes</b> scrolled up (Claude CLI &#8594; More settings &#8594; Answering; 0 = never) it
is sent <b>Ctrl+End</b>, Claude Code's own jump-to-bottom key, and is watched
again from the next pass. Only the view moves: nothing is typed into the
conversation, and the log records it as a keystroke.</li>
<li><b>Keep awake</b>, <b>Start on launch</b> and <b>Start with Windows</b>
live in <b>App settings</b>.
Keep awake stops
Modern Standby from killing the watcher during a long unattended wait; Start
on launch (on by default) means protection resumes by itself, including
after a self-update.</li>
<li>Only one copy runs at a time; a second launch shows the existing window
instead of double-typing into the same sessions.</li>
<li>The minimize button hides to the tray; the X button quits.</li>
<li>Everything in the log pane is also appended to
<code>%LOCALAPPDATA%\\auto_continue\\activity.log</code> (rotated at about
1 MB) for morning-after postmortems. Each line names the window as
<code>'title' #id</code> — the id is a short slice of the window handle, and
it is what makes the log answerable: titles are neither unique (two sessions
on the same task write identical lines) nor stable (a session renames itself
as it works), so without it a question like "was this window poked twice
inside the retry interval?" cannot be settled after the fact. The id changes
if the window is closed and reopened; it identifies a window, not a
project. A window the tool could not read that pass says so in its own line,
rather than looking identical to a window with nothing on it — that
difference is the first thing anyone needs when working out, after the fact,
whether the allowance really came back or the tool simply never looked.</li>
<li><b>Export log&#8230;</b> (main window) writes a copy of that log with the
window titles and your own typed prompts removed — windows appear as
<code>#id</code> only — and shows you the whole thing before it writes
anything. The raw file is safe where it sits and not safe to hand over: a
Claude Code window is named after its conversation, so the titles alone say
what this machine is working on. Nothing in the log is terminal content; it
never records what a session actually printed.</li>
<li>Settings are keyed by window <i>title</i>, so two windows with identical
titles share one set of settings: the same exclusion, model, effort and After
finish prompt.</li>
<li><b>Check updates</b> asks GitHub whether a newer release exists and can
download, verify and swap the exe in place. The download <b>resumes</b>: a
dropped connection keeps what arrived and the next attempt asks only for the
rest — and what survives is still there on the next launch, so a link that
drops more often than the transfer takes can still finish. A partial left
over from an older release is discarded rather than continued. If the swap cannot complete —
antivirus quarantines the staged file, the folder is read-only — the helper
starts the <i>previous</i> version back up rather than leaving you with
nothing running, and drops an <code>.update-failed.txt</code> marker beside
the exe. Only the packaged exe updates itself; running from source just
reports what is available.</li>
</ul>

<p><a href="https://github.com/zhh198903-ctrl/codex-cc-auto-continue">Project page on GitHub</a></p>
"""


# ===========================================================================
# Advanced settings sub-window
# ===========================================================================

class AdvancedDialog(QDialog):
    """Claude CLI's More settings: answering, triggers and local API.

    App-wide options (start-up, keep-awake) live in AppSettingsDialog.

    * **Triggers** — the regexes that decide when auto-continue fires. Editable
      because Anthropic re-words these banners without notice and a rename
      silently stops the tool from firing until a new build ships.
    """

    def __init__(self, parent, patterns: dict, auto: dict = None):
        super().__init__(parent)
        self.setWindowTitle("Claude CLI — More settings")
        # Set by the reset button. The dialog is then REJECTED rather than
        # accepted, so its now-stale values are never written back over the
        # defaults the reset is about to put in place.
        self.reset_requested = False
        # Tall enough for the Triggers page's editor and test box, never
        # taller than the screen actually has room for.
        _scr = QApplication.primaryScreen()
        _avail = _scr.availableGeometry().height() if _scr else 800
        self.resize(660, max(480, min(680, _avail - 60)))
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)
        self._tabs = tabs

        ans_tab = QWidget()
        tabs.addTab(ans_tab, "Answering")
        self._build_answer_tab(ans_tab, auto or {})

        trig_tab = QWidget()
        tabs.addTab(trig_tab, "Triggers")
        self._build_triggers_tab(trig_tab, patterns or {})

        api_tab = QWidget()
        tabs.addTab(api_tab, "Local API")
        self._build_api_tab(api_tab)
        tabs.setCurrentIndex(0)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        self.reset_btn = QPushButton("Reset Claude CLI settings…")
        self.reset_btn.setToolTip(
            "Timings, overrides, exclusions, trigger patterns and answering "
            "— all back to default. Asks first.")
        self.reset_btn.clicked.connect(self._request_reset)
        btns.addButton(self.reset_btn, QDialogButtonBox.ButtonRole.ResetRole)
        outer.addWidget(btns)

    def _build_api_tab(self, tab) -> None:
        """The switch for the local HTTP API.

        Reads and writes QSettings directly through the parent rather than
        joining the cfg/patterns/auto dicts the other tabs are handed: this
        setting belongs to the window, not to the watcher, and nothing in the
        tick needs to know about it.
        """
        root = QVBoxLayout(tab)
        # The dialog is also built parentless in tests, and a settings store is
        # a process-wide thing anyway — reaching through the parent for it was
        # a coupling with nothing to gain.
        st = getattr(self.parent(), "settings", None) or QSettings(
            "auto_continue", "gui")
        intro = QLabel(
            "A companion program on this machine can read the window table "
            "and ask for a line to be typed, instead of driving the terminals "
            "itself — two programs doing that at once fight over the "
            "foreground. Local only (127.0.0.1) and every request carries a "
            "token. See <b>Help</b>.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Short on purpose: a QCheckBox does not word-wrap, so its full label
        # becomes the dialog's minimum width and quietly overrides resize().
        # The explanation lives in the wrapped label above it.
        self.api_chk = QCheckBox("Allow local programs (token required)")
        self.api_chk.setChecked(bool(st.value(
            "local_api/enabled", True, type=bool)))
        root.addWidget(self.api_chk)

        warn = QLabel(
            "⚠ While this is on, any program running as you that can read the "
            "token file can type into your sessions. Turning it off closes "
            "the port and deletes the token file.")
        warn.setWordWrap(True)
        warn.setStyleSheet("font-weight: bold;")
        root.addWidget(warn)

        # Separate switch, off by default. Listing windows and typing into
        # them is one capability; handing over what a session has on screen —
        # the conversation, the code, whatever is up — is a different one, and
        # agreeing to the first is not agreeing to the second.
        self.api_text_chk = QCheckBox("Also allow reading session contents")
        self.api_text_chk.setChecked(bool(st.value(
            "local_api/text", False, type=bool)))
        root.addWidget(self.api_text_chk)

        twarn = QLabel(
            "Off by default. With it on, a caller can fetch the text on screen "
            "in any watched window — not just its status.")
        twarn.setWordWrap(True)
        root.addWidget(twarn)

        row = QHBoxLayout()
        row.addWidget(QLabel("Port"))
        self.api_port = QSpinBox()
        self.api_port.setRange(1024, 65535)
        self.api_port.setValue(int(st.value(
            "local_api/port", local_api.DEFAULT_PORT)))
        row.addWidget(self.api_port)
        row.addStretch(1)
        root.addLayout(row)

        # The symbolic form, not the expanded one: a real path has no
        # spaces to wrap at, so it would set the minimum width all by
        # itself -- the same trap the checkbox above avoids.
        path = QLabel(r"Token file: <code>%APPDATA%\Auto-Continue"
                      r"\local_api.json</code>")
        path.setWordWrap(True)
        path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(path)
        root.addStretch(1)

    def _request_reset(self) -> None:
        self.reset_requested = True
        self.reject()

    def _build_answer_tab(self, tab, auto: dict) -> None:
        root = QVBoxLayout(tab)

        # Group 1: the two Enter answers, with the warning that is ABOUT them
        # kept inside the same box so it cannot drift away from them.
        ans_box = QGroupBox("Answer prompts with Enter")
        ab = QVBoxLayout(ans_box)
        intro = QLabel(
            "Claude Code sometimes pauses on a chooser and waits. Enter takes "
            "the option it already pre-selected. See <b>Help</b>.")
        intro.setWordWrap(True)
        ab.addWidget(intro)

        self.auto_perm_chk = QCheckBox("Answer tool-permission requests")
        self.auto_perm_chk.setChecked(bool(auto.get("permission", True)))
        self.auto_perm_chk.setToolTip(
            "Requests to run a command or edit a file (the ones you see "
            "when bypass-permissions is off). Answering these AUTHORISES "
            "the work — turn this off if you want to approve them "
            "yourself.")
        ab.addWidget(self.auto_perm_chk)

        self.auto_choose_chk = QCheckBox("Answer choosers (off by default)")
        self.auto_choose_chk.setChecked(bool(auto.get("choose", False)))
        self.auto_choose_chk.setToolTip(
            "Ordinary numbered choosers — the session had stopped to ask, "
            "and picking the first option is what unblocks it. Never fires "
            "while a turn is running, never while you have anything typed "
            "in the input box, and once per chooser.")
        ab.addWidget(self.auto_choose_chk)

        warn = QLabel(
            "⚠ Permission answers only say yes to work already asked for. "
            "Chooser answers decide things — which option, and whether a "
            "plan starts running.")
        warn.setWordWrap(True)
        warn.setStyleSheet("font-weight: bold;")
        ab.addWidget(warn)
        root.addWidget(ans_box)

        # Group 2: scrolled-up windows. The explanation wraps on its own
        # line: a long label beside the spin box set the dialog's minimum
        # width past what it may open at (test_safeguard Z2 checks it).
        sb_box = QGroupBox("Scrolled-up windows")
        sbl = QVBoxLayout(sb_box)
        sb_text = QLabel(
            "A window you scroll up in is skipped — its live bottom is not "
            "on screen. Jump it back to the bottom after:")
        sb_text.setWordWrap(True)
        sbl.addWidget(sb_text)
        sb_row = QHBoxLayout()
        self.scroll_back_spin = QSpinBox()
        self.scroll_back_spin.setRange(0, 1440)
        self.scroll_back_spin.setSuffix(" min")
        self.scroll_back_spin.setSpecialValueText("never")
        self.scroll_back_spin.setValue(
            int(auto.get("scroll_back_min", DEFAULT_SCROLL_BACK_MIN)))
        self.scroll_back_spin.setToolTip(
            "A window you scrolled up in cannot be read, so it is skipped. "
            "Left that way it would be skipped for good. After this long it "
            "gets Ctrl+End (Claude Code's jump-to-bottom) and is watched "
            "again. Only the view moves; nothing is typed. 0 = never.")
        sb_row.addWidget(self.scroll_back_spin)
        sb_row.addStretch(1)
        sbl.addLayout(sb_row)
        root.addWidget(sb_box)
        root.addStretch(1)

    def result_auto(self) -> dict:
        return {"choose": self.auto_choose_chk.isChecked(),
                "permission": self.auto_perm_chk.isChecked(),
                "scroll_back_min": self.scroll_back_spin.value()}

    def result_api(self) -> dict:
        return {"enabled": self.api_chk.isChecked(),
                "text": self.api_text_chk.isChecked(),
                "port": int(self.api_port.value())}

    # ---- Triggers tab ----------------------------------------------------

    def _build_triggers_tab(self, tab, patterns: dict) -> None:
        """One editable regex per detection trigger, with a live match tester.

        Editing state is held in `self._pat_edits` (key → current source) and
        only flushed to the widget on selection change, so switching rows
        doesn't lose a half-typed pattern.
        """
        lay = QVBoxLayout(tab)
        intro = QLabel(
            "These regexes decide when auto-continue fires. Edit one if "
            "Anthropic re-words a banner and detection stops working — you "
            "don't have to wait for a new build. Matching is always "
            "case-insensitive."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        # Working copy: start from saved overrides, fall back to the defaults.
        self._pat_edits = {}
        for spec in TRIGGER_SPECS:
            src = str((patterns or {}).get(spec["key"], "") or "").strip()
            self._pat_edits[spec["key"]] = src or spec["default"]

        self.trig_list = QListWidget()
        self.trig_list.setFixedHeight(150)
        for spec in TRIGGER_SPECS:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, spec["key"])
            self.trig_list.addItem(it)
        lay.addWidget(self.trig_list)

        self.trig_help = QLabel("")
        self.trig_help.setWordWrap(True)
        self.trig_help.setStyleSheet("color: palette(mid);")
        lay.addWidget(self.trig_help)

        self.pat_edit = QPlainTextEdit()
        self.pat_edit.setFixedHeight(96)
        self.pat_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        _f = QFont("Consolas")
        _f.setStyleHint(QFont.StyleHint.Monospace)
        self.pat_edit.setFont(_f)
        lay.addWidget(self.pat_edit)

        row = QHBoxLayout()
        self.pat_status = QLabel("")
        self.pat_status.setWordWrap(True)
        row.addWidget(self.pat_status, 1)
        self.reset_one_btn = QPushButton("Reset this")
        self.reset_all_btn = QPushButton("Reset all")
        row.addWidget(self.reset_one_btn)
        row.addWidget(self.reset_all_btn)
        lay.addLayout(row)

        test_hint = QLabel(
            "Test — paste terminal text here to check what it matches:")
        test_hint.setWordWrap(True)
        lay.addWidget(test_hint)
        self.test_edit = QPlainTextEdit()
        self.test_edit.setPlaceholderText(
            "e.g. paste the banner Claude Code printed…")
        lay.addWidget(self.test_edit, 1)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        lay.addWidget(self.test_result)

        self._cur_key = None
        self.trig_list.currentRowChanged.connect(self._on_trigger_row)
        self.pat_edit.textChanged.connect(self._on_pattern_edited)
        self.test_edit.textChanged.connect(self._refresh_test)
        self.reset_one_btn.clicked.connect(self._reset_current_pattern)
        self.reset_all_btn.clicked.connect(self._reset_all_patterns)
        self._refresh_trigger_labels()
        self.trig_list.setCurrentRow(0)

    def _spec(self, key):
        for s in TRIGGER_SPECS:
            if s["key"] == key:
                return s
        return None

    def _refresh_trigger_labels(self) -> None:
        """Mark rows whose pattern differs from the built-in default."""
        for i in range(self.trig_list.count()):
            it = self.trig_list.item(i)
            key = it.data(Qt.ItemDataRole.UserRole)
            spec = self._spec(key)
            custom = self._pat_edits.get(key, "") != spec["default"]
            it.setText(spec["label"] + ("   • customised" if custom else ""))

    def _on_trigger_row(self, row: int) -> None:
        if row < 0:
            return
        key = self.trig_list.item(row).data(Qt.ItemDataRole.UserRole)
        self._cur_key = None                      # suppress the edit hook
        spec = self._spec(key)
        need = spec["groups"]
        self.trig_help.setText(
            spec["help"]
            + (f"  (needs {need} capture group(s))" if need else ""))
        self.pat_edit.setPlainText(self._pat_edits.get(key, ""))
        self._cur_key = key
        self._validate_current()

    def _on_pattern_edited(self) -> None:
        if self._cur_key is None:
            return
        self._pat_edits[self._cur_key] = self.pat_edit.toPlainText().strip()
        self._refresh_trigger_labels()
        self._validate_current()

    def _compile_current(self):
        """Compile the shown pattern → (regex|None, error_message|"")."""
        key = self._cur_key
        if key is None:
            return None, ""
        spec = self._spec(key)
        src = self._pat_edits.get(key, "").strip()
        if not src:
            return None, "empty — the built-in default will be used"
        try:
            rx = _re.compile(src, _re.IGNORECASE)
        except _re.error as e:
            return None, f"✗ invalid regex: {e}"
        if rx.groups < spec["groups"]:
            return None, (f"✗ needs {spec['groups']} capture group(s), "
                          f"found {rx.groups}")
        return rx, ""

    def _validate_current(self) -> None:
        rx, err = self._compile_current()
        if err:
            self.pat_status.setText(err)
            self.pat_status.setStyleSheet(
                "color: #e03131;" if err.startswith("✗") else "")
        else:
            spec = self._spec(self._cur_key)
            same = self._pat_edits.get(self._cur_key) == spec["default"]
            self.pat_status.setText(
                "✓ valid — built-in default" if same else "✓ valid — customised")
            self.pat_status.setStyleSheet("color: #2f9e44;")
        self._refresh_test()

    def _refresh_test(self) -> None:
        sample = self.test_edit.toPlainText()
        if not sample:
            self.test_result.setText("")
            return
        rx, err = self._compile_current()
        if rx is None:
            self.test_result.setText("(fix the pattern above to test)")
            self.test_result.setStyleSheet("color: #e03131;")
            return
        m = None
        for m in rx.finditer(sample):
            pass                                   # keep the LAST match
        if m is None:
            self.test_result.setText("✗ no match in the sample text")
            self.test_result.setStyleSheet("color: #e03131;")
            return
        shown = m.group(0)
        if len(shown) > 160:
            shown = shown[:160] + "…"
        tail = len(sample) - m.end()
        extra = ""
        # Mirror the runtime tail anchor: a match too far from the end is
        # treated as stale scrollback and would NOT fire.
        limit = {"limit": MAX_POST_MATCH_TAIL,
                 "limit_prompt": PROMPT_POST_MATCH_TAIL,
                 "switch_model": SWITCH_POST_MATCH_TAIL}.get(
                     self._cur_key, NETWORK_POST_MATCH_TAIL)
        if tail > limit:
            extra = (f"  ⚠ but it sits {tail} chars from the end (limit "
                     f"{limit}) — at runtime this counts as stale scrollback "
                     f"and would NOT fire")
        groups = ""
        if m.re.groups:
            groups = "   groups: " + ", ".join(
                repr(g) for g in m.groups())
        self.test_result.setText(f"✓ matched: {shown!r}{groups}{extra}")
        self.test_result.setStyleSheet(
            "color: #e8590c;" if extra else "color: #2f9e44;")

    def _reset_current_pattern(self) -> None:
        if self._cur_key is None:
            return
        self._pat_edits[self._cur_key] = self._spec(self._cur_key)["default"]
        self.pat_edit.setPlainText(self._pat_edits[self._cur_key])
        self._refresh_trigger_labels()

    def _reset_all_patterns(self) -> None:
        for spec in TRIGGER_SPECS:
            self._pat_edits[spec["key"]] = spec["default"]
        if self._cur_key is not None:
            self.pat_edit.setPlainText(self._pat_edits[self._cur_key])
        self._refresh_trigger_labels()

    # ---- accept / results ------------------------------------------------

    def accept(self) -> None:
        """Refuse to close on an unusable pattern — silently falling back to
        the default would leave the user believing their edit took effect."""
        bad = []
        for spec in TRIGGER_SPECS:
            src = self._pat_edits.get(spec["key"], "").strip()
            if not src or src == spec["default"]:
                continue
            try:
                rx = _re.compile(src, _re.IGNORECASE)
            except _re.error as e:
                bad.append(f"• {spec['label']}: {e}")
                continue
            if rx.groups < spec["groups"]:
                bad.append(f"• {spec['label']}: needs {spec['groups']} "
                           f"capture group(s), found {rx.groups}")
        if bad:
            QMessageBox.warning(
                self, "Invalid trigger pattern",
                "These patterns can't be used:\n\n" + "\n".join(bad)
                + "\n\nFix them on the Triggers tab, or press “Reset this”.")
            return

        super().accept()

    def result_patterns(self) -> dict:
        """Only genuinely-customised patterns are persisted, so a future
        build's improved default automatically wins for untouched triggers."""
        out = {}
        for spec in TRIGGER_SPECS:
            src = self._pat_edits.get(spec["key"], "").strip()
            if src and src != spec["default"]:
                out[spec["key"]] = src
        return out


# ===========================================================================

def main() -> int:
    if "--selftest" in sys.argv:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        profile = os.environ.get("AUTO_CONTINUE_SETTINGS_FILE")
        if not profile:
            # An isolated profile beside this copy; never change the user's settings.
            base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            profile = str(base / "selftest-settings.ini")
            os.environ["AUTO_CONTINUE_SETTINGS_FILE"] = profile
        if not os.environ.get('AUTO_CONTINUE_DISCOVERY_FILE'):
            os.environ['AUTO_CONTINUE_DISCOVERY_FILE'] = str(Path(profile).with_suffix('.selftest-api.json'))
        store = QSettings(profile, QSettings.Format.IniFormat)
        store.setValue("autostart", False)
        store.setValue("local_api/enabled", False)
        store.setValue("desktop/thread", "")
        store.sync()
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)
        class TestWindow(MainWindow):
            def _build_updater(self):
                self.updater_thread = QThread(self)
        try:
            win = TestWindow()
            def finish():
                win.close()
                base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
                (base / "selftest.log").write_text("selftest ok: Claude / Codex CLI / Desktop\n", encoding="utf-8")
                app.quit()
            QTimer.singleShot(1000, finish)
            return app.exec()
        except Exception as exc:
            base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            (base / "selftest.log").write_text(f"selftest FAILED: {exc!r}\n", encoding="utf-8")
            return 2
    app = QApplication(sys.argv)
    app.setApplicationName("Auto-Continue")
    app.setOrganizationName("auto_continue")

    # Single-instance guard: two copies would each type 'continue' into the
    # same windows (double submissions) and race each other's update staging.
    #
    # use_last_error + proper HANDLE restype: reading the "already exists"
    # status via a bare kernel32.GetLastError() is unreliable (an intervening
    # ctypes/Win32 call can clobber the thread's last-error); ctypes' own
    # last-error slot is captured atomically with the call.
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL,
                               wintypes.LPCWSTR]

    global _INSTANCE_MUTEX  # keep the handle alive for the process lifetime
    _INSTANCE_MUTEX = k.CreateMutexW(None, False, _MUTEX_NAME)
    already_exists = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS

    # Two shared auto-reset events. A second launch signals SHOW so the
    # running instance surfaces its window (Win11 buries tray icons in the
    # overflow flyout, making a hidden window otherwise hard to recover —
    # a dead-end 'already running' popup didn't help). The running instance
    # answers on ACK; a second launch that gets no ACK within 1.5 s concludes
    # the mutex is STALE (crashed/leaked prior owner) and takes over as the
    # primary, so a stuck mutex can never permanently brick startup.
    show_event = k.CreateEventW(None, False, False, _SHOW_EVENT_NAME)
    ack_event = k.CreateEventW(None, False, False, _ACK_EVENT_NAME)
    if already_exists:
        if ack_event:
            k.ResetEvent(ack_event)
        if show_event:
            k.SetEvent(show_event)          # ask the running instance to show
        acked = (ack_event
                 and k.WaitForSingleObject(ack_event, 1500) == 0)
        if acked:
            return 0                        # a live instance handled it
        # No ACK → stale mutex; fall through and run as the primary.

    # Hiding the main window to the tray must not exit the app — the
    # watcher thread needs to keep running.
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow()
    # --minimized (used by the "Start with Windows" boot entry) brings the app
    # up straight in the tray with no window on screen. Falls back to a normal
    # minimized window if no system tray is available.
    start_minimized = any(a in ("--minimized", "--min", "--tray")
                          for a in sys.argv[1:])
    if start_minimized and win.tray is not None:
        win.notify_started_in_tray()
    elif start_minimized:
        win.showMinimized()
    else:
        win.show()
    win.attach_show_listener(show_event, ack_event)
    def update_started():
        try:
            failure = updater.take_update_failure()
            if failure:
                win._append_log('err', failure)
            running = updater.take_resume_state()
            if running is not None:
                if not win.codex_service.monitor_state()['paused']:
                    for name, start, stop in (
                            ('claude', win.sig_claude_start, win.sig_claude_stop),
                            ('codex', win.sig_codex_start, win.sig_codex_stop),
                            ('codex_app', win.sig_codex_app_start, win.sig_codex_app_stop)):
                        (start if running[name] else stop).emit()
                win._append_log('info', 'Update complete; restoring previous monitor switches')
        except Exception as exc:
            win._append_log('err', 'Could not restore update state: ' + str(exc))
    QTimer.singleShot(0, update_started)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
