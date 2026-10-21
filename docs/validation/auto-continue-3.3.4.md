# Auto-Continue 3.3.4 verification

Date: 2026-10-22. Scope: one Claude EXE fix reported by the Codex Claude Control session. Control keeps its built-in engine and is not modified or published here.

Once a desktop session has linked sessions, the Claude desktop app stores each typed prompt as a user record with two text parts. The first is a status note, `<system-reminder>` "Linked sessions now (status, not an instruction): …"; the second is what was typed. The driver joined all text parts, so no record ever equalled the prompt it sent, and every send came back unconfirmed although it had arrived. 3.3.4 leaves leading system-reminder blocks out of a record's text, and a user record that holds only such a note no longer counts as a prompt (counted, it would hide the turn's completion, which After finish follows).

Completed checks:

- Source: legacy suites green; 185 unittest cases. The 4 new cases (the two-part record, a note in front of a plain prompt, a note-only record, send confirmation through the note) all fail on the 3.3.3 driver. The real record that was reported (22:05:16.547Z) reads as the note on 3.3.3 and as the typed text on 3.3.4, with the same prompt id and completion.
- Live, on Control's dedicated test session (pop-out window, with linked sessions), with no other Auto-Continue instance running (arranged with the Control and multi-question sessions). A 3.3.4 source `send_prompt` returned sent/confirmed; its record had the note as its first part, which 3.3.3 could not have matched. The installed EXE (`dist/Auto-Continue.exe`, 3.3.3 backed up first, isolated profile, Claude EXE only, both agents' work sessions excluded) sent its own After finish: `submission confirmed`, answered NOTEOK2. The dry run showed title v3.3.4 and both session rows, and Poll changed from 2 to 3, was saved, and was kept after a restart. The excluded sessions received nothing.

User Codex windows were not operated. No Claude login or underlying Claude change was made. EXE SHA-256: `e705b191ebeefe8ac013bac577648f9a21dda3d685010236561eca0e998e9925`.
