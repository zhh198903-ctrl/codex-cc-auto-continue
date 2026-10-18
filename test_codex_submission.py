"""Regression checks for native disabled Send and confirmed submission."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import QCoreApplication
import codex_app as app
from codex_watcher import CodexWatcher, State
from test_codex_app import FIXTURES, Node, replay


class NativeSurface:
    """A controllable native view: Invoke can succeed without submitting."""
    def __init__(self):
        self.value = ''
        self.writes = self.clicks = 0
        self.ack_on_click = True
        self.foreground = 123
        self.window = SimpleNamespace(hwnd=123, expected_identity='chat',
                                      control=SimpleNamespace(SetActive=lambda: None))
        self.pattern = self
        self.composer = SimpleNamespace(GetValuePattern=lambda: self.pattern,
                                        SetFocus=lambda: None)
        self.button = SimpleNamespace(IsEnabled=True, IsOffscreen=False, ClassName='',
            GetPropertyValue=lambda _: '', GetInvokePattern=lambda: SimpleNamespace(Invoke=self.invoke))
        self.view = app.View('chat', 'probe', app.Screen(identified=True, ready=True),
                             self.composer, self.button, user_turn='old', user_text='earlier')
        self.user = SimpleNamespace(GetForegroundWindow=lambda: self.foreground,
                                    SetForegroundWindow=lambda _: None)

    @property
    def Value(self):
        return self.value

    def SetValue(self, value):
        self.writes += 1
        self.value = value

    def invoke(self):
        self.clicks += 1
        if self.ack_on_click:
            self.ack()

    def ack(self):
        self.view.user_text = self.value
        self.view.user_turn = 'new'
        self.value = '随心输入\n'

    def screen(self, **kwargs):
        self.view.screen = replace(self.view.screen, **kwargs)


class PendingSubmission(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.surface = NativeSurface()
        patch.object(app, 'read_text', side_effect=lambda _: self.surface.view).start()
        patch.object(app.ctypes, 'windll', SimpleNamespace(user32=self.surface.user)).start()
        patch.object(app.time, 'sleep', return_value=None).start()
        # These cases isolate Send/ack behavior; native permission switching
        # is exercised separately against the captured App picker and probe.
        patch.object(app, 'apply_session_permissions', return_value=True).start()
        self.addCleanup(patch.stopall)
        self.watcher = CodexWatcher(app, provider='codex_app', label='Codex App')
        self.watcher.config.update(poll=1, retry=5, buffer=20, max_retries=1)
        self.state = State(123, 'probe', identity='chat')
        self.now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        self.watcher.now = lambda: self.now

    def observe(self, seconds=0):
        self.state.screen = self.surface.view.screen
        self.watcher._observe(self.state, self.surface.window, self.now + timedelta(seconds=seconds))

    def stage(self):
        return app.send_prompt(self.surface.window, 'continue')

    def test_exact_uia_send_restores_another_windows_foreground(self):
        self.surface.foreground = 461102
        def forbidden():
            raise AssertionError('Native send must not explicitly activate a window')
        self.surface.window.control.SetActive = forbidden
        self.surface.composer.SetFocus = lambda: setattr(self.surface, 'foreground', 123)
        self.surface.user.SetForegroundWindow = lambda hwnd: setattr(self.surface, 'foreground', hwnd.value)
        outcome = self.stage()
        self.assertTrue(outcome)
        self.assertEqual((self.surface.writes, self.surface.clicks), (1, 1))
        self.assertEqual(self.surface.foreground, 461102)

    def test_explicit_native_actions_never_fall_back_to_coordinate_clicks(self):
        control = SimpleNamespace(IsEnabled=True, IsOffscreen=False,
                                  Click=lambda: self.fail('No coordinate input allowed'))
        self.assertFalse(app._act(control, patterns_only=True))

    def test_focus_redraw_is_read_only_then_stages_and_sends_once(self):
        view = self.surface.view
        samples = [view, None, app.View('', 'probe', app.Screen(identified=True)), view]
        def read(_): return samples.pop(0) if samples else self.surface.view
        with patch.object(app, 'read_text', side_effect=read):
            self.assertTrue(self.stage())
        self.assertEqual((self.surface.writes, self.surface.clicks), (1, 1))

    def test_preflight_redraw_waits_for_exact_ready_identity_before_focus_or_write(self):
        samples = [None, app.View('', 'probe', app.Screen(identified=True)), self.surface.view]
        def read(_): return samples.pop(0) if samples else self.surface.view
        with patch.object(app, 'read_text', side_effect=read):
            self.assertTrue(self.stage())
        self.assertEqual((self.surface.writes, self.surface.clicks), (1, 1))

    def test_preflight_redraw_known_wrong_identity_never_focuses_or_writes(self):
        focus = unittest.mock.Mock()
        self.surface.composer.SetFocus = focus
        changed = replace(self.surface.view, identity='another')
        with patch.object(app, 'read_text', side_effect=[None, changed]) as read:
            self.assertEqual(self.stage().status, 'held')
        self.assertEqual(read.call_count, 2)
        focus.assert_not_called()
        self.assertEqual((self.surface.writes, self.surface.clicks), (0, 0))

    def test_focus_redraw_stops_on_known_changed_draft_running_or_blocked_view(self):
        for reason in ['identity', 'thread', 'draft', 'running', 'blocked']:
            with self.subTest(reason=reason):
                self.surface.window.expected_thread_hint = 'hint'
                view = replace(self.surface.view, thread_hint='hint')
                changed = replace(view, identity='other') if reason == 'identity' else (
                    replace(view, thread_hint='other') if reason == 'thread' else
                    replace(view, screen=replace(view.screen, **{reason:True})))
                with patch.object(app, 'read_text', side_effect=[view, None, changed]) as read:
                    outcome = self.stage()
                self.assertEqual(outcome.status, 'held')
                self.assertEqual(read.call_count, 3)
                self.assertEqual((self.surface.writes, self.surface.clicks), (0, 0))

    def test_focus_unreadable_deadline_stages_no_input(self):
        samples = [self.surface.view]
        def read(_): return samples.pop(0) if samples else None
        with patch.object(app, 'read_text', side_effect=read), \
             patch.object(app.time, 'monotonic', side_effect=[0, 0, 0, .4, 1]):
            outcome = self.stage()
        self.assertEqual(outcome.status, 'held')
        self.assertEqual(outcome.detail, 'focused_unreadable')
        self.assertEqual((self.surface.writes, self.surface.clicks), (0, 0))

    def test_disabled_send_waits_then_sends_once_without_retyping(self):
        s = self.surface
        s.button.IsEnabled = False
        s.screen(error_kind='quota_retry', error_id='quota')
        self.observe()
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 0, 0))
        self.assertEqual(self.state.status, 'waiting_send')
        s.screen(draft=True)
        for seconds in (10, 50, 500, 5000):
            self.observe(seconds)
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 0, 0))
        s.button.IsEnabled = True
        self.observe(5001)
        self.assertEqual((s.writes, s.clicks, self.state.retry_attempts), (1, 1, 1))
        self.assertEqual(self.state.status, 'sent')
        self.assertIsNone(self.state.submission)
        s.screen(draft=False, running=True)
        self.observe(5002)
        self.assertEqual(s.clicks, 1)

    def test_aria_disabled_and_native_grey_state_override_enabled(self):
        for flag in ('disabled=true', 'readonly=true;disabled=true;busy=false', 'busy=true'):
            with self.subTest(flag=flag):
                self.surface.button.GetPropertyValue = lambda _, flag=flag: flag
                self.assertFalse(app._send_available(self.surface.button))
        self.surface.button.GetPropertyValue = lambda _: ''
        self.surface.button.ClassName = 'size-token-button-composer opacity-50'
        outcome = self.stage()
        self.assertEqual(outcome.status, 'waiting_send')
        self.assertEqual(self.surface.clicks, 0)
        self.surface.button.ClassName = 'disabled:opacity-50 aria-disabled:cursor-default'
        self.assertTrue(app.poll_submission(self.surface.window, outcome.submission))
        self.assertEqual(self.surface.clicks, 1)

    def test_successful_invoke_without_ack_is_not_counted_or_repeated(self):
        self.surface.ack_on_click = False
        self.surface.screen(error_kind='quota_retry', error_id='quota')
        self.observe()
        self.assertEqual(self.state.status, 'confirming_send')
        self.assertEqual(self.surface.clicks, 1)
        self.assertEqual(self.state.retry_attempts, 0)
        self.observe(1)
        self.assertEqual(self.surface.clicks, 1)
        self.surface.ack()
        self.observe(2)
        self.assertEqual(self.state.retry_attempts, 1)
        self.assertEqual(self.state.status, 'sent')

    def test_user_edit_clear_or_conversation_switch_cancels_pending(self):
        for change in ('edit', 'clear', 'switch'):
            with self.subTest(change=change):
                self.surface.view.identity = 'chat'
                self.surface.value = ''
                self.surface.button.IsEnabled = False
                receipt = self.stage().submission
                self.assertIsNotNone(receipt)
                if change == 'switch':
                    self.surface.view.identity = 'another'
                else:
                    self.surface.value = 'user draft' if change == 'edit' else ''
                self.surface.button.IsEnabled = True
                outcome = app.poll_submission(self.surface.window, receipt)
                self.assertEqual(outcome.status, 'held')
                self.assertIsNone(outcome.submission)
                self.assertEqual(self.surface.clicks, 0)

    def test_unreadable_view_or_ambiguous_composer_retains_receipt(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        for view in (None, app.View('', 'probe', app.Screen(blocked=True))):
            with patch.object(app, 'read_text', return_value=view):
                outcome = app.poll_submission(self.surface.window, receipt)
            self.assertIs(outcome.submission, receipt)
            self.assertFalse(outcome)

    def test_open_menu_running_attention_or_reset_buffer_hold_pending(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        self.surface.button.IsEnabled = True
        for blocked in ({'blocked': True}, {'running': True}, {'error_kind': 'attention'}):
            original = self.surface.view.screen
            self.surface.screen(**blocked)
            self.assertFalse(app.poll_submission(self.surface.window, receipt))
            self.surface.view.screen = original
        self.assertFalse(app.poll_submission(self.surface.window, receipt, allow_click=False))
        self.assertEqual(self.surface.clicks, 0)
        self.assertTrue(app.poll_submission(self.surface.window, receipt))

    def test_future_reset_buffer_is_respected_for_already_staged_draft(self):
        self.surface.button.IsEnabled = False
        self.surface.screen(error_kind='quota_retry', error_id='q')
        self.observe()
        self.surface.button.IsEnabled = True
        self.surface.screen(error_kind='quota', reset_utc=self.now + timedelta(seconds=50), draft=True)
        self.observe(69)
        self.assertEqual(self.surface.clicks, 0)
        self.observe(70)
        self.assertEqual(self.surface.clicks, 1)

    def test_after_finish_loop_is_spent_only_after_submission(self):
        self.watcher.config['after_finish'] = {'probe': 'continue'}
        self.watcher.config['after_finish_loops'] = {'probe': 1}
        self.state.initialized = True
        self.state.seen_running = True
        self.state.idle_since = self.now - timedelta(seconds=6)
        self.surface.button.IsEnabled = False
        self.observe()
        self.assertIsNotNone(self.state.submission)
        self.assertEqual(self.watcher.config['after_finish_loops']['probe'], 1)
        self.surface.button.IsEnabled = True
        self.surface.screen(draft=True)
        self.observe(1)
        self.assertEqual(self.watcher.config['after_finish_loops']['probe'], 0)

    def test_exact_rollout_detects_fast_completion_missing_from_visible_history(self):
        self.watcher.activities = SimpleNamespace(completed_turn=lambda tid: 'rollout:full:new')
        self.state.thread_id = 'full'
        self.state.initialized = True
        self.state.completion_id = 'rollout:full:old'
        self.watcher.config['sessions']['full'] = dict(after_finish='continue', loops=1)
        self.surface.screen(completion_id='', running=False)
        self.observe()
        self.assertTrue(self.state.seen_running)
        self.assertEqual(self.state.completion_id, 'rollout:full:new')
        self.observe(6)
        self.assertEqual((self.surface.writes, self.surface.clicks), (1,1))
        self.assertEqual(self.watcher.config['sessions']['full']['loops'], 0)
        self.observe(12)
        self.assertEqual(self.surface.clicks, 1)

    def test_initial_exact_rollout_completion_is_a_baseline_not_a_trigger(self):
        self.watcher.activities = SimpleNamespace(completed_turn=lambda tid: 'rollout:full:old')
        self.state.thread_id = 'full'
        self.watcher.config['sessions']['full'] = dict(after_finish='continue', loops=1)
        self.observe()
        self.observe(6)
        self.assertEqual(self.surface.clicks, 0)
        self.assertEqual(self.watcher.config['sessions']['full']['loops'], 1)

    def test_changed_after_finish_setting_or_exclusion_cancels_receipt(self):
        self.surface.button.IsEnabled = False
        self.state.submission = self.stage().submission
        self.state.submit_action = 'after_finish'
        self.watcher.config['after_finish'] = {'probe': 'different prompt'}
        self.observe()
        self.assertIsNone(self.state.submission)
        self.state.submission = app.Submission('chat', 'continue', 'old')
        self.watcher.config['excluded'] = ['probe']
        self.observe()
        self.assertIsNone(self.state.submission)
        self.assertEqual(self.surface.clicks, 0)

    def test_cleared_composer_with_no_user_turn_is_not_submission(self):
        self.surface.ack_on_click = False
        receipt = self.stage().submission
        self.surface.value = '随心输入\n'
        self.assertFalse(app.poll_submission(self.surface.window, receipt))
        self.assertFalse(app._submitted(self.surface.view, receipt))

    def test_delayed_user_bubble_never_reinvokes_or_reports_retryable_failure(self):
        self.surface.ack_on_click = False
        receipt = self.stage().submission
        for draft in ('continue', '', 'new user draft'):
            self.surface.value = draft
            with patch.object(app.time, 'monotonic', return_value=receipt.invoked_at + 20):
                outcome = app.poll_submission(self.surface.window, receipt)
            self.assertEqual(outcome.status, 'unknown')
            self.assertIs(outcome.submission, receipt)
            self.assertEqual(self.surface.clicks, 1)

    def test_switch_after_native_invoke_is_unknown(self):
        self.surface.ack_on_click = False
        receipt = self.stage().submission
        self.surface.view.identity = 'another'
        self.assertEqual(app.poll_submission(self.surface.window, receipt).status, 'unknown')
        self.assertEqual(self.surface.clicks, 1)

    def test_other_windows_foreground_does_not_redirect_the_owned_draft(self):
        self.surface.button.IsEnabled = False
        receipt = self.stage().submission
        self.surface.button.IsEnabled = True
        self.surface.foreground = 456
        self.assertTrue(app.poll_submission(self.surface.window, receipt))
        self.assertEqual(self.surface.clicks, 1)
        self.assertEqual(self.surface.foreground, 456)

    def test_temporarily_unmounted_composer_preserves_bound_receipt_and_arm(self):
        watcher = self.watcher
        watcher.running = True
        watcher.activities = None
        self.surface.window.Name = 'probe'
        state = State(123, 'probe', identity='chat', thread_hint='hint',
                      thread_id='full-id', seen_running=True, submission=object())
        watcher.states[123] = state
        watcher.session_bindings[123] = ('chat', 'hint', 'full-id')
        driver = SimpleNamespace(terminal_handles=lambda: [123],
            window_from_handle=lambda _: self.surface.window,
            read_text=lambda _: self.surface.view,
            inspect_screen=lambda view, _: view.screen,
            session_key=lambda _, view: view.identity,
            window_exists=lambda _: True)
        watcher.driver = driver
        self.surface.view = app.View('', 'probe', app.Screen(identified=True, blocked=True))
        with patch.object(watcher, '_observe') as observe:
            watcher.tick()
            observe.assert_not_called()
            self.assertIs(watcher.states[123], state)
            self.assertTrue(state.blind and state.seen_running)
            self.assertIsNotNone(state.submission)
            self.assertEqual(watcher.session_bindings[123], ('chat', 'hint', 'full-id'))
            self.surface.view = app.View('chat', 'probe', app.Screen(identified=True, ready=True), thread_hint='hint')
            watcher.tick()
            observe.assert_called_once()
            self.assertIs(watcher.states[123], state)
            self.assertFalse(state.blind)


class CapturedNativeSubmission(unittest.TestCase):
    def test_bound_rollout_confirms_only_a_new_exact_user_message(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            header = dict(type='session_meta', payload=dict(id='full-thread'))
            def message(text):
                return dict(type='response_item', payload=dict(type='message', role='user',
                    content=[dict(type='input_text', text=text)]))
            def append(item):
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(item) + '\n')
            append(header)
            append(message('continue'))  # Historical matching text is insufficient.
            receipt = app.Submission('chat', 'continue', 'old', invoked_at=1)
            window = SimpleNamespace(expected_thread_id='full-thread')
            with patch.object(app, 'RecentActivities') as reader:
                reader.return_value.thread.return_value = dict(rollout_path=str(path))
                app._capture_rollout(window, receipt)
                self.assertFalse(app._rollout_submitted(receipt))
                append(message('continue\n'))
                self.assertTrue(app._rollout_submitted(receipt))
                view = app.View('chat', 'probe', app.Screen(), user_text='stale')
                self.assertTrue(app._submitted(view, receipt))
                view.identity = 'another'
                self.assertFalse(app._submitted(view, receipt))

    def test_rollout_other_user_thread_or_partial_record_cannot_confirm(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'rollout.jsonl'
            header = json.dumps(dict(type='session_meta', payload=dict(id='full-thread'))) + '\n'
            other = json.dumps(dict(type='response_item', payload=dict(type='message', role='user',
                content=[dict(type='input_text', text='different')]))) + '\n'
            match = other.replace('different', 'continue')
            with patch.object(app, 'RecentActivities') as reader:
                reader.return_value.thread.return_value = dict(rollout_path=str(path))
                for content in (other + match, match.rstrip('\n')):
                    path.write_text(header + content, encoding='utf-8')
                    receipt = app.Submission('chat', 'continue', 'old', invoked_at=1,
                        thread_id='full-thread', rollout_path=str(path), rollout_offset=len(header.encode()))
                    self.assertFalse(app._rollout_submitted(receipt))
                path.write_text(header.replace('full-thread', 'other-thread') + match, encoding='utf-8')
                self.assertFalse(app._rollout_submitted(receipt))

    def test_real_native_bubble_excludes_its_timestamp(self):
        window = replay('idle')
        captured = json.loads((FIXTURES / 'native_user_bubble.json').read_text(encoding='utf-8'))
        window.control.children.append(Node(captured, window.control))
        view = app.read_text(window)
        self.assertEqual(view.user_text, 'continue')
        self.assertTrue(view.user_turn)
        self.assertTrue(app._submitted(view, app.Submission(view.identity, 'continue', 'earlier-turn')))


if __name__ == '__main__':
    unittest.main()
