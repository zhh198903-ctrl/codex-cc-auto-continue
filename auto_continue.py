"""
Detection and keystroke primitives for the Auto-Continue watchdog.

This module owns everything the GUI watcher (gui.py — the loop the shipped
exe runs) needs to observe and act on Claude Code sessions:

  * UI Automation readers for Windows Terminal windows and their scrollback.
  * The detection patterns and parsers: the 5-hour limit banner, network-stuck
    states, safeguard blocks, the live-turn spinner, the model status bar.
  * Checked keystroke senders (foreground-verified; they refuse to type
    rather than land keys in the wrong window).

There is deliberately NO watcher loop here. One used to exist — a CLI
sibling of gui.py's tick — and every detection change then had to be made
and verified twice; more than once only one copy was fixed. Run the GUI
(Auto-Continue.pyw) instead; it does everything the CLI did and more.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytz
import uiautomation as auto

# Bumped on every shipped build so the running version is visible in the GUI
# title bar (and thus in the Windows Terminal window title the watchdog reads).
#
# BUMP THIS ONLY IN THE RELEASE COMMIT — never at the start of a dev cycle.
# The updater compares version strings alone, so a locally-built dev exe that
# already carries the upcoming number is a trap: `is_newer("1.0.16", "1.0.16")`
# is False, so that build reports "up to date" forever and NEVER updates to the
# real release. It happened — a dev exe built 2026-07-25 claimed v1.0.16 a day
# before v1.0.16 shipped, and would have silently kept running unreviewed code.
# A "-dev" suffix does not help: parse_version() strips it, so 1.0.17-dev and
# 1.0.17 compare equal. Leave this at the LAST RELEASED version while
# developing; release.yml refuses to publish if it disagrees with the tag.
APP_VERSION = "3.3.4"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Matches the full two-line Anthropic limit message. Anthropic has shipped
# several wordings; all are accepted:
#
#     You've hit your limit · resets Hpm (Asia/Shanghai)
#     /up·grade or /extra-usage to finish what you're working on.
#
#     You've hit your limit · resets H:MMpm (Asia/Shanghai)
#     /up·grade to increase your usage limit.
#
#     You've hit your session limit · resets Ham (Asia/Shanghai)
#     /up·grade to increase your usage limit.
#
#     You've hit your session limit · resets H:MMam (Asia/Shanghai)
#     /up·grade or /usage-credits to finish what you're working on.
#
# (The examples above are deliberately de-fanged — digits replaced with H/MM
# and a · inside "/upgrade" — so that viewing THIS file inside a watched
# terminal can never false-trigger the detector.)
#
# The leading "hit your ..." may say `limit`, `session limit`, `usage limit`,
# `weekly limit`, `daily usage limit` (up to ~3 qualifier words), or a
# hyphenated/numeric one like `5-hour limit`, etc. The reset time may or may
# not carry minutes (`Hpm` vs `H:MMpm`). Requiring the /upgrade follow-up
# line is what tells us this is a real rate-limit hit and not e.g. our own
# test output or this script's source code visible in the user's scrollback.
# The DOTALL `[\s\S]{0,400}` lets the two lines be separated by terminal
# padding/whitespace.
#
# The follow-up wording is a moving target — Anthropic ships `/usage-credits`
# (it was `/extra-usage` before the rename; that spelling is gone from here
# because Claude Code updates itself), with the tail either
# "to finish what you're working on" or "to increase your usage limit". So
# after "/upgrade" we accept ANY of the known continuations (either slash
# command, or either stable tail phrase). The separator glyph between "limit"
# and "resets" also varies (middle dot ·, bullet operator ∙, dot operator ⋅,
# en/em dash), so the class enumerates all of them.
LIMIT_RE = re.compile(
    r"You['’]ve hit your (?:[\w-]+\s+){0,3}limit\s*[·•‧․∙⋅⸱\-–—]?\s*"
    r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)"
    r"[\s\S]{0,400}?"
    r"(?:"
    r"/upgrade\b[^\n]{0,80}?"
    r"(?:/usage[-\s]?credits"
    r"|increase\s+your\s+usage\s+limit|finish\s+what\s+you)"
    # Seen live 2026-08-11: the follow-up line offered API billing and carried
    # no /upgrade at all, so demanding that word cost the window its entire
    # resume. The picker was answered and then nothing happened — a banner
    # that does not parse schedules no reset, and says nothing about it.
    r"|/log\s*in\b[^\n]{0,80}?(?:usage.billed|API\s+usage)"
    r")",
    re.IGNORECASE,
)

# Network-retry banner shown when Claude Code can't reach the API:
#     Retrying in 0s · attempt 7/10
# Some builds print the long form instead:
#     Retrying in 8 seconds… (attempt 7/10)
# When attempt count hits N/N, Claude has exhausted its automatic retries
# and the user normally has to type 'continue' to resume after the network
# is back. We mimic that. Anchoring on the "Retrying in <n>s" prefix avoids
# matching unrelated occurrences of "attempt N/M" in normal output. The
# `[^\n]{0,30}?` gap keeps both parts on the same line while allowing any
# separator glyphs (`· `, `… (`, `- `).
RETRY_RE = re.compile(
    r"Retrying\s+in\s+\d+\s*s(?:ec(?:ond)?s?)?\b"
    r"[^\n]{0,30}?"
    r"attempt\s+(\d+)\s*/\s*(\d+)",
    re.IGNORECASE,
)

# Bare-error variant — sometimes Claude Code prints the API error directly
# without the retry banner (e.g. when a tool call's result can't be POSTed
# back and there's still a shell tool running):
#     API Error: Unable to connect to API (ECONN·RESET)
# The parenthesized code varies with the underlying failure — ECONN·RESET,
# ETIMED·OUT, ECONN·REFUSED, ENOT·FOUND, EAI_·AGAIN, EHOST·UNREACH, undici's
# UND_·ERR_* family — so we accept any parenthesized E*/UND_ERR_* code after
# the fixed sentence. Node's generic "API Error: fetch fai·led" is the same
# stuck-until-continue state and is accepted too. (Examples de-fanged with ·
# so viewing this file in a watched terminal can't false-trigger.)
# Treat all of these the same as retry-exhausted: keep poking 'continue'
# until the connection comes back. Requiring the leading "API Error:" prefix
# avoids matching prose / logs that merely mention an errno.
ECONNRESET_RE = re.compile(
    r"API\s+Error:\s*(?:"
    r"Unable\s+to\s+connect\s+to\s+API\s*"
    r"\(\s*(?:E[A-Z0-9_]{2,}|UND_ERR_[A-Z_]+)\s*\)"
    # Any OTHER wording that carries an errno. Reported 2026-09-24: a
    # session ended its turn on "API Err·or: Connection dropped (ECONN·RESET)"
    # and sat idle for hours, because only "Unable to connect" was listed.
    # The prose around the code keeps changing (this is the third rewording
    # of the same condition in two weeks); the errno in parentheses is the
    # part that does not. Case-sensitive on the code itself, so an ordinary
    # parenthesised word — "(Enter)", "(Error)" — is not mistaken for one.
    r"|[^\n]{0,80}?\(\s*(?-i:E[A-Z0-9]{2,}[A-Z0-9_]*|UND_ERR_[A-Z_]+)\s*\)"
    r"|fetch\s+failed"
    # Reported live 2026-09-13: a session sat at its prompt behind two of
    # these and nothing ever poked it, because none of the wordings above
    # matched. This one carries no errno and no parentheses — the request
    # simply never came back — but it leaves the session in exactly the same
    # place, waiting for a 'continue' that only a human was going to type.
    r"|No\s+response\s+from\s+(?:the\s+)?API"
    # "the" is optional: the 09-13 banner said "from API", and by 09-23 the
    # in-flight notice for the same condition said "from the API". Wording
    # drifts; a final banner in the newer style must not sit unpoked. Still
    # anchored on "API Error:", so the in-flight notice (no such prefix, and
    # Claude Code is still retrying) never triggers a 'continue'.
    r")",
    re.IGNORECASE,
)

# Server-side truncation: a response is cut off partway through streaming,
# so the turn ends with a partial answer that 'continue' can resume. Two
# leading wordings seen so far — the 500-class "Server-error" one, and a
# later "Response-stalled" one — both closing with the same "…may be
# incomplete" footer sentence (plus a `✻ Sautéed for 1m 6s`-style completion
# marker). Unlike ECONNRESET this is NOT a connectivity failure: the request
# reached Anthropic, but the server erred / stalled partway through streaming,
# so the turn stops early. Typing 'continue' makes Claude resume from where it
# cut off. These arrive in bursts during overload, so we treat them like the
# other network-stuck states — keep poking 'continue' every retry_interval
# until a turn completes cleanly (the tail anchor clears the marker once a
# full response scrolls it out of range).
# The regex matches either known leading phrase OR — as a forward-compat net
# for future re-wordings — any `API Error` line ending in that shared footer.
# It uses \s+ / . (and the prose above avoids the verbatim strings) so this
# source can't self-trigger in a watched terminal.
SERVER_ERROR_RE = re.compile(
    r"API\s+Error:\s*(?:"
    r"Server\s+error\s+mid.response"        # 500-class truncation
    r"|Response\s+stalled\s+mid.stream"     # later stream-stall wording
    r"|[\s\S]{0,80}?The\s+response\s+above\s+may\s+be\s+incomplete"
    r")",
    re.IGNORECASE,
)

# Interactive limit picker (newer Claude Code builds). When the limit hits
# mid-turn, instead of printing the banner directly Claude Code first shows
# a modal choice:
#
#     What do you want to do?
#     > 1. Stop and wait for li·mit to reset
#       2. Upgrade your plan
#     Enter to confirm - Esc to cancel
#
# Option 1 is pre-selected; a bare Enter confirms it, after which the regular
# "You've hit your limit - resets ..." banner appears and the normal pending
# flow takes over. We detect the picker and press Enter for the user.
# (Example above de-fanged with a · inside "limit" for the same reason as the
# other patterns.)
LIMIT_PROMPT_RE = re.compile(
    r"What\s+do\s+you\s+want\s+to\s+do\?"
    r"[\s\S]{0,300}?"
    # Detection only. Nothing is ever typed in response to this picker: its
    # other option is PAID extra usage, and Enter takes whatever is
    # highlighted. Matching it only marks the window as waiting on a human,
    # so the pattern stays broad — a stricter one would miss a picker and
    # leave the tool silent about a window that has stopped.
    r"Stop\s+and\s+wait\s+for\s+limit\s+to\s+reset"
    r"[\s\S]{0,400}?"
    r"Enter\s+to\s+confirm",
    re.IGNORECASE,
)

# Dead-session state 'continue' can NOT fix — surfaced to the user instead
# of being poked at. Tail-anchored like the network patterns.
OAUTH_EXPIRED_RE = re.compile(
    r"OAuth\s+token\s+has\s+expired|Please\s+run\s+/login",
    re.IGNORECASE,
)

# How many trailing chars of the scrollback we scan each tick. Plenty for the
# message to appear, small enough that re.search stays cheap.
SCAN_TAIL_CHARS = 10000




# ---------------------------------------------------------------------------
# UI Automation helpers
# ---------------------------------------------------------------------------


def _cascadia_hwnds() -> list:
    """Visible top-level Windows Terminal windows, from the OS, in Z order.

    Plain Win32, no UI Automation: EnumWindows plus a class-name check stays
    in this process and took 1-5 ms for the whole desktop (2026-09-27).
    """
    u32 = ctypes.windll.user32
    found = []
    buf = ctypes.create_unicode_buffer(256)
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            u32.GetClassNameW(hwnd, buf, 256)
            if (buf.value == "CASCADIA_HOSTING_WINDOW_CLASS"
                    and u32.IsWindowVisible(hwnd)):
                found.append(int(hwnd))
        except Exception:
            pass
        return True

    if not u32.EnumWindows(proto(_cb), 0):
        raise OSError("EnumWindows failed")
    return found


def find_terminal_windows():
    """Return list of WindowControl for Windows Terminal top-level windows.

    The OS says which windows exist and UI Automation is asked about those
    alone, one handle each. Until 2.1.4 UIA listed the whole desktop
    (GetRootControl().GetChildren()), which asks every top-level window's
    owner in turn: one slow program made each pass wait 0.75-9 s
    (2026-09-27), the local API timed out behind it, and now and then it
    came back EMPTY while every terminal was open, the source of the
    'missing from this pass' warnings. Same set of windows either way:
    visible top-level windows of Windows Terminal's class.

    The old listing stays as the fallback, for when the OS listing fails or
    finds nothing to wrap.
    """
    try:
        hwnds = _cascadia_hwnds()
    except Exception:
        hwnds = []
    out = []
    for h in hwnds:
        try:
            c = auto.ControlFromHandle(h)
        except Exception:
            c = None
        # A handle UIA cannot wrap this instant is left out of the pass, as a
        # flaky window always was; the watcher keeps its state (IsWindow).
        if c is not None:
            out.append(c)
    if out:
        return out
    return _find_terminal_windows_uia()


def _find_terminal_windows_uia():
    """The pre-2.1.4 listing: every top-level window, via UIA.

    Every UIA access is guarded: a flaky child window that raises a COMError
    (e.g. EVENT_E_ALL_SUBSCRIBERS_FAILED) is skipped rather than aborting the
    whole enumeration — otherwise one bad window would blind the watchdog to
    every other terminal that tick.
    """
    out = []
    try:
        children = auto.GetRootControl().GetChildren()
    except Exception:
        return out
    for w in children:
        try:
            cls = w.ClassName or ""
        except Exception:
            continue
        if "CASCADIA" in cls.upper():
            out.append(w)
    return out


def terminal_window_from_handle(hwnd):
    """The Windows Terminal window with this handle, or None.

    For a caller that already knows which window it wants. Listing the
    desktop to find it, as find_terminal_windows does, asks every top-level
    window on the machine, and one slow app makes that take seconds: 0.75,
    5.6, 9.0 and 3.8 s for 17 windows, measured 2026-09-27, while asking the
    one window directly took about 2 ms. The local API paid that on every
    call, on top of waiting for the pass, and a caller timed out on it.

    Same answer set as the listing: a visible top-level terminal window.
    Anything else, including a closed or hidden window, is None.
    """
    try:
        h = int(hwnd)
        if not h:
            return None
        u32 = ctypes.windll.user32
        hv = ctypes.c_void_p(h)
        if not u32.IsWindow(hv) or not u32.IsWindowVisible(hv):
            return None
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hv, buf, 256)
        if "CASCADIA" not in buf.value.upper():
            return None
        ctrl = auto.ControlFromHandle(h)
        if ctrl is None or "CASCADIA" not in (ctrl.ClassName or "").upper():
            return None
        return ctrl
    except Exception:
        return None


def init_uia_thread() -> None:
    """Initialize UI Automation on the CURRENT thread. Call once, first thing,
    on any non-main thread that will touch uiautomation.

    uiautomation builds a single global IUIAutomation COM client on the first
    thread that uses it, and that client only returns a *live* view of the
    desktop tree when its thread has a properly initialized (STA) COM
    apartment. The GUI runs the watcher on a Qt worker thread; without this
    call that thread gets an uninitialized/implicit apartment and keeps
    seeing the snapshot of windows that existed when it started — so a
    terminal opened *after* the watchdog launched is never detected.

    `InitializeUIAutomationInCurrentThread()` is `comtypes.CoInitializeEx()`
    (STA by default). Idempotent enough to call once per thread; errors are
    swallowed because the main-thread/CLI path is already COM-initialized by
    comtypes at import.
    """
    try:
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass


def find_termcontrol(window_ctrl, depth=0, _limit=10):
    """Locate the TermControl (actual terminal surface) inside a WT window."""
    if depth > _limit:
        return None
    try:
        if (window_ctrl.ClassName or "") == "TermControl":
            return window_ctrl
    except Exception:
        pass
    try:
        for c in window_ctrl.GetChildren():
            r = find_termcontrol(c, depth + 1, _limit)
            if r:
                return r
    except Exception:
        pass
    return None


def list_tab_titles(window_ctrl) -> list:
    """Titles of ALL tabs in a WT window, read from its TabView's
    TabItemControls (shallow — the tab strip sits at depth 3, well above
    the terminal content subtree).

    Windows Terminal only exposes the ACTIVE tab's TermControl in the UIA
    tree — background tabs' content physically cannot be read. Callers use
    this list to detect (and warn about) sessions the watchdog can't see.
    Returns [] on any failure or when the structure doesn't match.
    """
    def _find_tabview(c, depth):
        if depth > 4:
            return None
        try:
            if (c.ClassName or "") == "Microsoft.UI.Xaml.Controls.TabView":
                return c
        except Exception:
            return None
        try:
            for ch in c.GetChildren():
                r = _find_tabview(ch, depth + 1)
                if r is not None:
                    return r
        except Exception:
            pass
        return None

    out = []
    try:
        tv = _find_tabview(window_ctrl, 0)
        if tv is None:
            return out
        for ch in tv.GetChildren():
            try:
                for item in ch.GetChildren():
                    if item.ControlTypeName == "TabItemControl":
                        out.append(item.Name or "")
            except Exception:
                pass
    except Exception:
        pass
    return out


def read_terminal_text(window_ctrl) -> str | None:
    """Return the tail of the visible+scrollback text of the active tab's
    TermControl (SCAN_TAIL_CHARS characters — all any caller scans).

    Fast path: collapse the document range to its end, extend the start back
    SCAN_TAIL_CHARS characters, and fetch only that slice — instead of
    marshalling the ENTIRE scrollback (potentially megabytes with WT's
    10k-line history) over cross-process COM every tick for every window.
    Falls back to the full-document read if the provider rejects the range
    surgery. `waitTime=0` skips uiautomation's default 0.5s post-op sleep.

    A prompt suggestion is left out of the input box: it is not something
    anyone typed (see `_suggestion_line`).
    """
    term = find_termcontrol(window_ctrl)
    if term is None:
        return None
    try:
        tp = term.GetTextPattern()
        if not tp:
            return None
        try:
            tr = tp.DocumentRange
            tr.MoveEndpointByRange(
                auto.TextPatternRangeEndpoint.Start, tr,
                auto.TextPatternRangeEndpoint.End, waitTime=0,
            )
            tr.MoveEndpointByUnit(
                auto.TextPatternRangeEndpoint.Start,
                auto.TextUnit.Character, -SCAN_TAIL_CHARS, waitTime=0,
            )
            text = tr.GetText(-1)
            if text:
                return _without_suggestion(text, tp, tr)
        except Exception:
            pass
        doc = tp.DocumentRange
        return _without_suggestion(doc.GetText(-1), tp, doc)
    except Exception:
        return None


# UIA_ForegroundColorAttributeId: a run's text colour as a COLORREF, or a
# "mixed" marker object when the range holds more than one colour.
_UIA_FOREGROUND_COLOR = 40008


def _uia_color(rng):
    """The one foreground colour of `rng`, or None (mixed, unsupported)."""
    try:
        value = rng.GetAttributeValue(_UIA_FOREGROUND_COLOR)
    except Exception:
        return None
    # Mixed and not-supported come back as COM marker objects, not numbers.
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _suggestion_line(tp, rng):
    """The input box's line when what it shows is Claude Code's prompt
    suggestion, not typed text; otherwise None.

    After a turn Claude Code may offer a next prompt, drawn IN the input box
    (Tab takes it, typing replaces it, Enter on the empty box sends nothing —
    checked live). As text it reads exactly like a draft, and a draft holds
    every send: a companion was refused with reason=draft for a box nobody
    had typed in (2026-10-21). Measured on Claude Code 2.1.283 in Windows
    Terminal: the suggestion's characters have another foreground colour than
    the glyph that starts the line (0x666666, dimmed, against 0xCCCCCC), and
    the caret sits right after the glyph. Typed text has the glyph's colour
    and the caret at its end, or wherever it was moved. Both signs are
    required; anything unreadable or in doubt stays a draft.

    Positions are counted from the start of the line, never taken from the
    range FindText returns: Windows Terminal's runs one character past the
    match (the glyph and its nbsp came back as three characters).
    """
    start = auto.TextPatternRangeEndpoint.Start
    end = auto.TextPatternRangeEndpoint.End
    char = auto.TextUnit.Character
    hit = None
    for glyph in ("\u276f\xa0", ">\xa0"):
        hit = rng.FindText(glyph, True, False)
        if hit:
            break
    if not hit:
        return None
    line = hit.Clone()
    line.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
    text = line.GetText(-1).rstrip("\r\n")
    if not _COMPOSER_LINE_RE.match(text):
        return None                     # the glyph does not start its line
    body = text[2:]
    shown = body.strip()
    if not shown:
        return None
    head = line.Clone()                 # the glyph and its nbsp
    head.MoveEndpointByRange(end, line, start, waitTime=0)
    head.MoveEndpointByUnit(end, char, 2, waitTime=0)
    if head.GetText(-1) != text[:2]:
        return None
    lead = len(body) - len(body.lstrip())
    span = head.Clone()                 # exactly the shown text
    span.MoveEndpointByRange(start, head, end, waitTime=0)
    span.MoveEndpointByUnit(end, char, lead + len(shown), waitTime=0)
    if lead:
        span.MoveEndpointByUnit(start, char, lead, waitTime=0)
    if span.GetText(-1) != shown:
        return None
    mark = head.Clone()                 # the glyph alone
    mark.MoveEndpointByUnit(end, char, -1, waitTime=0)
    glyph_color, text_color = _uia_color(mark), _uia_color(span)
    if glyph_color is None or text_color is None or glyph_color == text_color:
        return None
    selection = tp.GetSelection()
    if len(selection) != 1:
        return None
    caret = selection[0].textRange
    if caret.CompareEndpoints(start, caret, end) != 0:
        return None                     # text is selected, not a caret
    if caret.CompareEndpoints(start, head.textRange, end) != 0:
        return None
    return text


def _without_suggestion(text, tp, rng):
    """`text` with a prompt suggestion taken out of its input box line.

    Only looked for when the text alone says the box holds something, so an
    empty box costs nothing extra.
    """
    if not text or not composer_has_draft(text):
        return text
    try:
        shown = _suggestion_line(tp, rng)
    except Exception:
        return text
    if not shown:
        return text
    lines = text.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        row = lines[i].rstrip("\r\n")
        if _COMPOSER_LINE_RE.match(row) and row.rstrip() == shown.rstrip():
            lines[i] = row[:2] + lines[i][len(row):]
            return "".join(lines)
    return text


# ---------------------------------------------------------------------------
# Reset-time parsing
# ---------------------------------------------------------------------------


# Maximum number of characters that may appear *after* the limit-message
# match. If the post-match tail is longer than this, the message is buried in
# old scrollback (e.g. logs of a previous limit hit, or this script's own
# source viewed in the terminal) — ignore it.
#
# 6000, same as the network allowance below and for the same reason (learned
# the hard way in d3b7239 for ECONNRESET): Claude Code pads every line to the
# full window width and stacks a tall footer below the message — input box,
# border rules, multi-line status bar, hints, often a `※ recap:` block. On a
# wide terminal that footer alone can run past 4000 chars, which would make a
# *current* limit message look "stale" and never get auto-continued.
MAX_POST_MATCH_TAIL = 6000

# Network-stuck messages (retry banner / bare ECONNRESET) use the same
# allowance. Must stay below SCAN_TAIL_CHARS so the match is actually within
# the scanned slice.
NETWORK_POST_MATCH_TAIL = 6000

# The interactive limit picker is a modal drawn at the very bottom of the
# screen — when it is OPEN, little besides padding and the "Enter to confirm"
# footer can follow it. A tight allowance keeps us from pressing Enter long
# after the user dismissed the picker themselves (a bare Enter would submit
# whatever they have typed in the input box).
PROMPT_POST_MATCH_TAIL = 1500

# The "Switch model?" dialog and the safeguard picker need a LOOSER allowance
# than the limit picker. Both are modals, but Claude Code stacks its full
# footer below them — input box, border rules, multi-line status bar, hints —
# every line padded to the terminal width, so the 1500 used for the limit
# picker would make an OPEN dialog look stale. They are detected only so the
# chooser auto-answer leaves them alone: either one changes which model the
# session runs on, and that is the user's call. ~20 padded rows at 200
# columns clears a normal footer.
SWITCH_POST_MATCH_TAIL = 4000


def parse_limit_message(
    text: str, pattern=None
) -> tuple[int, int, str, str] | None:
    """Return (hour_12, minute, ampm, tz_name) from the *latest* limit line.

    Returns None if no match. The match must be near the end of the buffer —
    a stale limit message further back means Claude has already moved on.
    `minute` is 0 when the message omits minutes (e.g. `resets 11pm`).

    `pattern` may be a user-supplied compiled regex from the Advanced dialog
    (see TRIGGER_SPECS); it MUST expose the same four groups as LIMIT_RE
    (hour, minute, am/pm, tz) — `compile_trigger_patterns` enforces that
    before the override ever reaches here.
    """
    rx = pattern or LIMIT_RE
    matches = list(rx.finditer(text))
    if not matches:
        return None
    m = matches[-1]
    if len(text) - m.end() > MAX_POST_MATCH_TAIL:
        return None
    try:
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = (m.group(3) or "").lower()
        tz = (m.group(4) or "").strip()
    except (IndexError, ValueError):
        # A user override that compiled but yields non-numeric / missing
        # groups must not kill the tick — treat it as "no limit seen".
        return None
    return hour, minute, ampm, tz


# Claude Code's live spinner, e.g. "✽ Swirling… (2m 0s · ↓ 5.0k tokens)".
# The verb is randomised and the suffix varies, so anchor on the part that
# doesn't move: an elapsed clock followed by a middot, inside parentheses.
# The status bar's own parenthesised times ("(4h 37m / 5h)") use a slash, not
# a middot, and the finished-turn line ("Brewed for 3m 2s · …") has no
# parentheses — neither can match. The older builds' wording was a second
# alternative here until 2026-08-30, when sampling a live session showed the
# clock form carrying every one of 34 streaming samples and the old one none;
# Claude Code auto-updates, so the dead branch went rather than being kept
# for builds nobody runs.
RUNNING_RE = re.compile(
    r"\((?:\s*\d+\s*[hms])+\s*·"
    # The spinner line is REPLACED, timer and all, while Claude Code waits out
    # a first-byte timeout on its own:
    #   ✻ No response from the API after 6m · retrying once, waiting up to 10m
    # Reported 2026-09-23. Without this the turn looked finished for the whole
    # wait (up to 16 minutes), so after-finish typed the next task after 90s,
    # a due limit 'continue' could land, and the API told companions the
    # window was idle — all into a request that was still in flight.
    r"|·\s*retrying\b[^\n]{0,80}?\bwaiting\s+up\s+to\s+\d+\s*[hms]"
    # The spinner line by its SHAPE, not its timer: a spinner glyph, a verb,
    # an ellipsis. Captured live 2026-09-25 while a turn waited for its first
    # byte, the timer had no "·" after it — "✽ Effecting… (1m 51s)" — and in
    # a turn's first seconds there is no timer at all — "✶ Effecting…". Both
    # read as idle, so a session waiting on a slow API was taken for one stuck
    # on its PREVIOUS error and got 'continue' typed into its queue every
    # retry interval. A finished turn prints "✻ Crunched for 18m 51s · done"
    # — no ellipsis — so it is not mistaken for one still running.
    r"|^[ \t]*[·✢✳✶✻✽][ \t]+[^\s…]+(?:[ \t][^\s…]+){0,2}…",
    re.MULTILINE,
)


RUNNING_TAIL_CHARS = 2000


def session_running(text: str) -> bool:
    """True if Claude Code is streaming a turn RIGHT NOW.

    The spinner is redrawn just above the input box, so only the very bottom
    of the buffer says anything about the present; a spinner further up is
    scrollback from a turn that has already finished.
    """
    return RUNNING_RE.search(str(text or "")[-RUNNING_TAIL_CHARS:]) is not None


def session_running_after(text: str, pos: int) -> bool:
    """True if the session is streaming a turn *after* offset `pos`.

    An error banner only means "stuck" while nothing has run since. Once
    Claude Code is streaming again the banner is just scrollback, and resending
    'continue' does not unstick anything — it queues another prompt that lands
    when the turn ends. An 80-minute outage put eight of them into one session
    that way.
    """
    return RUNNING_RE.search(text, pos) is not None


# A prompt the session was given, echoed into its transcript, and a turn-end
# stamp ("✻ Cogitated for 29s · done 10:53") somewhere below it.
_PROMPT_ECHO_RE = re.compile(r"^[ \t]*[>❯][ \t ]+\S", re.MULTILINE)
_TURN_DONE_RE = re.compile(r"·\s*done\s+\d{1,2}:\d{2}")


def turn_completed_after(text: str, pos: int) -> bool:
    """True if a WHOLE turn has run since offset `pos`: a prompt, then its end.

    Recovery used to need the session caught mid-turn by a pass. A session that
    is only waiting on background work answers a 'continue' in seconds — "still
    at gate 1, I'll be told when the chain ends" — and that turn can start and
    finish between two passes. Seen 2026-09-25: the error stayed the last
    thing the tool believed about the window, and ten minutes later it was
    poked again for nothing. A prompt followed by a "· done HH:MM" below the
    error is a turn that has happened since; the error is history.

    The done-stamp directly under the error does NOT count — that belongs to
    the turn the error ended — which is why the prompt has to come first.
    """
    p = _PROMPT_ECHO_RE.search(text, pos)
    return bool(p and _TURN_DONE_RE.search(text, p.end()))


# A detector can only tell an application-printed banner from the user
# TYPING that same phrase into the composer if it looks at WHERE the match
# sits, not just what it says — e.g. asking for help with "API Error: Unable
# to conn·ect to API (ECONN·RESET)" in one's own message reads identically
# to the real banner. (De-fanged with · like the pattern comments above, so
# this comment cannot self-trigger the very detector it documents.) Sampled
# from a live Claude Code TUI
# (2026-08): the composer/input-box line always renders with '>' as its
# first character, followed by a non-breaking space (U+00A0) — `repr()`
# shows it as `'>\xa0                    '`.
#
# The NBSP is the load-bearing part of the prefix, not the '>' alone: the
# limit picker, safeguard picker and "Switch model?" dialog all print their
# pre-selected option with a leading "> " of their own (e.g. "> 1. Switch to
# ..."), but with a PLAIN space (U+0020) — never NBSP. Treating a plain space
# as equivalent would make this helper misfire on that legitimate app output
# and hide a real, open picker.
#
# The GLYPH, unlike the space, varies: ASCII '>' in some builds and U+276F
# in others — both seen on this machine within one afternoon. Matching only
# '>' made a line that genuinely held a half-typed message ("delete the
# folder", in a session that had just offered to delete one) read as an
# empty box; an empty box is what licenses pressing Enter, which would have
# SUBMITTED it. Both glyphs are accepted; the NBSP still does the
# separating. No other TUI chrome is guessed at, since guessing wrong there
# risks hiding a REAL banner instead.
_COMPOSER_LINE_RE = re.compile("^[>\u276f]\xa0")


def _is_composer_line(text: str, pos: int) -> bool:
    """True if `pos` falls on a line the user is actively composing (see
    _COMPOSER_LINE_RE), i.e. NOT a line Claude Code itself printed.

    Used by detectors where a false positive causes an unwanted keystroke —
    typing 'continue' would corrupt the user's half-written message, and the
    picker/dialog detectors send a bare Enter, which would SUBMIT it.
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    return _COMPOSER_LINE_RE.match(text[line_start:line_end]) is not None


# What may stand in front of a REAL banner on its own line: indentation and
# the glyphs Claude Code prints at the start of an output line. Measured over
# every live sample in test_parse.py: the prefix is always "", "  " or "● ".
_BANNER_LEAD_RE = re.compile(r"[\s ⎿●⏺✻✽✳·•*]*")
# The retry banner can also follow the error it is retrying on the same line:
#   ⎿  API Err·or (Connection error.) · Retry·ing in 3s · attempt 3/10
_RETRY_LEAD_RE = re.compile(
    r"[\s ⎿●⏺✻✽✳·•*]*API\s+Error\b[^\n]*?[·•]\s*")


def _is_prose_mention(text: str, pos: int, allow=None) -> bool:
    """True if the match at `pos` sits in the middle of a line of prose.

    Claude Code prints its error banners on a line of their own. The same
    words in the middle of a sentence are someone QUOTING the error — most
    often Claude itself, explaining a failure the user pasted. Seen live on
    2026-09-24: a reply that quoted the no-response line verbatim sat at the
    bottom of its own session, was read as the real thing, and got 'continue'
    typed into it (and would have kept getting one every retry interval).

    Not airtight: prose that happens to wrap so the quoted phrase begins a new
    row still looks like a banner. It closes the common case — the phrase
    mid-sentence — without touching any real banner seen so far.
    """
    line_start = text.rfind("\n", 0, pos) + 1
    lead = text[line_start:pos]
    if _BANNER_LEAD_RE.fullmatch(lead):
        return False
    if allow is not None and allow.fullmatch(lead):
        return False
    return True


def parse_retry_exhausted(text: str, pattern=None) -> bool:
    """True if the most recent network-retry banner shows N == total (e.g.
    `attempt 10/10`) and the banner sits near the tail of the buffer.

    A retry banner buried far up in scrollback (e.g. from a previous outage
    the user already cleared manually) is ignored.

    `pattern` may be a user-supplied compiled regex; it MUST expose the two
    numeric groups (attempt, total) that RETRY_RE does.

    A match sitting on a composer line (see `_is_composer_line`) is dropped —
    that's the user typing the phrase, not a real banner — so 'continue'
    never lands mid-message.
    """
    rx = pattern or RETRY_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())
               and not _is_prose_mention(text, m.start(), _RETRY_LEAD_RE)]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if (session_running_after(text, m.end())
            or turn_completed_after(text, m.end())):
        return False
    try:
        n, total = int(m.group(1)), int(m.group(2))
    except (IndexError, ValueError, TypeError):
        return False
    return n >= total > 0


def parse_econnreset_stuck(text: str, pattern=None) -> bool:
    """True if a bare `API Error: Unable to connect to API (E...)` (any
    errno / UND_ERR_* code) or the generic fetch-fai·led one appears near the
    tail of the buffer — i.e. Claude is currently stuck on a network error
    without the usual retry banner. Tail-anchored just like
    `parse_retry_exhausted`, so stale errors from a previous outage don't
    retrigger.

    A match on a composer line (see `_is_composer_line`) is dropped — e.g. a
    user asking for help by typing the exact error text reads identically to
    the real banner otherwise, and would get 'continue' typed into it.
    """
    rx = pattern or ECONNRESET_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())
               and not _is_prose_mention(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if (session_running_after(text, m.end())
            or turn_completed_after(text, m.end())):
        return False
    return True


def parse_server_error_stuck(text: str, pattern=None) -> bool:
    """True if a server-side truncation marker sits near the tail of the
    buffer — the response was cut off mid-stream (the 500 "Server-error"
    wording or the later "Response-stalled" one, both closing with the shared
    "…may be incomplete" footer), so Claude's last turn ended early and needs
    a 'continue' to resume. Tail-anchored exactly like `parse_econnreset_stuck`
    so a stale marker from an earlier, already-resolved outage doesn't
    retrigger.

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not a real banner.
    """
    rx = pattern or SERVER_ERROR_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())
               and not _is_prose_mention(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > NETWORK_POST_MATCH_TAIL:
        return False
    if (session_running_after(text, m.end())
            or turn_completed_after(text, m.end())):
        return False
    return True


# A safeguard block. When a model's safety guardrails block a turn, Claude
# Code stalls with a notice that names the model and says it can't respond
# with it. Nothing is typed into such a session: 'continue' would only be
# refused again. The watcher uses this to keep After finish from treating a
# blocked session as a finished one. (Until 3.2.0 it also started an opt-in
# model-recovery script; that was removed.) The default pattern is
# user-overridable in the Advanced dialog.
# Model-agnostic on purpose. The phrase is what identifies a safeguard block;
# the model in front of it is whatever is current, and that changes — Opus 4.8
# retired and Opus 5 shipped inside this project's lifetime, so naming one
# model here means the next one is simply not detected. `\w+` covers any
# family and version ("Fable 5's", "Opus 6's", something not invented yet).
# The `\s+` runs also keep this source from matching itself in a watched
# terminal, which is why they must survive any edit here.
# The version group takes whitespace only in front of a digit. Written as
# `\s*[\d.]*\s*` it let three whitespace runs share every blank after a
# word, and Claude Code pads each line to the window width, so a mostly
# empty screen cost O(n^2): 5.6 s for one 3.6 KB tail, holding the GIL, once
# a pass (2026-09-27, found as a 10 s stall in every pass).
DEFAULT_FABLE_PATTERN = (
    r"\b\w[\w.]{1,20}(?:\s{0,3}[\d.]+)?[’']?s?\s+safeguards\s+flagged"
    r"|can[’']?t\s+respond\s+to\s+this\s+request\s+with\s+\w[\w.]{1,20}"
)
FABLE_REFUSAL_RE = re.compile(DEFAULT_FABLE_PATTERN, re.IGNORECASE)


def fable_refusal_distance(text: str, pattern=None):
    """Chars between the END of the latest safeguard notice and the end of the
    buffer, or None if there is no in-range notice.

    Tail-anchored with the network allowance: a notice scrolled further up
    than that is history, not a block the session is sitting on now.
    """
    rx = pattern or FABLE_REFUSAL_RE
    matches = list(rx.finditer(text))
    if not matches:
        return None
    dist = len(text) - matches[-1].end()
    return dist if dist <= NETWORK_POST_MATCH_TAIL else None


def parse_fable_refusal(text: str, pattern=None) -> bool:
    """True if a safety-guardrail block sits near the tail of the buffer.
    `pattern` may be a user-compiled re.Pattern from the Advanced dialog;
    defaults to FABLE_REFUSAL_RE. Tail-anchored like the other network parsers
    so a stale notice further up scrollback doesn't retrigger."""
    return fable_refusal_distance(text, pattern) is not None


# The "Switch model?" confirmation dialog Claude Code shows when switching to a
# model whose cache differs (e.g. `/model fable` from a cached Opus). Option 1
# (the affirmative "Yes, switch…" line) is pre-selected, so a plain Enter
# confirms it. We anchor on the affirmative + negative option PAIR — not the
# far-above "Switch model?" header, which the padded multi-line description
# pushes ~900 chars away on a real (full-width) terminal. Detected so the
# chooser auto-answer never takes it: answering it changes the model.
# NOTE: the two option strings are deliberately NOT written out together
# anywhere in this file. This source is routinely displayed inside a watched
# terminal, and a verbatim pair here would make the tool detect a dialog that
# isn't there and press Enter into whatever the user was typing — the same
# de-fanging convention the limit / network patterns above follow.
SWITCH_MODEL_RE = re.compile(
    r"Yes,\s+switch\s+to[\s\S]{0,300}?No,\s+go\s+back",
    re.IGNORECASE,
)


# The picker Claude Code shows WITH the safeguard notice ("Session paused"):
# a numbered pair whose first option offers to switch to a fallback model and
# whose second offers to edit the prompt and retry on the blocked one. The two
# option strings are deliberately NOT reproduced verbatim here — writing them
# out on adjacent lines makes THIS FILE match the pattern below, so merely
# viewing the source in a watched terminal would look like an open picker and
# earn a blind Enter. (Same de-fanging convention as the patterns above; the
# test file splits the literals with string concatenation for this reason.)
#
# Option 1 is pre-selected, so a bare Enter would accept the model switch.
# That is why the chooser auto-answer must recognise it and leave it alone.
# Anchored on the option PAIR, and deliberately not naming either model: the
# fallback model name changes with every release. Written with \s+ so this
# source can't self-match in a watched terminal.
FABLE_PICKER_RE = re.compile(
    r"Switch\s+to\s+[\s\S]{0,120}?Edit\s+prompt\s+and\s+retry",
    re.IGNORECASE,
)


def parse_fable_picker(text: str, pattern=None) -> bool:
    """True if the safeguard picker is open at the tail of the buffer, i.e. a
    bare Enter would accept the pre-selected "Switch to <fallback>" option.
    Tail-anchored like the other modals so a stale one further up scrollback
    does not count.

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not an open picker, and a bare Enter would
    SUBMIT whatever else they'd typed."""
    rx = pattern or FABLE_PICKER_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > SWITCH_POST_MATCH_TAIL:
        return False
    return True


def parse_switch_model_prompt(text: str, pattern=None) -> bool:
    """True if the 'Switch model?' Yes/No dialog is open at the tail of the
    buffer (a plain Enter confirms the pre-selected 'Yes, switch to …').
    Tail-anchored so a stale one further up scrollback doesn't retrigger.

    A match on a composer line (see `_is_composer_line`) is dropped for the
    same reason as `parse_fable_picker` — a bare Enter here SUBMITS the
    user's half-typed message."""
    rx = pattern or SWITCH_MODEL_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > SWITCH_POST_MATCH_TAIL:
        return False
    return True


def parse_limit_prompt(text: str, pattern=None, limit_pattern=None) -> bool:
    """True if the interactive limit picker is currently open at the tail of
    the buffer — the "What do you want to do?" modal whose first option waits
    for the li·mit to reset. (Spelled with a · here for the same reason as the
    pattern above: the verbatim phrases would make THIS file read as an open
    picker in a watched terminal, and this detector presses Enter.)

    Two guards against pressing Enter into a session that already moved on:
      * tight tail anchor (PROMPT_POST_MATCH_TAIL) — the picker is a modal
        drawn at the very bottom when open;
      * if the regular limit banner appears AFTER the picker text, the picker
        was already confirmed — don't press Enter again.

    `limit_pattern` overrides the banner regex used by that second guard, so
    a user who customises the limit banner keeps the guard working.

    A match on a composer line (see `_is_composer_line`) is dropped — the
    user typing this text is not an open picker, and a bare Enter would
    SUBMIT whatever else they'd typed.
    """
    rx = pattern or LIMIT_PROMPT_RE
    matches = [m for m in rx.finditer(text)
               if not _is_composer_line(text, m.start())]
    if not matches:
        return False
    m = matches[-1]
    if len(text) - m.end() > PROMPT_POST_MATCH_TAIL:
        return False
    banner = list((limit_pattern or LIMIT_RE).finditer(text))
    if banner and banner[-1].start() >= m.end():
        return False
    return True


def parse_oauth_expired(text: str, pattern=None) -> bool:
    """True if the session is dead on an expired OAuth token near the tail.
    'continue' cannot fix this — callers should warn the user instead."""
    rx = pattern or OAUTH_EXPIRED_RE
    matches = list(rx.finditer(text))
    if not matches:
        return False
    m = matches[-1]
    return len(text) - m.end() <= NETWORK_POST_MATCH_TAIL


# ---------------------------------------------------------------------------
# User-editable trigger patterns
# ---------------------------------------------------------------------------
#
# Anthropic re-words these banners without warning (see the /extra-usage →
# /usage-credits rename, and the separator-glyph churn in LIMIT_RE), and each
# rename silently stops auto-continue from firing until the tool ships a new
# build. Exposing the patterns in the GUI's Advanced dialog lets a user fix a
# wording change themselves the same day.
#
# `groups` is the number of capture groups the parser INDEXES INTO. An
# override with fewer groups would raise mid-tick, so compile_trigger_patterns
# rejects it up front and the built-in default stays in force. Defaults are
# read back off the compiled objects (`.pattern`) so there is exactly one
# source of truth for each regex.
# A Claude Code chooser in general: it pauses the session and waits for a
# selection. Two shapes matter and both are covered by "the selected option
# is marked, and the options are numbered":
#
#     > 1. Do the thing            <- selection marker + first option
#       2. Do something else
#
# Deliberately NOT `\s` after the marker: the composer line is a '>' followed
# by U+00A0, and `\s` matches that, which would let a half-typed message read
# as a chooser. A plain space or tab is the chooser's own marker.
#
# The examples in this comment are de-fanged (the marker and the digit are
# separated by a word) so that reading this file in a watched terminal cannot
# make the watchdog press Enter.
# The row prefix: indentation, optionally a box rule, more indentation.
# Kept explicit rather than a loose `.{0,8}` so that ordinary prose ending
# in "> 1." cannot pose as a chooser.
_ROW = r"[^\S\n]{0,6}[\u2502\u2503\u2551|]?[^\S\n]{0,4}"
CHOOSER_RE = re.compile(
    r"(?m)^" + _ROW + r"[>\u276f][ \t]+1[.)]\s+\S[\s\S]{0,2000}?"
    r"^" + _ROW + r"2[.)]\s+\S")

# Claude Code's tool-permission prompt. It is a chooser like any other, but
# answering it AUTHORISES an action — an edit, a command — so it gets its own
# switch rather than riding along with the harmless ones. The tells are
# stable across its variants: the question, and the two option wordings that
# only ever appear on permission prompts.
PERMISSION_RE = re.compile(
    r"Do\s+you\s+want\s+to\s+\w+"
    r"|allow\s+all\s+\w+\s+during\s+this\s+session"
    r"|and\s+don['\u2019]t\s+ask\s+again"
    r"|tell\s+Claude\s+what\s+to\s+do\s+differently",
    re.IGNORECASE,
)


# How far above the tail a chooser may sit and still count as open. Bigger
# than the limit picker's allowance because what Claude Code draws BELOW a
# chooser varies: the input box and status bar always, and often a todo list
# as well. Measured live at 1556 characters on a session with seven tasks
# listed, which the 1500 shared with the limit picker missed by fifty-six —
# and a missed chooser is a session that waits until a human notices.
CHOOSER_POST_MATCH_TAIL = 4000


def _chooser_match(text: str, pattern=None):
    """The last chooser on screen, if one is genuinely showing.

    Shared by both parsers below. A chooser far up the scrollback is one the
    user already answered, and a match on the composer line is the user
    typing something that looks like one.
    """
    rx = pattern or CHOOSER_RE
    matches = list(rx.finditer(text or ""))
    if not matches:
        return None
    m = matches[-1]
    if len(text) - m.end() > CHOOSER_POST_MATCH_TAIL:
        return None
    if _is_composer_line(text, m.start()):
        return None
    # An OPEN chooser has replaced the input box — measured on a live prompt
    # caught while it waited: below the options there was only the
    # "Esc to cancel" hint and no composer at all. So a composer line after
    # the match means this is not open: either the session already answered
    # it and the box came back, or the text is a chooser QUOTED in
    # conversation. The second case is not hypothetical — a reply that
    # quoted a permission prompt verbatim got that quotation answered, twice.
    for line in text[m.end():].splitlines():
        if _COMPOSER_LINE_RE.match(line):
            return None
    return m


# The same option list with its cursor on ANY row. The arrow keys move the
# cursor, and so does picking a typed-answer row, so a chooser someone has
# touched no longer starts at the cursor on 1. Used only to HOLD this tool's
# own typing, never to answer: Enter answers whichever row the cursor is on,
# which is exactly why auto-answer stays on CHOOSER_RE (cursor on 1).
_CURSOR_ANY_RE = re.compile(
    r"(?m)^" + _ROW + r"[>\u276f][ \t]+\d{1,2}[.)][ \t]+\S")
_NUMBERED_ROW_RE = re.compile(
    r"(?m)^" + _ROW + r"(?:[>\u276f][ \t]+)?\d{1,2}[.)][ \t]+\S")


def _open_list_match(text: str, chooser_pattern=None):
    """The open option list's cursor row, wherever the cursor is, or None.

    The same "genuinely showing" rules as the answering parsers (near the
    tail, not the composer, no composer below it), plus a second numbered
    row nearby, so that one quoted "> 3. something" line is not a list.
    """
    m = _chooser_match(text, chooser_pattern)
    if m is not None:
        return m
    m = _chooser_match(text, _CURSOR_ANY_RE)
    if m is None:
        return None
    lo, hi = max(0, m.start() - 2000), min(len(text), m.end() + 2000)
    return m if len(_NUMBERED_ROW_RE.findall(text, lo, hi)) >= 2 else None


def prompt_open(text: str, chooser_pattern=None) -> bool:
    """True if an option list is waiting at the bottom, wherever its cursor."""
    return _open_list_match(text or "", chooser_pattern) is not None


def typing_hold_reason(text: str, chooser_pattern=None):
    """Why this tool must not type a line into this screen now, or None.

    Every line it types ends in Enter. On top of a half-typed draft that
    submits the draft and the line as one message; into an open prompt the
    text goes nowhere useful and the Enter picks whatever option is
    highlighted, which on a permission prompt is "Yes". Measured 2026-09-27
    in the harness: after-finish, the network retry and the limit resume all
    did one or the other. Unknown is not a reason to hold: an unreadable or
    unfamiliar screen types as before, so this cannot silently stop the
    resume that the tool exists for.
    """
    if not text:
        return None
    if composer_has_draft(text):
        return "the input box holds unsent text"
    if prompt_open(text, chooser_pattern):
        return "a prompt is open and waiting for an answer"
    return None


def _permission_context(text: str, m) -> str:
    """The text a permission prompt's tells actually live in.

    The chooser match itself is a poor place to look: the question sits on
    the line ABOVE the selection marker, and the giveaway option wordings
    ("and don't ask again") fall past the match's non-greedy end. So widen to
    a window around it — enough for the question and the full option list,
    not so much that an unrelated earlier permission prompt in the scrollback
    makes an ordinary chooser look dangerous.
    """
    return text[max(0, m.start() - 300):min(len(text), m.end() + 400)]


def parse_chooser_prompt(text: str, pattern=None, perm_pattern=None) -> bool:
    """True if a chooser is waiting that is NOT a permission prompt.

    Answering this kind of chooser only picks an option the session already
    offered; the session was going to sit there until someone did. Permission
    prompts are excluded here on purpose — they are gated by their own
    switch, because accepting one authorises work rather than merely
    unblocking it.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return False
    prx = perm_pattern or PERMISSION_RE
    return not prx.search(_permission_context(text, m))


def parse_permission_prompt(text: str, pattern=None, perm_pattern=None) -> bool:
    """True if the chooser waiting on screen is a tool-permission prompt.

    Separate from parse_chooser_prompt so the two can never be enabled by
    accident together: this one says yes to running a command or editing a
    file, which is only appropriate when the user has decided that this
    session may act unattended.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return False
    prx = perm_pattern or PERMISSION_RE
    return bool(prx.search(_permission_context(text, m)))


def chooser_signature(text: str, pattern=None):
    """A short stable id for the chooser currently on screen, or None.

    Enter must be pressed ONCE per chooser. "The pattern still matches" is
    not evidence the chooser is still open — an answered modal's text stays
    in the scrollback, and treating that as "still open" once fired eight
    Enters at one dialog, each submitting whatever was in the input box.
    Comparing this id against the last one answered is what makes it once.
    """
    m = _chooser_match(text, pattern)
    if m is None:
        return None
    return hashlib.sha1(
        m.group(0).encode("utf-8", "replace")).hexdigest()[:12]


PROMPT_TEXT_MAX = 200


def _prompt_line(text: str, m) -> str:
    """The line a human would read to know what is being asked.

    For a chooser the match starts at the "> 1." row, but the QUESTION sits on
    the line above it — that is the part worth showing on a watch face. Fall
    back to the matched line when there is nothing above, so this never comes
    back empty just because the prompt was drawn at the top of the viewport.
    """
    start = text.rfind("\n", 0, m.start()) + 1
    above_end = start - 1
    if above_end > 0:
        above_start = text.rfind("\n", 0, above_end) + 1
        above = text[above_start:above_end].strip()
        # Strip the box drawing a TUI wraps its prompts in; what is left is
        # either the question or nothing worth showing.
        above = above.strip("│┃|╎ \t")
        if len(above) > 3 and not above.strip("─━═╌ \t") == "":
            return above[:PROMPT_TEXT_MAX]
    line_end = text.find("\n", m.start())
    line = text[start:line_end if line_end != -1 else len(text)]
    return line.strip().strip("│┃|╎ \t")[:PROMPT_TEXT_MAX]


def classify_prompt(text: str, chooser_pattern=None, permission_pattern=None,
                    limit_pattern=None, limit_banner_pattern=None,
                    switch_pattern=None):
    """What this window is waiting on, or None if it is not waiting.

    A companion program showing "this session needs you" has to say WHICH kind
    of waiting it is, because the answers differ: a permission prompt and an
    ordinary chooser both take Enter, the limit picker's other option costs
    money and must never be answered automatically, and the switch-model
    dialog changes the session's model, so it is left to whoever is watching.

    Order is most-specific-first on purpose. The limit picker and the
    switch-model dialog are both choosers as far as CHOOSER_RE is concerned,
    so classifying by "is it a chooser" first would label every one of them
    the same and lose exactly the distinction that decides what is safe.
    """
    if not text:
        return None
    if parse_limit_prompt(text, limit_pattern, limit_banner_pattern):
        kind = "limit_picker"
        rx = LIMIT_PROMPT_RE
    elif parse_switch_model_prompt(text, switch_pattern):
        kind = "switch_model"
        rx = switch_pattern or SWITCH_MODEL_RE
    elif parse_permission_prompt(text, chooser_pattern, permission_pattern):
        kind = "permission"
        rx = None
    elif parse_chooser_prompt(text, chooser_pattern, permission_pattern):
        kind = "chooser"
        rx = None
    else:
        # The cursor is off the first row: the arrow keys moved it, or a
        # typed-answer row was picked. Still waiting, so still reported --
        # a watch lost the prompt the moment someone pressed Down
        # (2026-09-27). Only reported: answering stays on the parsers above,
        # which need the cursor on 1, because Enter answers the cursor's row.
        m = _open_list_match(text, chooser_pattern)
        if m is None:
            return None
        prx = permission_pattern or PERMISSION_RE
        kind = ("permission" if prx.search(_permission_context(text, m))
                else "chooser")
        rx = None
    if rx is None:
        m = _open_list_match(text, chooser_pattern)
    else:
        ms = list(rx.finditer(text))
        m = ms[-1] if ms else None
    out = {"kind": kind, "text": _prompt_line(text, m) if m else ""}
    shape = prompt_structure(text, chooser_pattern)
    if shape is not None:
        if shape.get("question"):
            out["text"] = shape["question"][:PROMPT_TEXT_MAX]
        out.update(shape)
    else:
        out.update({k: None for k in _SHAPE_KEYS})
    return out


# ---------------------------------------------------------------------------
# The shape of an open prompt, for a companion that redraws it elsewhere
# ---------------------------------------------------------------------------
# Read off real Claude Code 2.1.283 screens (cc-remote's S5 spike): the
# permission prompt, AskUserQuestion with one or several questions, check
# boxes, a typed-answer row, the preview layout, the review page, and the
# plan confirmation. Everything is what the SCREEN says, because keys act on
# the screen; what a prompt is about (a diff, a plan) is the session's, and
# is not read here. A field that cannot be read is None, never a guess.
_SHAPE_KEYS = ("sig", "title", "question", "tabs", "tab", "layout",
               "options", "below", "hint")
_SEP_SOLID_RE = re.compile(r"^\s*[\u2500\u2501]{10,}\s*$")
_SEP_DASH_RE = re.compile(r"^\s*[\u254c\u2504\u254d]{10,}\s*$")
_OPT_ROW_RE = re.compile(
    r"^(?P<lead>[ \t]{0,6}[\u2502\u2503\u2551|]?[ \t]{0,4})"
    r"(?P<cur>[>\u276f][ \t]+)?(?P<n>\d{1,2})[.)][ \t]+(?P<rest>\S.*?)\s*$")
_TAB_TOKEN_RE = re.compile(
    r"([\u2610\u2612\u2714])\s+([^\u2610\u2612\u2714\u2190\u2192]+?)"
    r"(?=\s{2,}|\s*[\u2610\u2612\u2714\u2192]|\s*$)")
_HINT_RE = re.compile(
    r"Enter\s+to\s+(?:select|confirm)|Esc\s+to\s+cancel|to\s+navigate"
    r"|Tab\s+to\s+amend|ctrl\+g\s+to\s+edit|to\s+add\s+notes",
    re.IGNORECASE)
_CHECK_RE = re.compile(r"^\[([^\]]?)\][ \t]*")
_PANEL_RE = re.compile(r"\s{2,}[\u250c\u2502\u2514]")
_INPUT_LABEL_RE = re.compile(
    r"^(?:type\s+something\.?|tell\s+claude\s+what\s+to\s+change)$",
    re.IGNORECASE)
_BOX_STRIP = "\u2502\u2503\u2551| \t"


def _tab_bar(line: str):
    """[{label, done} | {label, submit}] if `line` is a tab bar, else None."""
    if "\u2610" not in line and "\u2612" not in line:
        return None
    toks = _TAB_TOKEN_RE.findall(line)
    if not toks:
        return None
    rest = _TAB_TOKEN_RE.sub("", line)
    if rest.replace("\u2190", "").replace("\u2192", "").strip():
        return None                     # other words on the line: not a bar
    tabs = []
    for mark, label in toks:
        label = label.strip()
        if mark == "\u2714":
            tabs.append({"label": label, "submit": True})
        else:
            tabs.append({"label": label, "done": mark == "\u2612"})
    return tabs


def prompt_structure(text: str, chooser_pattern=None):
    """The open prompt as data (see _SHAPE_KEYS), or None if none is open."""
    m = _open_list_match(text or "", chooser_pattern)
    if m is None:
        return None
    lines = text.splitlines()
    c = text.count("\n", 0, m.start())
    if c >= len(lines) or not _OPT_ROW_RE.match(lines[c]):
        return None

    # Up to row 1 of this list.
    s = c
    for k in range(c, max(-1, c - 80), -1):
        ln = lines[k]
        if _SEP_SOLID_RE.match(ln) or _SEP_DASH_RE.match(ln) or _tab_bar(ln):
            break
        om = _OPT_ROW_RE.match(ln)
        if om and int(om.group("n")) == 1:
            s = k
            break

    # Down through the options, to the separator or the hint.
    options, extra, layout = [], [], "list"
    i, stop = s, None
    while i < len(lines):
        ln = lines[i]
        if _SEP_SOLID_RE.match(ln):
            stop = ("sep", i)
            break
        om = _OPT_ROW_RE.match(ln)
        # The footer is the last thing on screen. A hint-like line with a
        # rule still below it is inside the prompt -- the preview panel's
        # "Notes: press n to add notes" -- not its footer.
        if (om is None and _HINT_RE.search(ln) and not any(
                _SEP_SOLID_RE.match(x) for x in lines[i + 1:])):
            stop = ("hint", i)
            break
        if om is not None:
            rest = om.group("rest")
            pm = _PANEL_RE.search(rest)
            if pm:
                layout = "preview"
                rest = rest[:pm.start()].rstrip()
            col = om.start("rest")
            check = None
            cm = _CHECK_RE.match(rest)
            if cm:
                check = cm.group(1).strip() != ""
                col += cm.end()
                rest = rest[cm.end():]
            picked = False
            if rest.endswith("\u2714") and rest[:-1].strip():
                picked = True
                rest = rest[:-1].rstrip()
            options.append({"n": int(om.group("n")), "label": rest.strip(),
                            "desc": None, "cursor": bool(om.group("cur")),
                            "check": check, "picked": picked,
                            "input": False, "_col": col})
        elif ln.strip() and options and layout == "list":
            indent = len(ln) - len(ln.lstrip(" \t"))
            if indent >= options[-1]["_col"]:
                d = ln.strip()
                o = options[-1]
                o["desc"] = d if o["desc"] is None else o["desc"] + "\n" + d
            else:
                # An unnumbered action row inside the list (a multi-choice
                # question's "Submit"): not an option, so it goes with the
                # other unnumbered rows.
                extra.append({"n": None, "label": ln.strip(_BOX_STRIP)})
        i += 1

    below, hint = [], None
    if stop is not None and stop[0] == "sep":
        for ln in lines[stop[1] + 1:]:
            if not ln.strip():
                continue
            if _OPT_ROW_RE.match(ln) is None and _HINT_RE.search(ln):
                hint = ln.strip(_BOX_STRIP)
                break
            om = _OPT_ROW_RE.match(ln)
            if om:
                below.append({"n": int(om.group("n")),
                              "label": om.group("rest").strip()})
            else:
                below.append({"n": None, "label": ln.strip(_BOX_STRIP)})
    elif stop is not None:
        hint = lines[stop[1]].strip(_BOX_STRIP)
    below = extra + below

    # The question: the line just above row 1.
    question, qi = None, None
    for k in range(s - 1, max(-1, s - 4), -1):
        ln = lines[k]
        if not ln.strip():
            continue
        if not (_SEP_SOLID_RE.match(ln) or _SEP_DASH_RE.match(ln)
                or _tab_bar(ln)):
            question, qi = ln.strip(_BOX_STRIP), k
        break

    # Title and tabs: between the prompt's top rule and the question.
    title, tabs, review = [], None, False
    top = qi if qi is not None else s
    above, b = [], None
    for k in range(top - 1, max(-1, top - 40), -1):
        ln = lines[k]
        if _SEP_SOLID_RE.match(ln):
            b = k
            break
        t = _tab_bar(ln)
        if t is not None:
            tabs = t
            break
        above.insert(0, ln)
    body = [ln.strip(_BOX_STRIP) for ln in above]
    if any(x == "Review your answers" for x in body):
        review = True
        at = body.index("Review your answers")
        title = [x for x in body[at + 1:] if x][:6]
    else:
        if not any(x for x in body) and b is not None:
            # A plan: its text opens with a dashed rule under the title, and
            # the question's solid rule follows the text -- with a closing
            # dashed rule first in cc-remote's sample, without one on a live
            # 2.1.283 screen (the text ran straight into it). Find the
            # OPENING dashed rule either way; the title sits above it.
            k = b - 1
            if k >= 0 and _SEP_DASH_RE.match(lines[k]):
                k -= 1
            while (k >= 0 and k > b - 200 and not _SEP_DASH_RE.match(lines[k])
                   and not _SEP_SOLID_RE.match(lines[k])):
                k -= 1
            if k >= 0 and _SEP_DASH_RE.match(lines[k]):
                up = []
                for j in range(k - 1, max(-1, k - 8), -1):
                    if _SEP_SOLID_RE.match(lines[j]):
                        break
                    up.insert(0, lines[j].strip(_BOX_STRIP))
                body = up
        for x in body:
            if _SEP_DASH_RE.match(x):
                if title:
                    break
                continue
            if x:
                title.append(x[:80])
        title = title[:3]
    if tabs is None:
        # A tab bar above "Review your answers" sits past the title lines.
        for k in range(top - 1, max(-1, top - 14), -1):
            if _SEP_SOLID_RE.match(lines[k]):
                break
            t = _tab_bar(lines[k])
            if t is not None:
                tabs = t
                break

    # Which tab is showing. The highlight is not in the text, so this is
    # only said when it can be worked out: the review page is the submit
    # tab; otherwise the first unanswered question, unless an answered one
    # is being revisited (its choice carries a trailing check) -- then only
    # if there is exactly one it can be.
    tab = None
    if tabs:
        idx = list(range(len(tabs)))
        if review or (question or "").startswith("Ready to submit"):
            sub = [j for j in idx if tabs[j].get("submit")]
            tab = sub[0] if sub else None
        elif any(o["picked"] for o in options):
            done = [j for j in idx if tabs[j].get("done")]
            tab = done[0] if len(done) == 1 else None
        else:
            todo = [j for j in idx
                    if not tabs[j].get("submit") and not tabs[j].get("done")]
            tab = todo[0] if todo else None

    # The typed-answer row.
    for o in options:
        if (_INPUT_LABEL_RE.match(o["label"])
                or "with this feedback" in (o["desc"] or "")):
            o["input"] = True
    if options and not any(o["input"] for o in options):
        if hint and "ctrl+g to edit" in hint.lower() and not any(
                "plan" in (x or "").lower() for x in title):
            for o in options:
                if o["cursor"]:
                    o["input"] = True
        elif layout == "list" and any(
                (x["label"] or "").lower().startswith("chat about this")
                for x in below):
            # AskUserQuestion always ends its list with the typed answer;
            # once something is typed there, the label is that text.
            options[-1]["input"] = True
    for o in options:
        del o["_col"]

    # Question, option labels and the tab row's labels: none of them move
    # with the cursor, a check box or typing into the answer row. (The tab
    # INDEX would: it is inferred from the done marks, which a toggle flips.)
    ident = "\x1f".join(
        [question or ""]
        + ["<input>" if o["input"] else o["label"] for o in options]
        + [t["label"] for t in (tabs or [])])
    sig = hashlib.sha1(ident.encode("utf-8", "replace")).hexdigest()[:12]
    return {"sig": sig, "title": title, "question": question, "tabs": tabs,
            "tab": tab, "layout": layout, "options": options,
            "below": below, "hint": hint}


def composer_has_draft(text: str) -> bool:
    """True if the user has something half-typed in the input box.

    Every auto-answer path presses a bare Enter, and Enter with a draft in
    the box SUBMITS the draft. That is the whole risk being guarded against,
    and it exists only when a composer is on screen AND has text in it.

    An ABSENT composer is not a draft. Measured on a live session: while one
    of Claude Code's choosers is open it replaces the input box entirely —
    the screen ends with the "Enter to select / Tab to navigate / Esc to
    cancel" hint and no composer line exists. Treating that as "might be a
    draft" refused to act on every real chooser, i.e. on the only case the
    feature is for, while stand-ins that printed a composer line kept the
    tests green.

    An unreadable buffer is still "no", because nothing about it is known.
    """
    if not text:
        return False
    for line in text.splitlines():
        if _COMPOSER_LINE_RE.match(line):
            return bool(line[1:].strip())
    return False


def composer_text(text: str):
    """What the input box on screen holds, or None when there is no box.

    The box is the LAST composer line (the live one; a sent prompt shown in
    the conversation has a plain space after its glyph, see
    _COMPOSER_LINE_RE) together with the rows a long entry wraps onto, which
    run until the rule that closes the box.
    """
    if not text:
        return None
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if _COMPOSER_LINE_RE.match(lines[i]):
            out = [lines[i][2:]]
            for more in lines[i + 1:i + 40]:
                if more.strip().startswith("─"):
                    break
                out.append(more)
            return "\n".join(out)
    return None


TRIGGER_SPECS = [
    {
        "key": "limit",
        "label": "Rate-limit banner",
        "default": LIMIT_RE.pattern,
        "groups": 4,
        "help": "The “you’ve hit your limit · resets …” line. "
                "Must keep 4 capture groups in order: hour, minute, am/pm, "
                "timezone — they schedule the resume.",
    },
    {
        "key": "limit_prompt",
        "label": "Limit picker (modal)",
        "default": LIMIT_PROMPT_RE.pattern,
        "groups": 0,
        "help": "The interactive “What do you want to do?” chooser. "
                "Matching it marks the window as waiting for you; nothing is "
                "pressed, since its other option is paid extra usage.",
    },
    {
        "key": "retry",
        "label": "Network retry exhausted",
        "default": RETRY_RE.pattern,
        "groups": 2,
        "help": "The “Retrying in Ns · attempt N/M” banner. Must keep "
                "2 numeric capture groups: attempt and total.",
    },
    {
        "key": "econnreset",
        "label": "Connection error",
        "default": ECONNRESET_RE.pattern,
        "groups": 0,
        "help": "Bare API connection failures (errno / fetch failed) with no "
                "retry banner.",
    },
    {
        "key": "server_error",
        "label": "Truncated / stalled response",
        "default": SERVER_ERROR_RE.pattern,
        "groups": 0,
        "help": "Server-side mid-stream truncation — the turn ended early "
                "and ‘continue’ resumes it.",
    },
    {
        "key": "oauth",
        "label": "Dead session (login needed)",
        "default": OAUTH_EXPIRED_RE.pattern,
        "groups": 0,
        "help": "Expired OAuth token. Matching it only WARNS — no keys are "
                "ever sent, since ‘continue’ cannot fix it.",
    },
    {
        "key": "fable",
        "label": "Safeguard block",
        "default": DEFAULT_FABLE_PATTERN,
        "groups": 0,
        "help": "A turn blocked by a model's safeguards. Nothing is typed for "
                "it; a session showing one is not treated as finished by "
                "After finish.",
    },
    {
        "key": "fable_picker",
        "label": "Safeguard picker",
        "default": FABLE_PICKER_RE.pattern,
        "groups": 0,
        # Help text avoids quoting both option strings together — see the
        # de-fanging note above FABLE_PICKER_RE.
        "help": "The two-option chooser shown with the safeguard notice "
                "(“Session paused”). Its pre-selected option changes the "
                "model, so it is never auto-answered.",
    },
    {
        "key": "chooser",
        "label": "Any chooser (auto-answer)",
        "default": CHOOSER_RE.pattern,
        "groups": 0,
        "help": "A numbered chooser the session is waiting on. Only used "
                "when “Answer choosers” is enabled; Enter accepts the "
                "pre-selected first option.",
    },
    {
        "key": "permission",
        "label": "Tool-permission prompt",
        "default": PERMISSION_RE.pattern,
        "groups": 0,
        "help": "Tells a permission request apart from an ordinary chooser. "
                "It is EXCLUDED from “Answer choosers” and only answered "
                "when the separate permission switch is on.",
    },
    {
        "key": "switch_model",
        "label": "“Switch model?” dialog",
        "default": SWITCH_MODEL_RE.pattern,
        "groups": 0,
        "help": "The Yes/No confirmation shown when changing model with "
                "/model. It changes the model, so it is never auto-answered.",
    },
]

TRIGGER_DEFAULTS = {s["key"]: s["default"] for s in TRIGGER_SPECS}


def compile_trigger_patterns(overrides) -> tuple[dict, list]:
    """Compile user pattern overrides into {key: re.Pattern}.

    Returns (patterns, errors). Only keys that differ from the built-in
    default AND validate appear in `patterns`; everything else is omitted so
    callers fall back to the module-level default by passing None. `errors` is
    a list of human-readable strings for the GUI to surface — a bad override
    is REJECTED (default stays in force) rather than silently disabling a
    trigger, because a trigger that never fires is the failure mode this
    feature exists to prevent.
    """
    patterns, errors = {}, []
    specs = {s["key"]: s for s in TRIGGER_SPECS}
    for key, raw in (overrides or {}).items():
        spec = specs.get(key)
        if spec is None:
            continue
        src = str(raw or "").strip()
        if not src or src == spec["default"]:
            continue                      # unchanged → use the built-in
        try:
            rx = re.compile(src, re.IGNORECASE)
        except re.error as e:
            errors.append(f"{spec['label']}: invalid regex ({e}); "
                          f"keeping the built-in pattern")
            continue
        if rx.groups < spec["groups"]:
            errors.append(
                f"{spec['label']}: needs {spec['groups']} capture group(s) "
                f"but has {rx.groups}; keeping the built-in pattern")
            continue
        if rx.search("") is not None:
            # A pattern that can match nothing (".*", "x*", "foo|") matches at
            # every position, including a zero-width one at the very end — so
            # the tail anchor always passes and EVERY window looks stuck.
            # Left in, it would type 'continue' into every Claude session on
            # a loop, so reject it rather than let it through.
            errors.append(
                f"{spec['label']}: matches empty text, so it would fire on "
                f"every window; keeping the built-in pattern")
            continue
        patterns[key] = rx
    return patterns, errors


# Timezone ABBREVIATIONS Anthropic sometimes prints instead of IANA names.
# Mapped to a representative IANA zone so DST is handled for the region
# (e.g. "EDT" seen in summer still computes a correct winter reset).
TZ_ABBREV = {
    "UTC": "UTC", "GMT": "Etc/GMT",
    "EST": "America/New_York", "EDT": "America/New_York",
    "CST": "America/Chicago", "CDT": "America/Chicago",
    "MST": "America/Denver", "MDT": "America/Denver",
    "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "BST": "Europe/London",
    "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "JST": "Asia/Tokyo", "KST": "Asia/Seoul", "IST": "Asia/Kolkata",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
}


def next_reset_datetime(hour_12: int, minute: int, ampm: str,
                        tz_name: str) -> datetime:
    """
    Compute the next wall-clock occurrence of `hour_12:minute am/pm` in
    `tz_name`, return it as an aware UTC datetime.

    DST-correct: the target is built as a NAIVE local datetime and then
    localized with `tz.localize()`, so a reset that lands on the other side
    of a DST transition gets that side's UTC offset. (The old
    `aware.replace(hour=...)` approach kept *today's* fixed offset — the
    classic pytz pitfall — and was an hour off across transitions.)
    """
    hour_24 = hour_12 % 12 + (12 if ampm == "pm" else 0)
    name = (tz_name or "").strip()
    try:
        tz = pytz.timezone(TZ_ABBREV.get(name.upper(), name))
    except pytz.UnknownTimeZoneError:
        # Unknown zone string. The reset time is rendered in the *user's*
        # local zone, so the machine's own zone is the best guess — far
        # better than a hardcoded region. Fixed current offset (no DST
        # lookahead), fine for a <24h horizon fallback.
        local_tz = datetime.now().astimezone().tzinfo
        now_local = datetime.now(local_tz)
        target = now_local.replace(
            hour=hour_24, minute=minute, second=0, microsecond=0
        )
        if target <= now_local:
            target += timedelta(days=1)
        return target.astimezone(pytz.UTC)

    now_naive = datetime.now(tz).replace(tzinfo=None)
    target_naive = now_naive.replace(
        hour=hour_24, minute=minute, second=0, microsecond=0
    )
    if target_naive <= now_naive:
        target_naive += timedelta(days=1)
    try:
        # is_dst=True resolves the AMBIGUOUS hour — the one that repeats when
        # clocks fall back — to its FIRST occurrence. pytz's default picks
        # the second, which would park the window for an extra hour on a
        # reset that had already happened. Erring early is the cheaper
        # mistake: the fire path re-checks that the limit banner is still
        # current before typing, so a too-early wake-up is skipped, while a
        # too-late one is an hour of the session sitting idle. The flag is
        # ignored for every unambiguous time, i.e. all but one hour a year.
        target_local = tz.localize(target_naive, is_dst=True)
    except Exception:
        # Non-existent local time (spring-forward gap): shift an hour.
        target_local = tz.localize(target_naive + timedelta(hours=1),
                                   is_dst=True)
    return target_local.astimezone(pytz.UTC)


# ---------------------------------------------------------------------------
# Sending keystrokes
# ---------------------------------------------------------------------------


_user32 = ctypes.windll.user32
_user32.GetForegroundWindow.restype = wintypes.HWND


def _get_foreground_hwnd() -> int:
    return int(_user32.GetForegroundWindow() or 0)


def _set_foreground_hwnd(hwnd: int) -> None:
    if hwnd:
        try:
            _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def send_continue(window_ctrl, dry_run: bool = False) -> bool:
    """Activate the window's TermControl and type 'continue' + Enter.

    Returns True on success (or simulated success in dry-run), False if the
    target has no TermControl. Callers are responsible for any logging —
    this function intentionally does not write to stdout because callers
    may be running in a thread whose stdout codec can't encode the WT
    title's unicode glyphs.
    """
    return send_text_lines(window_ctrl, ["continue"], dry_run=dry_run)


# Typing is confirmed on the screen before the foreground goes back. Keys
# injected with SendInput are routed to whichever window is in front when
# Windows gets round to them, not when they were sent, and on a busy machine
# that lags behind the typing: a 216-character line lost its last word and its
# Enter to the previously active window while the call reported success
# (2026-10-21, a companion's preset). So the line must be seen in the input
# box before Enter is pressed, and seen gone from it before focus is handed
# back.
TYPE_CHUNK = 16             # characters typed between two foreground checks
TYPED_CONFIRM_S = 2.0       # for typed text to show, plus 10 ms a character
SUBMIT_CONFIRM_S = 3.0      # for Enter to take the line out of the box
UNREAD_SETTLE_S = 0.5       # an unreadable screen confirms nothing: wait


def _squash(text) -> str:
    """Text without whitespace: wrapping and padding differ, characters do not."""
    return "".join(str(text or "").split())


def _typed_landed(before: str, after: str, line: str) -> bool:
    """Whether `line` has arrived on screen since `before` was read."""
    want = _squash(line)
    box = composer_text(after)
    if box is not None:
        return want in _squash(box)
    # No Claude input box to look in (bash mode, another program): the line
    # must be on screen near the bottom more often than it was before.
    span = 2 * len(line) + 2500
    return _squash(after[-span:]).count(want) > _squash(before[-span:]).count(want)


def _line_submitted(pre: str, after: str, line: str) -> bool:
    """Whether Enter took `line` out of the box it was typed into."""
    if composer_text(pre) is not None:
        box = composer_text(after)
        return box is None or _squash(line) not in _squash(box)
    return after != pre


def _await_screen(window_ctrl, done, timeout: float):
    """Read the window until done(text) holds: True, False on timeout, or None
    when the screen cannot be read at all."""
    deadline = time.monotonic() + timeout
    while True:
        text = read_terminal_text(window_ctrl)
        if text is None:
            return None
        if done(text):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _send_refused(outcome, reason: str) -> bool:
    if outcome is not None:
        outcome["reason"] = reason
    return False


def send_text_lines(window_ctrl, lines, dry_run: bool = False,
                    outcome: dict | None = None) -> bool:
    """Activate the window's TermControl, then type each line followed by
    Enter, with a small pause between consecutive submissions so Claude
    Code can fully process slash commands before the next message arrives.

    True only once every line was seen arriving and being submitted (where
    the screen can be read). On False, `outcome["reason"]` (when a dict is
    passed) says why: 'foreground' (nothing typed), 'focus_lost' (another
    window took the foreground partway; part of a line may be in the box),
    'not_confirmed' (typed, but the screen never showed it arrive, or never
    showed Enter take it; nothing further was pressed), 'send_error'.

    Same caveats as send_continue: no stdout output.
    """
    if not lines:
        return True
    term = find_termcontrol(window_ctrl)
    if term is None:
        return False

    if dry_run:
        return True

    try:
        target_hwnd = int(window_ctrl.NativeWindowHandle or 0)
    except Exception:
        target_hwnd = 0

    saved_fg = _get_foreground_hwnd()
    try:
        # Two attempts to bring the target to the foreground, then VERIFY it
        # actually got there. SendKeys types into whatever has focus — if
        # SetForegroundWindow silently failed (Windows denies it while the
        # user is actively typing elsewhere), the old code injected
        # 'continue' into the user's editor/browser. Refusing to type and
        # returning False is strictly better: callers retry next tick.
        #
        # A zero/unreadable target_hwnd (NativeWindowHandle raised or was
        # falsy) means the foreground check CANNOT be performed — that must
        # be treated as "not verified", i.e. failure, not as an automatic
        # pass. The loop below still runs (SetActive/SetFocus are attempted
        # regardless) but never breaks early on a zero handle, and the
        # post-loop guard always fires when the handle is unreadable so
        # unverified typing never happens.
        for _ in range(2):
            try:
                window_ctrl.SetActive()
            except Exception:
                pass
            try:
                term.SetFocus()
            except Exception:
                pass
            # Tiny pause so the focus change actually lands.
            time.sleep(0.25)
            if target_hwnd and _get_foreground_hwnd() == target_hwnd:
                break
        if _get_foreground_hwnd() != target_hwnd:
            return _send_refused(outcome, "foreground")
        for i, line in enumerate(lines):
            line = str(line)
            if i > 0:
                # Slash commands like /effort apply synchronously but Claude
                # Code's TUI needs a beat to render the resulting state
                # before the next "continue" submission lands.
                time.sleep(0.6)
                # ...and re-check we still own the foreground. The check
                # above covers the FIRST line only; a focus steal during
                # that pause would put the rest of a multi-line sequence
                # (e.g. /model then continue) into whatever took over.
                # Stopping mid-sequence is recoverable — the caller retries
                # — while typing into the wrong app is not.
                if _get_foreground_hwnd() != target_hwnd:
                    return _send_refused(outcome, "focus_lost")
            before = read_terminal_text(window_ctrl)
            try:
                # In pieces, each behind a foreground check: a window that
                # takes over mid-line gets a few characters, not the rest.
                for k in range(0, len(line), TYPE_CHUNK):
                    if _get_foreground_hwnd() != target_hwnd:
                        return _send_refused(outcome, "focus_lost")
                    auto.SendKeys(sendkeys_literal(line[k:k + TYPE_CHUNK]),
                                  interval=0.02, waitTime=0)
                if line.strip() and before is not None:
                    seen = _await_screen(
                        window_ctrl,
                        lambda t: _typed_landed(before, t, line),
                        TYPED_CONFIRM_S + 0.01 * len(line))
                    if seen is False:
                        # Never press Enter on a line that did not all
                        # arrive: that would send part of it.
                        return _send_refused(outcome, "not_confirmed")
                if _get_foreground_hwnd() != target_hwnd:
                    return _send_refused(outcome, "focus_lost")
                pre = read_terminal_text(window_ctrl)
                auto.SendKeys("{Enter}", interval=0.02, waitTime=0)
            except Exception:
                # A throw here used to escape into the watcher's per-window
                # loop, which then had no guard of its own — so one
                # window's failed send stopped every window
                # after it from being looked at that tick, with a single
                # generic 'tick error' line as the only symptom. Report the
                # failure instead; callers already treat False as "retry
                # next tick". (This happened once for real, with braces in
                # free-typed text; sendkeys_literal escapes those now, but
                # the containment must not depend on having predicted every
                # input.)
                return _send_refused(outcome, "send_error")
            if pre is None:
                time.sleep(UNREAD_SETTLE_S + 0.005 * len(line))
            elif line.strip():
                gone = _await_screen(
                    window_ctrl, lambda t: _line_submitted(pre, t, line),
                    SUBMIT_CONFIRM_S)
                if gone is False:
                    return _send_refused(outcome, "not_confirmed")
            else:
                # A bare Enter answers a dialog; there is no text to watch,
                # only the screen moving on. Not insisted upon.
                _await_screen(window_ctrl, lambda t: t != pre, 1.0)
        return True
    finally:
        time.sleep(0.15)
        if saved_fg and saved_fg != target_hwnd:
            _set_foreground_hwnd(saved_fg)


def sendkeys_literal(text) -> str:
    """Escape text so uiautomation.SendKeys types it VERBATIM.

    SendKeys reads `{...}` as key syntax, so an unescaped brace doesn't just
    type the wrong thing — it RAISES (`{` → ValueError, `{task}` → TypeError).
    That exception used to escape send_text_lines and abort the whole tick,
    which meant every window enumerated after the offending one silently
    stopped being watched. After finish prompts are free text typed by the
    user, so braces are entirely plausible ("continue with the {plan}").
    Only braces are special here — %, ^, + and parentheses are
    literal to SendKeys unless they follow a modifier key spec.
    """
    # Single pass on purpose: chained str.replace would re-escape the braces
    # the first replacement just emitted ("{" → "{{}" → "{{{}}").
    out = []
    for ch in str(text):
        out.append("{{}" if ch == "{" else "{}}" if ch == "}" else ch)
    return "".join(out)


_KEY_TOKEN_RE = re.compile(r"\{[^{}]{1,24}\}")


def summarize_keyspec(keyspec: str) -> str:
    """A key spec with its literal text replaced by a character count.

    The log records what this tool DID, never what a session contains — and a
    key spec breaks that rule the moment a caller uses it to type words. The
    API's own sibling route already logs `lines=N` rather than the lines, and
    this brings /keys in line: '{Tab}' stays '{Tab}', while a typed sentence
    becomes '<24 chars>'. Key names are what a reader needs anyway ("did it
    press Enter?"); the prose was never the useful part.

    Seen live on 2026-09-21: a companion program typed a whole prompt through
    /keys and the activity log kept every word of it.
    """
    def _chars(n):
        return f"<{n} char>" if n == 1 else f"<{n} chars>"

    spec = str(keyspec or "")
    out, pos = [], 0
    for m in _KEY_TOKEN_RE.finditer(spec):
        if m.start() > pos:
            out.append(_chars(m.start() - pos))
        out.append(m.group(0))
        pos = m.end()
    if pos < len(spec):
        out.append(_chars(len(spec) - pos))
    # A run of one key reads as a count: a companion clearing a field with
    # eighty {Back} wrote all eighty into one log line (2026-09-27). Two
    # stay as they are -- "{Esc}{Esc}" is clearer than a count.
    runs = []
    for tok in out:
        if runs and runs[-1][0] == tok and tok.startswith("{"):
            runs[-1][1] += 1
        else:
            runs.append([tok, 1])
    return "".join(t * n if n < 3 else f"{t}×{n}" for t, n in runs)


def keyspec_problem(keyspec: str):
    """The first {...} in a SendKeys spec that cannot be pressed, or None.

    uiautomation only finds out while typing: an unknown name such as
    {Backspace} (its name is {Back}) is handed on as one "character" and
    raises there, which send_keys reports as a plain False. The API then
    answered 'foreground', and a caller that believed it would retry a
    request that can never work (seen 2026-09-27). Checked before the window
    is touched, with the same brace scan uiautomation uses.
    """
    text = str(keyspec or "")
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        r = text.find("}", i)
        if r == i + 1:                    # "{}}" is a literal "}"
            r = text.find("}", i + 2)
        if r == -1:
            return text[i:]
        tok = text[i:r + 1]
        parts = [p for p in text[i + 1:r].split(" ") if p]
        if not parts or len(parts) > 2 or (
                len(parts) == 2 and not parts[1].isdigit()):
            return tok
        name = parts[0]
        if len(name) != 1 and name.upper() not in auto.SpecialKeyNames:
            return tok
        i = r + 1
    return None


def send_keys(window_ctrl, keyspec: str, dry_run: bool = False) -> bool:
    """Activate the window's TermControl and send a raw uiautomation SendKeys
    spec (e.g. "{Esc}") with NO trailing Enter. Same foreground-safety and
    return contract as send_text_lines — used for interrupt keys like ESC that
    surface the "Switch model?" dialog without waiting for the turn to end."""
    if not keyspec:
        return True
    term = find_termcontrol(window_ctrl)
    if term is None:
        return False
    if dry_run:
        return True
    try:
        target_hwnd = int(window_ctrl.NativeWindowHandle or 0)
    except Exception:
        target_hwnd = 0
    saved_fg = _get_foreground_hwnd()
    try:
        # Same "unreadable handle means unverified, not verified" rule as
        # send_text_lines — see the comment there. A zero target_hwnd must
        # never cause an early break/pass; the post-loop check always fires.
        for _ in range(2):
            try:
                window_ctrl.SetActive()
            except Exception:
                pass
            try:
                term.SetFocus()
            except Exception:
                pass
            time.sleep(0.25)
            if target_hwnd and _get_foreground_hwnd() == target_hwnd:
                break
        if _get_foreground_hwnd() != target_hwnd:
            return False
        try:
            auto.SendKeys(str(keyspec), interval=0.02)
        except Exception:
            return False        # same containment as send_text_lines
        return True
    finally:
        # Raw keys mean nothing that could be looked for on screen, so unlike
        # send_text_lines nothing is confirmed here; a longer spec just keeps
        # the foreground longer before it is handed back (see TYPE_CHUNK).
        time.sleep(min(1.5, 0.15 + 0.005 * len(str(keyspec))))
        if saved_fg and saved_fg != target_hwnd:
            _set_foreground_hwnd(saved_fg)
