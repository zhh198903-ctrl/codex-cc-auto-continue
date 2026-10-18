"""Codex companion API execution on Auto-Continue's single UIA worker.

The caller never obtains UIA objects or its own keyboard driver. Operation
identities are retained before dispatch; uncertain mutations are not retried.
"""
from __future__ import annotations
from collections import OrderedDict
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import copy
import hashlib
import json
import os
import queue
import re
import secrets
import threading
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from PyQt6.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot
from codex_bridge.backend import CodexBackend, session_key, normalize_rate_limits
from codex_bridge.client import CodexError
from codex_bridge.desktop import NativeDesktopClient
import codex_app
import codex_cli
import native_control
from codex_sessions import session_options
from codex_watcher import State

LEASE_SECONDS = 60
CAPABILITIES = {'codex_control': 1, 'codex_operation_results': 1, 'codex_operation_timing': 1, 'codex_native_multiwindow': 1, 'codex_lease_seconds': LEASE_SECONDS, 'monitor_handoff': 1, 'codex_usage': 1, 'claude_usage': 1, 'codex_recent_sessions': 1}
ACTIONS = {'connect_cli', 'new_session', 'bind', 'unbind', 'scan', 'send', 'interrupt', 'focus', 'approve_once', 'deny', 'pick', 'claim', 'release', 'monitor_hold', 'monitor_release', 'monitor_activate', 'monitor_stop'}
MUTATIONS = {'send', 'interrupt', 'approve_once', 'deny', 'pick'}


def serial(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda x: x.isoformat() if isinstance(x, datetime) else asdict(x)))


def normalize_claude_usage(payload):
    adapted = {}
    for source, name, duration in [('five_hour', 'primary', 300), ('seven_day', 'secondary', 10080)]:
        value = payload.get(source) if isinstance(payload, dict) else None
        if not isinstance(value, dict):
            continue
        try:
            reset = datetime.fromisoformat(value['resets_at'].replace('Z', '+00:00'))
            if reset.tzinfo is not None:
                adapted[name] = {'usedPercent': value.get('utilization'), 'windowDurationMins': duration,
                                 'resetsAt': int(reset.timestamp())}
        except (KeyError, AttributeError, ValueError, OverflowError):
            pass
    result = normalize_rate_limits({'rateLimits': adapted})
    result['provider'] = 'claude'
    return result


def read_claude_usage():
    # Existing user's local Claude Code login only. Never refresh credentials,
    # invoke login, send a chat, or copy tokens into a discovery/result file.
    from pathlib import Path
    token = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
    if not token:
        folder = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        credentials = json.loads((folder / '.credentials.json').read_text(encoding='utf-8')).get('claudeAiOauth') or {}
        expires = credentials.get('expiresAt')
        if type(expires) in (int, float) and expires <= time.time() * 1000:
            raise ValueError('not_logged_in')
        token = credentials.get('accessToken')
    if not isinstance(token, str) or not token:
        raise ValueError('not_logged_in')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None
    request = Request('https://api.anthropic.com/api/oauth/usage',
                      headers={'Authorization': 'Bearer ' + token, 'anthropic-beta': 'oauth-2025-04-20',
                               'Accept': 'application/json', 'User-Agent': 'CodexClaudeControl/0.3.0'}, method='GET')
    with build_opener(NoRedirect()).open(request, timeout=10) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('oversized_usage')
        return normalize_claude_usage(json.loads(raw))


class CodexService(QObject):
    wake = pyqtSignal()
    publish = pyqtSignal(str, object)
    spent = pyqtSignal(str, int)
    rpc_snapshot = pyqtSignal(list)
    native_cli_action = pyqtSignal(dict)

    def __init__(self, cli_watcher, app_watcher, backend=None):
        super().__init__()
        self.cli_watcher, self.app_watcher = cli_watcher, app_watcher
        self.cli = backend or CodexBackend(log=lambda msg: self.publish.emit('log', {'level': 'info', 'msg': msg}))
        self.reader = NativeDesktopClient()
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=64)
        self.operations = OrderedDict()
        self.rows = {}
        self.catalog_limit = 1
        self.recent_desktop_ids = []
        self.leases = {}
        self.bound = set()
        self.native = {}
        self.native_process = {}
        self.tokens = {}
        self.policy = {}
        self.paused = set()
        self.pending_native = {}
        self.native_answered = {}
        self.closing = False
        self.monitors = {'codex': cli_watcher, 'codex_app': app_watcher}
        self.monitor_owner = ''
        self.monitor_deadline = 0.0
        self.monitor_saved = {}
        self.usage_lock = threading.Lock()
        self.usage_cache = {}
        self.usage_cached_at = {}
        self.monitor_engine = None
        self.monitor_export = lambda: {}
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh_guarded)
        self.wake.connect(self.drain)
        self.native_cli_action.connect(self.gui_cli_action)
        self.app_watcher.control_service = self
        self.cli_watcher.control_service = self

    def owns(self, tid, provider='codex'):
        with self.lock:
            sid = ('codex:desktop:' if provider == 'codex_app' else 'codex:') + tid
            return bool(tid and self.leases.get(sid, 0) > time.monotonic())

    def native_cli_owned(self):
        # UIA cannot prove the app-server thread ID of an arbitrary terminal.
        # While RPC is connected, recovery is dispatched by exact RPC identity.
        return self.cli.connected and self.cli.shared

    def native_window_owned(self, hwnd):
        # Redraws can temporarily erase the reader's thread identity. A live
        # claim still suppresses approval/permission switching on its HWND;
        # it never grants permission to execute an action on an unreadable UI.
        with self.lock:
            return any(binding[0] == hwnd and self.leases.get('codex:desktop:'+tid, 0) > time.monotonic()
                       for tid, binding in self.native.items() if tid in self.bound)

    @pyqtSlot()
    def start(self):
        self.detect_control_owner(startup=True)
        self.timer.start()
        self.refresh_guarded()

    @pyqtSlot()
    def refresh_guarded(self):
        if self.closing:
            return
        try:
            self.detect_control_owner()
            self.expire_monitor_hold()
            if not self.jobs.empty():
                # A timer already queued before an HTTP wake must not place
                # another full desktop scan ahead of explicit actions.
                self.drain()
                return
            self.refresh()
        except Exception as exc:
            self.publish.emit('log', {'level':'warn', 'msg':'Codex observation unavailable: '+type(exc).__name__})

    def snapshot(self, recent_limit=None):
        with self.lock:
            if recent_limit is not None:
                if type(recent_limit) is not int or not 1 <= recent_limit <= 100:
                    raise ValueError('bad_recent_limit')
                # The UIA worker expands its metadata observation on the next
                # tick. HTTP readers never run a driver or change automation.
                self.catalog_limit = max(self.catalog_limit, recent_limit)
            recent = self.recent_desktop_ids[:recent_limit] if recent_limit is not None else self.recent_desktop_ids
            rows = [row for row in self.rows.values() if recent_limit is None
                    or row['provider'] != 'codex_exe' or row.get('bound') or row['id'] in recent]
            return {'ts': int(time.time()*1000), 'sessions': copy.deepcopy(rows),
                    'recent_desktop_ids': list(recent),
                    'connected': self.cli.connected, 'error': self.cli.error}

    def usage(self, provider='codex'):
        # HTTP reader threads share a short cache; no GUI tick is blocked and
        # repeated watch refreshes cannot create concurrent metadata readers.
        with self.usage_lock:
            if provider not in {'codex', 'claude'}:
                return {'ok': False, 'reason': 'bad_provider'}
            ttl = 15 if provider == 'codex' else 60
            if provider in self.usage_cache and time.monotonic() - self.usage_cached_at[provider] < ttl:
                return copy.deepcopy(self.usage_cache[provider])
            try:
                result = self.cli.rate_limits() if provider == 'codex' else read_claude_usage()
            except Exception:
                result = {'ok': False, 'provider': provider, 'updated_at': int(time.time()),
                          'five_hour': None, 'weekly': None, 'reason': 'unavailable'}
            self.usage_cache[provider] = result
            self.usage_cached_at[provider] = time.monotonic()
            return copy.deepcopy(result)

    def operation(self, rid):
        with self.lock:
            op = self.operations.get(rid)
            return copy.deepcopy(op['result']) if op else {'ok': False, 'reason': 'no_operation', 'status': 'failed'}

    def submit(self, data, timeout=10):
        if not isinstance(data, dict) or data.get('action') not in ACTIONS or not isinstance(data.get('request_id'), str) or not 1 <= len(data['request_id']) <= 128:
            return {'ok': False, 'reason': 'bad_body', 'status': 'failed'}
        if any(key in data and not isinstance(data[key], str) for key in ('target_id','identity','text','cwd','executable','thread_id','prompt_id','prompt_sig','ask_id','nav','mode','observed_turn_id')) or any(key in data and not isinstance(data[key], bool) for key in ('shared','ephemeral')) or ('n' in data and (not isinstance(data['n'],int) or isinstance(data['n'],bool))) or data.get('mode','') not in {'','default','plan'}:
            return {'ok':False,'reason':'bad_body','status':'failed'}
        data = copy.deepcopy(data)
        rid = data['request_id']
        fingerprint = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self.lock:
            if self.closing:
                return {'ok': False, 'reason': 'stopped', 'status': 'failed'}
            if rid in self.operations:
                if self.operations[rid]['fingerprint'] != fingerprint:
                    return {'ok': False, 'reason': 'request_conflict', 'status': 'failed'}
                return copy.deepcopy(self.operations[rid]['result'])
            if len(self.operations) >= 512:
                # Never evict a mutation identity: an old retry must not become
                # a fresh Send/approval after many lease renewals.
                expired = next((key for key, op in self.operations.items() if not op['mutation'] and op['result']['status'] not in {'pending', 'unknown'}), None)
                if expired is None:
                    return {'ok': False, 'reason': 'busy', 'status': 'failed'}
                self.operations.pop(expired)
            event = threading.Event()
            self.operations[rid] = {'fingerprint': fingerprint, 'event': event, 'queued_at': time.monotonic(), 'mutation': data['action'] in MUTATIONS or data['action']=='new_session',
                                   'result': {'ok': False, 'status': 'pending', 'operation_id': rid}}
            try:
                self.jobs.put_nowait(data)
            except queue.Full:
                self.operations.pop(rid)
                return {'ok': False, 'reason': 'busy', 'status': 'failed'}
        self.wake.emit()
        event.wait(timeout)
        return self.operation(rid)

    @pyqtSlot()
    def drain(self):
        refresh_needed = False
        while not self.jobs.empty() and not self.closing:
            data = self.jobs.get_nowait()
            rid = data['request_id']
            started = time.monotonic()
            try:
                result = self.execute(data)
                result.setdefault('ok', True)
                result.setdefault('status', 'confirmed' if result['ok'] else 'unknown' if result.get('reason') == 'unknown' else 'failed')
            except CodexError as exc:
                unknown = data['action'] in MUTATIONS and exc.reason in {'timeout', 'disconnected', 'unfinished'}
                result = {'ok': False, 'status': 'unknown' if unknown else 'failed', 'reason': 'unknown' if unknown else exc.reason, 'detail': exc.detail}
            except Exception as exc:
                unknown = data['action'] in MUTATIONS
                result = {'ok': False, 'status': 'unknown' if unknown else 'failed', 'reason': 'unknown' if unknown else 'unavailable', 'detail': type(exc).__name__}
            result['operation_id'] = rid
            result['timing'] = {'queue_ms': round(max(0, started - self.operations[rid]['queued_at']) * 1000),
                                'execution_ms': round(max(0, time.monotonic() - started) * 1000)}
            if result['status'] == 'unknown':
                self.paused.add(data.get('target_id', ''))
            with self.lock:
                self.operations[rid]['result'] = result
                self.operations[rid]['event'].set()
            self.publish.emit('codex_action', result)
            # Renewals only change a deadline, and scan/connect already
            # refresh in execute. Never insert N native observations between
            # N queued operations. Every mutation retains its live guards.
            refresh_needed |= data['action'] not in {'claim', 'scan', 'connect_cli'}
        if refresh_needed and not self.closing:
            self.refresh_guarded()

    def execute(self, data):
        action, sid = data['action'], data.get('target_id', '')
        if action.startswith('monitor_'):
            return self.monitor_action(data)
        if self.monitor_owner and action not in {'scan', 'release', 'unbind'}:
            raise CodexError('monitor_paused', '独立 Auto-Continue 已暂停，由 Control 内置副本执行')
        if action == 'connect_cli':
            self.cli.connect(shared=data.get('shared', True), executable=data.get('executable', ''))
            if self.cli.shared:
                self.cli.refresh()
            self.refresh()
            for row in self.rows.values():
                if row['provider'] == 'codex_cli':
                    self.leases[row['id']] = time.monotonic() + LEASE_SECONDS
            return {'ok': True}
        if action == 'new_session':
            sid = self.cli.new_session(data.get('cwd', ''), '', executable=data.get('executable', ''), ephemeral=data.get('ephemeral', False))
            self.leases[sid] = time.monotonic()+LEASE_SECONDS
            if data.get('text', '').strip():
                self.cli.send(sid, data['text'])
            return {'ok': True, 'target_id': sid}
        if action == 'bind':
            tid = data.get('thread_id', '')
            record = self.app_watcher.activities.thread(tid)
            if record is None:
                raise CodexError('gone')
            candidates = []
            for hwnd in codex_app.terminal_handles():
                window = codex_app.window_from_handle(hwnd)
                view = codex_app.read_text(window)
                if view and view.screen.identified and view.title == record['title'] and self.app_watcher.activities.thread_id_for_title(view.title) == tid:
                    candidates.append((window, view))
            if not candidates:
                raise CodexError('desktop_not_visible', '请打开这个 Codex 聊天；同名聊天需先改为唯一标题再绑定')
            # A unique full thread can be open in several GUI processes. Keep
            # its existing GUI; enumeration/foreground order grants no rebind.
            previous = self.native.get(tid)
            selected = next((item for item in candidates if previous and
                             (item[0].hwnd, item[1].identity, item[1].thread_hint) == previous), None)
            if previous and selected is None:
                raise CodexError('changed', '原绑定窗口已切换或关闭，请先解除绑定再选择窗口')
            window, view = selected or min(candidates, key=lambda item: item[0].hwnd)
            owner = codex_app.process_identity(window.hwnd)
            fresh = codex_app.read_text(window)
            if (owner is None or codex_app.process_identity(window.hwnd) != owner or
                    fresh is None or not fresh.screen.identified or
                    (fresh.identity, fresh.thread_hint) != (view.identity, view.thread_hint)):
                raise CodexError('changed')
            if selected:
                if self.native_process.get(tid) != owner:
                    raise CodexError('changed')
                self.leases['codex:desktop:'+tid] = time.monotonic()+LEASE_SECONDS
                return {'ok': True, 'target_id': 'codex:desktop:'+tid, 'hwnd': window.hwnd, 'window_count': len(candidates)}
            completed = getattr(self.app_watcher.activities, 'completed_turn', None)
            baseline_completion = completed(tid) if completed else ''
            self.bound.add(tid)
            self.leases['codex:desktop:'+tid] = time.monotonic()+LEASE_SECONDS
            self.native[tid] = (window.hwnd, view.identity, view.thread_hint)
            self.native_process[tid] = owner
            self.app_watcher.primary_windows[tid] = window.hwnd
            self.app_watcher.states[window.hwnd] = State(window.hwnd, view.title, identity=view.identity,
                thread_id=tid, thread_hint=view.thread_hint, screen=view.screen, initialized=True,
                completion_id=baseline_completion or view.screen.completion_id, read_at=datetime.now(timezone.utc))
            self.app_watcher.session_bindings[window.hwnd] = (view.identity, view.thread_hint, tid)
            # Keep the user's automation setting: binding starts observation,
            # but does not turn recovery/after-finish on when they were stopped.
            return {'ok': True, 'target_id': 'codex:desktop:'+tid, 'hwnd': window.hwnd, 'window_count': len(candidates)}
        tid = sid.removeprefix('codex:desktop:') if sid.startswith('codex:desktop:') else sid.removeprefix('codex:')
        if action == 'unbind':
            self.bound.discard(tid)
            self.leases.pop(sid, None)
            previous = self.native.pop(tid, None)
            self.native_process.pop(tid, None)
            if previous and self.app_watcher.session_bindings.get(previous[0]) == (previous[1], previous[2], tid):
                self.app_watcher.session_bindings.pop(previous[0], None)
            return {'ok': True}
        if action == 'claim':
            if sid not in self.rows:
                raise CodexError('gone')
            self.leases[sid] = time.monotonic()+LEASE_SECONDS
            return {'ok': True, 'lease_seconds': LEASE_SECONDS}
        if action == 'release':
            self.leases.pop(sid, None)
            if sid in self.pending_native:
                self._finish_native(sid, False, 'cancelled')
            return {'ok': True}
        if action == 'scan' and not sid:
            self.refresh()
            return {'ok': True, **self.snapshot()}
        row = self.rows.get(sid)
        if row is None:
            raise CodexError('gone')
        if action == 'scan':
            self.refresh()
            return {'ok': True, 'session': self.rows.get(sid)}
        if not self.owns(tid, 'codex_app' if row['provider']=='codex_exe' else 'codex'):
            raise CodexError('not_claimed')
        if sid in self.paused and action != 'interrupt':
            raise CodexError('unknown', '先核对聊天中的上一条操作；结果不确定时不再发送')
        if data.get('identity') != row['identity']:
            raise CodexError('changed')
        if row['provider'] == 'codex_cli':
            if action == 'focus':
                raise CodexError('no_window', 'Codex CLI API 会话没有桌面窗口')
            if action == 'send':
                options=session_options(self.cli_watcher.config,tid,row['name'])
                continued=self.policy.get(tid,{}).get('continued',{})
                if continued and data.get('text')==continued['text'] and (data.get('observed_turn_id')==continued['source_turn'] or time.monotonic()-continued['at']<3):
                    return {'ok':True,'deduplicated':True}
                with self.cli.lock:
                    thread=copy.deepcopy(self.cli.threads.get(tid,{}))
                consumes = data.get('text','')==options['after_finish'] and self._finish_due(tid,thread,options)
                self.cli.send(sid, data.get('text', ''),options['model'],options['effort'],data.get('mode',''))
                self.policy.setdefault(tid, {})['armed'] = False
                if consumes:
                    self._spend_loop(tid,options)
                    self._remember_continuation(tid,thread,options)
            elif action == 'interrupt':
                self.cli.interrupt(sid)
            elif action in {'approve_once', 'deny'}:
                if not row.get('prompt') or row['prompt']['id'] != data.get('prompt_id'):
                    raise CodexError('no_prompt')
                self.cli.answer(data['prompt_id'], action == 'approve_once')
            elif action == 'pick':
                self.cli.pick(sid, data.get('ask_id', ''), data.get('n', 0), data.get('nav', ''), data.get('text', ''))
            return {'ok': True}
        if action == 'send' and tid in self.bound:
            bound = self.native.get(tid)
            state = self.app_watcher.states.get(bound[0]) if bound else None
            # Joining an already confirmed continuation performs no input.
            # Preserve this result across a native redraw, but only for the
            # same full thread, original binding, prompt and observed turn.
            if (state and state.thread_id == tid and state.identity == bound[1] == data['identity']
                    and state.thread_hint == bound[2] and state.continued_completion
                    and data.get('text') == state.continued_text
                    and data.get('observed_turn_id') == state.continued_completion):
                return {'ok': True, 'deduplicated': True}
        window, view, st = self._visible(tid)
        if view.identity != data['identity']:
            raise CodexError('changed')
        if action == 'pick' and (row.get('ask') or {}).get('id') != data.get('ask_id'):
            raise CodexError('no_prompt')
        if action in {'approve_once','deny'} and (row.get('prompt') or {}).get('id') != data.get('prompt_id'):
            raise CodexError('no_prompt')
        if action == 'send':
            if not data.get('text', '').strip():
                raise CodexError('no_lines')
            recent=bool(st.last_sent and 0<=(datetime.now(timezone.utc)-st.last_sent).total_seconds()<3)
            if st.continued_completion and data.get('text')==st.continued_text and (data.get('observed_turn_id')==st.continued_completion or recent):
                return {'ok':True,'deduplicated':True}
            if st.submission is not None:
                raise CodexError('busy')
            options=self.app_watcher._options(st)
            consumes=bool(self.app_watcher.running and st.seen_running and options['after_finish']==data['text'] and options['loops']!=0 and not view.screen.running and not view.screen.blocked and not view.screen.error_kind and view.screen.completion_id)
            sent = self.app_watcher._send(st, window, data['text'], datetime.now(timezone.utc), 'companion send', 'companion_after_finish' if consumes else 'companion')
            if st.submission is not None:
                self.pending_native[sid] = data['request_id']
                return {'ok': False, 'status': 'pending'}
            # A held attempt can leave the previous turn's status as "sent".
            # Confirm only this invocation, never an earlier submission.
            if not sent:
                raise CodexError('held', '聊天运行中、有草稿或原生界面未准备好')
            return {'ok': True}
        result = native_control.act(window, view.identity, view.thread_hint, action,
            data.get('prompt_sig', ''), data.get('n', 0), data.get('text', ''), data.get('nav', ''))
        if result.get('ok') and action in {'approve_once', 'deny'}:
            self.native_answered[tid] = data.get('prompt_sig', '')
            # A redraw or a permission-mode suggestion can hide the composer
            # after a successful answer. Never retain that answered card as
            # a new waiting approval during the unreadable interval.
            with self.lock:
                if sid in self.rows:
                    self.rows[sid].update(prompt=None, ask=None, prompt_sig='')
        return result

    def _visible(self, tid):
        bound = self.native.get(tid)
        if bound is None or tid not in self.bound:
            raise CodexError('desktop_not_bound')
        window = codex_app.window_from_handle(bound[0])
        owner = self.native_process.get(tid)
        if owner is not None and codex_app.process_identity(bound[0]) != owner:
            raise CodexError('changed', '绑定窗口的进程已经变化')
        view = native_control._read_stable(window) if window else None
        if window and view is not None and not view.identity:
            # An identified redraw without its chat identity still grants no
            # input. Re-observe once and keep the original full binding below.
            view = native_control._read_stable(window)
        if window and (view is None or view.screen.identified and not view.identity):
            raise CodexError('unreadable', '绑定窗口的原生界面暂时不可读')
        if view is None or not view.screen.identified or (view.identity, view.thread_hint) != bound[1:]:
            raise CodexError('changed', '绑定的 Codex 聊天当前不可见或已经切换')
        if owner is not None and codex_app.process_identity(bound[0]) != owner:
            raise CodexError('changed', '绑定窗口的进程已经变化')
        st = self.app_watcher.states.get(window.hwnd)
        if st is None or st.thread_id != tid:
            raise CodexError('changed')
        window.expected_identity, window.expected_thread_hint = view.identity, view.thread_hint
        window.expected_thread_id = tid
        st.screen = view.screen
        return window, view, st

    @pyqtSlot()
    def refresh(self):
        if self.closing:
            return
        rows = {}
        indexes, states = self.cli.snapshot()
        for index in indexes:
            tid=index.session_id.removeprefix('codex:')
            self.tokens[tid]=self.tokens.get(tid,0)+states[index.session_id].get('tokens',0)
            with self.cli.lock:
                thread=copy.deepcopy(self.cli.threads.get(tid,{}))
            self._automatic_cli(tid,thread)
        # Automatic recovery/approval can change a thread during this refresh.
        # Publish the resulting state rather than an idle pre-action snapshot.
        indexes, states = self.cli.snapshot()
        for index in indexes:
            state = states[index.session_id]
            tid = index.session_id.removeprefix('codex:')
            self.tokens[tid] = self.tokens.get(tid, 0) + state.get('tokens', 0)
            with self.cli.lock:
                thread = copy.deepcopy(self.cli.threads.get(tid, {}))
            ask = self.cli.ask(index.session_id, -1)
            rows[index.session_id] = {'id': index.session_id, 'provider': 'codex_cli', 'thread_id': tid,
                'identity': tid, 'name': index.name, 'cwd': index.cwd, 'updated': index.updated_at,
                'turn_id':thread.get('turn_id') or thread.get('completed_turn_id',''),
                'status': 'waiting' if state['waiting'] or state['prompt'] else 'busy' if state['running'] else 'idle',
                'prompt': serial(state['prompt']) if state['prompt'] else None, 'ask': ask.payload if ask else None,
                'last': state['last'], 'tokens': self.tokens[tid], 'capabilities': ['send','interrupt','answer','pick','context']}
        with self.lock:
            recent_limit = max(self.app_watcher.config['recent_limit'], self.catalog_limit)
        recent, error = self.app_watcher.activities.read(recent_limit) if self.app_watcher.activities is not None else ([], '')
        recent_ids = ['codex:desktop:'+item['thread_id'] for item in recent]
        # An explicitly bound chat remains addressable after it falls off the
        # recent page. Listing older metadata must never acquire a lease.
        for tid in list(self.bound):
            if 'codex:desktop:'+tid not in recent_ids:
                reader = getattr(self.app_watcher.activities, 'thread', None)
                item = reader(tid) if reader else None
                if item:
                    recent.append(dict(thread_id=tid, title=item['title'], cwd=item.get('cwd', '')))
        for item in recent:
            tid = item['thread_id']
            sid = 'codex:desktop:'+tid
            row = {'id': sid, 'provider': 'codex_exe', 'thread_id': tid, 'identity': '', 'name': '桌面 · '+item['title'],
                   'cwd': item.get('cwd', ''), 'updated': 0, 'status': 'unknown', 'prompt': None, 'ask': None,
                   'last': '', 'tokens': 0, 'capabilities': [], 'bound': tid in self.bound}
            if tid in self.bound and tid in self.native:
                row['identity'] = self.native[tid][1]
                row['turn_id'] = (self.rows.get(sid) or {}).get('turn_id', '')
            if tid in self.bound:
                try:
                    window, view, st = self._visible(tid)
                    if sid in self.pending_native and not self.monitor_owner:
                        if not self.owns(tid,'codex_app'):
                            self._finish_native(sid, False, 'cancelled')
                        elif st.submission is not None:
                            invoked = getattr(st.submission, 'invoked_at', None)
                            # A slow UIA read may consume the whole receipt
                            # window while the exact user turn is already in
                            # the rollout. Observe once before expiring it;
                            # an invoked companion Send is never clicked twice.
                            self.app_watcher._observe(st, window, datetime.now(timezone.utc))
                            if st.submission is None and sid in self.pending_native:
                                self._finish_native(sid, False, 'unknown' if st.status == 'unknown' else 'held')
                            elif (sid in self.pending_native and invoked is not None
                                  and time.monotonic()-invoked > 10):
                                self._finish_native(sid, False, 'unknown')
                    value = native_control.public_prompt(native_control.prompt(window, observed_view=view))
                    if value and self.native_answered.get(tid) == value['sig']:
                        value = None
                    row.update(identity=view.identity, hwnd=window.hwnd, turn_id=st.completion_id or view.screen.completion_id, status='waiting' if value or view.screen.blocked else 'busy' if view.screen.running else 'idle', capabilities=['send','interrupt','focus','answer','pick','context'])
                    if value:
                        previous = (self.rows.get(sid) or {}).get('prompt') or {}
                        same=bool(previous and (self.rows.get(sid) or {}).get('prompt_sig')==value['sig'])
                        pid = previous['id'] if same else 'cxp:'+session_key(sid)+':'+secrets.token_hex(12)
                        created = previous.get('created_at',time.time()) if previous.get('id')==pid else time.time()
                        row['prompt'] = {'id': pid, 'session_id': sid, 'tool': 'Codex 授权' if value['kind']=='perm' else 'Codex 提问', 'hint': value['text'][:319], 'created_at': created, 'kind': 'codex'}
                        row['ask'] = {'id': 'q:'+session_key(sid)+':desktop:'+pid.rsplit(':',1)[-1], 'k':session_key(sid), 'i':-1,
                                      'kind':value['kind'], 'cur':0,'esc':1,'t':'Codex','q':value['text'][:240],
                                      'o':[{'l':o['label'], **({'c':int(o['check'])} if o['check'] is not None else {}), **({'in':1} if o.get('input') else {})} for o in value['options']]}
                        row['prompt_sig'] = value['sig']
                except Exception as exc:
                    if isinstance(exc, CodexError) and exc.reason == 'unreadable':
                        previous = self.rows.get(sid) or {}
                        if previous.get('prompt'):
                            # Retain the same read-only card during unmounting.
                            # Every mutation still calls _visible and rechecks
                            # the live card signature; a real switch clears it.
                            row.update(status='waiting', prompt=previous['prompt'],
                                ask=previous.get('ask'), prompt_sig=previous.get('prompt_sig',''),
                                visibility='unreadable')
                    if sid in self.pending_native:
                        self._finish_native(sid, False, 'unknown')
                    pass
            rows[sid] = row
        with self.lock:
            self.rows = rows
            self.recent_desktop_ids = recent_ids
        rpc_rows = []
        for row in rows.values():
            if row['provider'] != 'codex_cli':
                continue
            options = session_options(self.cli_watcher.config, row['thread_id'], row['name'])
            rpc_rows.append(dict(hwnd=int(hashlib.sha256(row['id'].encode()).hexdigest()[:12],16)+2**48,
                title=row['name'], provider='codex', thread_id=row['thread_id'], rpc=True,
                status=row['status'], running=row['status']=='busy', identity=row['identity'],
                title_key=row['name'], model=options['model'], effort=options['effort'],
                current_model='',current_effort='',excluded=options['excluded'],
                reset_utc=None,last_sent_utc=None,tabs=1,prompt=row['prompt']))
        if self.cli.connected:
            self.rpc_snapshot.emit(rpc_rows)
        self.publish.emit('codex_snapshot', self.snapshot())

    def confirmed_native(self, st):
        sid = 'codex:desktop:'+st.thread_id
        if sid in self.pending_native:
            self._finish_native(sid, True)

    def _finish_native(self, sid, ok, reason=''):
        rid = self.pending_native.pop(sid, None)
        if rid is None:
            return
        result = {'ok':ok,'status':'confirmed' if ok else 'unknown' if reason=='unknown' else 'failed', 'operation_id':rid}
        if reason:
            result['reason']=reason
        if reason=='unknown':
            self.paused.add(sid)
        if not ok:
            tid=sid.removeprefix('codex:desktop:')
            bound=self.native.get(tid)
            st=self.app_watcher.states.get(bound[0]) if bound else None
            if st is not None:
                st.submission=None
                st.seen_running=False
        with self.lock:
            result['timing'] = dict(self.operations[rid]['result'].get('timing', {}),
                                  total_ms=round(max(0, time.monotonic() - self.operations[rid]['queued_at']) * 1000))
            self.operations[rid]['result']=result
            self.operations[rid]['event'].set()
        self.publish.emit('codex_action',result)

    @pyqtSlot(dict)
    def gui_cli_action(self, data):
        sid=data['target_id']
        tid=sid.removeprefix('codex:')
        if data['action'] in {'skip','cooldown'}:
            self.policy.setdefault(tid,{})['armed']=False
            return
        try:
            row=self.rows.get(sid,{})
            options=session_options(self.cli_watcher.config,tid,row.get('name',''))
            self.cli.send(sid,'continue',options['model'],options['effort'])
            self.policy.setdefault(tid,{})['armed']=False
        except CodexError as exc:
            self.publish.emit('log',{'level':'warn','msg':'Codex RPC action held: '+exc.reason})
        self.refresh()

    def _automatic_cli(self, tid, thread):
        if self.monitor_owner or not self.cli_watcher.running:
            return
        if not self.cli_watcher.running:
            return
        state = self.policy.setdefault(tid, {})
        started=thread.get('started_turn_id','')
        if started and state.get('seen_start')!=started:
            state.update(seen_start=started,armed=True,turn=started)
        active = (thread.get('status') or {}).get('type') == 'active'
        turn = thread.get('turn_id', '')
        if active:
            state.update(armed=True, turn=turn)
        if 'codex:'+tid in self.paused:
            return
        title = 'Codex · '+str(thread.get('name') or __import__('pathlib').Path(thread.get('cwd') or '.').name or tid[-8:])
        options = session_options(self.cli_watcher.config, tid, title)
        if options['excluded']:
            state['armed']=False
            return
        pending = [r for r in self.cli.pending.values() if r['tid']==tid]
        if pending:
            # Leases suspend configured permission auto-answering only. On
            # release/expiry restore that setting without changing it.
            if not self.owns(tid) and self.cli_watcher.config['auto_approve'] and not self.cli_watcher.config['dry_run']:
                from codex_bridge.backend import APPROVALS
                request=pending[0]
                if request['method'] in APPROVALS:
                    try:
                        self.cli.answer(request['id'],True)
                    except CodexError as exc:
                        if exc.reason in {'timeout','disconnected','unfinished'}:
                            self.paused.add('codex:'+tid)
            return
        if active:
            return
        if thread.get('turn_final')=='failed' and thread.get('turn_error'):
            error=thread['turn_error']
            message=json.dumps(error,ensure_ascii=False).lower()
            # Numeric status codes must be labelled. A reset timestamp or a
            # thread ID containing 401/403/429 is not an HTTP error code.
            statuses={int(code) for code in re.findall(r'\b(?:http(?:/\d\.\d)?|(?:http)?status(?:code)?)\b["\s:=\-]+(\d{3})\b',message)}
            if re.search(r'authentication|unauthorized|invalid.api.key|billing|credits',message) or statuses.intersection({401,403}):
                state.update(armed=False,attention=True)
                return
            quota=bool(re.search(r'usage.limit|rate.limit|quota',message) or 429 in statuses)
            network=bool(re.search(r'network|stream.*disconnect|connection|server.error|servererror|httpconnectionfailed',message) or statuses.intersection({502,503,504}))
            if not quota and not network:
                state.update(armed=False,attention=True)
                return
            if state.get('retries',0)>=self.cli_watcher.config['max_retries']:
                state.update(armed=False,attention=True)
                return
            now=datetime.now(timezone.utc)
            def reset(value):
                if isinstance(value,dict):
                    for key,item in value.items():
                        if key in {'resets_at','reset_at'} and isinstance(item,(int,float)):
                            return datetime.fromtimestamp(item,timezone.utc)
                        found=reset(item)
                        if found: return found
                return None
            due=reset(error) if quota else None
            if due is None and quota:
                due=codex_cli._reset_time(error.get('message',''),now)
            if due is not None:
                due+=timedelta(seconds=self.cli_watcher.config['buffer'])
                if now<due: return
            if time.monotonic()<state.get('next_retry',0):
                return
            if self.cli_watcher.config['dry_run']:
                return
            state['armed']=False
            try:
                self.cli.send('codex:'+tid,'continue',options['model'],options['effort'])
                state['retries']=state.get('retries',0)+1
                state['next_retry']=time.monotonic()+self.cli_watcher.config['retry']
                self.publish.emit('log',{'level':'fire','msg':'Codex RPC automatic recovery submitted: '+str(state['retries'])})
            except CodexError as exc:
                if exc.reason in {'timeout','disconnected','unfinished'}:
                    self.paused.add('codex:'+tid)
            return
        if thread.get('turn_final')=='completed':
            state['retries']=0
            state['attention']=False
        if not self._finish_due(tid,thread,options):
            return
        state['armed'] = False
        if self.cli_watcher.config['dry_run']:
            return
        try:
            self.cli.send('codex:'+tid, options['after_finish'], options['model'], options['effort'])
            self._spend_loop(tid,options)
            self._remember_continuation(tid,thread,options)
        except CodexError as exc:
            if exc.reason in {'timeout','disconnected','unfinished'}:
                self.paused.add('codex:'+tid)
            self.publish.emit('log', {'level':'warn','msg':'Codex automatic continuation held: '+exc.reason})

    def _finish_due(self,tid,thread,options):
        state=self.policy.get(tid,{})
        return bool(self.cli_watcher.running and options['after_finish'] and options['loops']!=0 and
                    not options['excluded'] and state.get('armed') and
                    state.get('turn')==thread.get('completed_turn_id') and thread.get('turn_final')=='completed')

    def _spend_loop(self,tid,options):
        if options['loops']>0:
            self.cli_watcher.config['sessions'].setdefault(tid,{})['loops']=options['loops']-1
            self.spent.emit(tid,options['loops']-1)

    def _remember_continuation(self,tid,thread,options):
        self.policy.setdefault(tid,{})['continued']={'source_turn':thread['completed_turn_id'],'text':options['after_finish'],'at':time.monotonic()}

    def context(self, sid, before=-1, after=-1, compact=False):
        if not sid.startswith('codex:desktop:'):
            if compact:
                return self.cli.context(sid, before, after, compact=True)
            return self.cli.context(sid, before, after)
        tid = sid.removeprefix('codex:desktop:')
        if tid not in self.bound:
            return {'items': [], 'more':False, 'newer':False}
        # Read only the exact indexed local rollout. App-tools interfaces can
        # disappear on upgrade, and multiple GUI windows share this history.
        return self.app_watcher.activities.context(tid, before, after)

    def close(self):
        with self.lock:
            self.closing = True
            for op in self.operations.values():
                if op['result']['status']=='pending':
                    op['result'].update(status='failed',reason='stopped')
                    op['event'].set()
        self.leases.clear()
        self.cli.close()
        if self.monitor_engine is not None:
            self.monitor_engine.close()

    def monitor_state(self):
        with self.lock:
            return {'paused': bool(self.monitor_owner), 'owner': self.monitor_owner,
                    'running': {name: bool(getattr(m, '_running', getattr(m, 'running', False)))
                                for name, m in self.monitors.items()}}

    @pyqtSlot()
    def start_claude(self): self.start_monitor('claude')

    @pyqtSlot()
    def start_codex(self): self.start_monitor('codex')

    @pyqtSlot()
    def start_codex_app(self): self.start_monitor('codex_app')

    def start_monitor(self, name):
        if not self.monitor_owner: self.monitors[name].start()

    def detect_control_owner(self, startup=False):
        if os.environ.get('AUTO_CONTINUE_CONTROL_OWNER_PID'):
            return  # This is the managed replacement itself.
        from pathlib import Path
        path = Path(os.environ.get('CONTROL_OWNER_FILE') or
                    str(Path(os.environ.get('APPDATA') or Path.home()) / 'CodexClaudeControl/control_owner.json'))
        try:
            info = json.loads(path.read_text(encoding='utf-8'))
            if info.get('owner') == self.monitor_owner:
                return
            probe = ProcessLifetime(info['engine_pid'])
            alive = probe.alive()
            probe.close()
            if not alive: return
            self.monitor_action(dict(action='monitor_hold', owner=info['owner'], engine_pid=info['engine_pid']))
            if startup:
                values = self.monitor_export()
                if str(values.get('autostart', False)).lower() in {'true', '1'}:
                    target, surface = values.get('watch_targets', 'claude'), values.get('codex_surfaces', 'cli')
                    self.monitor_saved = {'claude': target in {'claude', 'both'},
                        'codex': target in {'codex', 'both'} and surface in {'cli', 'all'},
                        'codex_app': target in {'codex', 'both'} and surface in {'app', 'all'}}
        except (OSError, ValueError, KeyError, TypeError, CodexError):
            return

    def monitor_action(self, data):
        owner = data.get('owner', '')
        if not isinstance(owner, str) or not owner or len(owner) > 128:
            raise CodexError('bad_body')
        kind = data['action']
        if kind == 'monitor_hold':
            pid = data.get('engine_pid')
            if pid is not None and (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
                raise CodexError('bad_body')
            if self.monitor_owner and self.monitor_owner != owner:
                raise CodexError('monitor_owned')
            if not self.monitor_owner:
                self.monitor_saved = self.monitor_state()['running']
                self.monitor_owner = owner
                for monitor in self.monitors.values():
                    monitor.stop()
                self.publish.emit('monitor', self.monitor_state())
            if pid is not None:
                if self.monitor_engine is None:
                    self.monitor_engine = ProcessLifetime(pid)
                elif self.monitor_engine.pid != pid:
                    raise CodexError('monitor_owned')
            self.monitor_deadline = time.monotonic() + LEASE_SECONDS
            settings = self.monitor_export()
            settings.update(codex_config=json.dumps(self.cli_watcher.config),
                            codex_app_config=json.dumps(self.app_watcher.config))
            return {'ok': True, 'lease_seconds': LEASE_SECONDS,
                    'running': copy.deepcopy(self.monitor_saved), 'settings': settings}
        if kind == 'monitor_release':
            budgets = data.get('budgets') or {}
            if not isinstance(budgets, dict) or any(not isinstance(v, dict) for v in budgets.values()):
                raise CodexError('bad_body')
            if self.monitor_owner != owner:
                raise CodexError('monitor_owned')
            if self.monitor_engine is not None and self.monitor_engine.alive():
                raise CodexError('engine_running')
            self.resume_monitors(budgets)
            return {'ok': True}
        if kind == 'monitor_activate':
            if self.monitor_owner:
                raise CodexError('monitor_paused')
            requested = data.get('running')
            if not isinstance(requested, dict) or set(requested) - set(self.monitors) or any(
                    not isinstance(v, bool) for v in requested.values()):
                raise CodexError('bad_body')
            for name, monitor in self.monitors.items():
                (monitor.start if requested.get(name, False) else monitor.stop)()
            return {'ok': True, **self.monitor_state()}
        if kind == 'monitor_stop':
            for monitor in self.monitors.values(): monitor.stop()
            return {'ok': True, **self.monitor_state()}
        raise CodexError('bad_body')

    def expire_monitor_hold(self):
        if self.monitor_owner and time.monotonic() >= self.monitor_deadline:
            # An expired heartbeat must never resume the independent driver
            # while its replacement still exists. The held process HANDLE
            # identifies that exact lifetime, even if Windows reuses its PID.
            if self.monitor_engine is None or not self.monitor_engine.alive():
                self.resume_monitors({})

    def resume_monitors(self, budgets):
        saved = self.monitor_saved
        self.policy.clear()
        self.monitor_owner = ''
        self.monitor_saved = {}
        if self.monitor_engine is not None:
            self.monitor_engine.close()
            self.monitor_engine = None
        for name, monitor in self.monitors.items():
            # Start from a fresh observation: an old completed turn must not
            # trigger a second continuation when the independent app resumes.
            states = getattr(monitor, 'states', getattr(monitor, '_states', {}))
            if name in {'codex', 'codex_app'}:
                transfer = budgets.get(name) or {}
                expected, current = transfer.get('expected') or {}, transfer.get('current') or {}
                from codex_watcher import clean_config
                expected, current = clean_config(expected), clean_config(current)
                for tid in current.get('sessions', {}):
                    state = next((st for st in states.values() if st.thread_id == tid), None)
                    title = state.title if state else ''
                    before = session_options(expected, tid, title)
                    actual = session_options(monitor.config, tid, title)
                    after = session_options(current, tid, title)
                    if before == actual and after['after_finish'] == before['after_finish'] and 0 <= after['loops'] <= before['loops']:
                        monitor.config['sessions'].setdefault(tid, {})['loops'] = after['loops']
                        monitor.session_loops_spent.emit(tid, after['loops'])
            states.clear()
            if saved.get(name): monitor.start()
        self.publish.emit('monitor', self.monitor_state())


class ProcessLifetime:
    def __init__(self, pid):
        import ctypes
        from ctypes import wintypes
        self.pid = pid
        self.kernel = ctypes.windll.kernel32
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.OpenProcess(0x100000, False, pid)
        if not self.handle: raise CodexError('gone')

    def alive(self):
        return self.handle is not None and self.kernel.WaitForSingleObject(self.handle, 0) == 258

    def close(self):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
