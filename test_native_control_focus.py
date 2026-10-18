"""Foreground activation may finish later, refuse, or outlive a UI binding."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import native_control


class FocusTests(unittest.TestCase):
    def run_focus(self, delay=0, accepted=True, alive=True, changed=False):
        clock = [0.0]
        user = SimpleNamespace(
            GetForegroundWindow=Mock(side_effect=lambda: 77 if accepted and clock[0] >= delay else 88),
            SetForegroundWindow=Mock(return_value=accepted),
            BringWindowToTop=Mock(return_value=True),
            IsWindow=Mock(return_value=alive))
        view = SimpleNamespace(screen=SimpleNamespace(identified=True), identity='bound', thread_hint='thread')
        fresh = SimpleNamespace(screen=view.screen, identity='changed' if changed else 'bound', thread_hint='thread')
        with patch.object(native_control.app, 'read_text', side_effect=[view, view, fresh]), \
             patch.object(native_control.app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(user32=user))), \
             patch.object(native_control.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(native_control.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = native_control.act(SimpleNamespace(hwnd=77), 'bound', 'thread', 'focus')
        self.assertLessEqual(user.SetForegroundWindow.call_count, 1)
        self.assertLessEqual(user.BringWindowToTop.call_count, 1)
        return result, user, clock[0]

    def test_delayed_activation_confirms_one_request(self):
        result, user, elapsed = self.run_focus(delay=.12)
        self.assertEqual(result, {'ok': True})
        self.assertGreaterEqual(elapsed, .12)
        self.assertEqual(user.SetForegroundWindow.call_count, 1)

    def test_refused_activation_stops_without_forcing(self):
        result, user, elapsed = self.run_focus(accepted=False)
        self.assertEqual(result, {'ok': False, 'reason': 'focus_failed'})
        self.assertFalse(user.BringWindowToTop.called)
        self.assertEqual(elapsed, 0)

    def test_timeout_is_bounded_and_never_repeats_activation(self):
        result, _, elapsed = self.run_focus(delay=10)
        self.assertEqual(result, {'ok': False, 'reason': 'focus_failed'})
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 1.03)

    def test_destroyed_window_stops_waiting(self):
        result, _, elapsed = self.run_focus(delay=10, alive=False)
        self.assertEqual(result, {'ok': False, 'reason': 'focus_failed'})
        self.assertEqual(elapsed, 0)

    def test_binding_change_during_activation_is_rejected(self):
        result, _, _ = self.run_focus(delay=.06, changed=True)
        self.assertEqual(result, {'ok': False, 'reason': 'changed'})

    def test_already_active_only_rechecks_binding(self):
        result, user, _ = self.run_focus()
        self.assertEqual(result, {'ok': True})
        self.assertFalse(user.SetForegroundWindow.called)
        self.assertFalse(user.BringWindowToTop.called)


class ActionReadTests(unittest.TestCase):
    def run_interrupt(self, reads):
        clock=[0.0]
        button=SimpleNamespace(ControlTypeName='ButtonControl',Name='停止',IsEnabled=True,IsOffscreen=False)
        with patch.object(native_control.app,'read_text',side_effect=reads) as reader, \
             patch.object(native_control,'document',return_value=[button]) as document, \
             patch.object(native_control,'_focus_stop_and_invoke',return_value=True) as invoke, \
             patch.object(native_control.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(native_control.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)):
            result=native_control.act(SimpleNamespace(hwnd=77),'bound','thread','interrupt')
        return result,invoke,document,reader,clock[0]

    def view(self,identity='bound',hint='thread',running=True,identified=True):
        return SimpleNamespace(identity=identity,thread_hint=hint,
            screen=SimpleNamespace(identified=identified,running=running))

    def test_redraw_before_stop_recovers_without_repeating_input(self):
        result,invoke,_,_,elapsed=self.run_interrupt([self.view(),None,self.view(),self.view(running=False)])
        self.assertEqual(result,{'ok':True})
        self.assertEqual(invoke.call_count,1)
        self.assertGreater(elapsed,0)

    def test_changed_thread_after_redraw_rejects_without_input(self):
        result,invoke,document,_,_=self.run_interrupt([self.view(),None,self.view(hint='other')])
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        invoke.assert_not_called();document.assert_not_called()

    def test_permanently_unreadable_is_bounded_and_never_clicks(self):
        result,invoke,document,reader,elapsed=self.run_interrupt(lambda window:None)
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        invoke.assert_not_called();document.assert_not_called()
        self.assertGreater(reader.call_count,1)
        self.assertGreaterEqual(elapsed,1);self.assertLess(elapsed,1.06)

    def test_known_non_codex_view_is_rejected_immediately(self):
        result,invoke,document,reader,elapsed=self.run_interrupt([self.view(identified=False)])
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        invoke.assert_not_called();document.assert_not_called()
        self.assertEqual(reader.call_count,1);self.assertEqual(elapsed,0)

    def test_unreadable_after_click_only_waits_for_receipt(self):
        result,invoke,_,_,_=self.run_interrupt([self.view(),self.view(),None,self.view(running=False)])
        self.assertEqual(result,{'ok':True})
        self.assertEqual(invoke.call_count,1)

    def test_mode_change_before_click_rejects_without_input(self):
        result,invoke,document,reader,elapsed=self.run_interrupt([self.view(),self.view(identified=False)])
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        invoke.assert_not_called();document.assert_not_called()
        self.assertEqual(reader.call_count,2);self.assertEqual(elapsed,0)


if __name__ == '__main__':
    unittest.main()
