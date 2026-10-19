# -*- coding: utf-8 -*-
"""What the Claude CLI watcher does about a safeguard block, now that model
recovery is gone (removed in 3.2.0).

The contract is short, and each part of it is something the removed feature
used to do differently:
  * a safeguard notice, its picker, or a per-model quota banner gets NO
    keystroke of any kind -- no /model, no /compact, no continue, no Enter;
  * a notice lingering in the scrollback does not blind the handlers that do
    act -- a limit or a dropped connection below it is still answered;
  * nothing about the old feature is left to switch on: no config slot, no
    Model recovery tab, no quota trigger, no status for it in the table;
  * the in-app help, which explains all of this, cannot trip a detector.

Stub windows, recorded sends and a controllable clock, the same plain
harness as test_gui_tick.py: no real UIA, no real keystrokes. Exits non-zero
on any failure.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime, timedelta

import pytz
from PyQt6.QtWidgets import QApplication, QLabel

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
T0 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=pytz.UTC)


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
SENT = []          # (hwnd, kind, payload)


def _send_lines(w, lines, dry_run=False):
    SENT.append((int(w.NativeWindowHandle), "text", list(lines)))
    return True


def _send_keys(w, keyspec, dry_run=False):
    SENT.append((int(w.NativeWindowHandle), "keys", keyspec))
    return True


gui.find_terminal_windows = lambda: WINDOWS
gui.read_terminal_text = lambda w: TEXTS.get(int(w.NativeWindowHandle), "")
gui.send_text_lines = _send_lines
gui.send_keys = _send_keys
gui.send_continue = lambda w, dry_run=False: _send_lines(w, ["continue"])
gui.list_tab_titles = lambda w: []
gui._hwnd_alive = lambda h: True

# Message bodies, split so displaying THIS file in a watched terminal cannot
# look like the real thing (same convention as test_parse.py).
_SG = "safegu" "ards flagged"
_SW = "Switch t" "o"
_ED = "Edit promp" "t and retry"
NOTICE = (f"API Error: Fable 5's {_SG} this message. They may flag safe "
          f"content.\nClaude Code can" "'t respond to this request with "
          f"Fable 5.\nRequest ID: req_0001")
PICKER = (f"Session paused\n\nFable 5's {_SG} this message.\n\n"
          f"> 1. {_SW} Opus 5\n  2. {_ED} with Fable 5")
QUOTA = ("You" + "'ve reached your Fable 5 " + "limit. Run /usage-credits "
         "to continue or switch models with /model.")
ECONN = "API Error: Unable to connect to API (ECONN" "RESET)"
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
NO_BOX = "\nEnter to select - Tab/Arrow keys to navigate - Esc to cancel"
EMPTY_BOX = "\n>" + chr(0xa0)


def new_watcher():
    w = gui.Watcher()
    w._running = True
    return w


def reset(hwnd_titles, text_by_hwnd):
    global WINDOWS, TEXTS
    WINDOWS = [FakeWin(h, t) for h, t in hwnd_titles]
    TEXTS = dict(text_by_hwnd)
    SENT.clear()
    set_now(T0)


def run(w, ticks, gap):
    for _ in range(ticks):
        w._tick()
        advance(gap)


# =============================================================================
print("---- A: nothing of the old feature is left to switch on ----")
check("A1 the watcher has no recovery config slot",
      not hasattr(gui.Watcher, "set_fable_config"))
check("A2 nor a recovery status for the table",
      not hasattr(gui, "ST_FABLE")
      and "fable" not in {str(k) for k in gui.STATUS_LABEL})
check("A3 nor a step script",
      not hasattr(gui, "DEFAULT_FABLE_STEPS")
      and not hasattr(gui, "_parse_recovery_steps"))
_keys = [s["key"] for s in ac.TRIGGER_SPECS]
check("A4 the quota trigger is gone, the guard triggers stay",
      "model_quota" not in _keys
      and {"fable", "fable_picker", "switch_model"} <= set(_keys))
check("A5 the quota detector itself is gone",
      not hasattr(ac, "parse_model_quota"))
# A store still holding the old override must not break compilation.
_pats, _errs = ac.compile_trigger_patterns(
    {"model_quota": "anything at all", "fable": "custom" " wording"})
check("A6 a stale override for the removed trigger is ignored, not an error",
      "model_quota" not in _pats and not _errs and "fable" in _pats)

# =============================================================================
print("---- B: a safeguard block gets no keystroke ----")
for _name, _text in (("notice", NOTICE + EMPTY_BOX),
                     ("notice with its picker", NOTICE + "\n" + PICKER + NO_BOX),
                     ("per-model quota banner", QUOTA + EMPTY_BOX)):
    reset([(1, "claude")], {1: _text})
    w = new_watcher()
    w.set_auto_answer(True, True)            # even with every answer switch on
    run(w, 6, 120)
    check(f"B1 {_name}: nothing typed over 12 minutes", SENT == [])
    check(f"B2 {_name}: the row reads as idle, not as recovering",
          w._states[1].status == gui.ST_IDLE)

# The two model-switching dialogs are left for a human even with both
# answer switches on: Enter on either changes the session's model.
_DIALOG = ("Switch model?\n> " "1" ". Yes, swi" "tch to Opus 5\n"
           "  2" ". No, go b" "ack")
for _name, _text in (("safeguard picker", PICKER), ("switch-model dialog", _DIALOG)):
    reset([(2, "claude")], {2: _text + NO_BOX})
    w = new_watcher()
    w.set_auto_answer(True, True)
    run(w, 3, 30)
    check(f"B3 the {_name} is not auto-answered", SENT == [])

# =============================================================================
print("---- C: a lingering notice does not blind the other handlers ----")
reset([(3, "claude")], {3: NOTICE + "\n" + BANNER})
w = new_watcher()
w._tick()
check("C1 a limit banner below a notice is still armed",
      w._states[3].reset_utc is not None)

reset([(4, "claude")], {4: NOTICE + "\n" + ECONN})
w = new_watcher()
w._tick()
check("C2 a dropped connection below a notice still gets its continue",
      (4, "text", ["continue"]) in SENT)

# =============================================================================
print("---- D: After finish treats a blocked session as blocked ----")
reset([(5, "win")], {5: "✶ Working… (3s · ↓ 1k tokens)"})
w = new_watcher()
w.set_after_finish({"win": "next task"})
w._tick()                                   # seen running
TEXTS[5] = NOTICE + EMPTY_BOX
run(w, 4, gui.AFTER_FINISH_SETTLE_S)
check("D1 the follow-up prompt is not typed into a blocked session", SENT == [])
TEXTS[5] = "all done here" + EMPTY_BOX
run(w, 3, gui.AFTER_FINISH_SETTLE_S)
check("D2 once the notice is gone, After finish works as before",
      (5, "text", ["next task"]) in SENT)

# =============================================================================
print("---- W: the in-app help must not describe itself into a trigger ----")
_help = gui.HELP_HTML.format(version=gui.APP_VERSION)
check("W1 help formats with the version", gui.APP_VERSION in _help)
check("W1b help explains that model recovery was removed, and why",
      "Model recovery was removed in 3.2.0" in _help
      and "Claude CLI &#8594; More settings &#8594; Model recovery" not in _help)
for _name, _fn in (
    ("safeguard notice", ac.parse_fable_refusal),
    ("safeguard picker", ac.parse_fable_picker),
    ("switch-model dialog", ac.parse_switch_model_prompt),
    ("limit picker", ac.parse_limit_prompt),
    ("connection error", ac.parse_econnreset_stuck),
    ("truncated response", ac.parse_server_error_stuck),
    ("oauth expired", ac.parse_oauth_expired),
):
    check(f"W2 help does not trip the {_name} detector", _fn(_help) is False)
check("W3 help does not trip the limit banner",
      ac.parse_limit_message(_help) is None)

# =============================================================================
print("---- Z: the More settings dialog ----")
# A QLabel without setWordWrap reports its whole one-line text as its minimum
# width, and a layout can never go below a child's minimum — so one forgotten
# wrap silently overrides resize() and the dialog opens wider than the screen.
_dlg = gui.AdvancedDialog(None, {})
_tabs = [_dlg._tabs.tabText(i) for i in range(_dlg._tabs.count())]
check("Z0 no Model recovery tab: " + ", ".join(_tabs),
      _tabs == ["Answering", "Triggers", "Local API"])
_MAXW = 900                        # comfortably inside a 1366-wide laptop
check(f"Z1 dialog opens under {_MAXW}px wide", _dlg.width() <= _MAXW)
check("Z2 the requested size is actually achievable "
      f"(min {_dlg.minimumSizeHint().width()} <= {_dlg.width()})",
      _dlg.minimumSizeHint().width() <= _dlg.width())
_unwrapped = [
    lbl.text()[:60] for lbl in _dlg.findChildren(QLabel)
    if len(lbl.text()) > 55 and not lbl.wordWrap()
]
check("Z3 every long label wraps: " + (_unwrapped[0] if _unwrapped else "-"),
      not _unwrapped)
_dlg.deleteLater()

print()
print("RESULT:", "ALL OK" if not failures else f"{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
