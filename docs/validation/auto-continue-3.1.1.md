# Auto-Continue 3.1.1 verification

Date: 2026-10-18. Scope: independent Windows EXE updates. Control keeps its built-in copy; no Control package was modified or published by this task.

The previous detached updater embedded UTF-8 installation paths in a batch file. A real no-console EXE test in a directory containing Chinese characters, spaces, a percent sign and an exclamation mark reproduced a failed replacement. The repaired helper keeps its script ASCII and passes paths through the Unicode process environment. The same real EXE test now succeeds.

`Update now` uses one action for download, integrity verification, replacement and restart. The normal window-close path saves settings and closes the local API and driver. The previous EXE is backed up, the PyInstaller restart environment is reset, and monitor switches are restored without changing Start on launch. A failed integrity check tries the other source; a replacement failure relaunches the old copy and leaves a visible failure message for startup.

Validation:

- Seven legacy suites, 154 unittest cases, and 93 pytest cases plus 19 subtests passed. The updater additions include an actual detached Windows helper test with Chinese, spaces, percent, exclamation and ampersand path characters.
- A frozen acceptance factory carrying the repaired updater and a test-only 3.1.0 label downloaded the **unmodified final 3.1.1 EXE** through a local HTTP server after a native Update now click. This is a pre-publication fixture, not a claim that the previously shipped 3.1.0 helper was changed remotely.
- After restart, the EXE hash matched the final release candidate; its backup matched the factory; no staged EXE remained. Poll changed through the native GUI from 6 to 7 survived. Codex CLI monitoring resumed while the other two monitors stayed stopped, and Start on launch stayed off. Driver dry-run was enabled and automatic approvals were disabled in this isolated profile; no input was sent to user sessions.
- The actual standalone installation was backed up and updated. Its native window showed 3.1.1, all three tabs, bottom activity log and five recent Codex activities; manual refresh changed the refresh timestamp. A native poll change from 2 to 3 survived closing and restarting that installed EXE.
- The installed packaged API reported 3.1.1, required authentication, enforced recent-count bounds and separate text consent, returned recent metadata and reported all monitors stopped for the local installation profile.
- User Codex windows were not operated. Claude login and live driver behavior were not exercised. The Control task was notified separately about its built-in update UI; its package remains outside this release.

Release hashes and public download verification are recorded in `docs/release/3.1.1-verification.json`.
