# Auto-Continue 3.1.2 verification

Date: 2026-10-18. Scope: independent Auto-Continue. Control continues using its built-in engine; this task does not modify or publish Control.

The public 3.1.0 independent EXE successfully downloaded and replaced itself with 3.1.1, then failed to reopen because its old restart helper inherited a deleted Python runtime directory. This happened before Python application code could run. The error and unmodified client's original hash were recorded. Consequently, the compatibility repair ships as a new 3.1.2 release instead of replacing already published 3.1.1 bytes.

The stock [PyInstaller 6.19.0 bootloader](https://github.com/pyinstaller/pyinstaller/blob/v6.19.0/bootloader/src/pyi_main.c) uses its archive filename environment variable to decide whether a process inherits a runtime. The release build gives a project-local copy a version-specific archive-identity variable, preserving PE layout and normal parent/child behavior. An executable from a different release therefore starts with a fresh runtime, including when invoked by the old updater. The complete transformation is in `versioned_bootloader.py`; stock installed tool files remain untouched. Build dependencies are pinned and unknown bootloader layouts fail the build.

Completed checks:

- Seven legacy suites; 154 unittest cases; 96 pytest cases and 19 subtests passed. The 15 updater cases cover installation, Unicode detached helper execution, integrity failover, monitor resume and release bootloader identity.
- The final frozen 3.1.2 EXE passed packaged startup with its own executable path inherited as the old archive, process level 1, a deleted application directory and no explicit environment-reset flag. This directly reproduces the legacy restart conditions.
- The actual local standalone installation was backed up and replaced with the same EXE. Its native title showed 3.1.2; all three tabs, bottom log, five recent Codex activities and manual refresh were verified. Poll changed from 3 to 4 through the native GUI and persisted after full exit and restart.
- The packaged local API reported 3.1.2, required authentication, enforced recent-count bounds and separate text consent, and reported all monitors stopped in the isolated verification profile.
- The 3.1.1 Unicode-path one-click upgrade and monitor restoration remain covered by their real EXE evidence and unchanged application update code. Public old-client upgrades to 3.1.2 are recorded after publication in `docs/release/3.1.2-verification.json`.

User Codex windows were not operated. Claude login and live driver behavior were not exercised. Control was notified separately about its built-in update UI and keeps its own package.
