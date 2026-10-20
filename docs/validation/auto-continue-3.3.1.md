# Auto-Continue 3.3.1 verification

Date: 2026-10-20. Scope: one fix to the updater's source chooser. Control keeps its built-in engine and is not modified or published here.

Found while accepting 3.3.0: the published 3.2.0 client's first Update now failed with "1 (of 2) futures unfinished". GitHub and the download-site mirror are probed at the same time; when one probe was still silent after the budget, the chooser raised and the download was abandoned even though the mirror was ready. A second click worked because both probes answered. 3.3.1 treats a probe that has not answered in time as unreachable, uses the other source, and does not wait for the hung probe.

Completed checks:

- Seven legacy suites (686 checks; four new chooser cases that reproduce the old "futures unfinished" error when the fix is removed), 174 unittest cases, 96 pytest cases and 19 subtests passed. Packaged `--selftest` passed.
- Installed EXE (`dist/Auto-Continue.exe`, 3.3.0 backed up first), isolated profile with Claude EXE on and nothing armed to send: title v3.3.1, four tabs, this session's Claude EXE row shown as Running, Poll changed from 2 to 3 in the GUI and kept after a full restart, update check reported up to date.
- Claude EXE behavior is unchanged from 3.3.0, whose real-session checks are recorded in `auto-continue-3.3.0.md`.

User Codex windows were not operated. No Claude login or underlying Claude change was made. EXE SHA-256: `7c59c91ce1c153d21844853229a60b2a4f95db130709c32f162df7096481a300`.
