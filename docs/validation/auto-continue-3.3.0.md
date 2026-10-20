# Auto-Continue 3.3.0 verification

Date: 2026-10-20. Scope: the new Claude EXE monitor (Claude desktop app, version 2.42304.2.0 on this machine). Control keeps its built-in engine and is not modified or published here.

Claude EXE reads the window through UI Automation (session title from the page, running state from the Stop button, the prompt box) and the session's transcript under `%USERPROFILE%\.claude\projects` (usage limits, API errors and turn completion, which Claude Code flags itself). It acts only on a session whose title matches exactly one desktop transcript, in a single-pane window, with an empty prompt box and no running turn. Sending writes the prompt box through ValuePattern and invokes the app's Send button; a send counts once the transcript records the new prompt.

Completed checks:

- Seven legacy suites (682 checks), 174 unittest cases (including `test_claude_app.py`, mutation-checked so each test turns red when the guard it covers is removed), 96 pytest cases and 19 subtests passed. Packaged `--selftest` passed.
- Real desktop session, source build: the watcher, limited to this verification session by title and transcript id, held while the turn ran, sent its marked After finish prompt once after the turn ended, the transcript confirmed the submission, and nothing was repeated during 180 s of observation.
- Installed EXE (`dist/Auto-Continue.exe`, 3.2.0 backed up first), isolated profile with only Claude EXE on: title v3.3.0, four tabs, this session's row shown as Running while busy, then the packaged EXE sent its own marked prompt once with "submission confirmed", and the spent run was saved. After the next turn finished and the session stayed idle for 32 s, the EXE log still showed one send and the transcript one marked prompt.

Not covered: permission prompts and questions (left to the user by design), model and effort changes (not done), and a live usage-limit or network error in the desktop app, which cannot be raised on demand; their classification is tested on real transcript records from this machine.

User Codex windows were not operated. No Claude login or underlying Claude change was made. EXE SHA-256: `58cfbbfe68638d1887ab6cf2bf07abcdad6d6e6837c618e269fc7c367744d080`.

Public acceptance: GitHub release v3.3.0 is Latest with the local EXE as its asset; the download site serves both 3.3.0 packages (size, SHA-256, ZIP CRC and HTTP 206 checked over the public link), only the Auto-Continue card changed, and the server keeps 3.3.0, 3.2.0, 3.1.2, 3.1.1 and 3.1.0. The unmodified published 3.2.0 client's first Update now failed ("1 (of 2) futures unfinished": the update source probe gave up while one source was silent); the second attempt chose the mirror, verified, replaced and reopened as 3.3.0 with settings kept and monitors off. The chooser is fixed in 3.3.1.
