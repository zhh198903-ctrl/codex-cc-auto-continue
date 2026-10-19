# Auto-Continue 3.2.0 verification

Date: 2026-10-19. Scope: the independent Auto-Continue, Claude CLI watcher. Control keeps its built-in engine and is not modified or published here.

3.2.0 removes Claude CLI model recovery (the opt-in step script that switched a session blocked by safeguards to a fallback model, waited, ran `/compact` and switched back, plus quota switching and target-model steering). A blocked session now waits for the user; After finish does not treat it as finished, and the safeguard picker and *Switch model?* dialog are never auto-answered. The release also drops the two-second in-pass re-list, dead limit-picker state, the unused General page of the Claude settings dialog and the v2.0.6 After finish migration.

Completed checks:

- Seven legacy suites (682 checks, including the new `test_safeguard.py`), 155 unittest cases, 96 pytest cases and 19 subtests passed. The packaged `--selftest` passed.
- Real Claude CLI, in a ccdebug probe session, with the 3.2.0 watcher limited to the probe's recorded window handle: a Bash permission prompt was answered once and the command ran; an AskUserQuestion chooser was answered once with option 1; the *Switch model?* dialog stayed open with both answer switches on; After finish typed its prompt once after the session finished and then reported no runs left. After `/exit` left a shell in the window, each 3.2.0 pass took 0.03 s, where the 3.1.2 source took 2.06 s per pass on the same window.
- The installed standalone EXE (`dist/Auto-Continue.exe`, 3.1.2 backed up first) ran with an isolated Claude-only profile in dry-run: title v3.2.0, Claude settings tabs Answering / Triggers / Local API, the one-time notice about the removed model recovery (from a copy of this machine's enabled setting), no repeat after restart, stored setting left unchanged, Poll changed 15 to 16 in the GUI and kept after a full restart, rows refreshed each pass, the open chooser detected without a key, local API version 3.2.0 with authentication enforced. With monitoring off, one Enter sent through the local API to the probe answered its chooser, exercising the packaged keystroke path.

User Codex windows were not operated. No Claude login or underlying Claude change was made. EXE SHA-256: `ce39b4a0212c3bd09401400c6a1f79573c4715ca93a14a59f420d05dc96456c4`.
