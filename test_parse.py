"""Unit-test the limit-message regex against realistic scrollback samples."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import auto_continue as ac
from auto_continue import (
    NETWORK_POST_MATCH_TAIL, SWITCH_POST_MATCH_TAIL,
    compile_trigger_patterns, parse_limit_message, parse_retry_exhausted,
    parse_econnreset_stuck, parse_server_error_stuck, parse_fable_refusal,
    parse_fable_picker, parse_switch_model_prompt, parse_limit_prompt,
    parse_oauth_expired,
    next_reset_datetime,
    send_text_lines, send_keys,
)
from datetime import datetime, timedelta
import pytz

# The Anthropic wordings of the follow-up line seen in the wild. UP3 is the
# current one; /extra-usage was renamed /usage-credits, and since 2026-08-30
# the old command is no longer an accepted alternative — UP still parses, but
# on its shared "finish what you" tail rather than on the dead command name.
UP = "/upgrade or /extra-usage to finish what you're working on."
UP2 = "/upgrade to increase your usage limit."
UP3 = "/upgrade or /usage-credits to finish what you're working on."
samples = [
    # NEWEST wording from user's 2026-07-11 screenshot: "session limit",
    # reset with minutes, /usage-credits follow-up. This is the exact case
    # that previously went undetected (regex only accepted /extra-usage).
    ("You've hit your session limit · resets 5:20am (Asia/Shanghai)\n" + UP3,
     (5, 20, "am", "Asia/Shanghai")),
    # /usage-credits with a whole-hour reset.
    ("You've hit your limit · resets 9pm (Asia/Shanghai)\n" + UP3,
     (9, 0, "pm", "Asia/Shanghai")),
    # /usage-credits + bullet-operator separator (∙ U+2219, not the middle
    # dot) — Claude Code emits this glyph in some builds.
    ("You've hit your session limit ∙ resets 5:20am (Asia/Shanghai)\n" + UP3,
     (5, 20, "am", "Asia/Shanghai")),
    # /usage-credits + dot-operator separator (⋅ U+22C5).
    ("You've hit your session limit ⋅ resets 3am (UTC)\n" + UP3,
     (3, 0, "am", "UTC")),
    # Exact form from the original screenshot (whole-hour reset).
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, 0, "pm", "Asia/Shanghai")),
    # Curly apostrophe.
    ("You’ve hit your limit · resets 11pm (Asia/Shanghai)\n" + UP,
     (11, 0, "pm", "Asia/Shanghai")),
    # AM, single-digit hour, different timezone.
    ("You've hit your limit · resets 3am (America/Los_Angeles)\n" + UP,
     (3, 0, "am", "America/Los_Angeles")),
    # New wording: reset time WITH minutes + the newer /upgrade line.
    ("You've hit your limit · resets 2:50pm (Asia/Shanghai)\n" + UP2,
     (2, 50, "pm", "Asia/Shanghai")),
    # New /upgrade wording but whole-hour reset.
    ("You've hit your limit · resets 9am (UTC)\n" + UP2,
     (9, 0, "am", "UTC")),
    # Old /upgrade wording but with minutes.
    ("You've hit your limit · resets 7:05pm (Asia/Shanghai)\n" + UP,
     (7, 5, "pm", "Asia/Shanghai")),
    # Two limit lines in same buffer — must pick the latest one.
    ("You've hit your limit · resets 7pm (Asia/Shanghai)\n" + UP + "\n"
     + "...later...\n"
     "You've hit your limit · resets 11:30pm (Asia/Shanghai)\n" + UP2,
     (11, 30, "pm", "Asia/Shanghai")),
    # No match.
    ("nothing relevant here", None),
    # Different bullet glyph (•).
    ("You've hit your limit • resets 4pm (UTC)\n" + UP, (4, 0, "pm", "UTC")),
    # Stale match buried far below — should be rejected.
    ("You've hit your limit · resets 11pm (Asia/Shanghai)\n" + UP
     + "\n" + "x" * 6000,
     None),
    # Limit phrase WITHOUT the /upgrade follow-up line (e.g. this script's
    # own source code, or test data) — must NOT match.
    ("# example: You've hit your limit · resets 11pm (Asia/Shanghai)\n"
     "# (used in unit tests; no upgrade line)",
     None),
    # NEW wording variant: "session limit" (seen 2026-05-28).
    ("You've hit your session limit · resets 12am (Asia/Shanghai)\n" + UP2,
     (12, 0, "am", "Asia/Shanghai")),
    # Curly apostrophe + session limit.
    ("You’ve hit your session limit · resets 12am (Asia/Shanghai)\n" + UP2,
     (12, 0, "am", "Asia/Shanghai")),
    # Hypothetical "weekly limit" — same shape, should also match.
    ("You've hit your weekly limit · resets 9:30am (UTC)\n" + UP2,
     (9, 30, "am", "UTC")),
    # Hypothetical "usage limit" wording — also matches.
    ("You've hit your usage limit · resets 4pm (Asia/Shanghai)\n" + UP,
     (4, 0, "pm", "Asia/Shanghai")),
    # --- Defect regression: multi-word / hyphenated qualifiers (previously
    # missed — LIMIT_RE only accepted a single \w+ qualifier). ---
    # Two-word qualifier: "daily usage limit".
    ("You've hit your daily usage limit · resets 6pm (Asia/Shanghai)\n" + UP2,
     (6, 0, "pm", "Asia/Shanghai")),
    # Hyphenated + numeric qualifier: "5-hour limit".
    ("You've hit your 5-hour limit · resets 8pm (Asia/Shanghai)\n" + UP2,
     (8, 0, "pm", "Asia/Shanghai")),
    # Three-word qualifier — still within the allowed bound.
    ("You've hit your weekly usage session limit · resets 1am (UTC)\n" + UP2,
     (1, 0, "am", "UTC")),
    # Bound check: MORE than 3 qualifier words must NOT match — keeps the
    # pattern from drifting into matching unrelated prose.
    ("You've hit your one two three four limit · resets 1am (UTC)\n" + UP2,
     None),
    # --- Different-timezone users (Anthropic renders the user's local tz). ---
    # West coast US.
    ("You've hit your session limit · resets 7pm (America/Los_Angeles)\n"
     + UP2, (7, 0, "pm", "America/Los_Angeles")),
    # London.
    ("You've hit your session limit · resets 11pm (Europe/London)\n" + UP2,
     (11, 0, "pm", "Europe/London")),
    # Tokyo with minutes.
    ("You've hit your session limit · resets 8:45am (Asia/Tokyo)\n" + UP2,
     (8, 45, "am", "Asia/Tokyo")),
    # Sao Paulo with hyphen in tz name.
    ("You've hit your session limit · resets 6pm (America/Sao_Paulo)\n"
     + UP2, (6, 0, "pm", "America/Sao_Paulo")),
    # UTC.
    ("You've hit your session limit · resets 12am (UTC)\n" + UP2,
     (12, 0, "am", "UTC")),
    # New York with EDT abbrev (Anthropic sometimes uses abbrev, sometimes
    # IANA). pytz won't recognize "EDT" but parse should still succeed —
    # next_reset_datetime falls back to Asia/Shanghai gracefully.
    ("You've hit your session limit · resets 9am (EDT)\n" + UP2,
     (9, 0, "am", "EDT")),
]

failures = 0
for i, (text, expected) in enumerate(samples):
    got = parse_limit_message(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1

print()
print("---- next_reset_datetime ----")


def check_reset(label, cond):
    global failures
    status = "OK " if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures += 1


# Basic invariants: result is aware UTC, in the future, within 24h, and has
# the requested wall-clock time in the requested zone.
def assert_reset(hour_12, minute, ampm, tz_name, expect_tz=None):
    res = next_reset_datetime(hour_12, minute, ampm, tz_name)
    now = datetime.now(pytz.UTC)
    ok = (res.tzinfo is not None and now < res <= now + timedelta(days=1))
    if expect_tz:
        local = res.astimezone(pytz.timezone(expect_tz))
        hour_24 = hour_12 % 12 + (12 if ampm == "pm" else 0)
        ok = ok and (local.hour, local.minute) == (hour_24, minute)
    check_reset(f"{hour_12}:{minute:02d}{ampm} ({tz_name})", ok)


assert_reset(2, 50, "pm", "Asia/Shanghai", "Asia/Shanghai")
assert_reset(12, 0, "am", "Asia/Shanghai", "Asia/Shanghai")   # midnight
assert_reset(12, 0, "pm", "Asia/Shanghai", "Asia/Shanghai")   # noon
assert_reset(11, 0, "pm", "America/New_York", "America/New_York")  # DST zone
assert_reset(9, 0, "am", "EDT", "America/New_York")   # abbrev → IANA map
assert_reset(6, 30, "pm", "PST", "America/Los_Angeles")
assert_reset(7, 0, "am", "NoSuch/Zone")               # local-tz fallback

# DST correctness: the target must carry the OFFSET OF THE TARGET MOMENT,
# not today's. Simulate by checking round-trip consistency: converting the
# result back to the zone must show exactly the requested wall-clock time
# (pytz normalize would reveal a wrong fixed offset as a shifted hour).
res = next_reset_datetime(11, 0, "pm", "America/New_York")
back = res.astimezone(pytz.timezone("America/New_York"))
check_reset("DST round-trip America/New_York 11pm",
            (back.hour, back.minute) == (23, 0))


# ---------- parse_retry_exhausted ----------
retry_samples = [
    # Plain attempt 10/10 near the tail — exhausted.
    ("· Working… (esc to interrupt)\n  Retry" "ing in 0s · attempt 10/10",
     True),
    # Mid-retry (7/10) — NOT exhausted.
    ("Retry" "ing in 0s · attempt 7/10", False),
    # 1/10 — definitely not exhausted.
    ("Retry" "ing in 12s · attempt 1/10", False),
    # Exhausted with bullet variant •
    ("Retry" "ing in 3s • attempt 10/10\n", True),
    # Older Claude variants without space before s.
    ("Retry" "ing in 0s·attempt 10/10", True),
    # Two retry sequences in the buffer — must look at the latest.
    ("Retry" "ing in 0s · attempt 10/10\n...later...\n"
     "Retry" "ing in 5s · attempt 3/10",
     False),
    # Latest is the exhausted one.
    ("Retry" "ing in 5s · attempt 3/10\n...later...\n"
     "Retry" "ing in 0s · attempt 10/10",
     True),
    # Trailing footer+recap of ~5000 chars (wide terminal) — within the
    # network tail allowance (6000), so still treated as current.
    ("Retry" "ing in 0s · attempt 10/10\n" + "x" * 5000, True),
    # Exhausted banner buried far above — stale, ignore.
    ("Retry" "ing in 0s · attempt 10/10\n" + "x" * 7000, False),
    # Unrelated text mentioning attempt 10/10 (no Retrying anchor).
    ("test failed on attempt 10/10 of the run", False),
    # No retry banner at all.
    ("nothing to see here", False),
    # 5/5 retry config (hypothetical lower max) — still exhausted (N >= total).
    ("Retry" "ing in 0s · attempt 5/5", True),
    # Long-form wording some builds print.
    ("Retry" "ing in 8 seconds… (attempt 10/10)", True),
    ("Retry" "ing in 8 seconds… (attempt 3/10)", False),
]

print()
print("---- parse_retry_exhausted ----")
for i, (text, expected) in enumerate(retry_samples):
    got = parse_retry_exhausted(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] retry sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_econnreset_stuck ----------
econn_samples = [
    # Reported live 2026-09-13 by a user whose session sat at its prompt
    # behind two of these while nothing poked it: no errno, no parentheses,
    # so none of the wordings below matched and the tool saw a healthy idle
    # window. Same stuck-until-continue state as the rest.
    ("  API Err" "or: No resp" "onse from API\n"
     "  Crunched for 15m 33s · done 12:48\n> \n", True),
    # ...but not once the session has started streaming again after it, and
    # not when the text is only sitting in the user's own input box.
    ("  API Err" "or: No resp" "onse from API\n"
     "  ✽ Composing… (12s · ↓ 92 tokens)\n", False),
    (">\xa0API Err" "or: No resp" "onse from API\n", False),
    # 2026-09-23: newer wording carries "the". A final banner in that style
    # must be poked like the old one...
    ("  API Err" "or: No resp" "onse from the API after 16m\n"
     "  Crunched for 16m 2s · done 12:48\n> \n", True),
    # 2026-09-24, pasted by the user: a CMIS session sat idle for hours
    # behind this — new wording, same errno.
    ("● API Err" "or: Connection dropped (ECONN" "RESET)\n\n"
     "✻ Cooked for 3h 44m 10s · done 14:53\n\n"
     "※ recap: 我在按规范逐轮检查 CMIS 工具，每轮修一个缺陷并发布。"
     "下一轮 08:52 自动开始。 (disable recaps in /config)\n> \n", True),
    # Any future wording that still carries the code is caught the same way...
    ("  API Err" "or: Socket closed unexpectedly (EP" "IPE)\n> \n", True),
    # ...but an ordinary parenthesised word is not an errno.
    ("  API Err" "or: press (Enter) to retry\n> \n", False),
    ("  API Err" "or: something failed (Error)\n> \n", False),
    # And the new wording quoted mid-sentence is still just a quote.
    ("  earlier the log said API Err" "or: Connection dropped (ECONN" "RESET) "
     "but it recovered\n> \n", False),
    # ...but the IN-FLIGHT notice is Claude Code still retrying on its own:
    # no "API Error:" prefix, and a 'continue' now would queue behind it.
    ("✻ No resp" "onse from the API after 6m · retrying once, "
     "waiting up to 10m\n> \n", False),
    # Exact form seen in the screenshot.
    ("⎿  · 电域模块 (S参数/RX Filter/CTLE/RX FFE/DFE) 直连光信号源...\n"
     "  API Error: Unable to conn" "ect to API (ECONNRESET)\n"
     "✻ Sautéed for 15m 46s · 1 shell still running",
     True),
    # Compact form.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)", True),
    # Trailing footer + multi-line recap (~5000 chars, wide terminal) — the
    # exact regression from Image #14: error not at the very bottom but still
    # current. Within the 6000 network tail allowance → must detect.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n"
     "* Worked for 15m 6s\n* recap: ...\n" + "x" * 5000, True),
    # Stale — buried far up in scrollback (beyond the network allowance).
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n" + "x" * 7000,
     False),
    # No match.
    ("everything fine here", False),
    # Bare token "ECONNRESET" without the API Error prefix — should NOT match
    # (avoids false-matching log files / source code mentioning the constant).
    ("socket error: ECONNRESET on fd 7", False),
    # The Retrying banner alone shouldn't trip this function.
    ("Retry" "ing in 0s · attempt 5/10", False),
    # Mixed case for the literal token.
    ("API Error: Unable to conn" "ect to API (econnreset)", True),
    # Two ECONNRESET lines — newer one near tail, should match.
    ("API Error: Unable to conn" "ect to API (ECONNRESET)\n...later...\n"
     "API Error: Unable to conn" "ect to API (ECONNRESET)",
     True),
    # Sibling errnos — same stuck state, must all be detected.
    ("API Error: Unable to conn" "ect to API (ETIMEDOUT)", True),
    ("API Error: Unable to conn" "ect to API (ECONNREFUSED)", True),
    ("API Error: Unable to conn" "ect to API (ENOTFOUND)", True),
    ("API Error: Unable to conn" "ect to API (EAI_AGAIN)", True),
    ("API Error: Unable to conn" "ect to API (EHOSTUNREACH)", True),
    # undici error codes.
    ("API Error: Unable to conn" "ect to API (UND_ERR_CONNECT_TIMEOUT)", True),
    # Node's generic wording.
    ("API Error: fetch fai" "led", True),
    # HTTP status errors are NOT network-stuck (Claude retries those itself).
    ("API Error: 500 {\"type\":\"error\"}", False),
    ("API Error: 529 overloaded", False),
]

print()
print("---- parse_econnreset_stuck ----")
for i, (text, expected) in enumerate(econn_samples):
    got = parse_econnreset_stuck(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] econn sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_server_error_stuck ----------
server_error_samples = [
    # Exact form from the user's 2026-07-11 screenshot (Image #7).
    ("● API Error: Server error mid-resp" "onse. The response above may be "
     "incomplete.\n✻ Sautéed for 1m 6s", True),
    # Compact form.
    ("API Error: Server error mid-resp" "onse.", True),
    # Trailing footer + recap (~5000 chars, wide terminal) — within the 6000
    # network tail allowance → still current.
    ("API Error: Server error mid-resp" "onse. The response above may be "
     "incomplete.\n" + "x" * 5000, True),
    # Stale — buried far up in scrollback (beyond the network allowance).
    ("API Error: Server error mid-resp" "onse.\n" + "x" * 7000, False),
    # No match.
    ("everything fine here", False),
    # Must NOT confuse with the connectivity error (that's econnreset's job,
    # but neither should false-match the other).
    ("API Error: Unable to conn" "ect to API (ECONNRESET)", False),
    # Prose merely mentioning a server error without the "API Error:" prefix
    # — must NOT match (avoids logs / this discussion in scrollback).
    ("the server returned an error mid-response, retrying", False),
    # Two markers — newer one near tail, should match.
    ("API Error: Server error mid-resp" "onse.\n...later...\n"
     "API Error: Server error mid-resp" "onse.", True),
    # 2026-07-23 re-wording (user Image #1): same truncation class, new
    # leading phrase "Response stalled mid-str·eam" + the shared footer.
    ("● API Error: Response stalled mid-str" "eam. The response above may be "
     "incomplete.\n※ Baked for 26m 35s", True),
    # Compact stalled form (no footer).
    ("API Error: Response stalled mid-str" "eam.", True),
    # Forward-compat: an unseen leading phrase, but the shared footer within
    # the same `API Error` line still classifies it as a truncation.
    ("API Error: Streaming interrupted. The response above may be "
     "incomplete.", True),
    # Stale stalled marker buried far up scrollback (beyond the allowance).
    ("API Error: Response stalled mid-str" "eam.\n" + "x" * 7000, False),
]

print()
print("---- parse_server_error_stuck ----")
for i, (text, expected) in enumerate(server_error_samples):
    got = parse_server_error_stuck(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] server-error sample {i}: got={got!r} "
          f"expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_fable_refusal ----------
# NOTE: like the limit-picker samples further down, every phrase the detectors
# anchor on is split with string concatenation so that DISPLAYING this test
# file inside a watched terminal cannot trigger a real Fable recovery — which
# would type /model, ESC and Enter into that live session. Keep the splits.
_SG = "safegu" "ards flagged"
_YES = "Yes, swi" "tch to"
_NO = "No, go b" "ack"

fable_refusal_samples = [
    # Exact terminal text (user Image #2): Fable safeguard block.
    ("● API Error: Fable 5's " + _SG + " this message "
     "(https://www.anthropic.com/legal/aup). They may flag safe, normal "
     "content as well.\n\nDouble press esc to edit your last message, or "
     "try a different model with /model.", True),
    # The 'can't respond ... with Fable' clause alone.
    ("Claude Code can" "'t respond to this request wi" "th Fable 5.", True),
    # Curly-apostrophe variant.
    ("Fable 5’s " + _SG + " this message", True),
    # No version number after Fable.
    ("Fable's " + _SG + " this message", True),
    # Unrelated prose mentioning Fable — must NOT match.
    ("everything is fine, still running on Fable 5", False),
    # Stale — buried far up scrollback. Padded off the CODE's own allowance so
    # the test tracks the constant instead of hard-coding a stale number.
    ("Fable 5's " + _SG + " this message\n"
     + "x" * (NETWORK_POST_MATCH_TAIL + 1000), False),
]

print()
print("---- parse_fable_refusal ----")
for i, (text, expected) in enumerate(fable_refusal_samples):
    got = parse_fable_refusal(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] fable sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_switch_model_prompt ----------
switch_model_samples = [
    # The dialog from the user's Image #6 (switching to Fable 5).
    ("Switch model?\nYour next response will be slower and use more tokens\n\n"
     "This conversation is cached for the current model. Switching to Fable 5 "
     "means the full history gets re-read on your next message.\n\n"
     "> 1. " + _YES + " Fable 5\n  2. " + _NO, True),
    # Switching-to-Opus variant.
    ("Switch model?\n\n> 1. " + _YES + " Opus 5\n  2. " + _NO, True),
    # No dialog.
    ("just some normal output here", False),
    # 'Switch model?' words but no Yes/No options — not the dialog.
    ("should we Switch model? maybe later", False),
    # Affirmative option WITHOUT the negative one — not the dialog, so a bare
    # Enter must not be pressed.
    ("> 1. " + _YES + " Fable 5", False),
    # A full-width terminal stacks its footer (input box, rules, status bar)
    # below the modal; the dialog must still be detected through it.
    ("Switch model?\n> 1. " + _YES + " Fable 5\n  2. " + _NO + "\n"
     + "x" * (SWITCH_POST_MATCH_TAIL - 500), True),
    # Stale — buried far up scrollback. Padded off the CODE's own allowance so
    # the test tracks the constant instead of hard-coding a stale number.
    ("Switch model?\n> 1. " + _YES + " Fable 5\n  2. " + _NO + "\n"
     + "x" * (SWITCH_POST_MATCH_TAIL + 500), False),
]

# ---------- parse_fable_picker ----------
# The chooser Claude Code shows WITH the safeguard notice. Option 1 is
# pre-selected, so a bare Enter performs the model switch — this, not /model,
# is the real recovery path. Literals split like everything else here.
_SW = "Switch t" "o"
_ED = "Edit promp" "t and retry"
fable_picker_samples = [
    # Verbatim from a real block (2026-08-04).
    ("Session paused\n\nFable 5's " + _SG + " this message. The safeguards "
     "are intentionally broad right now and may flag safe and routine "
     "coding, cybersecurity, or biology work.\n\n"
     "> 1. " + _SW + " Opus 5\n  2. " + _ED + " with Fable 5", True),
    # The fallback model name changes every release — must not be hardcoded.
    ("> 1. " + _SW + " Sonnet 5\n  2. " + _ED + " with Fable 5", True),
    # Only the first option, no pair — not the picker, so no blind Enter.
    ("> 1. " + _SW + " Opus 5", False),
    # Ordinary prose.
    ("I had to switch to a different model yesterday", False),
    # Stale — scrolled too far up to still be open.
    ("> 1. " + _SW + " Opus 5\n  2. " + _ED + " with Fable 5\n"
     + "x" * (SWITCH_POST_MATCH_TAIL + 500), False),
]

# The notice must be recognised for ANY model, including ones that do not
# exist yet. Naming one model here is how detection silently dies at the next
# rename — Opus 4.8 retired and Opus 5 shipped during this project's life.
print()
print("---- safeguard notice is model-agnostic ----")
for _m in ("Fable 5", "Opus 5", "Opus 6", "Sonnet 5", "Haiku 4.5",
           "Sonata 7", "Nova 12.3"):
    _s = f"API Error: {_m}'s " + _SG + " this message."
    _ok = parse_fable_refusal(_s) is True
    print(f"[{'OK ' if _ok else 'FAIL'}] future model {_m!r} still detected")
    if not _ok:
        failures += 1

for _s, _why in (
    ("everything is fine, still running on Fable 5", "prose naming a model"),
    ("the safeguards are intentionally broad right now", "safeguards, no flag"),
    ("we flagged this for review later", "flagged, no safeguards"),
):
    _ok = parse_fable_refusal(_s) is False
    print(f"[{'OK ' if _ok else 'FAIL'}] not fooled by {_why}")
    if not _ok:
        failures += 1


print()
print("---- parse_fable_picker ----")
for i, (text, expected) in enumerate(fable_picker_samples):
    got = parse_fable_picker(text)
    ok = got == expected
    print(f"[{'OK ' if ok else 'FAIL'}] picker sample {i}: "
          f"got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


print()
print("---- parse_switch_model_prompt ----")
for i, (text, expected) in enumerate(switch_model_samples):
    got = parse_switch_model_prompt(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] switch-model sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- classify_prompt ----------
# The local API reports WHICH kind of waiting a window is in, because the
# answers differ: an ordinary chooser and a permission prompt both take Enter,
# the limit picker's other option costs money and must never be auto-answered,
# and the switch-model dialog changes the model, so it is the user's. Classifying by
# "is it a chooser" alone would flatten all four into one and lose exactly the
# distinction that decides what is safe to do about it.
_CN = chr(0x276F)
_CV = chr(0x2502)
_CQ = "Claude has written up a plan. Would you like to proceed?"
_C_plain = f"{_CQ}{chr(10)}{_CN} 1" ". Yes{chr(10)}  2" ". No"
_C_plain = _CQ + chr(10) + _CN + " 1" ". Yes" + chr(10) + "  2" ". No"
_C_boxed = ("Ready?" + chr(10) + _CV + " " + _CQ + chr(10)
            + _CV + " " + _CN + " 1" ". Yes" + chr(10) + _CV + "   2" ". No")
_P_q = "Do you want to make this edit to parser.py?"
_C_perm = (_P_q + chr(10) + _CN + " 1" ". Yes" + chr(10)
           + "  2" ". Yes, and don" + chr(0x2019) + "t ask again this session"
           + chr(10) + "  3" ". No, tell Claude what to do differently")

_r = ac.classify_prompt(_C_plain)
check_reset("classify: a plain chooser is 'chooser'",
            _r and _r["kind"] == "chooser")
check_reset("classify: it carries the QUESTION, not the option row",
            _r and _r["text"] == _CQ)
_r = ac.classify_prompt(_C_boxed)
check_reset("classify: a boxed chooser reads the same, box rule stripped",
            _r and _r["kind"] == "chooser" and _r["text"] == _CQ)
_r = ac.classify_prompt(_C_perm)
check_reset("classify: a permission prompt is not just 'chooser'",
            _r and _r["kind"] == "permission" and _r["text"] == _P_q)
check_reset("classify: an idle screen is not waiting on anything",
            ac.classify_prompt("some output" + chr(10) + "> ") is None)
check_reset("classify: an unreadable screen is None, not an error",
            ac.classify_prompt("") is None)
check_reset("classify: the text is capped so a watch face can show it",
            len((ac.classify_prompt(
                "x" * 400 + chr(10) + _CN + " 1" ". Yes" + chr(10)
                + "  2" ". No") or {}).get("text", "")) <= ac.PROMPT_TEXT_MAX)

# ---------- parse_limit_prompt ----------
# The interactive limit picker. NOTE: key phrases below are built via string
# concatenation so that DISPLAYING this test file inside a watched terminal
# cannot false-trigger the detector.
PICKER = ("What do you want to do?\n"
          "> 1. Stop and wait for li" "mit to reset\n"
          "  2. Upgrade your plan\n"
          "Enter to conf" "irm · Esc to cancel")
BANNER = ("You've hit your li" "mit · resets 11pm (Asia/Shanghai)\n"
          "/upgra" "de to increase your usage limit.")
prompt_samples = [
    # Picker open at the tail — must press Enter.
    (PICKER, True),
    # Exact form from the screenshot, with preceding turn output.
    ("● 架构侦察一下,再上多智能体全面审查。\n"
     "✳ Waiting for 1 dynamic workflow to finish\n" + PICKER, True),
    # Some padding after the footer (narrow modal, wide terminal) — still ok.
    (PICKER + "\n" + " " * 800, True),
    # Buried under real output — stale, do NOT press Enter.
    (PICKER + "\n" + "x" * 2000, False),
    # Already confirmed: the limit banner appears AFTER the picker.
    (PICKER + "\n" + BANNER, False),
    # Banner BEFORE the picker (previous limit) — picker is current, Enter.
    (BANNER + "\n...much later...\n" + PICKER, True),
    # No picker at all.
    ("nothing here", False),
    # Similar words without the full three-part shape — no match.
    ("What do you want to do? open the pod bay doors\n"
     "Enter to conf" "irm", False),
]

print()
print("---- parse_limit_prompt ----")
for i, (text, expected) in enumerate(prompt_samples):
    got = parse_limit_prompt(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] prompt sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- parse_oauth_expired ----------
oauth_samples = [
    ("OAuth token has exp" "ired · Please run /log" "in", True),
    ("API Error: OAuth token has exp" "ired", True),
    ("OAuth token has exp" "ired\n" + "x" * 7000, False),  # stale
    ("everything fine", False),
]

print()
print("---- parse_oauth_expired ----")
for i, (text, expected) in enumerate(oauth_samples):
    got = parse_oauth_expired(text)
    ok = got == expected
    status = "OK " if ok else "FAIL"
    print(f"[{status}] oauth sample {i}: got={got!r} expected={expected!r}")
    if not ok:
        failures += 1


# ---------- compile_trigger_patterns (user-editable triggers) ----------
# The Advanced dialog lets users rewrite these regexes when Anthropic changes
# a banner's wording. A bad override must never silently disable a trigger or
# make one fire on everything, so validation is enforced here, not in the UI.
print()
print("---- compile_trigger_patterns ----")


def tcheck(label, cond):
    global failures
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        failures += 1


pats, errs = compile_trigger_patterns({})
tcheck("no overrides -> nothing compiled, no errors", pats == {} and errs == [])

pats, errs = compile_trigger_patterns({"econnreset": "BO" "OM"})
tcheck("valid override is accepted", list(pats) == ["econnreset"] and not errs)

pats, errs = compile_trigger_patterns({"econnreset": "(unclosed"})
tcheck("invalid regex rejected, default kept", pats == {} and len(errs) == 1)

# The limit banner's 4 groups feed the reset-time scheduler; fewer would raise
# mid-tick, so the override must be refused up front.
pats, errs = compile_trigger_patterns({"limit": "no groups here"})
tcheck("too-few-groups rejected", pats == {} and len(errs) == 1)

pats, errs = compile_trigger_patterns({"retry": r"tried (\d+) of (\d+)"})
tcheck("group-bearing override with enough groups accepted",
       list(pats) == ["retry"] and not errs)

# A pattern that can match the empty string matches at EVERY position — the
# tail anchor always passes, so every window would look stuck forever.
for empty_matcher in (".*", "x*", "fo" "o|"):
    pats, errs = compile_trigger_patterns({"econnreset": empty_matcher})
    tcheck(f"empty-matching {empty_matcher!r} rejected",
           pats == {} and len(errs) == 1)

# An override equal to the built-in default is not stored, so a future build's
# improved default still wins for triggers the user never really changed.
from auto_continue import TRIGGER_DEFAULTS as _TD
pats, errs = compile_trigger_patterns({"fable": _TD["fable"]})
tcheck("override identical to default is a no-op", pats == {} and not errs)

pats, errs = compile_trigger_patterns({"not_a_real_trigger": "x"})
tcheck("unknown trigger key ignored", pats == {} and not errs)

# End to end: a custom pattern actually drives the parser it belongs to.
pats, _ = compile_trigger_patterns({"fable": "custom" + " refusal wording"})
tcheck("custom pattern drives its parser",
       parse_fable_refusal("custom" " refusal wording", pats["fable"]) is True
       and parse_fable_refusal("Fable's " + _SG, pats["fable"]) is False)


# ---------- a streaming session is not stuck ----------
# Observed 2026-08-08: an 80-minute outage put EIGHT 'continue' prompts into
# one session. Each resend fired because the banner was still within the tail
# allowance — but the session had already recovered and was streaming, so every
# 'continue' just queued behind the running turn and all eight landed at once
# when it finished. A banner only means "stuck" while nothing has run since it.
print()
print("---- running-session gate ----")

_ERR = "API Error: Unable to conn" "ect to API (ECONNRESET)"
_RETRY = "Retry" "ing in 8s · attempt 10/10"
_TRUNC = "API Error: Server error mid-resp" "onse."
_BAR = "  [Opus 5] ███ 46% | 用量 █ 12% (2h 23m / 5h)\n  ⏱️  3h 59m"

running_samples = [
    # Live spinner after the banner: recovered, do not poke.
    (f"{_ERR}\n> \n  ✽ Swirling… (2m 0s · ↓ 5.0k tokens)", False),
    (f"{_ERR}\n  ✻ Misting… (2m 3s · ↓ 2.8k tokens · thinking)", False),
    # Spinner with no token counter — a shorter suffix, still the clock form.
    (f"{_ERR}\n  ✽ Working… (12s · " "esc to interrupt)", False),
    # Idle at the prompt: still stuck. The status bar's own parenthesised
    # times use a slash, not a middot, so they must not read as a spinner.
    (f"{_ERR}\n> \n{_BAR}", True),
    # Turn FINISHED after the banner — "Brewed for 3m 2s · …" carries no
    # parentheses, so it is not a spinner and the session is idle and stuck.
    (f"{_ERR}\n  ✻ Brewed for 3m 2s · 1 shell still running\n> ", True),
    # Spinner ABOVE the banner: that turn is the one that died. Still stuck.
    (f"  ✽ Swirling… (2m 0s · ↓ 5.0k tokens)\n{_ERR}\n> ", True),
]
for i, (text, expected) in enumerate(running_samples):
    tcheck(f"econnreset running-gate sample {i}",
           parse_econnreset_stuck(text) is expected)

# The gate applies to all three network detectors, so both tick loops inherit
# it — the GUI's and the CLI's — rather than one of them being fixed alone.
tcheck("retry-exhausted honours the gate",
       parse_retry_exhausted(f"{_RETRY}\n  ✽ Swirling… (9s · ↓ 1k tokens)")
       is False
       and parse_retry_exhausted(f"{_RETRY}\n> \n{_BAR}") is True)
tcheck("mid-stream truncation honours the gate",
       parse_server_error_stuck(f"{_TRUNC}\n  ✽ Swirling… (9s · ↓ 1k tokens)")
       is False
       and parse_server_error_stuck(f"{_TRUNC}\n> \n{_BAR}") is True)


# ---------- composer-line guard (defect 3) ----------
# Verified bug: text the user is TYPING is indistinguishable from a real
# banner by content alone —
# parse_econnreset_stuck("I'm asking for help: API Error: Unable to conn"
#                         "ect to API (ECONNRESET) -- any ideas?")
# returned True, so the watchdog would type 'continue' into a half-written
# message, or — for the picker/dialog detectors — press a bare Enter that
# SUBMITS it. The fix: a match whose line begins with '>' + NBSP (U+00A0),
# the composer's own prefix as sampled from a live terminal, is dropped.
print()
print("---- composer-line guard (defect 3) ----")

NBSP = " "  # non-breaking space -- see _COMPOSER_LINE_RE in auto_continue.py


def _composed(s):
    """Simulate `s` sitting on the composer/input-box line."""
    return ">" + NBSP + s


for label, fn, snippet in (
    ("retry-exhausted", parse_retry_exhausted, _RETRY),
    ("connection error", parse_econnreset_stuck, _ERR),
    ("truncated response", parse_server_error_stuck, _TRUNC),
):
    tcheck(f"{label}: real banner (no composer prefix) still detected",
           fn(snippet) is True)
    tcheck(f"{label}: same text on a composer line is NOT detected",
           fn(_composed(snippet)) is False)

# The user typing a message that happens to embed the picker/dialog wording
# must not earn a bare Enter that SUBMITS whatever else they had typed.
_picker_prose = "I want to " + _SW + " Opus 5, or " + _ED + " later"
tcheck("safeguard picker: composed user text is NOT detected",
       parse_fable_picker(_composed(_picker_prose)) is False)

_switch_prose = "should I say " + _YES + " Fable 5, or " + _NO + "?"
tcheck("switch-model dialog: composed user text is NOT detected",
       parse_switch_model_prompt(_composed(_switch_prose)) is False)

tcheck("limit picker: composed user text is NOT detected",
       parse_limit_prompt(_composed(PICKER)) is False)

# Defect-3 REGRESSION for the wrong first fix: treating a PLAIN space after
# '>' as the composer prefix would ALSO suppress the picker/dialog's own
# pre-selected "> 1. ..." option line — that's real Claude Code output, not
# the user's composer, and it must keep firing so the watchdog still presses
# Enter for the user.
tcheck("safeguard picker: real app-printed '> 1. ...' option line still fires",
       parse_fable_picker("> 1. " + _SW + " Opus 5\n  2. " + _ED + " with Fable 5")
       is True)
tcheck("switch-model dialog: real app-printed '> 1. ...' option line still fires",
       parse_switch_model_prompt(
           "Switch model?\n> 1. " + _YES + " Fable 5\n  2. " + _NO) is True)
tcheck("limit picker: real app-printed picker still fires",
       parse_limit_prompt(PICKER) is True)


# ---------- foreground-verification safety net (defect 1) ----------
# Verified bug: when window_ctrl.NativeWindowHandle is unreadable (raises, or
# is 0/None), `target_hwnd` was 0, and the old loop guard
#     if not target_hwnd or _get_foreground_hwnd() == target_hwnd: break
# broke on the FIRST iteration purely because `target_hwnd` was falsy — never
# actually checking whether the window reached the foreground — and the
# post-loop guard
#     if target_hwnd and _get_foreground_hwnd() != target_hwnd: return False
# was skipped for the same reason, so SendKeys fired with NO verification at
# all and the function still reported success. An unreadable handle must be
# treated as "cannot verify" == failure, exactly like a handle that IS
# readable but never reaches the foreground.
print()
print("---- foreground-verification safety net (defect 1) ----")


class _FakeTerm:
    """Stand-in for a WindowControl whose TermControl is itself: ClassName
    == 'TermControl' makes find_termcontrol(w) return w directly, so this one
    fake plays both `window_ctrl` and `term`."""
    ClassName = "TermControl"

    def __init__(self, hwnd, raise_on_handle=False):
        self._hwnd = hwnd
        self._raise = raise_on_handle
        self.set_active_calls = 0
        self.set_focus_calls = 0

    @property
    def NativeWindowHandle(self):
        if self._raise:
            raise OSError("handle unreadable")
        return self._hwnd

    def SetActive(self):
        self.set_active_calls += 1

    def SetFocus(self):
        self.set_focus_calls += 1

    def GetChildren(self):
        return []


def _fg_check(win, fg_hwnd, expect_ok, label, use_send_keys=False):
    """Run send_text_lines (or send_keys) against `win` with the foreground
    hwnd fixed at `fg_hwnd`, and check the result AND whether a keystroke was
    actually sent match `expect_ok`."""
    sent = []
    orig_fg = ac._get_foreground_hwnd
    orig_send = ac.auto.SendKeys
    ac._get_foreground_hwnd = lambda: fg_hwnd
    ac.auto.SendKeys = lambda *a, **k: sent.append(a)
    try:
        if use_send_keys:
            ok = send_keys(win, "{Esc}")
        else:
            ok = send_text_lines(win, ["continue"])
    finally:
        ac._get_foreground_hwnd = orig_fg
        ac.auto.SendKeys = orig_send
    got_ok = (ok is True) and bool(sent)
    got_blocked = (ok is False) and not sent
    cond = got_ok if expect_ok else got_blocked
    tcheck(label, cond)


# Handle raises on read (e.g. a COM error) — must be treated as unverified,
# never as an automatic pass, regardless of what the "foreground" window is.
_w = _FakeTerm(hwnd=0, raise_on_handle=True)
_fg_check(_w, fg_hwnd=999, expect_ok=False,
          label="send_text_lines: unreadable handle -> refuses to type")
_w = _FakeTerm(hwnd=0, raise_on_handle=True)
_fg_check(_w, fg_hwnd=999, expect_ok=False,
          label="send_keys: unreadable handle -> refuses to send", use_send_keys=True)

# Handle reads as a plain 0 (falsy but no exception) — same rule applies.
_w = _FakeTerm(hwnd=0, raise_on_handle=False)
_fg_check(_w, fg_hwnd=999, expect_ok=False,
          label="send_text_lines: zero handle -> refuses to type")
_w = _FakeTerm(hwnd=0, raise_on_handle=False)
_fg_check(_w, fg_hwnd=999, expect_ok=False,
          label="send_keys: zero handle -> refuses to send", use_send_keys=True)

# Sanity: a VALID handle that DOES reach the foreground must still work —
# the fix must not turn every send into a refusal.
_w = _FakeTerm(hwnd=555)
_fg_check(_w, fg_hwnd=555, expect_ok=True,
          label="send_text_lines: verified foreground -> types")
_w = _FakeTerm(hwnd=555)
_fg_check(_w, fg_hwnd=555, expect_ok=True,
          label="send_keys: verified foreground -> sends", use_send_keys=True)

# Sanity: a VALID handle that never reaches the foreground (SetForegroundWindow
# silently denied) must still refuse, as before this fix.
_w = _FakeTerm(hwnd=555)
_fg_check(_w, fg_hwnd=111, expect_ok=False,
          label="send_text_lines: valid handle never foregrounded -> refuses")
_w = _FakeTerm(hwnd=555)
_fg_check(_w, fg_hwnd=111, expect_ok=False,
          label="send_keys: valid handle never foregrounded -> refuses",
          use_send_keys=True)


# =============================================================================
print()
print("---- prompt suggestion is not a draft ----")
# Claude Code 2.1 can show a suggested next prompt IN the input box: dim
# text, caret in front of it. As plain text that is a draft, and a draft
# holds every send (2026-10-21: a companion was refused reason=draft for a
# box nobody had typed in). The colours and caret positions below are the
# ones read off a live 2.1.283 session in Windows Terminal.
_G = chr(0x276F) + "\xa0"           # the input box's glyph and its nbsp
_RULE = chr(0x2500) * 30
_NORMAL, _DIM = 13421772, 6710886   # 0xCCCCCC and the dimmed 0x666666


class _UiaDoc:
    def __init__(self, text, colors, caret, sel_end=None, colors_ok=True):
        self.text, self.colors = text, colors
        self.caret, self.sel_end = caret, sel_end
        self.colors_ok = colors_ok


class _UiaRange:
    """The few IUIAutomationTextRange calls the code makes, on a string."""

    def __init__(self, doc, s, e):
        self.doc, self.s, self.e = doc, s, e

    @property
    def textRange(self):            # the raw COM object, as uiautomation has
        return self

    def Clone(self):
        return _UiaRange(self.doc, self.s, self.e)

    def _at(self, ep):
        return self.s if ep == 0 else self.e

    def _put(self, ep, pos):
        pos = max(0, min(len(self.doc.text), pos))
        if ep == 0:
            self.s, self.e = pos, max(self.e, pos)
        else:
            self.e, self.s = pos, min(self.s, pos)

    def CompareEndpoints(self, ep, other, ep2):
        a, b = self._at(ep), other._at(ep2)
        return (a > b) - (a < b)

    def MoveEndpointByRange(self, ep, other, ep2, waitTime=0):
        self._put(ep, other._at(ep2))
        return True

    def MoveEndpointByUnit(self, ep, unit, count, waitTime=0):
        self._put(ep, self._at(ep) + count)
        return count

    def ExpandToEnclosingUnit(self, unit, waitTime=0):
        t = self.doc.text
        self.s = t.rfind("\n", 0, self.s) + 1
        nl = t.find("\n", self.s)
        self.e = len(t) if nl < 0 else nl + 1
        return True

    def FindText(self, needle, backward, ignore_case):
        t = self.doc.text
        i = (t.rfind(needle, self.s, self.e) if backward
             else t.find(needle, self.s, self.e))
        # Windows Terminal's match runs one character past the text found
        # (measured live: the glyph and its nbsp came back as three).
        return None if i < 0 else _UiaRange(
            self.doc, i, min(len(t), i + len(needle) + 1))

    def GetText(self, n):
        return self.doc.text[self.s:self.e]

    def GetAttributeValue(self, attr):
        seen = set(self.doc.colors[self.s:self.e])
        if not self.doc.colors_ok or len(seen) != 1:
            return object()         # the "mixed"/"not supported" markers
        return seen.pop()


class _UiaPattern:
    def __init__(self, doc):
        self.doc = doc

    @property
    def DocumentRange(self):
        return _UiaRange(self.doc, 0, len(self.doc.text))

    def GetSelection(self):
        end = self.doc.caret if self.doc.sel_end is None else self.doc.sel_end
        return [_UiaRange(self.doc, self.doc.caret, end)]


class _UiaTerm:
    ClassName = "TermControl"

    def __init__(self, doc):
        self.doc = doc

    def GetTextPattern(self):
        return _UiaPattern(self.doc)

    def GetChildren(self):
        return []


def _box_screen(shown, shown_color, caret_at, sel_len=0, colors_ok=True):
    """A screen whose input box shows `shown` in `shown_color` (one colour,
    or one per character); the caret at 'start', 'end' or an index."""
    head = "conversation\n" + _RULE + "\n"
    line = _G + shown + " " * 8 + "\n"
    tail = _RULE + "\n  status\n"
    text = head + line + tail
    per_char = (shown_color if isinstance(shown_color, list)
                else [shown_color] * len(shown))
    colors = ([_NORMAL] * (len(head) + 2) + per_char
              + [_NORMAL] * (len(line) - 2 - len(shown) + len(tail)))
    pos = {"start": 0, "end": len(shown)}.get(caret_at, caret_at)
    caret = len(head) + 2 + pos
    return _UiaTerm(_UiaDoc(text, colors, caret,
                            caret + sel_len if sel_len else None, colors_ok))


_SUGG = "delete the test files"
_t = ac.read_terminal_text(_box_screen(_SUGG, _DIM, "start"))
tcheck("suggestion: dim text, caret before it -> box reads empty",
       _t is not None and not ac.composer_has_draft(_t)
       and _SUGG not in _t and ac.composer_text(_t).strip() == "")
tcheck("suggestion: the rest of the screen is untouched",
       _t.startswith("conversation\n") and _t.endswith("  status\n"))
_t = ac.read_terminal_text(_box_screen("abc", _NORMAL, "end"))
tcheck("typed draft (normal colour, caret at end) stays a draft",
       ac.composer_has_draft(_t) and "abc" in _t)
_t = ac.read_terminal_text(_box_screen("abc", _NORMAL, 1))
tcheck("typed draft with the caret moved stays a draft",
       ac.composer_has_draft(_t))
_t = ac.read_terminal_text(_box_screen("abc", _NORMAL, "start"))
tcheck("typed draft with the caret at its start stays a draft (colour decides)",
       ac.composer_has_draft(_t))
_t = ac.read_terminal_text(_box_screen(_SUGG, _DIM, "end"))
tcheck("dim text with the caret after it stays a draft (caret decides)",
       ac.composer_has_draft(_t))
_t = ac.read_terminal_text(_box_screen(_SUGG, _DIM, "start", colors_ok=False))
tcheck("colours unreadable -> stays a draft", ac.composer_has_draft(_t))
_mixed = [_NORMAL] * 3 + [_DIM] * (len(_SUGG) - 3)
_t = ac.read_terminal_text(_box_screen(_SUGG, _mixed, 3))
tcheck("typed start + dim rest (mixed colours) stays a draft",
       ac.composer_has_draft(_t))
_t = ac.read_terminal_text(_box_screen(_SUGG, _DIM, "start", sel_len=4))
tcheck("text selected rather than a caret -> stays a draft",
       ac.composer_has_draft(_t))
_t = ac.read_terminal_text(_box_screen("", _DIM, "start"))
tcheck("empty box is still read as empty", _t is not None
       and not ac.composer_has_draft(_t))


# =============================================================================
print()
print("---- typing is confirmed before the foreground goes back ----")
# Keys sent with SendInput reach whichever window is in front when Windows
# gets to them. Handing the foreground back right after typing lost the end
# of a 216-character line and its Enter to the previous window, and the call
# still said ok (2026-10-21). The simulated box below shows typed text only
# after a few reads, and can drop the tail or ignore Enter.


class _Box:
    def __init__(self, lag=0, drop=0, take_enter=True, enter_lag=0,
                 steal_after=None):
        self.box, self.sent, self.events = "", [], []
        self.lag, self.drop = lag, drop
        self.take_enter, self.enter_lag = take_enter, enter_lag
        self.steal_after, self.typed_calls = steal_after, 0
        self.reads_since_type, self.reads_since_enter = 0, 0
        self.pending_enter = False
        self.fg = 555

    def keys(self, spec, interval=0.02, waitTime=0.5, **_):
        if spec == "{Enter}":
            self.events.append(("enter", self.shown()))
            self.pending_enter, self.reads_since_enter = True, 0
            return
        self.typed_calls += 1
        self.box += spec
        self.reads_since_type = 0
        self.events.append(("type", spec))
        if self.steal_after and self.typed_calls >= self.steal_after:
            self.fg = 111

    def shown(self):
        if self.reads_since_type < self.lag:
            return self.box[:len(self.box) // 2]
        return self.box[:len(self.box) - self.drop] if self.drop else self.box

    def read(self, w):
        self.reads_since_type += 1
        if self.pending_enter:
            self.reads_since_enter += 1
            if self.take_enter and self.reads_since_enter > self.enter_lag:
                self.sent.append(self.box)
                self.box, self.pending_enter = "", False
                self.events.append(("cleared", None))
        return ("conversation\n"
                + "".join(chr(0x276F) + " " + s + "\n" for s in self.sent)
                + _RULE + "\n" + _G + self.shown() + "\n" + _RULE
                + "\n  status\n")

    def restore(self, hwnd):
        self.events.append(("restore", hwnd))


def _run_send(box, lines):
    saved = (ac.read_terminal_text, ac.auto.SendKeys, ac._get_foreground_hwnd,
             ac._set_foreground_hwnd, ac.TYPED_CONFIRM_S, ac.SUBMIT_CONFIRM_S)
    calls = {"n": 0}

    def fg():
        calls["n"] += 1
        return 999 if calls["n"] == 1 else box.fg    # 1st read: saved fg

    ac.read_terminal_text = box.read
    ac.auto.SendKeys = box.keys
    ac._get_foreground_hwnd = fg
    ac._set_foreground_hwnd = box.restore
    ac.TYPED_CONFIRM_S, ac.SUBMIT_CONFIRM_S = 0.3, 0.3
    why = {}
    try:
        ok = ac.send_text_lines(_FakeTerm(hwnd=555), lines, outcome=why)
    finally:
        (ac.read_terminal_text, ac.auto.SendKeys, ac._get_foreground_hwnd,
         ac._set_foreground_hwnd, ac.TYPED_CONFIRM_S,
         ac.SUBMIT_CONFIRM_S) = saved
    return ok, why.get("reason"), box


_LONG = ("Explain TCP congestion control in depth, covering slow start, "
         "congestion avoidance, fast retransmit and fast recovery. Do not "
         "use tools.")
ok, why, b = _run_send(_Box(lag=3, enter_lag=2), [_LONG])
_kinds = [k for k, _ in b.events]
tcheck("long line typed in pieces, all of it sent once seen",
       ok is True and b.sent == [_LONG]
       and sum(1 for k in _kinds if k == "type") > 1)
tcheck("Enter pressed only after the whole line was on screen",
       [v for k, v in b.events if k == "enter"] == [_LONG])
tcheck("foreground handed back only after Enter took the line",
       "cleared" in _kinds and _kinds.index("restore") > _kinds.index("cleared"))

ok, why, b = _run_send(_Box(drop=7), [_LONG])
tcheck("tail never arrives -> no Enter, reason not_confirmed",
       ok is False and why == "not_confirmed"
       and "enter" not in [k for k, _ in b.events] and b.sent == [])

ok, why, b = _run_send(_Box(take_enter=False), ["continue"])
tcheck("Enter never takes the line -> reason not_confirmed",
       ok is False and why == "not_confirmed" and b.sent == [])

ok, why, b = _run_send(_Box(steal_after=2), [_LONG])
tcheck("another window takes the foreground mid-line -> typing stops",
       ok is False and why == "focus_lost"
       and sum(1 for k, _ in b.events if k == "type") == 2
       and "enter" not in [k for k, _ in b.events])

ok, why, b = _run_send(_Box(), ["/model sonnet", "", "continue"])
tcheck("multi-line sequence with a bare Enter still goes through",
       ok is True and b.sent == ["/model sonnet", "", "continue"])

_sent = []
_saved = (ac._get_foreground_hwnd, ac.auto.SendKeys)
ac._get_foreground_hwnd = lambda: 555
ac.auto.SendKeys = lambda *a, **k: _sent.append(a[0])
try:
    ok = send_text_lines(_FakeTerm(hwnd=555), ["continue"], outcome={})
finally:
    ac._get_foreground_hwnd, ac.auto.SendKeys = _saved
tcheck("unreadable screen: types as before (nothing to confirm against)",
       ok is True and "".join(_sent) == "continue{Enter}")

_why = {}
_saved = ac._get_foreground_hwnd
ac._get_foreground_hwnd = lambda: 111
try:
    ok = send_text_lines(_FakeTerm(hwnd=555), ["continue"], outcome=_why)
finally:
    ac._get_foreground_hwnd = _saved
tcheck("never reached the foreground -> reason foreground",
       ok is False and _why.get("reason") == "foreground")


# =============================================================================
print()
print("---- plan-mode approval ----")
# The prompt that ends plan mode is the case this feature most needs to
# clear: the plan is written, and the session waits on a chooser before any
# of it runs. Claude Code draws some dialogs plain and some inside a rounded
# box, so both renderings must be recognised — a boxed one going unmatched
# would fail silently, leaving the session exactly as stuck as before.
# Assembled from parts so this file cannot make a watched terminal press
# Enter when someone reads it.
_V = chr(0x2502)          # the box's vertical rule
_N = chr(0x276F)          # the selection marker
# Captured verbatim from a live session on 2026-08-10; the wording was
# guessed before that and guessed wrong, which is why the fixture now says
# where it came from.
_Q = ("Claude has written up a plan and is ready to execute. "
      "Would you like to proceed?")
_O1 = "1" ". Yes, and bypass permissions"
_O2 = "2" ". Yes, manually approve edits"
_O3 = "3" ". Tell Claude what to change"

_plan_plain = f"{_Q}\n{_N} {_O1}\n  {_O2}\n  {_O3}"
_plan_boxed = (f"Ready to code?\n{_V} {_Q}\n{_V} {_N} {_O1}\n"
               f"{_V}   {_O2}\n{_V}   {_O3}")

check_reset("plan approval (plain) is recognised as a chooser",
            ac.parse_chooser_prompt(_plan_plain) is True)
check_reset("plan approval (boxed) is recognised as a chooser",
            ac.parse_chooser_prompt(_plan_boxed) is True)
# It is NOT a permission request: it authorises nothing by itself, so it
# belongs to the ordinary switch. Classifying it as a permission prompt
# would leave it unanswered for anyone who turned that one off.
check_reset("plan approval is not treated as a permission request",
            ac.parse_permission_prompt(_plan_plain) is False
            and ac.parse_permission_prompt(_plan_boxed) is False)
check_reset("Enter would take the first option (bypass permissions)",
            ac._chooser_match(_plan_boxed).group(0).find(_O1) >= 0)

# The first option's wording tracks the session's current permission level.
# Both of these were captured verbatim from live sessions minutes apart, so
# neither may be relied on alone — what the detector must do is recognise
# the prompt in either dress. Note the marker here is U+276F, indented,
# which is how a real one renders.
_alt = ("   Claude has written up a plan and is ready to execute. "
        "Would you like to proceed?\n"
        "   " + chr(0x276F) + " 1" ". Yes, and use auto mode\n"
        "     2" ". Yes, manually approve edits\n"
        "     3" ". Tell Claude what to change")
check_reset("the auto-mode wording of the plan prompt is recognised too",
            ac.parse_chooser_prompt(_alt) is True)
check_reset("and it is still not a permission request",
            ac.parse_permission_prompt(_alt) is False)
# Prose that merely contains a marker and numbers must not qualify.
check_reset("ordinary prose is not mistaken for a chooser",
            ac.parse_chooser_prompt(
                "I weighed > 1. this and 2. that, both fine.") is False)

# Captured from a live session (2026-08-10). While one of Claude Code's
# choosers is open it REPLACES the input box: there is no composer line at
# all, just the navigation hint. An earlier version of the draft check read
# that as "cannot tell, so don't act" and therefore refused on every real
# chooser — the only case the feature exists for — while stand-ins that
# printed a composer line kept every test green. Absence of an input box is
# not a draft.
_LIVE_CHOOSER = (
    "\u80cc\u666f/\u4e3b\u9898\u505a\u6210\u4ec0\u4e48\u5f62\u5f0f\uff1f\n\n"
    "> 1" ". \u51e0\u5957\u9884\u8bbe\u4e3b\u9898\uff08\u63a8\u8350\uff09\n"
    "     \u6df1\u8272 / \u6d45\u8272 / \u62a4\u773c\u7eff\n"
    "  2" ". \u9884\u8bbe\u4e3b\u9898 + \u80cc\u666f\u8272\u81ea\u5b9a\u4e49\n"
    "     \u5728\u9884\u8bbe\u57fa\u7840\u4e0a\u518d\u7ed9\u4e00\u4e2a\u53d6\u8272\u5668\n"
    "  3" ". \u5b8c\u5168\u81ea\u7531\u53d6\u8272\n"
    "  4" ". Type something.\n"
    "  5" ". Chat about this\n\n"
    "Enter to select \u00b7 Tab/Arrow keys to navigate \u00b7 Esc to cancel")

check_reset("a live chooser with descriptions is recognised",
            ac.parse_chooser_prompt(_LIVE_CHOOSER) is True)
check_reset("a chooser that replaced the input box counts as no draft",
            ac.composer_has_draft(_LIVE_CHOOSER) is False)
check_reset("an idle session with an empty box is no draft either",
            ac.composer_has_draft("output\n>" + chr(0xa0) + "   ") is False)
check_reset("but text in the box IS a draft",
            ac.composer_has_draft(
                "output\n>" + chr(0xa0) + "half a thought") is True)

# The prompt glyph varies by build: ASCII '>' in one session, U+276F in
# another, both on this machine the same afternoon. Matching only the first
# read a real draft ("delete the folder", sitting in a session that had just
# offered to delete one) as an empty box — and an empty box is what licenses
# pressing Enter, which would have submitted it.
_NBSP = chr(0xa0)
for _mark, _name in ((">", "ascii"), (chr(0x276F), "U+276F")):
    check_reset(f"a draft behind the {_name} prompt marker is seen",
                ac.composer_has_draft(
                    "output\n" + _mark + _NBSP + "delete the folder") is True)
    check_reset(f"an empty box with the {_name} marker is not a draft",
                ac.composer_has_draft(
                    "output\n" + _mark + _NBSP + "    ") is False)
    # The same glyph plus an ORDINARY space is a chooser's selection marker,
    # not a composer; reading it as one would hide an open chooser.
    check_reset(f"the {_name} marker plus a plain space is not a composer",
                ac.composer_has_draft(
                    "output\n" + _mark + " 1. an option") is False)

# Long option descriptions, padded to the terminal width the way UIA reports
# them. Reproduces the live measurement that exposed the too-tight gap.
_pad = " " * 60
_long_desc = "\n".join(f"     description line {i}{_pad}" for i in range(6))
_LONG_CHOOSER = (
    "How much should the manual cover?\n\n"
    "> 1" ". Fix the errors and fill the common gaps (recommended)" + _pad
    + "\n" + _long_desc + "\n"
    "  2" ". Fix the errors only" + _pad + "\n"
    "     a shorter description\n"
    "  3" ". Write everything up\n\n"
    "Enter to select - arrow keys to navigate - Esc to cancel")
_gap = (_LONG_CHOOSER.index("  2" ". Fix")
        - _LONG_CHOOSER.index("> 1" ". Fix"))
check_reset(f"the fixture spans {_gap} chars, past the old 400 allowance",
            _gap > 400)
check_reset("a chooser with long option descriptions is still recognised",
            ac.parse_chooser_prompt(_LONG_CHOOSER) is True)
check_reset("and it is not mistaken for a permission request",
            ac.parse_permission_prompt(_LONG_CHOOSER) is False)

# What sits BELOW a chooser is not fixed. Measured on a live session that
# waited until someone noticed: the chooser ended 1556 characters from the
# end of the buffer — input box, status lines, and a seven-item todo list —
# against the 1500 the limit picker uses. Fifty-six characters short.
_pad2 = " " * 110
_below = "\n".join([
    "  7 tasks (5 done, 2 open)" + _pad2,
    "  [ ] Part 3: fix the mislabelled tags" + _pad2,
    "  [ ] Part 5: publish the release" + _pad2,
    "  [x] Part 0: reject installs without a digest" + _pad2,
    "  [x] Part 1: correct nine factual errors" + _pad2,
    "  [x] Part 2: document the security warnings" + _pad2,
    "   ... +2 completed" + _pad2,
    "  [x] Part 4: rebuild and verify the flat package" + _pad2,
    "  [x] Part 6: check the archive runs from a clean folder" + _pad2,
    "  " + chr(0x276F) + chr(0xa0) + "   ",
    "  [Opus 5] usage 17%" + _pad2,
    "  auto mode on (shift+tab to cycle)" + _pad2,
])
_WITH_TODOS = (
    "How should we handle the release?\n\n"
    "> 1" ". Commit and fix the tags first (recommended)" + _pad2 + "\n"
    "     push to the private remote; the upload can wait\n"
    "  2" ". Do all of it now" + _pad2 + "\n"
    "  3" ". Commit locally only\n\n" + _below)
_m = ac._chooser_match(_WITH_TODOS)
_dist = len(_WITH_TODOS) - _m.end() if _m else None
check_reset(f"the fixture really does trail the old 1500 allowance "
            f"(tail {_dist})",
            _dist is not None and _dist > 1500)
check_reset("a chooser with a todo list under it is still recognised",
            ac.parse_chooser_prompt(_WITH_TODOS) is True)
check_reset("its own allowance is looser than the limit picker's",
            ac.CHOOSER_POST_MATCH_TAIL > ac.PROMPT_POST_MATCH_TAIL)

# A REAL permission prompt, captured 2026-08-10 17:39 from a session whose
# folder carried an "ask" rule. The invented fixture had guessed its second
# option as "and don't ask again"; the real one reads "allow all edits during
# this session", which is now a tell of its own.
_perm_live = (
    "Do you want to create perm_check.txt?\n"
    "  " + chr(0x276F) + " 1" ". Yes\n"
    "    2" ". Yes, allow all edits during this session (shift+tab)\n"
    "    3" ". No\n"
    "  Esc to cancel - Tab to amend")
check_reset("the live permission prompt is recognised as one",
            ac.parse_permission_prompt(_perm_live) is True)
check_reset("and is kept OUT of the ordinary chooser switch",
            ac.parse_chooser_prompt(_perm_live) is False)

# The verb list used to be fixed, so this one would have been missed and
# then answered by the switch someone leaves on believing tool approvals are
# still theirs to give. Enter on it means yes, delete.
_perm_delete = (
    "Do you want to delete build.log?\n"
    "  " + chr(0x276F) + " 1" ". Yes\n"
    "    2" ". Yes, allow all edits during this session\n"
    "    3" ". No")
check_reset("a permission prompt with an unlisted verb is still caught",
            ac.parse_permission_prompt(_perm_delete) is True
            and ac.parse_chooser_prompt(_perm_delete) is False)

# An OPEN chooser has replaced the input box: measured on a live permission
# prompt caught in the instant it waited, there was nothing below the
# options but the "Esc to cancel" hint and no composer at all. So a composer
# line AFTER the match means the thing is not open.
#
# That is not a nicety. A reply in one window quoted a real permission
# prompt verbatim, and the watchdog answered the quotation — twice, five
# minutes apart, because the "still up after a while" backstop kept finding
# it. Anyone who asks Claude to explain a chooser reproduces this.
_open = ("Do you want to create x.txt?\n"
         "  " + chr(0x276F) + " 1" ". Yes\n"
         "    2" ". Yes, allow all edits during this session\n"
         "    3" ". No\n"
         "  Esc to cancel - Tab to amend")
_quoted = (_open + "\n\nthat is the prompt I saw earlier\n"
           + chr(0x276F) + chr(0xa0) + "   ")
check_reset("an open prompt (no input box beneath) still counts",
            ac.parse_permission_prompt(_open) is True)
check_reset("the same text quoted above an input box does NOT",
            ac.parse_permission_prompt(_quoted) is False
            and ac.parse_chooser_prompt(_quoted) is False)
# Same for an ordinary chooser someone pasted into a message.
_quoted_chooser = ("Ready to continue?\n"
                   "> " "1" ". Keep going\n"
                   "  2" ". Stop here\n\nwhat does that mean?\n"
                   ">" + chr(0xa0) + "   ")
check_reset("a quoted ordinary chooser is not answered either",
            ac.parse_chooser_prompt(_quoted_chooser) is False)


# =============================================================================
print()
print("---- DST: the hour that happens twice ----")
# When clocks fall back the stated reset hour occurs twice. pytz's default
# picks the SECOND, which parks the window for an extra hour on a reset that
# already happened. The first is chosen instead: the fire path re-checks the
# banner is still current, so waking early is skipped, while waking late is
# an hour of idle session.
import pytz as _pytz                                        # noqa: E402
from datetime import datetime as _dt, timedelta as _td      # noqa: E402

_tz = _pytz.timezone("America/New_York")
_ambiguous = _dt(2026, 11, 1, 1, 0)
_early = _tz.localize(_ambiguous, is_dst=True).astimezone(_pytz.UTC)
_late = _tz.localize(_ambiguous, is_dst=False).astimezone(_pytz.UTC)
check_reset("the fixture really is an ambiguous hour",
            _late - _early == _td(hours=1))

_saved_dt = ac.datetime


class _FakeDT(_dt):
    @classmethod
    def now(cls, tz=None):
        base = _tz.localize(_dt(2026, 11, 1, 0, 30), is_dst=True)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


ac.datetime = _FakeDT
try:
    _got = ac.next_reset_datetime(1, 0, "am", "America/New_York")
finally:
    ac.datetime = _saved_dt
check_reset(f"the ambiguous reset resolves to the earlier occurrence "
            f"(got {_got:%H:%M} UTC, wanted {_early:%H:%M})",
            _got == _early)


class _FakeDT2(_dt):
    @classmethod
    def now(cls, tz=None):
        base = _tz.localize(_dt(2026, 3, 8, 1, 0))
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


ac.datetime = _FakeDT2
try:
    _got2 = ac.next_reset_datetime(2, 30, "am", "America/New_York")
    _ok2 = _got2 is not None
except Exception as _e:
    _ok2 = False
    print("      raised:", _e)
finally:
    ac.datetime = _saved_dt
check_reset("a non-existent local time (spring forward) still yields a time",
            _ok2)



print("---- key specs are summarised, never quoted ----")
# The log records what this tool did, not what a session holds. /keys can
# carry a whole sentence — seen live on 2026-09-21, a companion program typed
# a prompt through it and every word landed in activity.log. Key NAMES are
# what a reader needs; the prose never was.
check_reset("KS1 a pure key token survives intact — the useful part",
      ac.summarize_keyspec("{Enter}") == "{Enter}")
check_reset("KS2 several tokens stay readable",
      ac.summarize_keyspec("{Esc}{Esc}") == "{Esc}{Esc}")
check_reset("KS2b a long run of one key becomes a count",
      ac.summarize_keyspec("{Back}" * 80) == "{Back}×80")
check_reset("KS2c runs are counted where they are, around other tokens",
      ac.summarize_keyspec("{Back}" * 3 + "hi{Enter}")
      == "{Back}×3<2 chars>{Enter}")
check_reset("KS3 typed words become a count, not the words",
      ac.summarize_keyspec("hello") == "<5 chars>")
check_reset("KS4 a sentence with a key keeps the key and drops the sentence",
      ac.summarize_keyspec("hi there{Enter}") == "<8 chars>{Enter}")
check_reset("KS5 one character reads as one char",
      ac.summarize_keyspec("a{Ctrl}") == "<1 char>{Ctrl}")
check_reset("KS6 empty stays empty", ac.summarize_keyspec("") == "")
check_reset("KS7 None does not explode", ac.summarize_keyspec(None) == "")
_secret = " 这个音频里的人说了什么？只回复转写出来的原文。"
check_reset("KS8 the real leaked prompt leaves no trace of itself",
      "音频" not in ac.summarize_keyspec(_secret)
      and ac.summarize_keyspec(_secret) == "<24 chars>")
check_reset("KS9 a brace-looking run that is not a token is still counted, "
      "not echoed",
      ac.summarize_keyspec("{this is not a key name at all}")
      == "<31 chars>")   # 29 inside + 2 braces; a name that long is not a key

print("---- an error QUOTED in prose is not an error ----")
# 2026-09-24 00:12:30: a reply in a watched session quoted the no-response
# line mid-sentence while explaining it; the tool read its own screen, saw the
# words near the bottom, and typed 'continue' into that session. Real banners
# sit on a line of their own (every live sample: prefix "", "  " or "● ").
_PROMPT = "\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
_INCIDENT = ("  09-13 收录的真实样本是 API Err" "or: No resp" "onse from API，"
             "而你贴的新提示已经写成 from the" + _PROMPT)
check_reset("PQ1 the exact line that fired on 2026-09-24 is no longer a stuck "
            "session", not ac.parse_econnreset_stuck(_INCIDENT))
check_reset("PQ2 the same error on a line of its own still is",
            ac.parse_econnreset_stuck("  API Err" "or: No resp" "onse from API"
                                      + _PROMPT))
check_reset("PQ3 ...including under the ⎿ glyph Claude Code prints",
            ac.parse_econnreset_stuck("  ⎿  API Err" "or: Unable to conn" "ect "
                                      "to API (ECONNRESET)" + _PROMPT))
check_reset("PQ4 a truncation marker quoted in prose is not a truncation",
            not ac.parse_server_error_stuck(
                "  the log said API Err" "or: Server error mid-resp" "onse. "
                "yesterday" + _PROMPT))
check_reset("PQ5 a retry banner quoted in prose is not exhaustion",
            not ac.parse_retry_exhausted(
                "  it showed Retry" "ing in 0s · attempt 10/10 then gave up"
                + _PROMPT))
check_reset("PQ6 but the retry banner riding on its own error line is real",
            ac.parse_retry_exhausted(
                "  ⎿  API Err" "or (Connection error.) · Retry" "ing in 0s · "
                "attempt 10/10" + _PROMPT))

print("---- a whole turn since the error means it is history ----")
# 2026-09-25: a session waiting on background shells answered the 'continue'
# in 29 seconds, between two passes; running was never caught, so it was poked
# again ten minutes later for nothing.
_B = "● API Err" "or: Connection dropped (ECONN" "RESET)"
_F = "\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n"
check_reset("TC1 error + only its own done-stamp is still stuck",
            ac.parse_econnreset_stuck(_B + "\n\n✻ Worked for 3m · done 10:43" + _F))
check_reset("TC2 error, then a prompt and a finished turn, is recovered",
            not ac.parse_econnreset_stuck(
                _B + "\n\n✻ Worked for 3m · done 10:43\n\n> continue\n\n"
                "● still at gate 1, waiting on the chain\n\n"
                "✻ Cogitated for 29s · done 10:44 · 2 shells still running" + _F))
check_reset("TC3 a continue still QUEUED (never run) is not recovery",
            ac.parse_econnreset_stuck(
                _B + "\n\n✻ Crunched for 18m · done 9:50\n\n> continue\n"
                "  ctrl+x ctrl+s to send now" + _F))
check_reset("TC4 a draft in the input box is not a prompt that ran",
            ac.parse_econnreset_stuck(
                _B + "\n\n✻ Worked for 3m · done 10:43\n" + "─" * 40
                + "\n❯ contin\n" + "─" * 40))

print("---- a turn waiting on the API is still a running turn ----")
# Claude Code replaces the spinner line, timer and all, while it waits out a
# first-byte timeout itself. Everything that waits for "idle" (after-finish,
# a due limit 'continue', the <idle> recovery step, the API's running field)
# must read that as running -- it was read as finished (2026-09-23).
_FOOT = "\n" + "─" * 40 + "\n❯ \n" + "─" * 40 + "\n  ⏵⏵ accept edits on"
check_reset("RW1 the normal spinner is running",
            ac.session_running("✻ Crunching… (6m 3s · ↓ 1.2k tokens)" + _FOOT))
check_reset("RW2 the first-byte retry wait is running too",
            ac.session_running("✻ No resp" "onse from the API after 6m · "
                               "retrying once, waiting up to 10m" + _FOOT))
check_reset("RW3 a finished turn is not",
            not ac.session_running("✻ Worked for 9s · done 21:32" + _FOOT))
check_reset("RW5 a first-byte wait whose timer has no '·' is running "
            "(captured live 2026-09-25)",
            ac.session_running("✽ Effecting… (1m 51s)" + _FOOT))
check_reset("RW6 a turn's first seconds, no timer at all, is running",
            ac.session_running("✶ Effecting…" + _FOOT))
check_reset("RW7 a turn that has FINISHED is not, even with a long timer",
            not ac.session_running("✻ Crunched for 18m 51s · done 9:50" + _FOOT))
check_reset("RW8 prose with an ellipsis is not a spinner",
            not ac.session_running("  I tried again… it failed" + _FOOT))
# The exact shape that got 'continue' queued into a live request, three times
# in an hour: the previous turn's error banner above, the new turn spinning
# below it with no '·' in its timer.
_LIVE = ("● API Err" "or: No resp" "onse from API (waited 9m, then 10m on the "
         "retry).\n\n✻ Crunched for 18m 51s · done 9:50\n\n> continue\n"
         "  ctrl+x ctrl+s to send now\n\n✽ Effecting… (1m 51s)\n" + _FOOT)
check_reset("RW9 an error ABOVE a turn that is spinning again is not stuck",
            not ac.parse_econnreset_stuck(_LIVE))
check_reset("RW4 nor is prose that merely mentions retrying",
            not ac.session_running("we are retrying the build later today"
                                   + _FOOT))


print("---- KP: a key spec checked before it is typed ----")
# uiautomation finds a bad key name only while typing, and the API reported
# that as 'foreground' -- "try again" -- for {Backspace}, which can never work
# (its name is {Back}). 2026-09-27, live against the ccdebug probe.
for _spec in ("{Enter}", "{Back}{Back}", "{Ctrl}{End}", "{Esc}", "hi{Enter}",
              "{{}x{}}", "{a 3}", "{Shift}{Tab}"):
    tcheck(f"KP1 {_spec!r} is pressable", ac.keyspec_problem(_spec) is None)
tcheck("KP2 {Backspace} is named as the problem",
       ac.keyspec_problem("x{Backspace}") == "{Backspace}")
tcheck("KP3 a count that is not a number is refused",
       ac.keyspec_problem("{a b}") == "{a b}")
tcheck("KP4 an unclosed brace is refused, not typed half-way",
       ac.keyspec_problem("ok{") == "{")
tcheck("KP5 an empty {} is refused, as uiautomation would",
       ac.keyspec_problem("{}") == "{}")

print("---- HP: when this tool must not type a line ----")
# Every line this tool types ends in Enter: on a draft it submits both as one
# message, into an open prompt the Enter picks the highlighted option. The
# cursor may be on any row -- the arrow keys move it -- so "open" cannot
# depend on it sitting on 1 (2026-09-27). Fixtures keep the marker and the
# digit apart so this file cannot make a watched terminal press Enter.
_C = "❯ "
_Q = "Which approach?\n"
_HINT = "\n\nEnter to select · Esc to cancel"
_ON1 = _Q + _C + "1" ". Small\n  2" ". Large\n  3" ". Other" + _HINT
_ON3 = _Q + "  1" ". Small\n  2" ". Large\n" + _C + "3" ". Other" + _HINT
_BOX = "\n❯ "
tcheck("HP1 cursor on 1 is an open prompt", ac.prompt_open(_ON1))
tcheck("HP2 cursor on 3 is an open prompt too", ac.prompt_open(_ON3))
tcheck("HP3 answered: the input box is back below it, so not open",
       not ac.prompt_open(_ON3 + "\n\nreply text" + _BOX))
tcheck("HP4 one quoted '> 3.' line is not a list",
       not ac.prompt_open("see step\n" + _C + "3" ". Other\nand more text"))
tcheck("HP5 far up the scrollback is not open",
       not ac.prompt_open(_ON3 + "\n" + ("x" * 120 + "\n") * 40))
tcheck("HP6 a draft is a reason to hold",
       ac.typing_hold_reason("output\n❯ half a thought")
       == "the input box holds unsent text")
tcheck("HP7 an open prompt is a reason to hold",
       "prompt is open" in (ac.typing_hold_reason(_ON3) or ""))
tcheck("HP8 an empty box is not", ac.typing_hold_reason("output" + _BOX) is None)
tcheck("HP9 an unreadable screen is not: unknown types as before",
       ac.typing_hold_reason("") is None)

print("---- V8: an open prompt as data (cc-remote's S5 screens) ----")
# Real Claude Code 2.1.283 screens read over UIA in cc-remote's cc_debug
# window (their docs/spikes/S5-cc-choosers.md, copied here, not linked).
# Rows are ASSEMBLED so this file never holds a cursor marker next to a row
# number: viewed in a watched terminal it must not look like a chooser.
_CUR = "\u276f "


def _R(n, label, cur=False, lead="  "):
    return (lead[:-2] + _CUR if cur else lead) + str(n) + ". " + label


_SEP = "\u2500" * 78
_DASH = "\u254c" * 76
_HNT = ("Enter to select \u00b7 Tab/Arrow keys to navigate \u00b7 "
        "Esc to cancel")
_BAR0 = "\u2190  \u2610 Color  \u2610 Extras  \u2714 Submit  \u2192"
_FORM1 = "\n".join([
    "earlier conversation", _SEP, _BAR0, "Which color for the demo?",
    _R(1, "Red", cur=True), "     warm", _R(2, "Green"), "     calm",
    _R(3, "Blue"), "     cool", _R(4, "Type something."), _SEP,
    _R(5, "Chat about this"), _HNT])
_p = ac.classify_prompt(_FORM1)
tcheck("V8a a two-question form reads as a chooser", _p and _p["kind"] == "chooser")
tcheck("V8b its question", _p["question"] == "Which color for the demo?"
       and _p["text"] == "Which color for the demo?")
tcheck("V8c its tabs, with Submit marked",
       _p["tabs"] == [{"label": "Color", "done": False},
                      {"label": "Extras", "done": False},
                      {"label": "Submit", "submit": True}] and _p["tab"] == 0)
tcheck("V8d options in screen order, with their descriptions",
       [(o["n"], o["label"], o["desc"]) for o in _p["options"]]
       == [(1, "Red", "warm"), (2, "Green", "calm"), (3, "Blue", "cool"),
           (4, "Type something.", None)])
tcheck("V8e cursor on 1, no check boxes, nothing picked",
       [o["cursor"] for o in _p["options"]] == [True, False, False, False]
       and all(o["check"] is None and o["picked"] is False
               for o in _p["options"]))
tcheck("V8f only the typed-answer row is an input",
       [o["input"] for o in _p["options"]] == [False, False, False, True])
tcheck("V8g the row under the rule is 'below', with its number",
       _p["below"] == [{"n": 5, "label": "Chat about this"}])
tcheck("V8h hint verbatim, layout list, no title",
       _p["hint"] == _HNT and _p["layout"] == "list" and _p["title"] == [])

# Down: the cursor leaves row 1. Still reported, same sig -- and NOT
# something auto-answer will act on.
_FORM1D = _FORM1.replace(_CUR + "1", "  1").replace("  2. Green",
                                                    _CUR + "2. Green")
_pd = ac.classify_prompt(_FORM1D)
tcheck("V8i after Down the prompt is still there (it vanished before)",
       _pd is not None and _pd["options"][1]["cursor"]
       and not _pd["options"][0]["cursor"])
tcheck("V8j same prompt, same sig", _pd["sig"] == _p["sig"])
tcheck("V8k auto-answer still needs the cursor on 1",
       not ac.parse_chooser_prompt(_FORM1D))

_BAR1 = "\u2190  \u2612 Color  \u2610 Extras  \u2714 Submit  \u2192"
_MULTI = "\n".join([
    _SEP, _BAR1, "Which extras should be added?",
    _R(1, "[ ] Sound", cur=True), "         beeps", _R(2, "[ ] Light"),
    "         flash", _R(3, "[ ] Vibrate"), "         buzz",
    _R(4, "[ ] Type something"), "     Submit", _SEP,
    _R(5, "Chat about this"), _HNT])
_pm = ac.classify_prompt(_MULTI)
tcheck("V8l multi-choice: check boxes read, labels without them",
       [(o["label"], o["check"]) for o in _pm["options"]]
       == [("Sound", False), ("Light", False), ("Vibrate", False),
           ("Type something", False)])
tcheck("V8m descriptions line up under the label, not the box",
       [o["desc"] for o in _pm["options"]] == ["beeps", "flash", "buzz", None])
tcheck("V8n the unnumbered Submit row goes to below with n null",
       _pm["below"] == [{"n": None, "label": "Submit"},
                        {"n": 5, "label": "Chat about this"}])
tcheck("V8o second question is the current tab", _pm["tab"] == 1)
_MULTI2 = (_MULTI.replace("[ ] Sound", "[\u2714] Sound")
           .replace("[ ] Vibrate", "[\u2714] Vibrate")
           .replace("\u2610 Extras", "\u2612 Extras"))
_pm2 = ac.classify_prompt(_MULTI2)
tcheck("V8p toggles read as checked",
       [o["check"] for o in _pm2["options"]] == [True, False, True, False])
tcheck("V8q and do not change the sig", _pm2["sig"] == _pm["sig"])

_REVIEW = "\n".join([
    _SEP, "\u2190  \u2612 Color  \u2612 Extras  \u2714 Submit  \u2192",
    "Review your answers", " \u25cf Which color for the demo?",
    "   \u2192 Blue", " \u25cf Which extras should be added?",
    "   \u2192 Sound, Vibrate", "Ready to submit your answers?",
    _R(1, "Submit answers", cur=True), _R(2, "Cancel")])
_pr = ac.classify_prompt(_REVIEW)
tcheck("V8r review page: the submit question and its two options",
       _pr["question"] == "Ready to submit your answers?"
       and [o["label"] for o in _pr["options"]] == ["Submit answers", "Cancel"])
tcheck("V8s its title is the answers under 'Review your answers'",
       _pr["title"] == ["\u25cf Which color for the demo?", "\u2192 Blue",
                        "\u25cf Which extras should be added?",
                        "\u2192 Sound, Vibrate"])
tcheck("V8t and the current tab is Submit", _pr["tab"] == 2)

_BACK = "\n".join([
    _SEP, "\u2190  \u2612 Color  \u2612 Extras  \u2714 Submit  \u2192",
    "Which color for the demo?", _R(1, "Red"), "     warm", _R(2, "Green"),
    "     calm", _R(3, "Blue \u2714"), "     cool",
    _R(4, "Purple", cur=True), _SEP, _R(5, "Chat about this"),
    _HNT + " \u00b7 ctrl+g to edit in Notepad"])
_pb = ac.classify_prompt(_BACK)
tcheck("V8u a revisited answer is picked, its mark off the label",
       _pb["options"][2]["label"] == "Blue" and _pb["options"][2]["picked"])
tcheck("V8v typed text in the answer row is its label, and it is the input",
       _pb["options"][3]["label"] == "Purple" and _pb["options"][3]["input"]
       and _pb["options"][3]["cursor"])
tcheck("V8w typing does not change the sig of the question it answers",
       _pb["sig"] == ac.classify_prompt(_BACK.replace("Purple", "Pu"))["sig"])

_ONE = "\n".join([
    _SEP, " \u2610 Name", "Pick a project name", _R(1, "Alpha", cur=True),
    "     first", _R(2, "Beta"), "     second", _R(3, "Type something."),
    _SEP, _R(4, "Chat about this"),
    "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel"])
_po = ac.classify_prompt(_ONE)
tcheck("V8x one question: a one-tab bar, first tab current",
       _po["tabs"] == [{"label": "Name", "done": False}] and _po["tab"] == 0
       and [o["input"] for o in _po["options"]] == [False, False, True])

_PANEL = "\u2502 [ A ] [ B ] [ C ]                        \u2502"
_PREVIEW = "\n".join([
    " \u2610 Layout", "Which layout?",
    _R(1, "Grid", cur=True) + " " * 25 + "\u250c" + "\u2500" * 42 + "\u2510",
    _R(2, "List") + " " * 25 + _PANEL,
    " " * 34 + _PANEL, " " * 34 + "\u2514" + "\u2500" * 42 + "\u2518",
    " " * 34 + "Notes: press n to add notes", _SEP, "  Chat about this",
    "Enter to select \u00b7 \u2191/\u2193 to navigate \u00b7 n to add notes"
    " \u00b7 Esc to cancel"])
_pv = ac.classify_prompt(_PREVIEW)
tcheck("V8y preview layout: labels without the panel, no descriptions",
       _pv["layout"] == "preview"
       and [(o["label"], o["desc"]) for o in _pv["options"]]
       == [("Grid", None), ("List", None)])
tcheck("V8z its 'Chat about this' has no number, so n is null",
       _pv["below"] == [{"n": None, "label": "Chat about this"}]
       and not any(o["input"] for o in _pv["options"]))

_PERM = "\n".join([
    "\u25cf Write(opts.txt)", _SEP, " Create file", " opts.txt", _DASH,
    "  1 opts", _DASH, " Do you want to create opts.txt?",
    _R(1, "Yes", cur=True, lead="   "),
    _R(2, "Yes, and switch to accept edits (auto-approve file edits and "
          "common file commands) for this session (shift+tab)", lead="   "),
    _R(3, "No", lead="   "), " Esc to cancel \u00b7 Tab to amend"])
_pp = ac.classify_prompt(_PERM)
tcheck("V8A permission prompt: kind, title, question",
       _pp["kind"] == "permission" and _pp["title"] == ["Create file", "opts.txt"]
       and _pp["question"] == "Do you want to create opts.txt?")
tcheck("V8B its three options and its hint",
       [o["label"][:14] for o in _pp["options"]]
       == ["Yes", "Yes, and switc", "No"]
       and _pp["hint"] == "Esc to cancel \u00b7 Tab to amend"
       and _pp["below"] == [])
_PERM3 = (_PERM.replace(_CUR + "1", "  1")
          .replace("   3. No", " " + _CUR + "3. No"))
_pp3 = ac.classify_prompt(_PERM3)
tcheck("V8C cursor on No: still a permission prompt, cursor on 3",
       _pp3 and _pp3["kind"] == "permission" and _pp3["options"][2]["cursor"])
tcheck("V8D and auto-permission would NOT press Enter on it",
       not ac.parse_permission_prompt(_PERM3))

_PLAN = "\n".join([
    " " + "\u2500" * 76, "  Ready to code?", "  Here is Claude's plan:",
    " " + _DASH, "  Create plan.txt containing the word plan.", " " + _DASH,
    " " + "\u2500" * 76,
    "  Claude has written up a plan and is ready to execute. "
    "Would you like to proceed?",
    _R(1, "Yes, and use auto mode", cur=True, lead="    "),
    _R(2, "Yes, manually approve edits", lead="    "),
    _R(3, "Tell Claude what to change", lead="    "),
    "       shift+tab to approve with this feedback",
    "  ctrl+g to edit in Notepad \u00b7 C:\\plans\\plan-x.md"])
_pl = ac.classify_prompt(_PLAN)
tcheck("V8E plan confirmation: title from above the plan text",
       _pl["title"] == ["Ready to code?", "Here is Claude's plan:"])
tcheck("V8F its question and the feedback row as the input",
       _pl["question"].endswith("Would you like to proceed?")
       and [o["input"] for o in _pl["options"]] == [False, False, True]
       and _pl["options"][2]["desc"] == "shift+tab to approve with this feedback")
tcheck("V8G no Esc in its hint", "Esc to cancel" not in _pl["hint"])
_PLAN3 = (_PLAN.replace(_CUR + "1", "  1")
          .replace("    3. Tell Claude what to change",
                   "  " + _CUR + "3. no file, just rep"))
_pl3 = ac.classify_prompt(_PLAN3)
tcheck("V8H typed feedback keeps the row an input (by its description)",
       _pl3["options"][2]["input"] and _pl3["options"][2]["label"]
       == "no file, just rep" and not _pl3["options"][0]["input"])

# The live 2.1.283 shape (ccdebug, 2026-09-28) differs from the sample: a
# blank line between the two title lines, and no closing dashed rule -- the
# plan text, numbered steps and all, runs straight into the question's rule.
_PLAN_LIVE = "\n".join([
    "  " + "\u2500" * 116, "   Ready to code?", "", "   Here is Claude's plan:",
    "  " + "\u254c" * 116, "   Plan: create plan_probe.txt", "",
    "   Steps", "", "   " + "1" ". Use the Write tool to create the file.",
    "  " + "\u2500" * 116,
    "   Claude has written up a plan and is ready to execute. "
    "Would you like to proceed?",
    _R(1, "Yes, and switch to bypass permissions", cur=True, lead="     "),
    _R(2, "Yes, manually approve edits", lead="     "),
    _R(3, "Tell Claude what to change", lead="     "),
    "        shift+tab to approve with this feedback",
    "   ctrl+g to edit in Notepad \u00b7 C:\\plans\\plan-y.md"])
_pll = ac.classify_prompt(_PLAN_LIVE)
tcheck("V8H2 live plan shape: both title lines, the plan's own numbered "
       "step not taken for an option",
       _pll["title"] == ["Ready to code?", "Here is Claude's plan:"]
       and [o["n"] for o in _pll["options"]] == [1, 2, 3])

tcheck("V8I nothing open, nothing reported", ac.classify_prompt(
    "all done\n\u276f\u00a0") is None)
tcheck("V8J an answered form with the input box back is not open",
       ac.classify_prompt(_FORM1 + "\n\nok\n\u276f\u00a0") is None)

print("---- RX: no default trigger is slow on a padded screen ----")
# Claude Code pads every line to the window width, so a screen is mostly
# blanks. The safeguard pattern once let three whitespace runs share them:
# 5.6 s for one 3.6 KB tail, holding the GIL, every pass (2026-09-27). Every
# default gets the worst shape: words at line ends, then long blank runs.
import re as _rx
import time as _tm
_pad = "".join((("Fable 5.1 word." if _i % 7 == 0 else "").ljust(119) + "\n")
               for _i in range(84))[-ac.SCAN_TAIL_CHARS:]
for _k, _v in ac.TRIGGER_DEFAULTS.items():
    _c = _rx.compile(_v, _rx.IGNORECASE)
    _t = _tm.time()
    list(_c.finditer(_pad))
    _d = _tm.time() - _t
    tcheck(f"RX1 {_k} scans a padded 10 KB screen fast ({_d * 1000:.1f} ms)",
           _d < 0.05)
_t = _tm.time()
ac.fable_refusal_distance(_pad)
tcheck("RX2 the safeguard check itself, as the pass calls it",
       _tm.time() - _t < 0.05)

print("---- EN: the window list comes from the OS ----")
# The desktop-wide UIA listing took 0.75-9 s a pass (2026-09-27) and
# sometimes came back empty with every terminal open. The OS says which
# windows exist; UIA is asked about those alone, and the old listing is only
# the fallback.
_real_hw, _real_cfh, _real_uia = (ac._cascadia_hwnds, ac.auto.ControlFromHandle,
                                  ac._find_terminal_windows_uia)
_uia_calls = []


class _Ctl:
    def __init__(self, h): self.NativeWindowHandle = h


try:
    ac._find_terminal_windows_uia = lambda: _uia_calls.append(1) or ["uia"]
    ac._cascadia_hwnds = lambda: [11, 12, 13]
    ac.auto.ControlFromHandle = lambda h: _Ctl(h)
    _got = ac.find_terminal_windows()
    tcheck("EN1 every terminal the OS reports is wrapped, in its order",
           [c.NativeWindowHandle for c in _got] == [11, 12, 13])
    tcheck("EN2 without the slow desktop listing", _uia_calls == [])

    def _flaky(h):
        if h == 12:
            raise RuntimeError("COMError")
        return _Ctl(h)
    ac.auto.ControlFromHandle = _flaky
    tcheck("EN3 one handle UIA cannot wrap is left out, not the whole pass",
           [c.NativeWindowHandle for c in ac.find_terminal_windows()] == [11, 13])

    def _boom():
        raise OSError("EnumWindows failed")
    ac._cascadia_hwnds = _boom
    tcheck("EN4 if the OS listing fails, the old UIA listing answers",
           ac.find_terminal_windows() == ["uia"])
    ac._cascadia_hwnds = lambda: []
    tcheck("EN5 and when the OS finds none, UIA is asked before saying none",
           ac.find_terminal_windows() == ["uia"])
finally:
    ac._cascadia_hwnds, ac.auto.ControlFromHandle,         ac._find_terminal_windows_uia = _real_hw, _real_cfh, _real_uia
tcheck("EN6 the real OS listing runs here and returns plain handles",
       all(isinstance(h, int) for h in ac._cascadia_hwnds()))

print("---- WH: a window asked for by handle ----")
# The local API asks its one window directly instead of listing the desktop
# (the listing took up to 9 s on 2026-09-27). It must still only ever hand
# back a visible terminal window, or a caller could type into anything with
# a handle.
import ctypes as _ct
tcheck("WH1 no handle, no window", ac.terminal_window_from_handle(0) is None)
tcheck("WH2 a handle that is not a window is None",
      ac.terminal_window_from_handle(1) is None)
tcheck("WH3 a real window that is not a terminal is None",
      ac.terminal_window_from_handle(
          int(_ct.windll.user32.GetDesktopWindow())) is None)
tcheck("WH4 garbage is None, not an exception",
      ac.terminal_window_from_handle("not a handle") is None)


sys.exit(1 if failures else 0)
