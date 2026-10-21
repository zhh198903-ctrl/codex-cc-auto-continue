# Codex/CC Auto-Continue

## Claude CLI typing fixes (3.3.2)

- **A suggested prompt is not a draft.** After a turn Claude Code may show a suggested next message in the input box, in dim text. As text it looks like something typed, so it held every limit resume, After finish prompt and API send as a draft. The input box is now read with its colours and cursor: text dimmer than the box's prompt sign, with the cursor in front of it, counts as an empty box. Typed text (normal colour, cursor after it or wherever it was moved) still holds everything, and an unreadable box still counts as a draft. Measured on Claude Code 2.1.283 in Windows Terminal; the suggestion setting itself is left alone.
- **Typed lines are confirmed on the screen.** Windows delivers typed keys to whichever window is in front when it processes them, and the previous window was put back in front right after typing. On a busy machine the end of a long line and its Enter went to that window while the send counted as done. Each line is now typed in short pieces behind a foreground check. Enter is pressed only once the whole line shows in the input box, and the previous window comes back only after Enter has taken the line out. A send that cannot be confirmed fails with a reason (`focus_lost`, `not_confirmed`) instead of reporting success.

## Claude EXE: the Claude desktop app (3.3.0)

A fourth monitor type, **off by default**, for Claude Code sessions in the Claude desktop app. It handles the session showing in each Claude window, the way Codex EXE does:

- **Which session**: the page inside the window is titled `<session title> - Claude Code`, and Claude Code writes the same title into that session's history file under `%USERPROFILE%\.claude\projects`. Nothing is done until the title matches exactly one desktop session; same-titled sessions, split view or a missing history leave the window alone, with one log line saying why.
- **Pop-out windows** (3.3.3): a session opened with *Open in → new window* titles its page with just the session title. That bare title is accepted only with a single prompt box carrying the Code session's own buttons and exactly one name on the page matching a desktop session's history; same-titled sessions are still left alone.
- **What it reads**: running/idle and the prompt box from the window (UI Automation); usage limits, connection errors and turn completion from the history, where Claude Code flags its own API errors.
- **What it does**: after a usage limit, `continue` at the reset time plus Buffer; after a connection error, `continue` every Retry interval up to Maximum retries; After finish once per completed turn within its Loops. Safeguard blocks, a model's own limit and login problems get nothing typed and show *Needs attention*.
- **How it types**: writes into the prompt box and presses the app's Send button (no simulated keys), only with an empty box, no running turn and the same session still showing; a send counts once the history records the new prompt. Since 3.3.4 the status note the desktop app puts in front of a prompt once a session has linked sessions ("Linked sessions now …") is left out of that comparison; before, every such send was reported unconfirmed although it arrived.
- **Not yet**: permission prompts and questions are left to you; model and effort are not changed.

## Claude CLI simplified (3.2.0)

Version 3.2.0 removes Claude CLI's **model recovery** — the opt-in script that switched a session blocked by safeguards to a fallback model, waited for it to finish, ran `/compact` and switched back, plus its quota switching and its steering of windows back to a target model. It typed far more than `continue` and depended on Claude Code's dialogs and status bar staying exactly as they were. A session blocked by safeguards, or out of its own model's allowance, now simply waits for you; **After finish** does not count it as finished, and the safeguard picker and the *Switch model?* dialog are never auto-answered. Anyone who had model recovery switched on is told once in the log; its stored settings are left unused.

The same release drops other Claude CLI machinery that no longer earned its keep: the two-second second look a pass took whenever a known window was missing (a leftover from the desktop-wide UI Automation listing, and a per-pass stall for any terminal where Claude Code had exited), the v2.0.6 After finish migration, the unused General page of the Claude settings dialog and some dead limit-picker state. Limit, network, chooser, After finish, model/effort overrides, triggers and the local API behave as in 3.1.2.

## One-click updates (independent EXE)

In the independent executable, **Update now** downloads, verifies and restarts in one action. Settings and the three monitor switches survive the restart. The previous executable is saved as `Auto-Continue.exe.bak`; a failed download or verification never installs its bytes, and a failed replacement relaunches the previous copy and reports the failure.

Control continues using its built-in Auto-Continue, managed by the Control package. Its bundled copy is not a standalone installation and must not be manually overwritten or have its version marker removed.

Version 3.1.2 also isolates startup between release versions so existing independent 3.0.0/3.1.0 updaters can reopen the replaced EXE without inheriting a deleted temporary runtime. A legacy updater may still display its existing restart confirmation once; 3.1.1 and later complete Update now without a second confirmation.

If an old independent EXE cannot complete an update from a Unicode installation path, close it and replace it once with the current standalone download. Subsequent updates use the repaired helper. This instruction does not apply to Control's built-in copy.

## Local Control API revision 1

Version 3.1.0 includes the Codex companion API for Codex Claude Control: sessions, typed actions, operation results, context and SSE. The CLI backend and native driver are maintained here and mirrored into Control with a SHA-256 manifest. A 60-second per-provider/thread lease suspends automatic approvals and permission-mode changes while retaining configured recovery and follow-ups. Confirmed follow-ups consume the budget once; uncertain writes pause the target. Bundled copies update with the full Control package. This release includes the Control integration. Validation covers the source regressions and the packaged local API; complete Control-client acceptance is tracked separately.

A Windows GUI watchdog for [Claude Code](https://github.com/anthropics/claude-code), Codex CLI in Windows Terminal, and the Codex desktop App. Select any combination of Codex EXE, Codex CLI and Claude CLI; switch their settings with three equal tabs. Each driver has independent detection, state, retries and configuration.

It resumes recoverable failures and can send a configured follow-up after a completed turn. Claude retains its existing behavior. Codex model and reasoning settings use the actual session's native controls.

> **Windows-only.** Tested on Windows 11 with native Windows Terminal and the installed Codex desktop App. The release contains one executable: `Auto-Continue.exe`.

## Download

**[⬇️ Auto-Continue.exe (latest release)](https://github.com/zhh198903-ctrl/codex-cc-auto-continue/releases/latest)** — single-file Windows executable, no Python or dependencies required. Double-click to run.

Prefer source? See [Install](#install) below.

## Codex CLI and desktop App (v3.0.0)

> Version 3.0.0 supports Codex desktop, Codex CLI and Claude CLI. Download the matching binary from the release page.

- Three equal tabs, **Codex EXE**, **Codex CLI** and **Claude CLI**, switch the settings and shared table. Enable any combination independently; disabled types show no settings. The activity log stays at the bottom. Global startup/keep-awake options live in **App settings**. Dry-run remains a saved diagnostic setting and is hidden from the GUI.
- CLI and EXE share form construction for **Poll**, **Buffer**, **Retry**, exclusions, model/effort settings and **After finish**; their detection, state, retry limits and configuration remain independent. Claude trigger expressions and slash commands are not applied to Codex.
- Codex EXE lists the **five most recent Codex activities** by default; **Recent** accepts 1–100. It excludes CLI, subagent, archived, ChatGPT and ChatGPT Work conversations. Every activity has independent model, effort, exclusion and after-finish settings, keyed by thread identity rather than title, so duplicate titles stay independent.
- Hidden activities can be listed and configured. Automatic input requires a verified match with a visible native Codex conversation; background conversations are never switched or typed into. CLI watches the active tab/pane of each Windows Terminal window.
- Per-session **Model**, **Effort**, **Now**, **Skip**, **Exclude** and **After finish** use the corresponding driver. Model/effort are applied through the native session controls; an unavailable value holds the send. Open model menus and unfinished edits survive polling.
- Each Codex **Advanced** dialog offers **Full access** (default), **Approve for me** or **Ask for approval**, plus **Automatically approve tool permissions** (default on). Turning automatic approval off holds a pending permission card. Approval does not answer ordinary choosers, model dialogs or login prompts. CLI and EXE settings are separate.
- Native quota messages after commentary/tool output are recognised. During native internal retries, automatic input is held. Historical errors and quoted error text do not schedule retries. CLI waits for its parsed reset deadline plus Buffer; App quota messages without a deadline use bounded Retry attempts instead.
- The App retains its own staged follow-up while native Send is unavailable and confirms a submitted user message before consuming retry or after-finish budgets. Editing the draft, changing conversations, Skip or Exclude cancels the pending send.
- After-finish budgets are saved after a confirmed submission; zero prevents further follow-ups. Drafts, active turns, unreadable UI and unknown terminal identity hold input. Authentication, billing and unrecognised native errors require attention. The companion API now exposes separate Codex session routes; reading conversation contents still requires the separate opt-in switch.
- Native verification used Codex CLI **0.160.0** and Codex App **26.930.3930.0** with its **0.160.0** runtime, isolated test homes and loopback fault injection: 13 CLI scenarios, seven App scenarios and three permission modes per driver. The source regression suite contains **122 checks**. See [verification details](docs/validation/codex-3.0.0.md) for methods and limits.

The following sections describe the existing Claude watcher.

## How it works

1. **Detect** — Polls every Windows Terminal window's scrollback via UI Automation. When the literal Anthropic limit message appears (both lines: `You've hit your limit · resets <H>[:<MM>]<am|pm> (<TZ>)` followed by the `/upgrade …` line), the row turns yellow and a countdown starts. Both Anthropic wordings of that follow-up line are recognized — `/upgrade or /extra-usage to finish what you're working on.` and `/upgrade to increase your usage limit.` — and the reset time may carry minutes (`2:50pm`) or not (`11pm`).
   Newer Claude Code builds first show an interactive picker instead of the banner (`What do you want to do?` / `1. Stop and wait for limit to reset` / `2. Upgrade your plan`). The watchdog detects that picker, presses **Enter** to confirm option 1, then picks up the banner that appears.
2. **Schedule** — Parses the reset time in the named timezone (IANA names and common abbreviations like `EDT`; DST-correct), computes the next occurrence in UTC, adds a small buffer (default 20 s past the reset).
3. **Fire** — At fire time, re-checks the limit message is still current (if you already continued manually, it skips instead of injecting a redundant `continue`), brings the target Windows Terminal window forward, **verifies the window actually reached the foreground** (if Windows refuses — e.g. you're actively typing elsewhere — it retries next tick rather than typing into the wrong app), sends `/model <name>` / `/effort <level>` + Enter (if configured for that row), then `continue` + Enter. Restores the previously-focused window.

The watcher runs on a Qt worker thread so UIA reads (which take 100 ms+) never block the UI.

## Install

### Option A — Prebuilt exe (recommended)

Grab **`Auto-Continue.exe`** from the [latest release](https://github.com/zhh198903-ctrl/codex-cc-auto-continue/releases/latest) and double-click it. That's it.

### Option B — From source

```powershell
git clone https://github.com/zhh198903-ctrl/codex-cc-auto-continue.git
cd codex-cc-auto-continue
pip install -r requirements.txt
```

To build your own exe from source: `pip install -r requirements-build.txt`, then `python build_release.py` → output at `dist/Auto-Continue.exe`. Builds use pinned PyInstaller 6.19.0 and a project-local release bootloader identity so old updaters can launch a new release safely.

The stock bootloader comes from [PyInstaller 6.19.0](https://github.com/pyinstaller/pyinstaller/tree/v6.19.0/bootloader). The complete release-identity modification is reproducible in `versioned_bootloader.py`; it changes a local copy of the archive-identity environment key before normal EXE assembly. The bootloader license and distribution exception are included in `licenses/PyInstaller-COPYING.txt`.

`python _make_dist_zip.py` packages the executable, README, LICENSE and matching skill into `packages/Auto-Continue_dist_v<version>.zip`. `--out-dir` may select another directory inside this project. Build through `build_release.py` so unrelated DLL directories in the launching shell do not enter the bundle. Keep test executables outside `dist`; the release directory must contain only `Auto-Continue.exe`.

## Run

```powershell
python gui.py
```

Or double-click **`Auto-Continue.pyw`** — the `.pyw` extension launches under `pythonw.exe`, so no extra console window appears alongside the GUI.

(There used to be a separate CLI runner in `auto_continue.py`. It was removed in v2.0.5: it was a second copy of the watcher loop that had to be fixed in lockstep with the GUI's — and more than once wasn't — while offering strictly fewer features. `auto_continue.py` is now the detection/keystroke library the GUI builds on. Dry-run remains a saved diagnostic setting and is hidden from the GUI.)

## GUI features

- **Live table** of every Windows Terminal window — title, status, reset time, countdown, last sent, per-row model dropdown, per-row effort dropdown, action buttons.
- **Status colors** — yellow = ⏳ Waiting, orange = ▶ Sending, green = ✓ Sent (5 s flash), gray = Cooldown, light gray = Excluded.
- **Action buttons per row:**
  - **Now** — fire `continue` immediately (bypasses the timer; respects the per-row effort setting).
  - **Skip** — cancel a pending continue for this row.
  - **Exclude** — never watch this window again (persisted across restarts).
  - **Clear cooldown** — reset the 15-min post-send suppression so the row can re-detect immediately (useful for testing).
- **Reset Claude CLI settings** — in its More settings dialog, restores Claude settings to its shipped default: timings, per-window model/effort overrides, exclusions and trigger patterns. Asks first and names what it clears; dry-run, keep-awake and the start-up options are left alone.
- **Model dropdown per row** — `(none)` / `default` / `opus` / `sonnet` / `haiku` / `fable`, matching Claude Code's `/model` aliases. The box is **editable**: type a family the list doesn't have yet and it is passed to `/model` verbatim, so a newly shipped model works without waiting for a new build. `(none)` leaves the session on whatever model it's already using; any other value prefixes the fire sequence with `/model <name>` + Enter so the session switches before continuing. Persisted per stable window title.
- **Effort dropdown per row** — `(none)` / `low` / `medium` / `high` / `xhigh` / `max` / `ultracode`, matching Claude Code's own `/effort` slider. When set, the fire sequence becomes `/effort <level>` + Enter → wait 0.6 s → blank Enter (confirms the "Change effort level?" dialog) → wait 0.6 s → `continue` + Enter. (Model, if set, goes first: `/model <name>` → `/effort <level>` → `continue`.) Persisted per stable window title. (`ultracode` = xhigh + workflows; it's newer, so sessions on Claude Code older than 4.7 won't recognize it.)
- **Diagnostic dry-run** — saved setting, hidden from the GUI; detects and logs without pressing keys.
- **Keep awake** — calls `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)` so Windows Modern Standby doesn't kill the watcher process during a long unattended wait.
- **Minimize to system tray** — minimize button hides the window to the right-side tray; X button fully exits. Right-click the tray icon for **Show window** / **Quit**.
- **DPI awareness** set to per-monitor v2 before any Qt or UIA DLL loads, so high-DPI displays render correctly without a startup warning.
- **Per-window cooldown** — 15 minutes after a successful fire, the row's detection is suppressed so the lingering limit message in scrollback doesn't trigger a re-fire.
- **Built-in self-update** (v1.0.8+) — checks GitHub for a newer release once on every launch (and via the **Check updates** button / tray **Check for updates…** entry). When a newer version exists, a banner offers **Update now**: it downloads the new exe on a background thread (with a progress %) — **resuming** after a dropped connection, and keeping what arrived across launches, so a slow link that drops more often than the transfer takes can still finish; a partial left from an older release is discarded rather than continued — verifies its SHA-256, asks to restart, then swaps the exe in place and relaunches — no manual download needed. If the swap can't complete (staged file quarantined by antivirus, install directory not writable), the helper gives up after ~2.5 minutes, **relaunches the previous version anyway** so you are never left with nothing running, and drops an `Auto-Continue.exe.update-failed.txt` marker next to the exe. Only the packaged `.exe` self-updates; running from source just reports availability. (Update checks use a single unauthenticated GitHub API call; nothing else is sent.)
- **Network-stuck recovery** — detects the retry-exhausted banner (`Retrying in 0s · attempt N/N`, long-form wording too) *and* bare API network errors (`API Error: Unable to connect to API (ECONN·RESET)` — any errno: `ETIMEDOUT`, `ECONNREFUSED`, `ENOTFOUND`, `EAI_AGAIN`, undici `UND_ERR_*`, plus `fetch failed`), then re-sends `continue` every retry interval until the connection recovers. HTTP-status errors (500/529) are left to Claude Code's own retries.
- **Limit picker left to you** — the `What do you want to do?` modal also offers paid extra usage, so nothing is pressed on it (since v2.0.13); the window shows **⏎ Limit prompt**, and the reset time is still read from the banner above it, so the wait-then-continue flow is unaffected.
- **Auto-answer choosers** (v2.0.7+, two separate switches under **Advanced → Answering** — **Answer choosers** ships **off**, **Answer tool-permission requests** ships **on**) — Claude Code sometimes stops on a chooser and waits; until someone picks, the window is idle for no better reason than an unanswered question. Both switches press a single **Enter**, accepting the option the chooser already pre-selected — never a typed digit, so a mis-detection costs a harmless empty Enter rather than sending a stray `1` to Claude. Three gates make that safe: the turn must not be running, your input box must be **empty** (Enter with a draft in it would submit the draft), and the chooser's signature must differ from the last one answered, so one chooser gets exactly one Enter (the answered text lingers on screen, so "it still matches" proves nothing). **Answer choosers** (off by default, because it is the switch that decides things) covers ordinary questions — including the prompt that ends **plan mode**, which is what lets an unattended session go straight from an approved plan into the work. Note what Enter takes there: that prompt's first option also sets the session's permission level, back to whatever it was started with — captured live, a plainly-started session offered *Yes, and use auto mode* and was in auto mode afterwards, while one started with permissions bypassed offered *Yes, and bypass permissions* and returned to bypass. Start your sessions bypassed and approving a plan this way puts it back to bypassed and begins the work. **Answer tool-permission requests** covers the run-a-command / edit-a-file confirmations you see when bypass-permissions is off — that one *authorises work* rather than merely unblocking, which is why it is a separate switch you can turn off on its own. Both patterns are editable under **Triggers**.
- **OAuth-expired warning** — an expired-OAuth-token notice can't be fixed by `continue`; the watchdog logs a warning once instead of poking at a dead session.
- **Start on launch** (v1.0.9+, default on) — the watcher starts automatically when the app opens, including the relaunch after a self-update, so protection never silently lapses.
- **Single instance** — a second copy of the exe warns and exits instead of double-typing `continue` into the same windows.
- **Persistent activity log** — everything in the log pane is also appended to `%LOCALAPPDATA%\auto_continue\activity.log` (rotated at ~1 MB) for overnight postmortems.
- **Editable trigger patterns** (v1.0.16+) — **Advanced… → Triggers** exposes every detection regex (limit banner, limit picker, retry-exhausted, connection error, truncated response, dead session, the safeguard notice and the two model-switching dialogs) as an editable, case-insensitive pattern. Anthropic re-words these banners without notice — the `/extra-usage` → `/usage-credits` rename silently stopped detection until a new build shipped — so you can fix a wording change yourself the same day. Each row shows its purpose, a **Reset this / Reset all** button, and a live **test box**: paste the terminal text and it tells you what matched, which capture groups it produced, and whether the match sits too far up the scrollback to count at runtime. Overrides are validated on OK (must compile; must keep the capture groups the parser indexes; must not match empty text, which would fire on every window) and only genuinely-changed patterns are stored, so future builds' improved defaults still win for anything you didn't touch.
- **After finish** (per window) — the row's *After finish…* button stores a prompt; when that session finishes a run and sits idle past a settle delay, the prompt is typed automatically so the window keeps producing instead of waiting for a human. The dialog's **Loops (runs left)** field is a countdown, default 1: each firing spends one and the new value is saved immediately, so a restart (or a self-update) can never re-arm a prompt that already did its run — the window's next task never inherits the previous task's follow-up. At 0 the prompt stays visible but nothing is typed; set Loops again, or edit the prompt, to re-arm. **unlimited** never counts down. A session never seen running, a session blocked by safeguards, or one inside the post-fire cooldown never qualifies as "finished".
- Source files containing the literal string (e.g. test fixtures, this README).
- Past limit messages still in scrollback that the user already handled.
- Random text that happens to contain "You've hit your limit" but not the `/upgrade` follow-up.

If an Anthropic wording change breaks detection, fix it in **Advanced → Triggers** (no rebuild needed); the patterns in `auto_continue.py` are only the defaults.

## Foreground rights / SendKeys notes

When the GUI fires keystrokes, it:

1. Saves the currently foreground window handle.
2. `SetActive` on the target Windows Terminal window, then `SetFocus` on its TermControl.
3. Waits 250 ms for the focus change to land.
4. `auto.SendKeys("continue{Enter}", interval=0.02)` → Win32 `SendInput`.
5. Restores the original foreground window.

This works because the GUI is an interactive Qt app the user launched — it has Windows foreground rights. Subprocesses spawned without a UI cannot reliably take foreground; tests of the same code path from a headless launcher fail silently. If you need fully headless operation, run the GUI as a normal interactive app and let it be the foreground-rights holder.

## Known limitations

- **Windows-only.** UIA + SendInput + Modern Standby calls are all Win32-specific.
- **Windows Terminal required.** ConHost classic console isn't enumerated (the regex would still match its scrollback if it were, but the Watcher's `find_terminal_windows()` filters by the WT class `CASCADIA_HOSTING_WINDOW_CLASS`).
- **Only the active tab/pane** of each WT window is watched — Windows Terminal does not expose background tabs' content in the UIA tree, so this is a hard platform limit. The app detects multi-tab windows, marks the row with **⚠ N tabs**, and logs which tab titles are invisible. Run parallel sessions in separate windows (drag each tab out of the tab bar) for full coverage.
- **Two windows with identical titles** share one exclusion / model / effort / After finish setting (settings are keyed by title).
- **Custom trigger patterns are yours to get right.** Overrides are validated for compilability, capture groups and empty matches, but not for runtime cost — a pathological regex can slow the poll loop. Use **Reset all** to get back to the built-ins.
- **The 15-min cooldown** is hard-coded. After a fire, the row won't re-detect for 15 minutes even if a new limit message appears (rare in practice — Anthropic limits don't fire that fast back-to-back).
- **`/effort max`** triggers a confirmation dialog that we auto-confirm with an extra Enter. Other levels also trigger this dialog (verified May 2026), so the extra Enter is always sent when an effort level is configured.
- **Modern Standby** can throttle background Python processes even with keep-awake enabled in some power profiles. If the watcher dies overnight while keep-awake was on, file an issue with the Windows event log around the death time.

## License

MIT — see `LICENSE`.
