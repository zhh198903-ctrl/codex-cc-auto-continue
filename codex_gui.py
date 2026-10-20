"""Independent Codex GUI settings and dispatch; Claude settings stay in gui.py."""
import copy
import json
import uuid
from datetime import datetime

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices

import claude_app
import codex_app
from cli_routing import claude_row_allowed

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                            QFormLayout, QHBoxLayout,
                            QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                            QSpinBox, QTabBar, QVBoxLayout, QWidget)

from codex_activity import MAX_LIMIT
from codex_watcher import CodexWatcher, clean_config
from codex_sessions import session_options
from watcher_ui import TIMING_FIELDS, settings_card, timing_spin

MODEL_LEVELS = ["", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
                "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
EFFORT_LEVELS = ["", "low", "medium", "high", "xhigh", "max", "ultra"]
PROVIDER_LABELS = {"codex_app": "Codex EXE", "codex": "Codex CLI", "claude": "Claude CLI",
                   "claude_app": "Claude EXE"}
# Providers whose settings and row actions go through a CodexWatcher.
WATCHED = ("codex", "codex_app", "claude_app")
ACTIVITY_LABELS = {"running": "Running", "completed": "Completed", "interrupted": "Interrupted",
                   "quota": "Usage limit", "network": "Network error", "failed": "Failed", "unknown": "Unknown"}


class CodexGuiMixin:
    def _init_cli_gui(self):
        self._watch_targets = "claude"
        self._cli_rows = {"claude": [], "codex": [], "codex_app": [], "claude_app": []}
        self._cli_running = {"claude": False, "codex": False, "codex_app": False, "claude_app": False}
        self._watch_requested = False
        self._codex_config = clean_config({})
        self._codex_app_config = clean_config({"poll": 2})
        # Claude EXE is its own switch, off until the user turns it on.
        self._claude_app_config = clean_config({"poll": 2})
        self._claude_app_enabled = False
        self._codex_surfaces = "cli"
        self._view_provider = "claude"
        self._codex_recent_rows = []
        self._codex_recent_error = ""
        self._codex_recent_read_at = None

    def _build_cli_controls(self, root):
        self.provider_tabs = QTabBar()
        self.provider_tabs.setExpanding(True)
        for provider in ("codex_app", "codex", "claude", "claude_app"):
            index = self.provider_tabs.addTab(PROVIDER_LABELS[provider])
            self.provider_tabs.setTabData(index, provider)
        self.provider_tabs.currentChanged.connect(self._on_provider_view)
        root.addWidget(self.provider_tabs)
        self.target_checks = {}
        for provider in PROVIDER_LABELS:
            check = QCheckBox("Monitor this type")
            check.setToolTip("Enable " + PROVIDER_LABELS[provider] + " monitoring. Changing tabs only changes the view.")
            check.toggled.connect(self._on_target_selection)
            self.target_checks[provider] = check
        self.codex_recent_spin = QSpinBox()
        self.codex_recent_spin.setRange(1, MAX_LIMIT)
        self.codex_recent_spin.setValue(self._codex_app_config["recent_limit"])
        self.codex_recent_spin.setToolTip("Number of recent local Codex sessions to monitor. ChatGPT conversations are excluded. Saved across restarts.")
        self.codex_recent_spin.valueChanged.connect(
            lambda value: self._on_codex_timing("recent_limit", value, "codex_app"))

        self.target_cards = {"claude": settings_card(
            "Settings", {"poll": self.interval_spin, "buffer": self.buffer_spin,
                          "retry": self.retry_spin}, self.advanced_btn,
            enabled=self.target_checks["claude"])}
        for provider, label in (("codex_app", "Codex EXE"), ("codex", "Codex CLI"), ("claude_app", "Claude EXE")):
            config = self._codex_settings(provider)
            spins = {}
            for key, _label, _tip in TIMING_FIELDS:
                spin = timing_spin(config[key], 0 if key == "buffer" else 5 if key == "retry" else 1,
                                   600 if key == "buffer" else 3600)
                spin.valueChanged.connect(lambda value, k=key, p=provider: self._on_codex_timing(k, value, p))
                spins[key] = spin
            advanced = QPushButton("More settings…")
            advanced.clicked.connect(lambda _checked=False, p=provider: self._open_codex_settings(p))
            extra = (("Recent activities", self.codex_recent_spin),) if provider == "codex_app" else ()
            self.target_cards[provider] = settings_card("Settings", spins, advanced, extra,
                                                        enabled=self.target_checks[provider])
            if provider == "codex_app":
                self._codex_app_spins = spins
                self.codex_app_settings_btn = advanced
            elif provider == "claude_app":
                self._claude_app_spins = spins
                self.claude_app_settings_btn = advanced
            else:
                self._codex_spins = spins
                self.codex_settings_btn = advanced
        self.codex_controls = self.target_cards["codex"]
        self.codex_app_controls = self.target_cards["codex_app"]
        self.cards_panel = QWidget()
        cards = QHBoxLayout(self.cards_panel)
        cards.setContentsMargins(0, 0, 0, 0)
        for provider in self.target_checks:
            cards.addWidget(self.target_cards[provider], 1)
        root.addWidget(self.cards_panel)
        self.selection_hint = QLabel("Enable a monitor type to start watching.")
        self.selection_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.selection_hint)
        self._update_cli_controls()

    def _build_monitor_status(self, layout):
        bar = QHBoxLayout()
        self.codex_recent_status = QLabel("Start monitoring or refresh to read local activities.")
        self.codex_recent_status.setWordWrap(True)
        bar.addWidget(self.codex_recent_status, 1)
        self.recent_refresh_btn = QPushButton("Refresh activities")
        self.recent_refresh_btn.clicked.connect(lambda: self.sig_codex_app_config.emit(copy.deepcopy(self._codex_app_config)))
        bar.addWidget(self.recent_refresh_btn)
        layout.addLayout(bar)
        self._update_codex_recent_status()

    def _on_codex_recent_snapshot(self, rows, error):
        self._codex_recent_rows = rows
        self._codex_recent_error = error
        self._codex_recent_read_at = datetime.now()
        self._merge_cli_rows()

    def _update_codex_recent_status(self):
        if not hasattr(self, "codex_recent_status"):
            return
        provider = self._view_provider
        mode = "Monitoring" if self._cli_running[provider] else "Monitoring paused"
        if provider != "codex_app":
            text = f"{PROVIDER_LABELS[provider]} · {mode} · {len(self._cli_rows[provider])} windows"
        elif self._codex_recent_error:
            text = self._codex_recent_error
        else:
            text = f"Codex EXE · {mode} · {len(self._codex_recent_rows)} activities"
            if self._codex_recent_read_at:
                text += " · refreshed " + self._codex_recent_read_at.strftime("%H:%M:%S")
        self.codex_recent_status.setText(text)
        self.recent_refresh_btn.setVisible(provider == "codex_app")

    def _set_provider_view(self, provider):
        for index in range(self.provider_tabs.count()):
            if self.provider_tabs.tabData(index) == provider:
                self.provider_tabs.setCurrentIndex(index)
                return

    def _on_provider_view(self, index):
        self._view_provider = self.provider_tabs.tabData(index)
        self._update_cli_controls()
        self._merge_cli_rows()
        self._save_cli_settings()

    def _update_cli_controls(self):
        selected = self._selected_clis()
        for provider, check in self.target_checks.items():
            check.blockSignals(True)
            check.setChecked(provider in selected)
            check.blockSignals(False)
            self.target_cards[provider].setVisible(provider == self._view_provider)
        self.cards_panel.setVisible(True)
        self.selection_hint.setVisible(not selected)
        self.start_btn.setEnabled(bool(selected) or self.start_btn.text() == "Stop")
        self._update_codex_recent_status()

    def _sync_codex_controls(self):
        for key, spin in self._codex_spins.items():
            spin.blockSignals(True)
            spin.setValue(self._codex_config[key])
            spin.blockSignals(False)
        for key, spin in self._codex_app_spins.items():
            spin.blockSignals(True)
            spin.setValue(self._codex_app_config[key])
            spin.blockSignals(False)
        for key, spin in self._claude_app_spins.items():
            spin.blockSignals(True)
            spin.setValue(self._claude_app_config[key])
            spin.blockSignals(False)
        self.codex_recent_spin.blockSignals(True)
        self.codex_recent_spin.setValue(self._codex_app_config["recent_limit"])
        self.codex_recent_spin.blockSignals(False)

    def _on_codex_timing(self, key, value, provider="codex"):
        self._codex_settings(provider)[key] = value
        self._apply_codex_config()

    def _build_codex_worker(self):
        # One UIA thread serializes keyboard operations. The watchers have
        # separate objects, timers, states, settings and drivers.
        self.codex_worker = CodexWatcher()
        self.codex_worker.moveToThread(self.worker_thread)
        self.sig_codex_start.connect(self.codex_worker.start)
        self.sig_codex_stop.connect(self.codex_worker.stop)
        self.sig_codex_config.connect(self.codex_worker.configure)
        self.sig_codex_fire.connect(self.codex_worker.fire_now)
        self.sig_codex_skip.connect(self.codex_worker.skip)
        self.sig_codex_cooldown.connect(self.codex_worker.clear_cooldown)
        self.codex_worker.snapshot.connect(self._on_codex_snapshot)
        self.codex_worker.running_changed.connect(self._on_codex_running)
        self.codex_worker.log.connect(self._append_log)
        self.codex_worker.loops_spent.connect(self._on_codex_loops_spent)
        self.codex_app_worker = CodexWatcher(codex_app, "codex_app", "Codex App")
        self.codex_app_worker.moveToThread(self.worker_thread)
        self.sig_codex_app_bind.connect(self.codex_app_worker.bind_session)
        for action in ("start", "stop", "config", "fire", "skip", "cooldown"):
            slot = {"config": "configure", "fire": "fire_now", "cooldown": "clear_cooldown"}.get(action, action)
            getattr(self, "sig_codex_app_" + action).connect(getattr(self.codex_app_worker, slot))
        self.codex_app_worker.snapshot.connect(self._on_codex_app_snapshot)
        self.codex_app_worker.recent_snapshot.connect(self._on_codex_recent_snapshot)
        self.codex_app_worker.running_changed.connect(self._on_codex_app_running)
        self.codex_app_worker.log.connect(self._append_log)
        self.codex_app_worker.loops_spent.connect(lambda title, left: self._on_codex_loops_spent(title, left, "codex_app"))
        self.codex_app_worker.session_loops_spent.connect(self._on_codex_session_loops_spent)
        self.claude_app_worker = CodexWatcher(claude_app, "claude_app", "Claude EXE")
        self.claude_app_worker.moveToThread(self.worker_thread)
        for action in ("start", "stop", "config", "fire", "skip", "cooldown"):
            slot = {"config": "configure", "fire": "fire_now", "cooldown": "clear_cooldown"}.get(action, action)
            getattr(self, "sig_claude_app_" + action).connect(getattr(self.claude_app_worker, slot))
        self.claude_app_worker.snapshot.connect(self._on_claude_app_snapshot)
        self.claude_app_worker.running_changed.connect(self._on_claude_app_running)
        self.claude_app_worker.log.connect(self._append_log)
        self.claude_app_worker.loops_spent.connect(lambda title, left: self._on_codex_loops_spent(title, left, "claude_app"))
        self.claude_app_worker.session_loops_spent.connect(
            lambda sid, left: self._on_codex_session_loops_spent(sid, left, "claude_app"))

    def _codex_settings(self, provider):
        if provider == "claude_app":
            return self._claude_app_config
        return self._codex_app_config if provider == "codex_app" else self._codex_config

    def _codex_row_config(self, row):
        return self._codex_settings(row.get("provider"))

    def _codex_row_options(self, row):
        thread_id = row.get("thread_id", "") if row.get("provider") in ("codex_app", "claude_app") else ""
        return session_options(self._codex_row_config(row), thread_id, row["title"])

    def _selected_clis(self):
        selected = ["claude"] if self._watch_targets in ("claude", "both") else []
        if self._watch_targets in ("codex", "both"):
            if self._codex_surfaces in ("cli", "all"):
                selected.append("codex")
            if self._codex_surfaces in ("app", "all"):
                selected.append("codex_app")
        if self._claude_app_enabled:
            selected.append("claude_app")
        return tuple(selected)

    def _on_target_selection(self, _checked):
        claude = self.target_checks["claude"].isChecked()
        cli = self.target_checks["codex"].isChecked()
        app = self.target_checks["codex_app"].isChecked()
        self._claude_app_enabled = self.target_checks["claude_app"].isChecked()
        self._watch_targets = "both" if claude and (cli or app) else "claude" if claude else "codex" if cli or app else "none"
        if cli or app:
            self._codex_surfaces = "all" if cli and app else "cli" if cli else "app"
        self._update_cli_controls()
        if self._watch_requested:
            self._start_selected_watchers()
        self._merge_cli_rows()
        self._save_cli_settings()

    def _start_selected_watchers(self):
        if getattr(getattr(self, 'codex_service', None), 'monitor_owner', ''):
            self._append_log('info', 'Monitoring paused while Codex Claude Control uses its bundled driver')
            return
        selected = self._selected_clis()
        self._watch_requested = bool(selected)
        (self.sig_claude_start if "claude" in selected else self.sig_claude_stop).emit()
        (self.sig_codex_start if "codex" in selected else self.sig_codex_stop).emit()
        (self.sig_codex_app_start if "codex_app" in selected else self.sig_codex_app_stop).emit()
        (self.sig_claude_app_start if "claude_app" in selected else self.sig_claude_app_stop).emit()

    def _stop_watchers(self):
        self._watch_requested = False
        self.sig_claude_stop.emit()
        self.sig_codex_stop.emit()
        self.sig_codex_app_stop.emit()
        self.sig_claude_app_stop.emit()

    def _on_claude_snapshot(self, rows):
        self._cli_rows["claude"] = [dict(r, provider="claude") for r in rows
                                    if claude_row_allowed(r["hwnd"])]
        self._merge_cli_rows()

    def _on_codex_rpc_snapshot(self, rows):
        self._cli_rows["codex"] = rows
        self._merge_cli_rows()

    def _on_codex_rpc_loops_spent(self, tid, left):
        self._codex_config["sessions"].setdefault(tid, {})["loops"] = left
        self._save_cli_settings()

    def _on_codex_snapshot(self, rows):
        service = getattr(self, "codex_service", None)
        if service and service.cli.connected:
            return
        self._cli_rows["codex"] = rows
        self._merge_cli_rows()

    def _on_codex_app_snapshot(self, rows):
        self._cli_rows["codex_app"] = rows
        self._merge_cli_rows()

    def _on_claude_app_snapshot(self, rows):
        self._cli_rows["claude_app"] = rows
        self._merge_cli_rows()

    def _merge_cli_rows(self):
        provider = self._view_provider
        rows = list(self._cli_rows[provider])
        if provider == "codex_app":
            activities = []
            used_windows = set()
            for recent in self._codex_recent_rows:
                activity = dict(recent, provider="codex_app", activity_row=True,
                                record_only=True, hwnd=None, reset_utc=None,
                                last_sent_utc=None, excluded=False, model="", effort="",
                                current_model=recent.get("model", ""),
                                current_effort=recent.get("effort", ""))
                matches = [r for r in rows if r.get("thread_id") == recent["thread_id"]]
                if len(matches) == 1:
                    live = matches[0]
                    activity.update(live)
                    activity.update(activity_row=True, record_only=False,
                                    cwd=recent.get("cwd", ""), updated_utc=recent.get("updated_utc"))
                    if live.get("running") and live["status"] == "idle":
                        activity["status"] = "running"
                    used_windows.add(live["hwnd"])
                activities.append(activity)
            rows = activities + [r for r in rows if r["hwnd"] not in used_windows]
        self._on_snapshot(rows)
        self._update_codex_recent_status()

    def _on_claude_running(self, running):
        self._cli_running["claude"] = running
        self._on_running_changed(any(self._cli_running.values()))

    def _on_codex_running(self, running):
        self._cli_running["codex"] = running
        self._on_running_changed(any(self._cli_running.values()))

    def _on_codex_app_running(self, running):
        self._cli_running["codex_app"] = running
        self._update_codex_recent_status()
        self._on_running_changed(any(self._cli_running.values()))

    def _on_claude_app_running(self, running):
        self._cli_running["claude_app"] = running
        self._update_codex_recent_status()
        self._on_running_changed(any(self._cli_running.values()))

    def _load_cli_settings(self):
        target = self.settings.value("watch_targets", "claude", type=str)
        self._watch_targets = target if target in ("claude", "codex", "both", "none") else "claude"
        try:
            raw = json.loads(self.settings.value("codex_config", "{}", type=str))
        except (ValueError, TypeError):
            raw = {}
        self._codex_config = clean_config(raw)
        surfaces = self.settings.value("codex_surfaces", "cli", type=str)
        self._codex_surfaces = surfaces if surfaces in ("cli", "app", "all") else "cli"
        try:
            app_raw = json.loads(self.settings.value("codex_app_config", '{"poll":2}', type=str))
        except (ValueError, TypeError):
            app_raw = {"poll": 2}
        self._codex_app_config = clean_config(app_raw)
        try:
            claude_raw = json.loads(self.settings.value("claude_app_config", '{"poll":2}', type=str))
        except (ValueError, TypeError):
            claude_raw = {"poll": 2}
        self._claude_app_config = clean_config(claude_raw)
        self._claude_app_enabled = self.settings.value("claude_app_enabled", False, type=bool)
        self._sync_codex_controls()
        default_view = next((p for p in PROVIDER_LABELS if p in self._selected_clis()), "claude")
        view = self.settings.value("gui_view_provider", default_view, type=str)
        self._view_provider = view if view in PROVIDER_LABELS else default_view
        self.provider_tabs.blockSignals(True)
        self._set_provider_view(self._view_provider)
        self.provider_tabs.blockSignals(False)
        self._update_cli_controls()
        self._merge_cli_rows()
        self.sig_codex_config.emit(copy.deepcopy(self._codex_config))
        self.sig_codex_app_config.emit(copy.deepcopy(self._codex_app_config))
        self.sig_claude_app_config.emit(copy.deepcopy(self._claude_app_config))

    def _save_cli_settings(self):
        self.settings.setValue("watch_targets", self._watch_targets)
        self.settings.setValue("gui_view_provider", self._view_provider)
        self.settings.setValue("codex_config", json.dumps(self._codex_config))
        self.settings.setValue("codex_surfaces", self._codex_surfaces)
        self.settings.setValue("codex_app_config", json.dumps(self._codex_app_config))
        self.settings.setValue("claude_app_config", json.dumps(self._claude_app_config))
        self.settings.setValue("claude_app_enabled", self._claude_app_enabled)

    def _apply_codex_config(self):
        self._codex_config = clean_config(self._codex_config)
        self._codex_app_config = clean_config(self._codex_app_config)
        self._claude_app_config = clean_config(self._claude_app_config)
        self._sync_codex_controls()
        self.sig_codex_config.emit(copy.deepcopy(self._codex_config))
        self.sig_codex_app_config.emit(copy.deepcopy(self._codex_app_config))
        self.sig_claude_app_config.emit(copy.deepcopy(self._claude_app_config))
        # Codex changes write only Codex keys; no Claude settings are saved.
        self._save_cli_settings()
        self._last_render_sig = None
        self._render_table()

    def _open_codex_settings(self, provider="codex"):
        provider = provider if provider in ("codex_app", "claude_app") else "codex"
        config = self._codex_settings(provider)
        label = PROVIDER_LABELS[provider]
        dlg = QDialog(self)
        dlg.setWindowTitle(label + " — More settings")
        dlg.setMinimumWidth(420)
        dlg.resize(460, 260)
        layout = QVBoxLayout(dlg)
        hint = QLabel({
            "codex_app": "Only Codex sessions are monitored. ChatGPT and ChatGPT Work conversations are excluded. Per-session settings apply when that session is visible in an App window.",
            "claude_app": "Claude Code sessions in the Claude desktop app. Only the session showing in a window is acted on, and only after its title matches exactly one session record. See Help.",
        }.get(provider, "These settings apply only to Codex CLI. Use a separate Windows Terminal window for each session."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        fields = {}
        for key, field_label, low, high in [("max_retries", "Maximum retries", 1, 100)]:
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setValue(config[key])
            fields[key] = spin
            form.addRow(field_label, spin)
        layout.addLayout(form)
        permissions = auto_approve = None
        if provider == "codex" or (provider == "codex_app" and hasattr(codex_app, "apply_session_permissions")):
            permissions = QComboBox()
            permissions.setObjectName("codex_permission_mode")
            for text, value in (("Full access (default)", "full-access"), ("Approve for me", "approve-for-me"), ("Ask for approval", "ask-for-approval")):
                permissions.addItem(text, value)
            permissions.setCurrentIndex(permissions.findData(config["permission_mode"]))
            form.addRow("Permissions", permissions)
            auto_approve = QCheckBox("Automatically approve permission requests")
            auto_approve.setObjectName("codex_auto_approve")
            auto_approve.setChecked(config["auto_approve"])
            form.addRow(auto_approve)
        note = QLabel(
            "Exclude and After finish are set per session in the table. Permission prompts and questions wait for your input; model and effort are not changed." if provider == "claude_app"
            else "Model / Effort, Exclude and After finish are set per Codex session in the table. Background session settings are applied when that session is bound to a visible window. Permission dialogs wait for your input." if provider == "codex_app" and permissions is None
            else "Permissions are applied before continuation. Automatic approval handles tool permission requests; other menus wait for your input. Model / Effort, Exclude and After finish are set in the table.")
        note.setWordWrap(True)
        layout.addWidget(note)
        reset = QPushButton("Reset " + label + " settings…")
        def reset_codex():
            if QMessageBox.question(dlg, "Reset " + label, "Reset " + label + " timings, exclusions, model / effort and after-finish settings?") != QMessageBox.StandardButton.Yes:
                return
            if provider == "codex_app":
                self._codex_app_config = clean_config({"poll": 2})
            elif provider == "claude_app":
                self._claude_app_config = clean_config({"poll": 2})
            else:
                self._codex_config = clean_config({})
            self._apply_codex_config()
            dlg.reject()
        reset.clicked.connect(reset_codex)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._codex_settings(provider).update({key: spin.value() for key, spin in fields.items()})
            if permissions is not None:
                self._codex_settings(provider).update(permission_mode=permissions.currentData(), auto_approve=auto_approve.isChecked())
            self._apply_codex_config()

    def _cli_action(self, row, action):
        if row.get("rpc") and action in {"fire", "skip", "cooldown"}:
            self.codex_service.native_cli_action.emit(dict(target_id="codex:"+row["thread_id"], action=action))
            return
        provider = row.get("provider")
        codex = provider in WATCHED
        thread_id = row.get("thread_id", "")
        if action == "link" and thread_id and provider == "codex_app":
            candidates = self._codex_link_candidates(row)
            if self._cli_running["codex_app"] and len(candidates) == 1:
                native = candidates[0]
                self.sig_codex_app_bind.emit(dict(thread_id=thread_id, hwnd=native["hwnd"],
                                                identity=native["identity"], thread_hint=native["thread_hint"]))
            return
        if action == "open" and thread_id and provider == "codex_app":
            try:
                ident = str(uuid.UUID(thread_id))
            except (ValueError, TypeError, AttributeError):
                return
            QDesktopServices.openUrl(QUrl("codex://threads/" + ident))
            return
        prefix = {"codex_app": "sig_codex_app_", "claude_app": "sig_claude_app_"}.get(provider, "sig_codex_")
        if action in ("fire", "skip", "cooldown") and (row.get("hwnd") is None or row.get("record_only")):
            return  # Configuration is editable; input requires a bound window.
        if action == "fire":
            (getattr(self, prefix + "fire") if codex else self.sig_fire_now).emit(row["hwnd"])
        elif action == "skip":
            (getattr(self, prefix + "skip") if codex else self.sig_skip).emit(row["hwnd"])
        elif action == "cooldown":
            (getattr(self, prefix + "cooldown") if codex else self.sig_clear_cooldown).emit(row["hwnd"])
        elif action == "after_finish":
            if codex:
                self._on_codex_after_finish(row["title"], provider, thread_id)
            else:
                self._on_after_finish_clicked(row["title"])
        elif action in ("exclude", "include"):
            if codex:
                if thread_id:
                    self._codex_settings(provider)["sessions"].setdefault(thread_id, {})["excluded"] = action == "exclude"
                    self._apply_codex_config()
                    return
                excluded = self._codex_settings(provider)["excluded"]
                if action == "exclude" and row["title"] not in excluded:
                    excluded.append(row["title"])
                elif action == "include" and row["title"] in excluded:
                    excluded.remove(row["title"])
                self._apply_codex_config()
            elif action == "exclude":
                self._do_exclude(row["hwnd"], row["title"])
            else:
                self._do_unexclude(row["title"])

    def _on_codex_override(self, title, kind, value, provider="codex", thread_id=""):
        if thread_id:
            self._codex_settings(provider)["sessions"].setdefault(thread_id, {})[kind] = value or ""
            self._apply_codex_config()
            return
        values = self._codex_settings(provider)[kind + "_overrides"]
        if value:
            values[title] = value
        else:
            values.pop(title, None)
        self._apply_codex_config()

    def _codex_link_candidates(self, row):
        return [r for r in self._cli_rows["codex_app"] if r["title"] == row["title"]
                and not r.get("thread_id") and r.get("identity") and r.get("thread_hint")]

    def _on_codex_loops_spent(self, title, left, provider="codex"):
        self._codex_settings(provider)["after_finish_loops"][title] = left
        self._save_cli_settings()
        self._last_render_sig = None
        self._render_table()

    def _on_codex_session_loops_spent(self, thread_id, left, provider="codex_app"):
        self._codex_settings(provider)["sessions"].setdefault(thread_id, {})["loops"] = left
        self._save_cli_settings()
        self._last_render_sig = None
        self._render_table()

    def _on_codex_after_finish(self, title, provider="codex", thread_id=""):
        config = self._codex_settings(provider)
        options = session_options(config, thread_id, title)
        dlg = QDialog(self)
        dlg.setWindowTitle(PROVIDER_LABELS.get(provider, "Codex CLI") + " — After finish")
        layout = QVBoxLayout(dlg)
        hint = QLabel(f"Prompt for {title!r} after a completed turn sits idle. Empty = off. Use one line.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        edit = QPlainTextEdit(options["after_finish"])
        layout.addWidget(edit)
        loops = QSpinBox()
        loops.setRange(-1, 10000)
        loops.setSpecialValueText("Unlimited")
        initial_left = options["loops"]
        loops.setValue(initial_left)
        form = QFormLayout()
        form.addRow("Runs left (0 = spent)", loops)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        def accept():
            prompt = edit.toPlainText().strip()
            if "\n" in prompt or "\r" in prompt:
                QMessageBox.warning(dlg, PROVIDER_LABELS.get(provider, "Codex") + " prompt", "Use a single-line prompt.")
                return
            active = self._codex_settings(provider)
            current = session_options(active, thread_id, title)
            old_prompt = current["after_finish"]
            left = current["loops"] if loops.value() == initial_left else loops.value()
            if thread_id:
                session = active["sessions"].setdefault(thread_id, {})
                session["after_finish"] = prompt
                session["loops"] = 1 if left == 0 and prompt and prompt != old_prompt else left
                self._apply_codex_config()
                dlg.accept()
                return
            if prompt:
                active["after_finish"][title] = prompt
                # Preserve any budget spent while this modal dialog was open.
                active["after_finish_loops"][title] = 1 if left == 0 and prompt != old_prompt else left
            else:
                active["after_finish"].pop(title, None)
                active["after_finish_loops"].pop(title, None)
            self._apply_codex_config()
            dlg.accept()
        buttons.accepted.connect(accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        dlg.exec()
