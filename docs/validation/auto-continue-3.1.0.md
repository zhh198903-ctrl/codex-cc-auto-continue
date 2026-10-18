# Auto-Continue 3.1.0 release validation

Validated on Windows on 2026-10-18. The release product is Auto-Continue;
the separate Control application and its bundled copy are not published here.

## Source checks

- 235 Codex regression checks and 66 subtests passed on the current sources
  and on the curated release snapshot.
- Seven legacy script suites passed: parsing, recovery, GUI ticks, local API,
  self-trigger protection, updater and single-instance coordination.
- Published CLI fixture normalisation from 3.0.0 was retained; all 34 CLI and
  session checks passed against those retained fixtures.
- New checks cover local context fidelity and paging, separate text consent,
  recent-session bounds, question-card coexistence, shared-session windows,
  focus/binding changes, uncertain receipts and worker queue ordering.
- GitHub CI runs the existing checks and the new Control integration checks.

## Actual installed EXE

The clean snapshot was built through build_release.py. The previous project
EXE was backed up and dist/Auto-Continue.exe was replaced with the build.
Installed and built bytes have identical SHA-256:

`85146a60fa0ee3c32a005c25575664c6365e5973931c1f1212564828d73dbe9d`

The actual 3.1.0 window started with three monitor tabs and the bottom log.
A GUI tab change was saved into an isolated project-owned profile and
survived an actual EXE restart. Recent Codex activities and manual refresh
were checked with automatic dispatch stopped.

Authenticated requests to the packaged EXE verified version and capabilities,
recent metadata and monitor status. Missing tokens were rejected with 401;
conversation text stayed disabled with 403; invalid recent limits returned
400. Test tokens and user session metadata are excluded from release files.

## Limits

This release validates Auto-Continue and its packaged API. It does not claim
a new full Control-client acceptance run. Native send, stop, approval and
question interactions were not exercised against user working conversations.
Their changed paths are covered by source regressions and captured fixtures.
Claude login and live Claude sessions were not driven. Native quota/reset,
external outages and model quality were not newly exercised in this release.
