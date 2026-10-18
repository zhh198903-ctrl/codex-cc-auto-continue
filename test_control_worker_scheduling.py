"""Queued actions cannot be starved by renewal-driven desktop scans."""
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PyQt6.QtCore import QCoreApplication
from codex_bridge.client import CodexError
from control_service import CodexService
import native_control


class WorkerSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.app = QCoreApplication.instance() or QCoreApplication([])
        watcher = SimpleNamespace(config={'recent_limit': 5}, activities=None, states={})
        backend = SimpleNamespace(connected=False, close=lambda: None)
        self.service = CodexService(SimpleNamespace(), watcher, backend)
        self.service.wake.disconnect()
        self.service.rows['codex:full-thread'] = dict(id='codex:full-thread')
        self.service.refresh = Mock()

    def tearDown(self):
        self.service.close()

    def enqueue(self, action, rid):
        return self.service.submit(dict(action=action, request_id=rid,
                                        target_id='codex:full-thread'), timeout=0)

    def test_renewals_do_not_rescan_and_queued_action_runs_before_one_observation(self):
        events = []
        execute = self.service.execute
        self.service.refresh.side_effect = lambda: events.append('refresh')
        def run(data):
            events.append(data['action'])
            return execute(data) if data['action'] == 'claim' else {'ok': True}
        self.service.execute = run
        for i in range(5):
            self.enqueue('claim', 'renew-' + str(i))
        self.enqueue('send', 'one-send')
        self.service.drain()
        self.assertEqual(events, ['claim'] * 5 + ['send', 'refresh'])
        self.assertTrue(self.service.owns('full-thread'))
        self.assertTrue(self.service.operation('one-send')['ok'])

    def test_only_renewals_do_not_scan(self):
        self.enqueue('claim', 'renew')
        self.service.drain()
        self.service.refresh.assert_not_called()
        self.assertTrue(self.service.operation('renew')['ok'])

    def test_timer_with_waiting_action_drains_before_observing(self):
        events = []
        self.service.execute = lambda data: events.append('action') or {'ok': True}
        self.service.refresh.side_effect = lambda: events.append('refresh')
        self.enqueue('interrupt', 'stop')
        self.service.refresh_guarded()
        self.assertEqual(events, ['action', 'refresh'])

    def test_explicit_scan_does_not_perform_second_observation(self):
        self.service.snapshot = Mock(return_value={})
        self.enqueue('scan', 'scan')
        # A scoped scan performs its own refresh in execute.
        self.service.drain()
        self.service.refresh.assert_called_once()

    def test_release_resumes_automatic_observation(self):
        self.service.leases['codex:full-thread'] = 1e30
        self.enqueue('release', 'release')
        self.service.drain()
        self.assertFalse(self.service.owns('full-thread'))
        self.service.refresh.assert_called_once()

    def test_unknown_send_is_not_repeated_or_made_confirmed_by_observation(self):
        execute = Mock(side_effect=CodexError('timeout'))
        self.service.execute = execute
        self.enqueue('send', 'uncertain')
        self.service.drain()
        first = self.service.operation('uncertain')
        self.assertEqual(first['status'], 'unknown')
        self.assertIn('codex:full-thread', self.service.paused)
        self.assertEqual(self.enqueue('send', 'uncertain'), first)
        execute.assert_called_once()

    def test_pending_native_keeps_stage_timing_until_original_confirmation(self):
        self.service.execute = lambda data: self.service.pending_native.update(
            {'codex:full-thread': data['request_id']}) or {'ok': False, 'status': 'pending'}
        with patch('control_service.time.monotonic', return_value=100):
            self.enqueue('send', 'staged')
        with patch('control_service.time.monotonic', side_effect=[102, 105]):
            self.service.drain()
        pending = self.service.operation('staged')
        self.assertEqual(pending['timing'], dict(queue_ms=2000, execution_ms=3000))
        self.assertEqual(pending['status'], 'pending')
        with patch('control_service.time.monotonic', return_value=108):
            self.service._finish_native('codex:full-thread', True)
        confirmed = self.service.operation('staged')
        self.assertEqual(confirmed['timing'], dict(queue_ms=2000, execution_ms=3000, total_ms=8000))
        self.assertEqual(confirmed['status'], 'confirmed')

    def test_native_deadline_checks_fresh_receipt_before_declaring_unknown(self):
        for receipt in (True, False):
            with self.subTest(receipt=receipt):
                service = self.service
                sid, tid, rid = 'codex:desktop:one', 'one', 'late-native'
                state = SimpleNamespace(thread_id=tid, submission=SimpleNamespace(invoked_at=1),
                                        status='confirming_send', completion_id='', seen_running=False)
                view = SimpleNamespace(identity='exact', screen=SimpleNamespace(blocked=False, running=False, completion_id=''))
                service.cli.snapshot = lambda: ([], {})
                service.cli.error = ''
                service.app_watcher.activities = SimpleNamespace(read=lambda _: ([dict(thread_id=tid, title='owned')], ''))
                service.app_watcher.states = {123: state}
                service.native = {tid: (123, 'exact', 'hint')}
                service.bound = {tid}
                service.leases[sid] = 1e30
                service.pending_native[sid] = rid
                service.operations[rid] = dict(queued_at=0, event=threading.Event(), result=dict(status='pending', timing={}))
                service._visible = Mock(return_value=(object(), view, state))
                def observe(st, window, now):
                    if receipt:
                        st.submission = None
                        service.confirmed_native(st)
                service.app_watcher._observe = Mock(side_effect=observe)
                with patch('control_service.time.monotonic', return_value=12), patch.object(native_control, 'prompt', return_value=None):
                    CodexService.refresh(service)
                self.assertEqual(service.operation(rid)['status'], 'confirmed' if receipt else 'unknown')
                service.app_watcher._observe.assert_called_once()


class PromptObservationTests(unittest.TestCase):
    def test_same_read_observation_reuses_controls_without_rescanning(self):
        view = SimpleNamespace(screen=SimpleNamespace(identified=True), thread_hint='hint',
                               identity='id', approval_button=None, question_card=None, controls=[])
        with patch.object(native_control.app, 'read_text') as read, \
             patch.object(native_control, 'document') as document:
            self.assertIsNone(native_control.prompt(object(), observed_view=view))
        read.assert_not_called()
        document.assert_not_called()

    def test_action_prompt_never_uses_an_earlier_observation(self):
        view = SimpleNamespace(screen=SimpleNamespace(identified=False))
        with patch.object(native_control.app, 'read_text', return_value=view) as read:
            self.assertIsNone(native_control.prompt(object()))
        read.assert_called_once()

    def test_approval_reads_card_without_walking_full_chat(self):
        deny = SimpleNamespace(Name='拒绝', ControlTypeName='ButtonControl', IsOffscreen=False, IsEnabled=True,
                               GetRuntimeId=lambda: [2])
        approve = SimpleNamespace(Name='允许一次', ControlTypeName='ButtonControl', IsOffscreen=False, IsEnabled=True,
                                  GetRuntimeId=lambda: [1])
        actions = SimpleNamespace(GetChildren=lambda: [approve, deny], GetParentControl=lambda: 'card')
        approve.GetParentControl = lambda: actions
        view = SimpleNamespace(screen=SimpleNamespace(identified=True), thread_hint='hint',
                               identity='id', approval_button=approve)
        with patch.object(native_control.app, 'read_text', return_value=view) as read, \
             patch.object(native_control.app, 'walk', return_value=[SimpleNamespace(ControlTypeName='TextControl', Name='Approve command')]), \
             patch.object(native_control, 'document') as document:
            value = native_control.prompt(object())
        self.assertEqual(value['kind'], 'perm')
        self.assertEqual([label for _, label, _ in value['choices']], ['允许一次', '拒绝'])
        read.assert_called_once()
        document.assert_not_called()


if __name__ == '__main__':
    unittest.main()
