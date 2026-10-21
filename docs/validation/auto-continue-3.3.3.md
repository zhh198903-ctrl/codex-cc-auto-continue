# Auto-Continue 3.3.3 verification

Date: 2026-10-21. Scope: one Claude EXE fix reported by the Codex Claude Control session. Control keeps its built-in engine and is not modified or published here.

A Code session opened with *Open in → new window* in the Claude desktop app (2.42304.2.0) titles its page with the bare session title, without " - Claude Code". The driver took only suffixed pages as Code sessions, so `read_text` returned None for such a window: no row, no limit resume, no After finish. 3.3.3 accepts a bare page name when the window has exactly one ProseMirror prompt box with the Code session's toolbar around it, and exactly one page name matches a desktop transcript's title. Same-titled sessions are still held (`same_title`).

Completed checks:

- Source: legacy suites green; 181 unittest cases (7 new, reading a fake UIA tree: main-window page, pop-out page, a bare name without a session, without the toolbar, with two prompt boxes, two session names on one page, the same title twice). Each guard was removed in turn, and a case failed each time.
- Real pop-out window (HWND 22022238, 156 UIA nodes; the page is named after the session only): 3.3.3's `read_text` returned the title, session 64ab933c, one prompt box, the toolbar, ready. The main window's session read as before.
- Packaged `--selftest` passed. The installed EXE (`dist/Auto-Continue.exe`, 3.3.2 backed up first) ran with an isolated profile: Claude EXE monitoring only, with Control's own work session excluded by title. Control and its bundled engine were stopped for the check at Control's request. Dry run: title v3.3.3, and the pop-out session had its own row. Poll changed from 2 to 3 in the window, was saved, and was kept after a restart. Live: after one kickoff prompt sent by the source driver, the EXE's own After finish went into the pop-out window (`Claude EXE #150085e: after-finish prompt; submission confirmed`). The transcript shows both prompts answered (POPOUT1, POPOUTOK), and Control's work session received nothing.

User Codex windows were not operated. No Claude login or underlying Claude change was made. EXE SHA-256: `01fed42c391f8301014e282c8cd351598f1acf4026d5f32dbcac276cbed50ee1`.

Public acceptance: GitHub release v3.3.3 is Latest, with the local EXE as its asset (same SHA-256); source checks passed on main and on the tag. The download site serves both 3.3.3 packages (size, SHA-256, ZIP CRC and HTTP 206 checked over the public link). Only the Auto-Continue card changed, and the server keeps 3.3.3, 3.3.2, 3.3.1, 3.3.0 and 3.2.0. The unmodified published 3.3.2 client updated on its first Update now: it chose the mirror, then verified, replaced and reopened as 3.3.3, with settings kept, monitors off and the previous EXE saved as `.bak`.
