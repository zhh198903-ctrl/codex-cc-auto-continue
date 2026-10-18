"""Independent Codex watcher, sharing only the GUI's serialized worker thread."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

import codex_cli as cli
from codex_activity import RecentActivities, activity_limit
from codex_sessions import clean_sessions, session_options

DEFAULT_CONFIG = {"poll": 10, "buffer": 20, "retry": 30, "max_retries": 10,
                  "recent_limit": 5,
                  "permission_mode": "full-access", "auto_approve": True,
                  "dry_run": False, "excluded": [], "after_finish": {},
                  "after_finish_loops": {}, "model_overrides": {}, "effort_overrides": {},
                  "sessions": {}}


def clean_config(value) -> dict:
    source = value if isinstance(value, dict) else {}
    out = dict(DEFAULT_CONFIG)
    for name, bounds in {"poll": (1, 3600), "buffer": (0, 600),
                         "retry": (5, 3600), "max_retries": (1, 100)}.items():
        try:
            out[name] = min(bounds[1], max(bounds[0], int(source.get(name, out[name]))))
        except (ValueError, TypeError):
            pass
    out["dry_run"] = source.get("dry_run") is True
    out["auto_approve"] = source.get("auto_approve", True) is True
    if source.get("permission_mode") in ("full-access", "approve-for-me", "ask-for-approval"):
        out["permission_mode"] = source["permission_mode"]
    out["recent_limit"] = activity_limit(source.get("recent_limit", 5))
    out["sessions"] = clean_sessions(source.get("sessions", {}))
    excluded = source.get("excluded", [])
    out["excluded"] = [str(x) for x in excluded] if isinstance(excluded, list) else []
    prompts = source.get("after_finish", {})
    out["after_finish"] = {str(k): str(v).strip() for k, v in prompts.items()
        if isinstance(v, str) and v.strip() and "\n" not in v and "\r" not in v} if isinstance(prompts, dict) else {}
    out["after_finish_loops"] = {}
    for kind in ("model", "effort"):
        values = source.get(kind + "_overrides", {})
        out[kind + "_overrides"] = {str(k): str(v) for k, v in values.items()
            if isinstance(v, str) and v and "\n" not in v and "\r" not in v} if isinstance(values, dict) else {}
    loops = source.get("after_finish_loops", {})
    if isinstance(loops, dict):
        for key, value in loops.items():
            try:
                out["after_finish_loops"][str(key)] = max(-1, int(value))
            except (TypeError, ValueError):
                pass
    return out


@dataclass
class State:
    hwnd: int
    title: str
    status: str = "idle"
    screen: cli.Screen = cli.Screen()
    read_at: datetime | None = None
    reset_utc: datetime | None = None
    quota_id: str = ""
    consumed_quota: str = ""
    last_sent: datetime | None = None
    retry_sent: datetime | None = None
    retry_attempts: int = 0
    last_error: str = ""
    completion_id: str = ""
    initialized: bool = False
    seen_running: bool = False
    idle_since: datetime | None = None
    blind: bool = False
    identity: str = ""
    quota_attempts: int = 0
    quota_sent: datetime | None = None
    quota_generation: str = ""
    submission: object | None = None
    submit_action: str = ""
    submit_reason: str = ""
    thread_id: str = ""
    thread_hint: str = ""
    continued_completion: str = ""
    continued_text: str = ""


class CodexWatcher(QObject):
    snapshot = pyqtSignal(list)
    recent_snapshot = pyqtSignal(list, str)
    log = pyqtSignal(str, str)
    running_changed = pyqtSignal(bool)
    loops_spent = pyqtSignal(str, int)
    session_loops_spent = pyqtSignal(str, int)

    def __init__(self, driver=None, provider="codex", label="Codex CLI"):
        super().__init__()
        self.driver = driver or cli
        self.provider = provider
        self.label = label
        self.config = clean_config({})
        self.states: dict[int, State] = {}
        self.session_bindings = {}
        self.primary_windows = {}
        self.thread_states = {}
        self.latest_rows = []
        self.activities = RecentActivities() if provider == "codex_app" else None
        self.running = False
        self._uia_initialized = False
        self.timer = QTimer(self)
        self.timer.setInterval(self.config["poll"] * 1000)
        self.timer.timeout.connect(self.tick)
        self.now = lambda: datetime.now(timezone.utc)

    @pyqtSlot(dict)
    def configure(self, config):
        new = clean_config(config)
        if new["dry_run"] != self.config["dry_run"]:
            self.states.clear()
        self.config = new
        self.timer.setInterval(new["poll"] * 1000)
        self._refresh_recent_activities()

    def _refresh_recent_activities(self):
        if self.activities is not None:
            rows, error = self.activities.read(self.config["recent_limit"])
            self.recent_snapshot.emit(rows, error)

    @pyqtSlot()
    def start(self):
        if self.running:
            return
        if not self._uia_initialized:
            self.driver.auto.InitializeUIAutomationInCurrentThread()
            self._uia_initialized = True
        self.states.clear()
        self.primary_windows.clear()
        self.thread_states.clear()
        self.running = True
        self.running_changed.emit(True)
        self.timer.start()
        self.log.emit("info", self.label + " watcher started")
        self.tick()

    @pyqtSlot()
    def stop(self):
        self.running = False
        self.timer.stop()
        for st in self.states.values():
            st.seen_running = False
            st.idle_since = None
        self.running_changed.emit(False)

    @pyqtSlot(int)
    def fire_now(self, hwnd):
        if not self.running:
            return
        st = self.states.get(hwnd)
        window = self.driver.window_from_handle(hwnd)
        if st is not None and window is not None and not self._options(st)["excluded"]:
            self._send(st, window, "continue", self.now(), "manual continue")
            self._publish()

    @pyqtSlot(int)
    def skip(self, hwnd):
        st = self.states.get(hwnd)
        if st is not None:
            st.submission = None
            st.consumed_quota = st.quota_id
            st.reset_utc = None
            st.status = "idle"
            self._publish()

    @pyqtSlot(int)
    def clear_cooldown(self, hwnd):
        st = self.states.get(hwnd)
        if st is not None:
            st.last_sent = st.retry_sent = None
            st.retry_attempts = 0
            st.quota_attempts = 0
            st.quota_sent = None
            st.consumed_quota = st.quota_id = ""
            st.reset_utc = None
            st.status = "idle"
            self._publish()

    def _sent(self, st, now, reason, action):
        st.submission = None
        st.last_sent = now
        st.status = "sent"
        dry_run=self.config['dry_run'] and not action.startswith('companion')
        prefix = "[dry-run] " if dry_run else ""
        confirmation = "; submission confirmed" if self.provider == "codex_app" and not dry_run else ""
        self.log.emit("fire", f"{prefix}Codex #{st.hwnd:x}: {reason}{confirmation}")
        st.seen_running = False
        st.idle_since = None
        service = getattr(self, "control_service", None)
        if service and action.startswith("companion"):
            service.confirmed_native(st)
        if action == "quota":
            st.consumed_quota = st.quota_id
            st.reset_utc = None
            st.quota_sent = now
            st.quota_attempts += 1
        elif action == "retry":
            st.retry_sent = now
            st.retry_attempts += 1
        elif action in {"after_finish","companion_after_finish"} and not dry_run:
            st.continued_completion=st.completion_id or st.screen.completion_id
            st.continued_text=self._options(st)['after_finish']
            left = self._options(st)["loops"]
            if left > 0:
                if st.thread_id:
                    self.config["sessions"].setdefault(st.thread_id, {})["loops"] = left - 1
                    self.session_loops_spent.emit(st.thread_id, left - 1)
                else:
                    self.config["after_finish_loops"][st.title] = left - 1
                    self.loops_spent.emit(st.title, left - 1)

    def _options(self, st):
        return session_options(self.config, st.thread_id, st.title)

    @pyqtSlot(dict)
    def bind_session(self, request):
        """An explicit user choice links an ambiguous title to its session ID."""
        if not self.running or self.activities is None:
            return
        data = self.activities.thread(request.get("thread_id", ""))
        hwnd = request.get("hwnd")
        st = self.states.get(hwnd)
        window = self.driver.window_from_handle(hwnd) if st is not None else None
        view = self.driver.read_text(window) if window is not None else None
        if (data is None or view is None or not view.screen.identified or
                window.Name != data["title"] or view.identity != request.get("identity") or
                not view.thread_hint or view.thread_hint != request.get("thread_hint")):
            self.log.emit("warn", "Codex session binding held; refresh and open the exact session first")
            return
        self.session_bindings[hwnd] = (view.identity, view.thread_hint, data["id"])
        self.primary_windows[data['id']] = hwnd
        self.states[hwnd] = State(hwnd, window.Name, identity=view.identity,
                                  thread_id=data["id"], thread_hint=view.thread_hint,
                                  screen=view.screen, read_at=st.read_at)
        self._publish()

    def _send(self, st, window, text, now, reason, action="") -> bool:
        if (self.provider == 'codex_app' and st.thread_id and
                self.primary_windows.get(st.thread_id, st.hwnd) != st.hwnd):
            return False
        if st.submission is not None:
            return False
        prepare = getattr(self.driver, "prepare", None)
        if prepare:
            prepare(window, st.identity)
        if self.provider == "codex_app":
            window.expected_thread_hint = st.thread_hint
            window.expected_thread_id = st.thread_id
        # The Codex driver performs its own last-moment screen/focus checks.
        dry_run = self.config['dry_run'] and not action.startswith('companion')
        options = self._options(st)
        permissions = getattr(self.driver, "apply_session_permissions", None)
        service = getattr(self, "control_service", None)
        if service and self.provider == "codex" and service.native_cli_owned():
            st.status = "rpc_controlled"
            return False
        owned_window = bool(service and self.provider == 'codex_app' and service.native_window_owned(st.hwnd))
        if permissions and not dry_run and not (service and (service.owns(st.thread_id,self.provider) or owned_window)):
            if not permissions(window, self.config["permission_mode"]):
                self.log.emit("warn", f"Codex #{st.hwnd:x}: permissions could not be applied; continue held")
                return False
        model, effort = options["model"], options["effort"]
        if (model or effort) and not dry_run:
            if not self.driver.apply_session_options(window, model, effort):
                self.log.emit("warn", f"Codex #{st.hwnd:x}: session model / effort could not be applied; continue held")
                return False
        outcome = self.driver.send_prompt(window, text, dry_run=dry_run)
        if getattr(outcome, "submission", None) is not None:
            st.submission = outcome.submission
            st.submit_reason, st.submit_action = reason, action
            st.status = outcome.status
            self.log.emit("info", f"Codex #{st.hwnd:x}: own prompt staged; waiting for native Send and submission confirmation")
            return False
        if not outcome:
            detail = getattr(outcome, 'detail', '') or 'draft, menu, running, unreadable, or focus'
            self.log.emit("warn", f"Codex #{st.hwnd:x}: {reason} held ({detail})")
            return False
        # Permission/model pickers can take several seconds. Cooldown starts
        # after actual submission, rather than before those native actions.
        self._sent(st, self.now(), reason, action)
        return True

    @pyqtSlot()
    def tick(self):
        if not self.running:
            return
        self._refresh_recent_activities()
        now = self.now()
        seen = set()
        observed = []
        try:
            handles = self.driver.terminal_handles()
        except Exception as exc:
            self.log.emit("warn", f"Codex enumeration failed: {type(exc).__name__}")
            return
        for hwnd in handles:
            seen.add(hwnd)
            try:
                window = self.driver.window_from_handle(hwnd)
                if window is None:
                    continue
                text = self.driver.read_text(window)
                if not text:
                    st = self.states.get(hwnd)
                    if st is not None:
                        st.blind = True
                        st.status = "unknown"
                    continue
                screen = self.driver.inspect_screen(text, now)
                if not screen.identified:
                    # An exited Codex or another active tab never inherits a
                    # pending send, retry counter, or after-finish arm.
                    self.states.pop(hwnd, None)
                    self.session_bindings.pop(hwnd, None)
                    continue
                title = window.Name or f"Codex #{hwnd:x}"
                identity = self.driver.session_key(window, text) if hasattr(self.driver, "session_key") else ""
                if self.provider == 'codex_app' and not identity:
                    # React can temporarily unmount the native composer. That
                    # is unreadable state, not proof of a different chat.
                    # Preserve the exact pending receipt and continuation arm;
                    # observation and mutations resume only after identity reads.
                    st = self.states.get(hwnd)
                    if st is not None:
                        st.blind = True
                        st.status = 'unknown'
                    continue
                thread_id = self.activities.thread_id_for_title(title) if self.activities is not None else ""
                thread_hint = getattr(text, "thread_hint", "")
                binding = self.session_bindings.get(hwnd)
                if binding:
                    if binding[:2] == (identity, thread_hint):
                        thread_id = binding[2]
                    else:
                        self.session_bindings.pop(hwnd, None)
                previous = self.states.get(hwnd)
                if previous is not None and (identity != previous.identity or thread_id != previous.thread_id or
                                             thread_hint != previous.thread_hint):
                    self.states.pop(hwnd, None)
                st = self.states.setdefault(hwnd, State(hwnd, title))
                st.identity = identity
                st.thread_id = thread_id
                st.thread_hint = thread_hint
                st.title, st.screen, st.read_at, st.blind = title, screen, now, False
                observed.append((st, window))
            except Exception as exc:
                self.log.emit("warn", f"Codex #{hwnd:x}: {type(exc).__name__}: {exc}")
        if self.provider == 'codex_app':
            observed = self._one_window_per_thread(observed)
        for st, window in observed:
            try:
                self._observe(st, window, now)
            except Exception as exc:
                self.log.emit('warn', f'Codex #{st.hwnd:x}: {type(exc).__name__}: {exc}')
        for hwnd in list(self.states):
            if hwnd not in seen and not self.driver.window_exists(hwnd):
                self.states.pop(hwnd, None)
                self.session_bindings.pop(hwnd, None)
        self._publish()

    def _one_window_per_thread(self, observed):
        """Read every GUI, but automate each full thread once per poll.

        Selection survives z-order changes. A live but switched/unreadable
        primary holds its mirrors. A closed primary can transfer cooldowns to
        a mirror only when no native submission receipt remains uncertain.
        """
        groups, selected = {}, []
        for st, window in observed:
            if not st.thread_id:
                selected.append((st, window))
            else:
                groups.setdefault(st.thread_id, []).append((st, window))
        service = getattr(self, 'control_service', None)
        for tid, members in groups.items():
            bound = service.native.get(tid) if service and tid in service.bound else None
            primary = bound[0] if bound else self.primary_windows.get(tid)
            chosen = next((item for item in members if item[0].hwnd == primary), None)
            prior = self.thread_states.get(tid)
            if chosen is None and (bound or primary and
                    (self.driver.window_exists(primary) or prior and prior.submission is not None)):
                for st, _ in members:
                    st.status = 'held'
                continue
            if chosen is None:
                chosen = min(members, key=lambda item: item[0].hwnd)
                st, window = chosen
                if prior is not None:
                    st = replace(prior, hwnd=st.hwnd, title=st.title, identity=st.identity,
                                 thread_hint=st.thread_hint, screen=st.screen,
                                 read_at=st.read_at, blind=False)
                    self.states[st.hwnd] = st
                    chosen = st, window
            self.primary_windows[tid] = chosen[0].hwnd
            self.thread_states[tid] = chosen[0]
            selected.append(chosen)
        return selected

    def _observe(self, st, window, now):
        screen = st.screen
        completed = getattr(self.activities, 'completed_turn', None)
        if self.provider == 'codex_app' and st.thread_id and completed:
            stamp = completed(st.thread_id)
            if stamp:
                # UIA can keep an older virtualized message list. Exact local
                # turn records also detect a fast completion between polls.
                screen = st.screen = replace(screen, completion_id=stamp)
        service = getattr(self, "control_service", None)
        if service and "codex:desktop:" + st.thread_id in service.paused:
            st.status = "attention"
            return
        options = self._options(st)
        if not st.initialized:
            st.completion_id = screen.completion_id
            st.initialized = True
        if options["excluded"]:
            st.submission = None
            st.status = "excluded"
            st.seen_running = False
            st.idle_since = None
            return
        if screen.permission:
            service = getattr(self, "control_service", None)
            controlled = bool(service and (service.owns(st.thread_id,self.provider)
                or self.provider == 'codex_app' and service.native_window_owned(st.hwnd)
                or self.provider == "codex" and service.native_cli_owned()))
            identified_thread = self.provider != 'codex_app' or bool(st.thread_id)
            if self.config["auto_approve"] and identified_thread and not controlled and not self.config["dry_run"] and not (screen.draft or screen.running):
                prepare = getattr(self.driver, "prepare", None)
                if prepare:
                    prepare(window, st.identity)
                if self.provider == "codex_app":
                    window.expected_thread_hint = st.thread_hint
                    window.expected_thread_id = st.thread_id
                approve = getattr(self.driver, "approve_permission", None)
                if approve and approve(window):
                    st.status = "approved"
                    self.log.emit("info", f"Codex #{st.hwnd:x}: native permission request approved once")
                    return
            st.status = "prompt"
            st.idle_since = None
            return
        if st.submission is not None:
            if screen.error_kind == "quota" and screen.reset_utc is not None:
                st.reset_utc = screen.reset_utc
            if st.submit_action == "after_finish" and (
                    options["loops"] == 0 or options["after_finish"] != st.submission.prompt):
                st.submission = None
                st.status = "held"
                return
            prepare = getattr(self.driver, "prepare", None)
            if prepare:
                prepare(window, st.identity)
            if self.provider == "codex_app":
                window.expected_thread_hint = st.thread_hint
                window.expected_thread_id = st.thread_id
            due = st.reset_utc is None or now >= st.reset_utc + timedelta(seconds=self.config["buffer"])
            if st.submit_action.startswith('companion') and getattr(st.submission, 'invoked_at', None) is not None:
                due = False  # The explicit operation invokes native Send at most once.
            outcome = self.driver.poll_submission(window, st.submission, retry_seconds=self.config["retry"], allow_click=due)
            if outcome:
                self._sent(st, now, st.submit_reason, st.submit_action)
            else:
                st.submission = outcome.submission
                st.status = outcome.status
            return
        if screen.running:
            st.seen_running = True
            st.idle_since = None
            st.status = "quota_busy" if screen.error_kind in ("quota", "quota_retry") else "retry_busy" if screen.error_kind == "network" else "idle"
            if screen.error_kind and screen.error_id != st.last_error:
                st.last_error = screen.error_id
                self.log.emit("warn", f"Codex #{st.hwnd:x}: {screen.error_kind}; App is retrying; automatic input held")
            return
        if screen.blocked or screen.draft or not screen.ready:
            st.status = "prompt" if screen.blocked else "held"
            st.idle_since = None
            return
        if screen.error_kind:
            st.idle_since = None
            st.seen_running = False
            if screen.error_id != st.last_error:
                self.log.emit("warn", f"Codex #{st.hwnd:x}: {screen.error_kind}" +
                              (f"; reset {screen.reset_utc.isoformat()}" if screen.reset_utc else ""))
                st.last_error = screen.error_id
            if screen.error_kind == "quota":
                generation = screen.reset_utc.isoformat() if screen.reset_utc else ""
                if generation and generation != st.quota_generation:
                    st.quota_generation = generation
                    st.quota_attempts = 0
                    st.quota_sent = None
                if st.quota_id != screen.error_id:
                    st.quota_id = screen.error_id
                    st.reset_utc = screen.reset_utc
                if st.consumed_quota == st.quota_id:
                    st.status = "cooldown"
                    return
                if not st.reset_utc:
                    st.status = "attention"
                    return
                if st.quota_attempts >= self.config["max_retries"]:
                    st.status = "attention"
                    return
                st.status = "pending"
                if now >= st.reset_utc + timedelta(seconds=self.config["buffer"]):
                    if st.quota_sent and now - st.quota_sent < timedelta(seconds=self.config["retry"]):
                        st.status = "cooldown"
                        return
                    self._send(st, window, "continue", now, "usage reset → continue", "quota")
                return
            if screen.error_kind in ("network", "quota_retry"):
                if st.retry_attempts >= self.config["max_retries"]:
                    st.status = "attention"
                    return
                st.status = "retry"
                if st.retry_sent is None or now - st.retry_sent >= timedelta(seconds=self.config["retry"]):
                    reason = "usage limit without reset time → continue" if screen.error_kind == "quota_retry" else "network recovery → continue"
                    self._send(st, window, "continue", now, reason, "retry")
                return
            st.status = "attention"
            return
        st.reset_utc = None
        st.last_error = ""
        st.status = "sent" if st.last_sent and now - st.last_sent < timedelta(seconds=5) else "idle"
        # A completed turn can start and finish between polls. Native turn-end
        # stamps are also observed, rather than relying solely on a spinner.
        if screen.completion_id and screen.completion_id != st.completion_id:
            st.seen_running = True
            st.retry_attempts = 0
            st.retry_sent = None
            st.quota_attempts = 0
            st.quota_sent = None
            st.completion_id = screen.completion_id
        prompt = options["after_finish"]
        left = options["loops"]
        if not prompt or left == 0 or not st.seen_running:
            return
        if st.idle_since is None:
            st.idle_since = now
        elif now - st.idle_since >= timedelta(seconds=5):
            self._send(st, window, prompt, now, "after-finish prompt", "after_finish")

    def _publish(self):
        rows = []
        states = list(self.states.values())
        if self.provider == 'codex_app':
            groups = {}
            for st in states:
                if st.thread_id:
                    groups.setdefault(st.thread_id, []).append(st)
            states = [st for st in states if not st.thread_id]
            for tid, members in groups.items():
                states.append(next((st for st in members if st.hwnd == self.primary_windows.get(tid)),
                                   min(members, key=lambda st: st.hwnd)))
        for st in states:
            s = st.screen
            options = self._options(st)
            rows.append({"hwnd": st.hwnd, "title": st.title, "provider": self.provider,
                         "status": st.status, "reset_utc": st.reset_utc,
                         "last_sent_utc": st.last_sent, "retry_last_sent_utc": st.retry_sent,
                         "model": options["model"], "effort": options["effort"],
                         "thread_id": st.thread_id,
                         "identity": st.identity, "thread_hint": st.thread_hint,
                         "current_model": s.model, "current_effort": s.effort,
                         "tabs": len(groups[st.thread_id]) if self.provider == 'codex_app' and st.thread_id else 1,
                         "excluded": options["excluded"],
                         "title_key": st.title, "running": s.running,
                         "prompt": {"kind": "codex_menu"} if s.blocked else None,
                         "read_utc": st.read_at})
        self.latest_rows = rows
        self.snapshot.emit(rows)
