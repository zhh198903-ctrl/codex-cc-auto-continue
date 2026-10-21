# -*- coding: utf-8 -*-
"""Limit / retry / picker / oauth tests for gui.Watcher._tick — the loop the
shipped exe actually runs.

These scenarios used to live in test_tick.py against the CLI watcher, which
was a second copy of this logic. Deleting that copy (v2.0.5) would have left
the core limit flow with no coverage at all, so the scenarios moved here, onto
the loop that ships. What the tick does about a safeguard block is
test_safeguard.py's job.

Stub windows, recorded sends, a controllable clock. Exits non-zero on any
failure.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta

import pytz
from PyQt6.QtWidgets import QApplication

import auto_continue as ac
import gui

_app = QApplication.instance() or QApplication([])

failures = 0


def check(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


class FakeDT(datetime):
    _now = None

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls._now.astimezone().replace(tzinfo=None)
        return cls._now.astimezone(tz)


gui.datetime = FakeDT
# The limit path computes the next reset via auto_continue.next_reset_datetime,
# which consults ITS module's clock — patching gui's alone leaves the pending
# armed against the real date.
ac.datetime = FakeDT


def set_now(dt):
    FakeDT._now = dt


def advance(secs):
    set_now(FakeDT._now + timedelta(seconds=secs))


class FakeTimer:
    @staticmethod
    def singleShot(ms, fn):
        pass


gui.QTimer = FakeTimer


class FakeWin:
    def __init__(self, hwnd, title):
        self.NativeWindowHandle = hwnd
        self.Name = title


WINDOWS = []
TEXTS = {}
SENT = []


def _send_lines(w, lines, dry_run=False, outcome=None):
    SENT.append((int(w.NativeWindowHandle), list(lines)))
    return True


gui.find_terminal_windows = lambda: WINDOWS
# The API looks its one window up by handle instead of listing the desktop;
# in here that is the same fake list.
gui.terminal_window_from_handle = lambda h: next(
    (w for w in WINDOWS if int(w.NativeWindowHandle) == int(h)), None)
gui.read_terminal_text = lambda w: TEXTS.get(int(w.NativeWindowHandle), "")
gui.send_text_lines = _send_lines
gui.send_keys = lambda w, k, dry_run=False: True
gui.send_continue = lambda w, dry_run=False: _send_lines(w, ["continue"])
gui.list_tab_titles = lambda w: []


def new_watcher():
    w = gui.Watcher()
    w._running = True
    return w


def reset(hwnd_titles, text_by_hwnd):
    global WINDOWS, TEXTS
    WINDOWS = [FakeWin(h, t) for h, t in hwnd_titles]
    TEXTS = dict(text_by_hwnd)
    SENT.clear()


# Fixtures — de-fanged (adjacent-string splits) so displaying THIS file in a
# watched terminal can't trigger the real watchdog, same convention as every
# other file in the repo. test_selftrigger.py enforces it.
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
RETRY_EXHAUSTED = "Retry" "ing in 0s · attempt 10/10"
ECONN = "API Error: Unable to conn" "ect to API (ECONN" "RESET)"
TRUNC = "API Error: Server error mid-resp" "onse."
OAUTH = "OAuth to" "ken has expired. Please run /log" "in."
SPIN = "  ✽ Swirling… (9s · ↓ 1k tokens)"

# 11pm Asia/Shanghai == 15:00 UTC. Start the clock well before it.
T0 = datetime(2026, 7, 26, 10, 0, 0, tzinfo=pytz.UTC)
FIRE_UTC = datetime(2026, 7, 26, 15, 0, 0, tzinfo=pytz.UTC)


# =============================================================================
print("---- A: the full limit lifecycle ----")
set_now(T0)
reset([(1, "win")], {1: BANNER})
w = new_watcher()
w._tick()
st = w._states[1]
check("A1 pending armed at 15:00 UTC", st.reset_utc == FIRE_UTC)
advance(3600)
w._tick()
check("A2 nothing sent while waiting", not SENT)
set_now(FIRE_UTC + timedelta(seconds=w._buffer + 5))
w._tick()
check("A3 fired once", SENT == [(1, ["continue"])])
check("A4 pending cleared, fired key kept",
      st.reset_utc is None and st.fired_key is not None)
SENT.clear()
# The banner lingers in scrollback; after the cooldown it must not re-arm.
advance(16 * 60)
w._tick()
check("A5 stale banner does not re-arm after cooldown",
      st.reset_utc is None and not SENT)
# Banner scrolls away -> fired key released, ready for the next real hit.
TEXTS[1] = "all clear"
w._tick()
check("A6 fired key cleared once the banner is gone", st.fired_key is None)
TEXTS[1] = BANNER
w._tick()
check("A7 a new banner re-arms", st.reset_utc is not None)

# =============================================================================
# A due fire must not land mid-turn. The re-verify above only notices a manual
# resume once its output has pushed the banner past MAX_POST_MATCH_TAIL, so a
# session that resumed seconds ago still shows it — and the continue would be
# queued by Claude Code to run when the turn ends, an unwanted extra turn.
set_now(T0)
reset([(1, "win")], {1: BANNER})
w = new_watcher()
LOGS_A = []
w.log.connect(lambda k, m: LOGS_A.append((k, m)))
w._tick()
SENT.clear()
TEXTS[1] = chr(10).join([BANNER, SPIN])  # resumed, banner still in range
set_now(FIRE_UTC + timedelta(seconds=w._buffer + 5))
w._tick()
check("A8a a due fire is held back while the session is streaming", not SENT)
check("A8b and the pending is kept, not cancelled",
      w._states[1].reset_utc is not None)
check("A8c and the hold is explained once",
      sum(1 for _, m in LOGS_A if "mid-turn" in m) == 1)
advance(60)
w._tick()
check("A8d still held, and not re-announced every tick",
      not SENT and sum(1 for _, m in LOGS_A if "mid-turn" in m) == 1)
TEXTS[1] = BANNER                         # the turn ended
advance(60)
w._tick()
check("A8e fires as soon as the session goes quiet",
      SENT == [(1, ["continue"])])
SENT.clear()

# The "held" news is announced once per hold, and a hold that ends some other
# way than by firing (you continued manually, so the banner went) must not
# leave the flag stuck — the NEXT genuine hold would then be silent.
set_now(T0)
reset([(1, "win")], {1: BANNER})
w = new_watcher()
LOGS_A2 = []
w.log.connect(lambda k, m: LOGS_A2.append((k, m)))
w._tick()
TEXTS[1] = chr(10).join([BANNER, SPIN])      # streaming at fire time
set_now(FIRE_UTC + timedelta(seconds=w._buffer + 5))
w._tick()
check("A9a first hold announced", any("mid-turn" in m for _, m in LOGS_A2))
TEXTS[1] = "you continued it yourself, banner long gone"
advance(60)
w._tick()
check("A9b the pending is released when the banner goes",
      w._states[1].reset_utc is None)
# A fresh limit arrives, comes due, and is again held mid-turn. It has to be
# a DIFFERENT reset time: re-showing the same banner is deliberately ignored,
# because fired_key remembers the one already dealt with.
BANNER_LATER = ("You've hit your li" "mit · resets 9pm (Asia/Shanghai)"
                + chr(10) + "/upgra" "de to increase your usage limit.")
LOGS_A2.clear()
TEXTS[1] = BANNER_LATER
advance(60)
w._tick()
check("A9c a genuinely new limit re-arms", w._states[1].reset_utc is not None)
TEXTS[1] = chr(10).join([BANNER_LATER, SPIN])
set_now(w._states[1].reset_utc + timedelta(seconds=w._buffer + 5))
w._tick()
check("A9d a later hold is announced too, not swallowed by a stuck flag",
      any("mid-turn" in m for _, m in LOGS_A2))
SENT.clear()

# =============================================================================
print("---- B: retry-exhausted banner pokes until recovery ----")
set_now(T0)
reset([(2, "win")], {2: RETRY_EXHAUSTED})
w = new_watcher()
w._tick()
check("B1 poke on attempt N/N", SENT == [(2, ["continue"])])
SENT.clear()
advance(w._retry_interval - 10)
w._tick()
check("B2 resend rate-limited inside the retry interval", not SENT)
advance(20)
w._tick()
check("B3 re-poke after the retry interval", SENT == [(2, ["continue"])])
SENT.clear()
TEXTS[2] = "recovered, back to work"
w._tick()
check("B4 recovery ends the outage and types nothing more",
      not w._states[2].retry_active and not SENT)
# The send stamp deliberately SURVIVES recovery. It used to be cleared here,
# and that is what let a window which keeps failing and recovering be poked
# far more often than the interval allows: every recovery re-armed an
# immediate poke. Measured overnight on 2026-09-14 — one window took 83
# 'continue's in 8.4 hours, 71 of the 82 gaps under the configured 600s.
check("B4a but the send stamp survives it, so the interval is a real floor",
      w._states[2].retry_last_sent_utc is not None)

# The property that matters: fail, recover, fail again inside the interval,
# and the second failure must NOT be poked yet.
TEXTS[2] = RETRY_EXHAUSTED
advance(5)                       # well inside w._retry_interval, whatever it is
w._tick()
check("B4b a failure returning inside the interval waits its turn",
      not SENT)
advance(w._retry_interval)
w._tick()
check("B4c and is poked once the interval has actually passed",
      SENT == [(2, ["continue"])])
SENT.clear()
# ...while a window quiet for hours is not made to wait at all: its stamp is
# old, so a genuinely new outage fires at once.
TEXTS[2] = "all quiet"
w._tick()
advance(6 * 3600)
TEXTS[2] = RETRY_EXHAUSTED
w._tick()
check("B4d a new outage hours later still fires immediately",
      SENT == [(2, ["continue"])])
SENT.clear()

# Every resend is a keystroke and must be RECORDED as one. Repeats used to be
# logged at 'info' so the tray balloon would not pop every retry interval —
# but that made the log under-count what had been typed (counting 'fire' lines
# is exactly how the 83-continue defect was found) and published them over the
# API as ordinary log lines, so a companion program watching 'fire' to know a
# window is about to change under it heard nothing. The balloon, not the
# level, is what gets held back now: level 'fire-quiet'.
set_now(T0)
reset([(2, "win")], {2: RETRY_EXHAUSTED})
w = new_watcher()
LOGS_B5 = []
w.log.connect(lambda k, m: LOGS_B5.append((k, m)))
w._tick()                                        # first resend of the outage
advance(w._retry_interval + 1)
w._tick()                                        # second resend, same outage
_resends = [k for k, m in LOGS_B5 if "resending 'continue'" in m]
check("B5a both resends of one outage are logged", len(_resends) == 2)
check("B5b the first is a plain fire (balloon and all)",
      _resends[0] == "fire")
check("B5c the repeat is still a fire, only quieter — not demoted to info",
      _resends[1] == "fire-quiet")
check("B5d no keystroke of an outage is logged below fire level",
      not any(k in ("info", "warn") and "resending 'continue'" in m
              for k, m in LOGS_B5))
SENT.clear()

# A screen that could not be READ is not a screen with no error on it. Both
# arrive as an empty tail, and treating the unreadable one as recovery threw
# away the retry throttle: the next pass that did read the error fired at
# once, so a flaky read turned a 10-minute poke into a per-minute one.
set_now(T0)
reset([(2, "win")], {2: RETRY_EXHAUSTED})
w = new_watcher()
LOGS_B = []
w.log.connect(lambda k, m: LOGS_B.append((k, m)))
w._tick()
check("B5 poking while the error is on screen", SENT == [(2, ["continue"])])
SENT.clear()
LOGS_B.clear()
TEXTS[2] = ""                       # the read failed this pass
advance(5)
w._tick()
check("B6 an unreadable screen is not reported as recovery",
      not any("cleared" in m for _, m in LOGS_B))
check("B7 and the window stays in the retry state",
      w._states[2].retry_active)
check("B8 and the retry throttle is not thrown away",
      w._states[2].retry_last_sent_utc is not None)
TEXTS[2] = RETRY_EXHAUSTED          # the next pass reads it fine
advance(5)                          # still well inside the retry interval
w._tick()
check("B9 so the next readable pass does not fire early", not SENT)

# =============================================================================
print("---- C: bare connection errors and truncation, gated on running ----")
for name, text in (("ECONN", ECONN), ("truncation", TRUNC)):
    set_now(T0)
    reset([(3, "win")], {3: text})
    w = new_watcher()
    w._tick()
    check(f"C1 {name}: idle stuck session is poked", SENT == [(3, ["continue"])])
    # The same banner with a live spinner after it is a session that already
    # recovered — poking it queues junk (measured: eight continues in one
    # 80-minute outage).
    set_now(T0)
    reset([(3, "win")], {3: text + "\n" + SPIN})
    w = new_watcher()
    w._tick()
    check(f"C2 {name}: a streaming session is left alone", not SENT)

# =============================================================================
print("---- D: oauth-expired never gets keystrokes ----")
set_now(T0)
reset([(4, "win")], {4: OAUTH})
w = new_watcher()
w._tick()
w._tick()
check("D1 no keystrokes for a dead session", not SENT)

# =============================================================================
print("---- E: one bad window can't blind the tick ----")
set_now(T0)


class BoomWin(FakeWin):
    @property
    def Name(self):
        raise ValueError("flaky UIA child")


reset([(6, "good")], {5: "x", 6: RETRY_EXHAUSTED})
WINDOWS.insert(0, BoomWin.__new__(BoomWin))
WINDOWS[0].NativeWindowHandle = 5
w = new_watcher()
w._tick()
check("E1 later window still processed", (6, ["continue"]) in SENT)

# ...and a window that throws MIDWAY through processing, after the tick has
# already committed to it, must not take the rest of the loop down either.
# Only one block of the tick used to be guarded; everything after it ran bare,
# so one window's failure meant every window enumerated later went unwatched
# for that pass — with a single generic 'tick error' line as the symptom.
set_now(T0)


class _BoomText(str):
    """Reads fine once (so the window is adopted), then raises."""


_reads = {"n": 0}


def _flaky_read(w):
    h = int(w.NativeWindowHandle)
    if h == 8:
        _reads["n"] += 1
        raise RuntimeError("UIA read blew up mid-window")
    return TEXTS.get(h, "")


reset([(8, "bad"), (9, "good")], {9: RETRY_EXHAUSTED})
_saved_read = gui.read_terminal_text
gui.read_terminal_text = _flaky_read
LOGS_E = []
w = new_watcher()
w.log.connect(lambda k, m: LOGS_E.append((k, m)))
try:
    w._tick()
finally:
    gui.read_terminal_text = _saved_read
check("E2 a window that throws does not stop the ones after it",
      (9, ["continue"]) in SENT)
check("E3 and the failure is reported against the window that caused it",
      any(k == "err" and "tick error on" in m for k, m in LOGS_E))

# =============================================================================
print("---- F: closed windows drop their state ----")
set_now(T0)
reset([(7, "win")], {7: BANNER})
w = new_watcher()
w._tick()
check("F1 state exists while the window does", 7 in w._states)
reset([], {})
w._tick()
check("F2 state dropped once the window closes", 7 not in w._states)

# The costly confusion: find_terminal_windows() is documented to return a
# partial list (and an empty one when the root enumeration throws), so
# "absent from this pass" cannot mean "closed". It used to, and one COM
# hiccup then wiped every window's state — including the things that cannot
# be recovered by looking at the screen again.
_alive_real = gui._hwnd_alive
LOGS_F = []


def _ghost_watcher(hwnd=7):
    set_now(T0)
    reset([(hwnd, "win")], {hwnd: BANNER})
    w = new_watcher()
    LOGS_F.clear()
    w.log.connect(lambda k, m: LOGS_F.append((k, m)))
    w._tick()
    return w


try:
    gui._hwnd_alive = lambda h: True          # the window is still open
    w = _ghost_watcher()
    st_before = w._states[7]
    st_before.last_sent_utc = T0              # a cooldown only memory knows
    st_before.chooser_sig = "answered-once"   # a chooser already answered
    reset([], {})                             # UIA hands back nothing
    w._tick()
    check("F3 a still-open window missed by one pass keeps its state",
          7 in w._states)
    check("F4 and keeps which chooser was already answered",
          getattr(w._states.get(7), "chooser_sig", None) == "answered-once")
    check("F5 and keeps the send cooldown",
          getattr(w._states.get(7), "last_sent_utc", None) == T0)
    check("F6 the hiccup is reported rather than passing silently",
          any(k == "warn" and "missing from this pass" in m for k, m in LOGS_F))
    check("F6b and names WHICH window, so a repeat offender can be spotted",
          any("missing from this pass" in m and f"#{gui._wid(7)}" in m
              for _, m in LOGS_F))
    LOGS_F.clear()
    w._tick()
    check("F7 and reported once, not once per tick",
          not any("missing from this pass" in m for _, m in LOGS_F))

    # A queued row command must survive the same hiccup: the window is still
    # there to receive it.
    w2 = _ghost_watcher(11)
    w2._cmd_fire_now.add(11)
    reset([], {})
    w2._tick()
    check("F8 a queued row command survives a missed pass",
          11 in w2._cmd_fire_now or 11 in w2._states)

    # And the real close still prunes, so a recycled handle starts clean.
    gui._hwnd_alive = lambda h: False
    w3 = _ghost_watcher(13)
    reset([], {})
    w3._tick()
    check("F9 a genuinely closed window still drops its state",
          13 not in w3._states)

finally:
    gui._hwnd_alive = _alive_real

# Log lines name a window by title, and titles are neither unique nor stable:
# two sessions on the same task write identical lines, and a session renames
# itself as it works. Without an id, "was this window poked twice inside the
# retry interval?" cannot be answered from the log at all -- separate windows
# blur together and one window splits into several.
set_now(T0)
reset([(0x1234, "Claude Code"), (0x5678, "Claude Code")],
      {0x1234: RETRY_EXHAUSTED, 0x5678: RETRY_EXHAUSTED})
w = new_watcher()
LOGS_W = []
w.log.connect(lambda k, m: LOGS_W.append(m))
w._tick()
check("W1 same-titled windows are told apart in the log",
      any("#1234" in m for m in LOGS_W) and any("#5678" in m for m in LOGS_W))
check("W2 the id rides along with the human-readable title",
      any("'Claude Code' #1234" in m for m in LOGS_W))
# The id must follow the WINDOW, not the name it happens to be showing.
reset([(0x1234, "a totally different task now")], {0x1234: RETRY_EXHAUSTED})
LOGS_W.clear()
advance(w._retry_interval + 5)
w._tick()
check("W3 the id survives the window renaming itself",
      any("#1234" in m for m in LOGS_W))

# A log is safe where it lives and unsafe once it travels: a Claude Code
# window is named after its conversation, and the after-finish prompt is the
# user's own words. Redaction has to drop those while keeping what a
# diagnosis needs -- the id, the sentence, and literal slash commands.
NL = chr(10)
_r = gui.redact_log_line
_secret = "the Q4 pricing model for client X"
_cases = [
    ("2026-01-01 00:00:00  [warn]  network retries exhausted on "
     "'" + _secret + "' #0a40; sending 'continue' every 600s until recovery"),
    ("2026-01-01 00:00:00  [fire]  after-finish → '" + _secret +
     "' #0a40: '" + _secret + "'"),
    ("2026-01-01 00:00:00  [fire]  sending ['/effort high', '', 'continue'] "
     "to 'win' #1234"),
    ("2026-01-01 00:00:00  [info]  after-finish on '" + _secret +
     "': no runs left (Loops 0)"),
]
_out = [_r(c) for c in _cases]
check("R1 nothing that names the work survives redaction",
      not any(_secret in o for o in _out))
check("R2 the window id survives, since that is what identifies it",
      all("#0a40" in o for o in _out[:2]))
check("R3 literal slash commands survive — they describe no work",
      "/effort high" in _out[2])
check("R4 'continue' survives too", "'continue'" in _out[0])
check("R5 a pre-id line still loses its title",
      "'…'" in _out[3] and _secret not in _out[3])
check("R6 a line naming no window is left alone",
      _r("2026-01-01 00:00:00  [info]  up to date (v2.0.17)")
      == "2026-01-01 00:00:00  [info]  up to date (v2.0.17)")

# An unreadable screen must SAY so. Every detector reports "nothing on
# screen" for a window it could not read exactly as for an empty one, so a
# log without this line cannot answer whether the tool looked at all.
set_now(T0)
reset([(21, "win")], {21: ""})
w = new_watcher()
LOGS_R = []
w.log.connect(lambda k, m: LOGS_R.append((k, m)))
w._tick()
check("R7 an unreadable screen is reported",
      any(k == "warn" and "could not read the screen" in m for k, m in LOGS_R))
LOGS_R.clear()
advance(60)
w._tick()
check("R8 and only once, not once per tick",
      not any("could not read the screen" in m for _, m in LOGS_R))
TEXTS[21] = "back on screen" + NL + "> "
advance(60)
w._tick()
check("R9 recovery of the read is reported too",
      any("readable again" in m for _, m in LOGS_R))


# =============================================================================
print("---- G: after-finish types the follow-up when a run completes ----")
# A watched session that finishes its run sits idle until a human notices.
# The per-window after-finish prompt keeps it producing — but only a session
# that actually RAN and then went quiet counts as finished; a fresh shell at
# a prompt, a blocked session, or a fresh fire's cooldown must not qualify.
NOTICE_G = ("API Error: Fable 5's safegu" "ards flagged this message. "
            "Claude Code can't respond with Fable 5.")
IDLE = "some finished output\n> "


def af_watcher(cmd="next task: refactor the parser", loops=None):
    set_now(T0)
    reset([(9, "win")], {9: IDLE + "\n" + SPIN})
    w = new_watcher()
    w.set_after_finish({"win": cmd})
    if loops is not None:
        w.set_after_finish_loops({"win": loops})
    return w


def af_cycle(w, cmd_output=IDLE):
    """Drive one completed run: running seen -> idle -> settle -> tick."""
    TEXTS[9] = cmd_output + "\n" + SPIN
    w._tick()
    TEXTS[9] = cmd_output
    advance(60)
    w._tick()
    advance(gui.AFTER_FINISH_SETTLE_S)
    w._tick()


w = af_watcher()
w._tick()                                       # running observed
TEXTS[9] = IDLE                                 # turn ends
advance(60)
w._tick()                                       # idle #1 — starts the clock
check("G1 no fire on the first idle sighting", not SENT)
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G2 fires the configured prompt once settled",
      SENT == [(9, ["next task: refactor the parser"])])
SENT.clear()
advance(120)
w._tick()
advance(120)
w._tick()
check("G3 does not repeat while still idle", not SENT)
# The prompt started a run which finished again — but Loops counts the runs
# REMAINING and the default is 1, now spent. Typing the same follow-up after
# every run is an infinite loop unless the user asked for it.
af_cycle(w)
check("G4 the default single run is spent — no second fire", not SENT)
check("G4a and the remaining count was decremented to zero",
      w._after_finish_loops.get("win") == 0)

# Reported 2026-09-23: Claude Code REPLACES the spinner line, timer and all,
# while it waits out a first-byte timeout on its own ("retrying once, waiting
# up to 10m"). With no timer on screen the turn looked finished, so the 90s
# settle ran out and the next task was typed into a request still in flight.
# Split so this file cannot trip the watchdog it tests.
_WAITING = ("✻ No resp" "onse from the API after 6m · retrying once, "
            "waiting up to 10m")
SENT.clear()
w = af_watcher()
w._tick()                                       # running observed
TEXTS[9] = IDLE + chr(10) + _WAITING              # first-byte wait begins
for _ in range(12):                             # 12 minutes of waiting
    advance(60)
    w._tick()
check("G2w a first-byte retry wait is not a finished turn -- nothing typed "
      "into a request that is still in flight", not SENT)
TEXTS[9] = IDLE                                 # the retry landed and finished
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G2x and once it really finishes, after-finish still fires",
      SENT == [(9, ["next task: refactor the parser"])])
SENT.clear()

# Spending a run is reported so the GUI can persist it — the whole reason
# Loops counts DOWN instead of up.
w = af_watcher()
SPENT = []
w.loops_spent.connect(lambda k, n: SPENT.append((k, n)))
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G4b the decrement is announced for persistence",
      SPENT == [("win", 0)])
SENT.clear()

# ...and a restart therefore cannot re-arm it: a fresh worker loading the
# persisted 0 must leave the window alone, even though the prompt text is
# still configured. This is the failure the countdown exists to prevent —
# the window's NEXT task inheriting the previous task's follow-up.
w = af_watcher(loops=0)
AF_LOGS = []
w.log.connect(lambda k, m: AF_LOGS.append((k, m)))
for _ in range(2):
    af_cycle(w)
check("G4c a restart with 0 runs left types nothing", not SENT)
check("G4d and it says so exactly once",
      len([m for k, m in AF_LOGS if "no runs left" in m]) == 1)

# Loops = unlimited (-1) is the old behaviour: re-arm after every run, and
# never decrement.
w = af_watcher(loops=-1)
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()                                       # fire 1
SENT.clear()
af_cycle(w)                                     # fire 2
check("G4e unlimited re-arms after every completed run",
      SENT == [(9, ["next task: refactor the parser"])])
check("G4f and unlimited never counts down",
      w._after_finish_loops.get("win") == -1)
SENT.clear()

# Loops = 2: two fires, counting down, then it stops.
w = af_watcher(loops=2)
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()                                       # fire 1
check("G4g one run spent leaves one",
      w._after_finish_loops.get("win") == 1)
af_cycle(w)                                     # fire 2
check("G4h Loops=2 fires twice", len(SENT) == 2)
SENT.clear()
af_cycle(w)                                     # spent
check("G4i the third completed run gets nothing", not SENT)

# Setting Loops again re-arms the same prompt — no need to retype it.
w.set_after_finish_loops({"win": 1})
af_cycle(w)
check("G4j setting Loops again re-arms the prompt",
      SENT == [(9, ["next task: refactor the parser"])])
SENT.clear()

# Dry-run must not spend the budget: the send types nothing and still
# reports success, so counting there charged a run that never happened — and
# because the count is persisted, one dry-run pass left every configured
# prompt at zero runs for good.
w = af_watcher()
w._dry_run = True
SPENT = []
w.loops_spent.connect(lambda k, n: SPENT.append((k, n)))
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S)
w._tick()
check("G4k dry-run leaves the remaining count alone",
      w._after_finish_loops.get("win", 1) == 1 and not SPENT)
SENT.clear()


# Never ran while watched -> never fires.
set_now(T0)
reset([(9, "win")], {9: IDLE})
w = new_watcher()
w.set_after_finish({"win": "anything"})
for _ in range(3):
    w._tick()
    advance(gui.AFTER_FINISH_SETTLE_S + 5)
check("G5 a session never seen running is left alone", not SENT)

# A standing safeguard notice is a BLOCK, not a finish.
w = af_watcher()
w._tick()
TEXTS[9] = NOTICE_G + "\n" + IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S + 5)
w._tick()
check("G6 a blocked session is not poked", not SENT)

# No configured command -> feature entirely off for the window.
set_now(T0)
reset([(9, "win")], {9: IDLE + "\n" + SPIN})
w = new_watcher()
w._tick()
TEXTS[9] = IDLE
advance(60)
w._tick()
advance(gui.AFTER_FINISH_SETTLE_S + 5)
w._tick()
check("G7 no command, no keystrokes", not SENT)

# Sanitisation: garbage collapses instead of reaching the tick.
w = new_watcher()
w.set_after_finish({"win": "   ", "other": 42, "ok": " do things "})
check("G8 blank entries are dropped, values coerced and trimmed",
      w._after_finish == {"other": "42", "ok": "do things"})
w.set_after_finish_loops({"a": "2", "b": -5, "c": "abc", "d": 0})
check("G9 loops values are coerced; garbage dropped, unlimited clamped",
      w._after_finish_loops == {"a": 2, "b": -1, "d": 0})
w.set_after_finish_loops("not-a-dict")
check("G10 a corrupt loops store collapses to defaults",
      w._after_finish_loops == {})

# The stored countdown is read back through the same coercion the worker
# uses, so a hand-edited or truncated store cannot raise at start-up.
check("G11 a stored 0 means spent and -1 unlimited, as written",
      gui.coerce_after_finish_loops({"a": 0, "b": 3, "c": -1})
      == {"a": 0, "b": 3, "c": -1})
check("G11c garbage and out-of-range values are coerced, not raised",
      gui.coerce_after_finish_loops(
          {"a": "2", "b": "x", "c": -9, "d": None})
      == {"a": 2, "c": -1})
check("G11d a non-dict store collapses instead of exploding",
      gui.coerce_after_finish_loops("not-a-dict") == {}
      and gui.coerce_after_finish_loops(None) == {})

# The After finish dialog snapshots the remaining count when it opens, and a
# modal exec() keeps pumping queued signals — so the worker can spend a run
# while the dialog is up. Writing the untouched pre-fire number back on OK
# would silently re-arm a prompt that had just been used, which is exactly
# the repeat-firing the budget exists to prevent.
check("G12 a run spent while the dialog was open wins over the snapshot",
      gui.reconcile_loops(chosen=1, snapshot=1, fired=0) == 0)
check("G12b but moving the spinner is an explicit instruction and wins",
      gui.reconcile_loops(chosen=5, snapshot=1, fired=0) == 5)
check("G12c with no fire in the meantime the dialog's value stands",
      gui.reconcile_loops(chosen=1, snapshot=1, fired=None) == 1)
check("G12d setting the spinner back to the snapshot after a fire re-arms",
      gui.reconcile_loops(chosen=0, snapshot=0, fired=0) == 0)
check("G12e unlimited chosen during a fire is still honoured",
      gui.reconcile_loops(chosen=-1, snapshot=1, fired=0) == -1)


# =============================================================================
print("---- H: auto-answering a chooser the session is waiting on ----")
# Claude Code stops on a chooser and waits; until someone picks, the window
# is idle for no better reason than an unanswered question. Enter accepts the
# option it already pre-selected. Fixtures are de-fanged (the marker and the
# digit split apart) so this file cannot make a watched terminal press Enter.
_M = "> "
CHOOSER = "Ready to continue?\n" + _M + "1" ". Keep going\n  2" ". Stop here"
PERMISSION = ("Do you want to proceed?\n" + _M + "1" ". Yes\n"
              "  2" ". Yes, and don" + chr(8217) + "t ask again\n"
              "  3" ". No, and tell Claude what to do differently")
# An input box with nothing typed in it. NOT what sits under an open
# chooser — a chooser replaces the box entirely while it waits, measured on
# a live prompt — so this belongs only to scenarios about the box itself.
EMPTY_BOX = "\n>" + chr(0xa0) + "   "
# What an open chooser really looks like from below: the hint, no box.
NO_BOX = "\nEnter to select - Tab/Arrow keys to navigate - Esc to cancel"
DRAFT_BOX = "\n>" + chr(0xa0) + "half a thought"

set_now(T0)
reset([(20, "win")], {20: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)          # choosers ship off; this is
                                       # a chooser scenario
w._tick()
check("H1 an ordinary chooser is answered with a bare Enter",
      SENT == [(20, [""])])
SENT.clear()
w._tick()
advance(30)
w._tick()
check("H2 the same chooser is answered exactly once", not SENT)

# The answered text lingers, so a NEW chooser must still be recognised.
TEXTS[20] = ("Ready to continue?\n" + _M + "1" ". Something else\n"
             "  2" ". No" + NO_BOX)
advance(5)
w._tick()
check("H3 a different chooser is answered", SENT == [(20, [""])])
SENT.clear()

# A draft in the input box turns Enter into "submit my half-written message".
set_now(T0)
reset([(21, "win")], {21: CHOOSER + DRAFT_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H4 nothing is pressed while the user has text typed", not SENT)

# Mid-turn there is nothing to answer.
set_now(T0)
reset([(22, "win")], {22: CHOOSER + "\n" + SPIN + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H5 a running turn is left alone", not SENT)

# Permission requests authorise work, so they ride on their own switch.
set_now(T0)
reset([(23, "win")], {23: PERMISSION + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, False)                  # choosers on, permission off
w._tick()
check("H6 a permission request is NOT answered by the chooser switch",
      not SENT)
w.set_auto_answer(True, True)
w._tick()
check("H7 ...and IS answered once its own switch is on",
      SENT == [(23, [""])])
SENT.clear()

set_now(T0)
reset([(24, "win")], {24: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(False, True)                  # choosers off
w._tick()
check("H8 an ordinary chooser is left alone when its switch is off",
      not SENT)

# Both default ON — a fresh worker answers without being configured.
# The two do not ship the same way, on purpose. A permission request only
# says yes to work already asked for; a chooser is where a DECISION sits —
# which option, and whether a written plan starts running — so that one is
# the owner's to switch on.
check("H9 choosers ship OFF, permission requests ship ON",
      new_watcher()._auto_choose is False
      and new_watcher()._auto_permission is True)

# The shape a real chooser actually has: no input box at all. Every fixture
# above prints one, which is what let a "no box means maybe a draft" rule
# pass the suite while refusing to act on every real chooser.
set_now(T0)
reset([(26, "win")], {26: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H14 a chooser that replaced the input box is still answered",
      SENT == [(26, [""])])
SENT.clear()

# Questions arrive in runs — a form asks several and then offers Submit as
# one more chooser — so after answering one, look again soon instead of
# waiting a whole poll. Measured live: a four-step form took three minutes
# at the default interval.
gui.QTimer.scheduled = []
set_now(T0)
reset([(28, "win")], {28: CHOOSER + NO_BOX})
w = new_watcher()
w.set_auto_answer(True, True)
w._tick()
check("H16 answering schedules a fast follow-up tick",
      SENT == [(28, [""])] and w._retick_at is not None)
SENT.clear()

# The safeguard picker and the "Switch model?" confirmation are choosers
# too, but confirming either CHANGES WHICH MODEL the session runs on, and
# that is the user's decision -- so neither is ever auto-answered.
_SG2 = "safegu" "ards flagged"
_SW2 = "Switch t" "o"
_ED2 = "Edit promp" "t and retry"
PICKER2 = (f"Session paused\n\nFable 5's {_SG2} this message.\n\n"
           f"> " "1" f". {_SW2} Opus 5\n  2" f". {_ED2} with Fable 5")
DIALOG2 = ("Switch model?\n> " "1" ". Yes, swi" "tch to Opus 5\n"
           "  2" ". No, go b" "ack")

for _name, _text in (("safeguard picker", PICKER2),
                     ("switch-model dialog", DIALOG2)):
    set_now(T0)
    reset([(27, "win")], {27: _text + NO_BOX})
    w = new_watcher()
    w._tick()
    w._tick()
    check(f"H15 the {_name} is left for the user, not auto-answered",
          not SENT)

# A send is REFUSED when the window will not come to the foreground — which
# is correct, since typing blind is how keys land in the wrong app. What was
# not correct was announcing the answer first: live, a window that could not
# be focused produced a "fire" line claiming it had been answered, once per
# poll, while nothing had been typed at all.
set_now(T0)
reset([(25, "win")], {25: CHOOSER + NO_BOX})
_ok = {"v": False}
_saved_send = gui.send_text_lines
gui.send_text_lines = lambda w, lines, dry_run=False, outcome=None: (
    _send_lines(w, lines) if _ok["v"] else False)
LOGS_H = []
w = new_watcher()
w.set_auto_answer(True, True)
w.log.connect(lambda k, m: LOGS_H.append((k, m)))
try:
    for _ in range(3):
        advance(5)
        w._tick()
    check("H10 a refused send types nothing", not SENT)
    check("H11 and is not reported as an answer",
          not any("answered the" in m for _k, m in LOGS_H))
    check("H12 the failure is reported once, not once per poll",
          len([m for k, m in LOGS_H
               if k == "warn" and "wouldn't come forward" in m]) == 1)
    # Once the window can be focused, the answer goes through and says so.
    _ok["v"] = True
    advance(5)
    w._tick()
    check("H13 the retry succeeds and reports the answer",
          SENT == [(25, [""])]
          and any("answered the chooser" in m for _k, m in LOGS_H))
finally:
    gui.send_text_lines = _saved_send
SENT.clear()



# ---------------------------------------------------------------------------
# S: @pyqtSlot signatures must match the signals wired to them
# ---------------------------------------------------------------------------
# A decorator that declares FEWER arguments than its signal does not fail
# loudly. PyQt truncates the emitted arguments, the call raises TypeError
# inside the slot, and Qt answers an unhandled slot exception with qFatal --
# a hard process abort. That is what killed the packaged exe with 0xc0000409
# in Qt6Core a few seconds after every after-finish send: @pyqtSlot(str) on
# _on_loops_spent, fed by loops_spent = pyqtSignal(str, int). Nothing showed
# up in the activity log, because the process died inside the slot, before
# the slot's own log line. Three separate crashes before it was caught, and
# each one left every watched window unwatched.
import re as _re

_src = open("gui.py", encoding="utf-8").read()
_SIG = _re.compile(r"(\w+)\s*=\s*pyqtSignal\(([^)]*)\)")
_SLOT = _re.compile(r"@pyqtSlot\(([^)]*)\)\s*(?:\n\s*#[^\n]*)*\n\s*def\s+(\w+)\(")
_DEF = _re.compile(r"def\s+(\w+)\(self(?:,\s*([^)]*))?\)")
_CONN = _re.compile(r"\.(\w+)\.connect\(self\.(?:worker\.)?(\w+)\)")


def _count(s):
    return len([x for x in s.split(",") if x.strip()])


_sigs = {m.group(1): _count(m.group(2)) for m in _SIG.finditer(_src)}
_slots = {m.group(2): _count(m.group(1)) for m in _SLOT.finditer(_src)}
_defs = {m.group(1): _count(m.group(2) or "") for m in _DEF.finditer(_src)}

check("S0 the scan actually found the slots (guards against a dead regex)",
      len(_slots) > 20 and "_on_loops_spent" in _slots)

_bad = [f"{n}: slot declares {c}, function takes {_defs[n]}"
        for n, c in _slots.items() if n in _defs and _defs[n] != c]
check("S1 every @pyqtSlot matches its function arity: "
      + (_bad[0] if _bad else "-"), not _bad)

_wired = [f"{sig}({_sigs[sig]}) -> {slot}({_slots[slot]})"
          for sig, slot in
          [(m.group(1), m.group(2)) for m in _CONN.finditer(_src)]
          if sig in _sigs and slot in _slots
          and _sigs[sig] != _slots[slot]]
check("S2 every connected signal matches its slot: "
      + (_wired[0] if _wired else "-"), not _wired)

check("S3 loops_spent still carries BOTH key and remaining count",
      _sigs.get("loops_spent") == 2 and _slots.get("_on_loops_spent") == 2)


# ---------------------------------------------------------------------------
# T: a limit banner read AFTER its reset already passed
# ---------------------------------------------------------------------------
# The banner carries only a time of day, so "the next 9:30pm" is tomorrow once
# 9:30pm has gone. Reading one late — an unwatched window, or a pattern that
# only began matching after an update — then parks the session for a full day
# over a limit that lifted hours ago. Measured live on 2026-08-11: a 21:30
# reset parsed at 21:51 was scheduled for 21:31 the NEXT day.
_stale = gui.STALE_RESET_H
check("T1 the staleness horizon clears the 5-hour window", _stale >= 6)

# The rule itself, on explicit datetimes so no frozen clock is involved:
# anything computed further out than the horizon is a banner whose moment has
# gone, and anything inside it is a real wait.
_base = datetime(2026, 8, 11, 14, 0, tzinfo=pytz.UTC)


def _is_stale(hours_out):
    return (_base + timedelta(hours=hours_out)) - _base > timedelta(
        hours=gui.STALE_RESET_H)


check("T2 a reset 23h out is treated as a banner already passed",
      _is_stale(23))
check("T3 a reset 2h out is a real wait, not stale", not _is_stale(2))
check("T4 the boundary is exclusive, so 6h exactly still waits",
      not _is_stale(gui.STALE_RESET_H))

# And the guard has to be wired into the scheduling path, not just defined.
_gsrc = open("gui.py", encoding="utf-8").read()
check("T5 the scheduler consults the horizon before arming a countdown",
      "STALE_RESET_H" in _gsrc.split("new_reset = next_reset_datetime")[1][:2000])

# =============================================================================
print("---- V: the local API hands typing to the watcher ----")
# The HTTP side is covered in test_local_api.py. What matters here is the far
# end: an external caller must go through the same window lookup and the same
# dry-run promise as the tool's own sends, because it reaches the terminal by
# exactly the same route.
import queue as _q                                          # noqa: E402

set_now(T0)
reset([(31, "win")], {31: IDLE})
w = new_watcher()
LOGS_V = []
w.log.connect(lambda k, m: LOGS_V.append((k, m)))
SENT.clear()

_out = _q.Queue(1)
w.api_send(31, ["hello"], _out)
check("V1 an external send reaches the window",
      _out.get_nowait() == {"ok": True} and SENT == [(31, ["hello"])])
check("V2 and is logged as coming from outside, at fire level",
      any(k == "fire" and "api: send" in m for k, m in LOGS_V))

SENT.clear()
_out = _q.Queue(1)
w.api_send(999, ["hello"], _out)
check("V3 an hwnd that is not there says so and types nothing",
      _out.get_nowait() == {"ok": False, "reason": "no_window"} and not SENT)

# Dry-run is a promise that nothing gets typed. It has to hold for callers
# too, or the checkbox means less than it says.
SENT.clear()
LOGS_V.clear()
w.set_dry_run(True)
_out = _q.Queue(1)
w.api_send(31, ["hello"], _out)
res = _out.get_nowait()
check("V4 dry-run types nothing for an external caller either",
      res.get("ok") and res.get("dry_run") and not SENT)
check("V5 and still says a caller asked",
      any("api: send" in m for _, m in LOGS_V))
w.set_dry_run(False)

# A send that the foreground check refuses must be reported, not swallowed.
SENT.clear()
_real_send = gui.send_text_lines
gui.send_text_lines = lambda *a, **k: False
try:
    _out = _q.Queue(1)
    w.api_send(31, ["hello"], _out)
    check("V6 a refused send is reported as foreground, not as success",
          _out.get_nowait() == {"ok": False, "reason": "foreground"})
finally:
    gui.send_text_lines = _real_send

# A caller may type WORDS through /keys, not just key names — a companion
# program did exactly that on 2026-09-21 and the log kept the whole prompt.
# The log says what this tool did; it is not a place for someone's prompt.
SENT.clear()
LOGS_V.clear()
_secret_keys = "转写这段音频{Enter}"
_out = _q.Queue(1)
w.api_keys(31, _secret_keys, _out)
_keylines = [m for k, m in LOGS_V if "api: keys" in m]
check("V7 an external keystroke is still logged at fire level",
      len(_keylines) == 1 and any(k == "fire" and "api: keys" in m
                                  for k, m in LOGS_V))
check("V8 but the typed words are NOT in the log line",
      "转写" not in _keylines[0] and "音频" not in _keylines[0])
check("V9 — a count stands in for them",
      "<6 chars>" in _keylines[0])
check("V10 while the key name survives, which is the part worth reading",
      "{Enter}" in _keylines[0])

# The same promise under dry-run, which logs on a different line of code.
LOGS_V.clear()
w.set_dry_run(True)
_out = _q.Queue(1)
w.api_keys(31, _secret_keys, _out)
_dry = [m for k, m in LOGS_V if "api: keys" in m]
check("V11 dry-run's own log line keeps the words out too",
      len(_dry) == 1 and "音频" not in _dry[0] and "<6 chars>" in _dry[0])
w.set_dry_run(False)

# Text typed after an unsent draft is submitted with it as one message
# (2026-09-27: a companion's preset went out as the tail of a half sentence).
# A draft is the user's: nothing typed, nothing cleared, the caller told.
SENT.clear()
LOGS_V.clear()
TEXTS[31] = "earlier output\n> an old prompt echo\n❯\xa0half a sentence"
_out = _q.Queue(1)
w.api_send(31, ["summarise progress"], _out)
check("V15 a send onto a draft is refused as 'draft'",
      _out.get_nowait() == {"ok": False, "reason": "draft"})
check("V16 and nothing is typed, so the draft is left as it was",
      SENT == [])
check("V17 the log says why, without quoting the draft",
      any("not typed" in m and "half a sentence" not in m for _, m in LOGS_V))
TEXTS[31] = "earlier output\n> an old prompt echo\n❯\xa0"
_out = _q.Queue(1)
w.api_send(31, ["summarise progress"], _out)
check("V18 an empty box takes the send, and an old prompt echo above is "
      "not mistaken for a draft",
      _out.get_nowait() == {"ok": True}
      and SENT == [(31, ["summarise progress"])])
TEXTS[31] = IDLE

# A spec uiautomation cannot press is refused before the window is touched,
# and named as such -- 'foreground' would tell the caller to try again.
_pressed = []
_real_keys_v = gui.send_keys
gui.send_keys = lambda w, k, dry_run=False: _pressed.append(k) or True
try:
    _out = _q.Queue(1)
    w.api_keys(31, "{Backspace}", _out)
    _r = _out.get_nowait()
    check("V12 an unknown key name is answered bad_keyspec, naming it",
          _r == {"ok": False, "reason": "bad_keyspec", "key": "{Backspace}"})
    check("V13 and the window is not touched at all", _pressed == [])
    _out = _q.Queue(1)
    w.api_keys(31, "{Back}", _out)
    check("V14 while the real name goes through",
          _out.get_nowait() == {"ok": True} and _pressed == ["{Back}"])
finally:
    gui.send_keys = _real_keys_v

# =============================================================================
print("---- X: the snapshot says what a window is waiting on ----")
# A companion program showing "this session needs you" reads this field. It
# follows the same rule as `running`: only a readable pass may change it, or a
# UIA hiccup would report every waiting window as no longer waiting — which is
# the one moment someone is actually waiting to be told.
_XN = chr(0x276F)
_XQ = "Claude has written up a plan. Would you like to proceed?"
CHOOSE = _XQ + chr(10) + _XN + " 1" ". Yes" + chr(10) + "  2" ". No"

set_now(T0)
reset([(41, "win")], {41: CHOOSE})
w = new_watcher()
w._tick()
row = w._make_snapshot()[0]
check("X1 a waiting window reports what kind of waiting",
      row["prompt"] and row["prompt"]["kind"] == "chooser")
check("X2 and carries the question, not the option row",
      row["prompt"]["text"] == _XQ)
check("X3 with the time it was first seen",
      row["prompt"]["seen_at"] is not None)
_first_seen = row["prompt"]["seen_at"]

# The same prompt still there a minute later is not a NEW prompt: re-stamping
# it every pass would make "waiting 4 minutes" impossible to show.
advance(60)
w._tick()
check("X4 seen_at marks when it appeared, not when it was last looked at",
      w._make_snapshot()[0]["prompt"]["seen_at"] == _first_seen)

# An unreadable pass must not report the prompt as gone.
TEXTS[41] = ""
advance(60)
w._tick()
check("X5 an unreadable pass keeps the last known answer",
      w._make_snapshot()[0]["prompt"] is not None)

# ...but a readable pass showing no prompt does clear it.
TEXTS[41] = "all done here" + chr(10) + "> "
advance(60)
w._tick()
check("X6 a readable pass with nothing waiting clears it",
      w._make_snapshot()[0]["prompt"] is None)

# =============================================================================
print("---- Y: the on-demand rescan reads, and only reads ----")
# A caller polling this every few seconds must never be able to make the tool
# type. Deciding to send belongs to the tick alone; this refreshes what is
# KNOWN about a window and returns it.
import queue as _q2

set_now(T0)
reset([(51, "win")], {51: IDLE})
w = new_watcher()
w._tick()
SENT.clear()
TEXTS[51] = CHOOSE                      # a prompt appears between passes
out = _q2.Queue(1)
w.api_scan(51, out)
res = out.get_nowait()
check("Y1 a rescan sees a prompt the last pass could not have",
      res["ok"] and res["window"]["prompt"]["kind"] == "chooser")
check("Y2 and types nothing while doing it", not SENT)
check("Y3 the refreshed row is the same shape the snapshot serves",
      set(res["window"]) == set(w._make_snapshot()[0]))
check("Y4 and the snapshot now agrees with it",
      w._make_snapshot()[0]["prompt"] is not None)

out = _q2.Queue(1)
w.api_scan(999, out)
check("Y5 an hwnd that is not there says so",
      out.get_nowait() == {"ok": False, "reason": "no_window"})

TEXTS[51] = ""
out = _q2.Queue(1)
w.api_scan(51, out)
check("Y6 an unreadable screen is read_blind, not an empty screen",
      out.get_nowait() == {"ok": False, "reason": "read_blind"})
check("Y7 and the previous answer still stands",
      w._make_snapshot()[0]["prompt"] is not None)

# Dry-run is a promise about keystrokes; reading is not a keystroke.
TEXTS[51] = CHOOSE
w.set_dry_run(True)
out = _q2.Queue(1)
w.api_scan(51, out)
check("Y8 dry-run does not suppress a read", out.get_nowait()["ok"])
w.set_dry_run(False)

# Claude Code renames its window to a summary of the conversation as soon as
# the first message lands. A caller matching sessions to windows by name would
# be told the old title until the next full pass — a minute of looking at the
# wrong window — so the rescan re-reads it.
WINDOWS[0].Name = "✳ a completely new title"
out = _q2.Queue(1)
w.api_scan(51, out)
row = out.get_nowait()["window"]
check("Y9 a rescan picks up a renamed window straight away",
      row["title"] == "✳ a completely new title")
check("Y10 and title_key follows it",
      row["title_key"] == "a completely new title")

# The LIST must follow a rescan too, and before the rescan answers. /windows
# served the last pass's rows, so a caller that rescanned and then listed was
# told a prompt was there, then that it was not, for up to a pass (Claude
# Control, 2026-09-27: its approval page reopened every few seconds).
reset([(52, "win")], {52: IDLE})
w = new_watcher()
w._tick()
_seen_at_answer = []


class _Peek:
    """An out-queue that looks at the published rows when the answer lands."""
    def put(self, r):
        _seen_at_answer.append((r, [dict(x) for x in (w.latest_rows or [])]))


TEXTS[52] = CHOOSE
w.api_scan(52, _Peek())
_r, _rows_then = _seen_at_answer[-1]
check("Y11 a rescan's prompt is already in the published rows when it "
      "answers", _r["ok"] and _rows_then
      and _rows_then[0]["prompt"] is not None)
_host_y = gui.MainWindow.__new__(gui.MainWindow)
_host_y.worker = w
_host_y.codex_service = None  # Explicit fixture state; Qt __init__ is not run.
_host_y._latest_snapshot = []               # the GUI copy lags; not used
_listed = gui.MainWindow._api_snapshot(_host_y)["windows"]
check("Y12 so /windows says what the rescan said",
      _listed and _listed[0]["prompt"] is not None)
check("Y13 every row says when its screen was read",
      _listed[0]["read_utc"] == FakeDT._now.isoformat())

# A rescan of a window this tool once typed into carried a raw datetime and
# could not be turned into JSON, so the caller got no reply (2026-09-27).
import json as _jy
_st = w._states[52]
_st.last_sent_utc = FakeDT._now
_host_y._api_last_scan = {}
_host_y.sig_api_wake = type("W", (), {"emit": staticmethod(w.api_drain)})()
_host_y._api_last_send = {}
_resp = gui.MainWindow._api_do_scan(_host_y, 52)
try:
    _js = _jy.dumps(_resp)
except TypeError:
    _js = None
check("Y14 a rescan of a window that was once sent to is valid JSON",
      _js is not None and _resp["window"]["last_sent_utc"]
      == FakeDT._now.isoformat())
check("Y15 and so is the list", _jy.dumps(
    gui.MainWindow._api_snapshot(_host_y)) is not None)

# =============================================================================
print("---- Z2: the one API route that changes the user's settings ----")
# test_local_api.py stops at the ctx boundary, so the half that actually
# touches QSettings, the checkbox and the log had never run. That half is
# where the surprise would live if it lived anywhere.
import queue as _q3
from PyQt6.QtCore import QSettings as _QS


class _FakeSig:
    def __init__(self): self.sent = []
    def emit(self, *a): self.sent.append(a)


_win = gui.MainWindow.__new__(gui.MainWindow)
# A settings domain of its own: a test must never reach into the config a
# running instance is using (this file learned that the hard way).
_win.settings = _QS("auto_continue_selftest", "gui")
_win.settings.clear()
_win._auto_permission, _win._auto_choose = True, False
_zlogs = []
_win._append_log = lambda lvl, msg: _zlogs.append((lvl, msg))
_win.sig_set_auto_answer = _FakeSig()

_o = _q3.Queue(1)
gui.MainWindow._api_apply_settings(_win, {"auto_permission": False}, _o)
_res = _o.get_nowait()
check("Z2a the caller is told the new state, not just 'ok'",
      _res == {"ok": True, "permission_autoanswer": False,
               "chooser_autoanswer": False})
check("Z2b the flag the tick reads actually changed",
      _win._auto_permission is False)
check("Z2c and it is persisted, not just held in memory",
      str(_win.settings.value("auto_permission")).lower() in ("false", "0"))
check("Z2d the change is logged at fire level — same as a keystroke, "
      "because it is the same kind of event",
      _zlogs == [("fire", "api: settings auto_permission=off")])
check("Z2e the watcher is told at once, not at the next dialog OK",
      _win.sig_set_auto_answer.sent == [(False, False)])

_zlogs.clear()
_o = _q3.Queue(1)
gui.MainWindow._api_apply_settings(_win, {"auto_permission": False}, _o)
_o.get_nowait()
check("Z2f setting it to what it already is says nothing — a client polling "
      "this must not fill the log", _zlogs == [])
_win.settings.clear()

# The hop that crosses threads. Every check above calls the handler directly,
# and test_local_api.py stubs the route — so the one piece that runs on a
# DIFFERENT thread was never executed by any test, and it was broken: the
# request hung for the full timeout on every call, caught only by driving a
# real instance before release. This drives it from a genuine non-Qt thread,
# the way an HTTP request does, and pumps the GUI event loop meanwhile.
import threading as _thz
from PyQt6.QtCore import QObject as _QObj, pyqtSignal as _pSig


class _Host(_QObj):
    sig_api_settings = _pSig(object, object)


_host = _Host()
_mw = gui.MainWindow.__new__(gui.MainWindow)
_mw.settings = _QS("auto_continue_selftest", "gui")
_mw.settings.clear()
_mw._auto_permission, _mw._auto_choose = True, False
_mw._append_log = lambda lvl, msg: None
_mw.sig_set_auto_answer = _FakeSig()
_mw.sig_api_settings = _host.sig_api_settings
_host.sig_api_settings.connect(
    lambda ch, o: gui.MainWindow._api_apply_settings(_mw, ch, o))

_result = {}


def _from_http_thread():
    _result["r"] = gui.MainWindow._api_do_settings(
        _mw, {"auto_permission": False})


_t = _thz.Thread(target=_from_http_thread)
_t0 = __import__("time").time()
_t.start()
while _t.is_alive() and __import__("time").time() - _t0 < 15:
    _app.processEvents()          # the GUI thread doing its job
    __import__("time").sleep(0.01)
_t.join(1)
_elapsed = __import__("time").time() - _t0
check("Z2g a request from a non-Qt thread completes instead of hanging",
      _result.get("r", {}).get("ok") is True)
check(f"Z2h and does so promptly, not by riding out the timeout "
      f"({_elapsed:.2f}s)", _elapsed < 3)
check("Z2i and the change really landed",
      _mw._auto_permission is False)
_mw.settings.clear()

# =============================================================================
print("---- Z3: a quiet fire is still a fire everywhere it is recorded ----")
# The GUI half of B5. 'fire-quiet' must differ from 'fire' in exactly one
# way — no tray balloon — and be indistinguishable from it in the log file,
# the log view and the API event stream. Anything else and the level is back
# to carrying two meanings at once.


class _FakeTray:
    def __init__(self): self.shown = []
    def showMessage(self, *a): self.shown.append(a)


_q4 = gui.MainWindow.__new__(gui.MainWindow)
_q4.tray = _FakeTray()
_q4._log_buffer = []
_written, _published, _rendered = [], [], []
_q4._write_log_file = lambda ts, lvl, msg: _written.append((lvl, msg))
_q4._api_publish = lambda kind, payload: _published.append((kind, payload))
_q4._render_log_line = lambda ts, lvl, msg: _rendered.append((lvl, msg))

gui.MainWindow._append_log(_q4, "fire", "loud one")
gui.MainWindow._append_log(_q4, "fire-quiet", "quiet one")

check("Z3a the quiet fire is written to the log FILE as a fire",
      _written == [("fire", "loud one"), ("fire", "quiet one")])
check("Z3b and shown in the log view as a fire, so 'fire-quiet' never "
      "reaches a reader as a level of its own",
      _rendered == [("fire", "loud one"), ("fire", "quiet one")])
check("Z3c and published on the API as a fire event — a companion program "
      "watching 'fire' must hear every keystroke",
      [k for k, _ in _published] == ["fire", "fire"])
check("Z3d the kept buffer agrees too, so a theme flip cannot reveal it",
      [lvl for _, lvl, _ in _q4._log_buffer] == ["fire", "fire"])
check("Z3e and the ONLY difference is the balloon",
      len(_q4.tray.shown) == 1 and _q4.tray.shown[0][1] == "loud one")

# =============================================================================
print("---- QT: a quick turn between passes is still a recovery ----")
# 2026-09-25: a session waiting on background shells answered the 'continue'
# in 29 seconds — between two passes — and went quiet again on purpose. With
# running never caught, the error stayed "current" and it was poked again ten
# minutes later for nothing.
_R = "─" * 60
ERR_ONLY = chr(10).join([ECONN, "", "  ✻ Worked for 3m · done 10:43", "",
                         _R, "❯ ", _R])
ANSWERED = chr(10).join([ECONN, "", "  ✻ Worked for 3m · done 10:43", "",
                         "> continue", "",
                         "● still at gate 1, waiting on the chain", "",
                         "  ✻ Cogitated for 29s · done 10:44 · 2 shells running",
                         "", _R, "❯ ", _R])
set_now(T0)
reset([(42, "win")], {42: ERR_ONLY})
w = new_watcher()
LOGS_QT = []
w.log.connect(lambda k, m: LOGS_QT.append((k, m)))
w._tick()
check("QT1 the stuck session is poked once", SENT == [(42, ["continue"])])
SENT.clear()
TEXTS[42] = ANSWERED                      # it answered and finished, unseen
advance(60)
w._tick()
check("QT2 the finished turn is read as recovery",
      any("cleared" in m for _, m in LOGS_QT)
      and not w._states[42].retry_active)
advance(w._retry_interval + 60)
w._tick()
check("QT3 and it is NOT poked again a retry interval later", not SENT)

# =============================================================================
print("---- SA: a view scrolled away from the live bottom is not acted on ----")
# Captured 2026-09-25: a session hard at work, its view scrolled up inside
# Claude Code. The rows on show held an OLD network error and nothing after it
# (the spinner and prompt box are simply not drawn while scrolled), with the
# "Jump to bottom" hint where they would be. That read as stuck, and
# 'continue' would have been typed into a turn in flight.
_RULE = "─" * 60
SCROLLED = chr(10).join([
    ECONN, "", "  ✻ Crunched for 18m 51s · done 9:50", "",
    "● Picking up round 82. I'm writing the docs now.",
    "  Made 1 scratchpad edit, ran 1 shell command",
    "                                 Jump to bottom (ctrl+End) ↓",
    _RULE])
AT_BOTTOM_STUCK = chr(10).join([ECONN, "", "  ✻ Crunched for 18m 51s · done 9:50",
                                "", _RULE, "❯ ", _RULE])
set_now(T0)
reset([(41, "win")], {41: SCROLLED})
w = new_watcher()
LOGS_SA = []
w.log.connect(lambda k, m: LOGS_SA.append((k, m)))
w._tick()
check("SA1 an old error seen while scrolled away types nothing", not SENT)
check("SA2 and says why, once",
      sum(1 for _, m in LOGS_SA if "scrolled up" in m) == 1)
advance(700)
w._tick()
check("SA3 still scrolled a retry interval later: still nothing, and no "
      "second announcement",
      not SENT and sum(1 for _, m in LOGS_SA if "scrolled up" in m) == 1)
check("SA4 its state is kept while it is out of sight", 41 in w._states)
TEXTS[41] = AT_BOTTOM_STUCK                     # the view is back at the bottom
advance(60)
w._tick()
check("SA5 back at the bottom it is announced",
      any("back at the bottom" in m for _, m in LOGS_SA))
check("SA6 and a genuinely stuck session there is poked as usual",
      SENT == [(41, ["continue"])])
SENT.clear()

# Left scrolled, a window is skipped for good. After the set time (default
# 60 min) it gets Claude Code's own jump-to-bottom key — verified on a live
# ccdebug session 2026-09-27: PageUp showed the hint, Ctrl+End removed it,
# and nothing was typed into the input box.
KEYS_SB = []
_real_keys = gui.send_keys
gui.send_keys = lambda w, k, dry_run=False: (
    KEYS_SB.append((int(w.NativeWindowHandle), k, dry_run)) or True)
try:
    set_now(T0)
    reset([(43, "win")], {43: SCROLLED})
    w = new_watcher()
    LOGS_SB = []
    w.log.connect(lambda k, m: LOGS_SB.append((k, m)))
    w._tick()
    advance(59 * 60)
    w._tick()
    check("SB1 under an hour scrolled up: left alone", KEYS_SB == [])
    advance(2 * 60)
    w._tick()
    check("SB2 past the hour: exactly one Ctrl+End, to that window",
          KEYS_SB == [(43, gui.SCROLL_TO_BOTTOM_KEYS, False)])
    check("SB3 recorded as a keystroke, without a tray balloon",
          any(k == "fire-quiet" and "back to the bottom" in m
              for k, m in LOGS_SB))
    advance(60)
    w._tick()
    check("SB4 still showing scrolled a minute later: not repeated at once",
          len(KEYS_SB) == 1)
    TEXTS[43] = AT_BOTTOM_STUCK
    advance(60)
    w._tick()
    check("SB5 once it is at the bottom the clock is cleared",
          w._states[43].scrolled_since is None)
    # 0 = never
    KEYS_SB.clear()
    set_now(T0)
    reset([(44, "win")], {44: SCROLLED})
    w = new_watcher()
    w.set_scroll_back(0)
    w._tick()
    advance(5 * 3600)
    w._tick()
    check("SB6 set to 0 it never scrolls a window back", KEYS_SB == [])
    # dry-run types nothing for real
    KEYS_SB.clear()
    set_now(T0)
    reset([(45, "win")], {45: SCROLLED})
    w = new_watcher()
    w.set_dry_run(True)
    w._tick()
    advance(61 * 60)
    w._tick()
    check("SB7 dry-run passes dry_run through, so nothing is really sent",
          KEYS_SB == [(45, gui.SCROLL_TO_BOTTOM_KEYS, True)])
finally:
    gui.send_keys = _real_keys

# =============================================================================
print("---- AQ: busy from the local API means nothing was done ----")
# Found by a companion's test window on 2026-09-27: a send timed out into
# "busy" while its signal stayed queued, the watcher typed it once the pass
# ended, the caller retried as busy invites, and the text went in twice --
# the second Enter approving a permission prompt the first had raised.
import threading as _thq                                    # noqa: E402
import time as _tq                                          # noqa: E402


class _Wake:
    """sig_api_wake stand-in: what the watcher does when woken."""
    def __init__(self, fn=None): self.fn = fn
    def emit(self):
        if self.fn:
            self.fn()


def _api_host(worker, wake=None):
    mw = gui.MainWindow.__new__(gui.MainWindow)
    mw.worker = worker
    mw.codex_service = None
    mw._api_last_send = {}
    mw.sig_api_wake = _Wake(wake)
    return mw


_real_to, _real_started = gui.local_api.SEND_TIMEOUT_S, \
    gui.local_api.STARTED_WAIT_S
gui.local_api.SEND_TIMEOUT_S = 0.2
_real_send_aq = gui.send_text_lines
try:
    set_now(T0)
    reset([(71, "win")], {71: IDLE})
    w = new_watcher()
    # A watcher stuck in a long pass: woken, but it does not get there.
    mw = _api_host(w, wake=None)
    r = gui.MainWindow._api_do_send(mw, 71, ["hello"])
    check("AQ1 an action not reached in time is answered busy",
          r == {"ok": False, "reason": "busy"})
    w.api_drain()           # the pass ends and the watcher reaches the queue
    w._tick()
    check("AQ2 and it is NOT typed later, once the watcher gets there",
          not any(h == 71 and ln == ["hello"] for h, ln in SENT))

    # The retry busy invites: nothing happened, so it is not a repeat, and
    # it goes through exactly once.
    SENT.clear()
    mw.sig_api_wake = _Wake(w.api_drain)
    r = gui.MainWindow._api_do_send(mw, 71, ["hello"])
    check("AQ3 an immediate retry after busy is not rate-limited",
          r == {"ok": True})
    check("AQ4 and the text goes in once, not twice",
          SENT == [(71, ["hello"])])

    # An action that has started is typing: never answer busy mid-action.
    def _slow_send(win, lines, dry_run=False, outcome=None):
        _tq.sleep(0.6)
        return _send_lines(win, lines)
    gui.send_text_lines = _slow_send
    SENT.clear()
    mw = _api_host(w, wake=lambda: _thq.Thread(target=w.api_drain).start())
    _t0 = _tq.time()
    r = gui.MainWindow._api_do_send(mw, 71, ["slow"])
    _el = _tq.time() - _t0
    check("AQ5 a send that started before the timeout is answered with its "
          "real result, not busy", r == {"ok": True})
    check(f"AQ6 which meant waiting for it to finish ({_el:.2f}s)",
          _el >= 0.55 and SENT == [(71, ["slow"])])
    gui.send_text_lines = _real_send_aq

    # Past STARTED_WAIT_S too: still not busy, since it may have typed.
    gui.local_api.STARTED_WAIT_S = 0.1
    _gate = _thq.Event()

    def _stuck_send(win, lines, dry_run=False, outcome=None):
        _gate.wait(5)
        return _send_lines(win, lines)
    gui.send_text_lines = _stuck_send
    mw = _api_host(w, wake=lambda: _thq.Thread(target=w.api_drain).start())
    r = gui.MainWindow._api_do_send(mw, 71, ["stuck"])
    _gate.set()
    check("AQ7 a started action that outlasts every wait says 'unfinished', "
          "never busy", r == {"ok": False, "reason": "unfinished"})
    _tq.sleep(0.2)
    gui.send_text_lines = _real_send_aq
    gui.local_api.STARTED_WAIT_S = _real_started

    # The queue itself: a withdrawn ticket is skipped, the next one runs.
    ran = []
    t1, t2 = gui._ApiTicket(), gui._ApiTicket()
    w.api_enqueue(lambda t: (ran.append(1), t.put({"ok": True})), t1)
    w.api_enqueue(lambda t: (ran.append(2), t.put({"ok": True})), t2)
    t1.cancel()
    w.api_drain()
    check("AQ8 the drain drops a withdrawn action and runs the next",
          ran == [2] and not t1.done() and t2.result == {"ok": True})
    t3 = gui._ApiTicket()
    check("AQ9 a started ticket cannot be withdrawn",
          t3.start() and not t3.cancel())

    # Between windows, not after the pass: an action that arrives while the
    # first window is being read runs before the second one is.
    ORDER = []
    reset([(73, "a"), (74, "b")], {73: IDLE, 74: IDLE})
    w = new_watcher()
    _real_read = gui.read_terminal_text

    def _read(win):
        h = int(win.NativeWindowHandle)
        ORDER.append(f"read {h}")
        if h == 73 and "api" not in ORDER:
            w.api_enqueue(lambda t: (ORDER.append("api"),
                                     t.put({"ok": True})), gui._ApiTicket())
        return TEXTS.get(h, "")
    gui.read_terminal_text = _read
    try:
        w._tick()
    finally:
        gui.read_terminal_text = _real_read
    _first = [x for i, x in enumerate(ORDER) if x not in ORDER[:i]]
    check(f"AQ10 a caller's action is taken between two windows of a pass "
          f"({_first})", _first[:3] == ["read 73", "api", "read 74"])
    check("AQ11 the old mid-pass flag, which answered busy for a whole pass, "
          "is gone", "_in_tick" not in open("gui.py", encoding="utf-8").read())

    # A spec that cannot be typed did nothing, so the corrected retry right
    # behind it must not be throttled as a repeat (seen live 2026-09-27).
    reset([(75, "win")], {75: IDLE})
    w = new_watcher()
    mw = _api_host(w, wake=w.api_drain)
    r1 = gui.MainWindow._api_do_keys(mw, 75, "{Backspace}")
    r2 = gui.MainWindow._api_do_keys(mw, 75, "{Back}")
    check("AQ13 a bad key spec is refused without counting as a send",
          r1.get("reason") == "bad_keyspec" and r2 == {"ok": True})

    # A settings change the caller was told busy about must not land later.
    _ms = gui.MainWindow.__new__(gui.MainWindow)
    _ms.settings = _QS("auto_continue_selftest", "gui")
    _ms.settings.clear()
    _ms._auto_permission, _ms._auto_choose = True, False
    _ms._append_log = lambda lvl, msg: None
    _ms.sig_set_auto_answer = _FakeSig()
    _tk = gui._ApiTicket()
    _tk.cancel()
    gui.MainWindow._api_apply_settings(_ms, {"auto_permission": False}, _tk)
    check("AQ12 a withdrawn settings change is not applied late",
          _ms._auto_permission is True and not _tk.done())
    _ms.settings.clear()
finally:
    gui.send_text_lines = _real_send_aq
    gui.local_api.SEND_TIMEOUT_S = _real_to
    gui.local_api.STARTED_WAIT_S = _real_started

# =============================================================================
print("---- HT: the tool's own typing holds for a draft or an open prompt ----")
# Measured 2026-09-27: after-finish typed its prompt + Enter into a
# permission prompt left for a human (auto-permission off) -- the Enter picks
# the highlighted "Yes" -- and onto a draft; the network retry and the limit
# resume typed 'continue' onto a draft, submitting both as one message.
_AF = "next task: refactor the parser"
_PERM_OPEN = PERMISSION + NO_BOX
_MOVED = (PERMISSION.replace(_M + "1", "  1").replace("  3" ". No", _M + "3" ". No")
          + NO_BOX)
_DRAFT = "some finished output" + DRAFT_BOX
_EMPTY = "some finished output" + EMPTY_BOX


def _af_run(screen, choose=False, perm=False):
    w = af_watcher(_AF)
    w.set_auto_answer(choose, perm)
    logs = []
    w.log.connect(lambda k, m: logs.append((k, m)))
    w._tick()                                   # running seen
    TEXTS[9] = screen
    for _ in range(4):
        advance(60)
        w._tick()
    return [s for s in SENT if s[1] == [_AF]], logs


_sent, _logs = _af_run(_PERM_OPEN)
check("HT1 after-finish does not type into a permission prompt left for a "
      "human", _sent == [] and not any(s[1] == [""] for s in SENT))
check("HT2 and says once why it is waiting",
      sum("after-finish on" in m and "is held" in m for _, m in _logs) == 1)
_sent, _ = _af_run(_MOVED, perm=True)
check("HT3 nor into one whose cursor was moved off 1 (and auto-answer, "
      "which needs the cursor on 1, leaves it too)",
      _sent == [] and not any(s[1] == [""] for s in SENT))
_sent, _ = _af_run(_DRAFT)
check("HT4 nor onto a draft", _sent == [])
_sent, _ = _af_run(_EMPTY)
check("HT5 an empty input box still gets it", len(_sent) == 1)

set_now(T0)
reset([(61, "win")], {61: ECONN + "\n" + _DRAFT})
w = new_watcher()
_nl = []
w.log.connect(lambda k, m: _nl.append((k, m)))
for _ in range(3):
    w._tick()
    advance(700)
check("HT6 a network-stuck window with a draft gets no 'continue' on top",
      not any(s[1] == ["continue"] for s in SENT))
check("HT7 said once, not every pass",
      sum("not typing 'continue' over it" in m for _, m in _nl) == 1)
TEXTS[61] = ECONN + "\n" + _EMPTY
w._tick()
check("HT8 cleared, the retry resumes",
      any(s[1] == ["continue"] for s in SENT))

set_now(T0)
reset([(62, "win")], {62: BANNER})
w = new_watcher()
w._tick()                                       # schedules the fire
TEXTS[62] = BANNER + "\n" + _DRAFT
set_now(FIRE_UTC + timedelta(minutes=5))
w._tick()
advance(60)
w._tick()
check("HT9 a due limit resume holds while a draft is in the box",
      not any("continue" in s[1] for s in SENT))
TEXTS[62] = BANNER + "\n" + _EMPTY
advance(60)
w._tick()
check("HT10 and goes out once the box is clear",
      any(s[1][-1:] == ["continue"] for s in SENT))

print()
print("RESULT:", "ALL OK" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
