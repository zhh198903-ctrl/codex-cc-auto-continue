"""Background Stop commits once only after exact focus and identity checks."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import native_control


class StopFocusTests(unittest.TestCase):
    def run_stop(self, change=''):
        foreground=[88]
        user=NS(GetForegroundWindow=Mock(side_effect=lambda:foreground[0]),
                SetForegroundWindow=Mock(side_effect=lambda h:foreground.__setitem__(0,h.value)))
        focus=Mock(side_effect=lambda:foreground.__setitem__(0,77))
        old=NS(GetRuntimeId=lambda:[1,2],SetFocus=focus)
        fresh_button=NS(ControlTypeName='ButtonControl',Name='停止',IsEnabled=change!='disabled',
                        IsOffscreen=change=='offscreen',HasKeyboardFocus=change!='unfocused',
                        GetRuntimeId=lambda:[1,3] if change=='button' else [1,2])
        fresh=NS(identity='other' if change=='identity' else 'bound',
                 thread_hint='other' if change=='thread' else 'thread',
                 screen=NS(identified=change!='mode',running=change!='finished'),
                 controls=[fresh_button,fresh_button] if change=='ambiguous' else [fresh_button])
        with patch.object(native_control,'_read_stable',return_value=None if change=='unreadable' else fresh), \
             patch.object(native_control.app,'ctypes',NS(windll=NS(user32=user))), \
             patch.object(native_control.app,'_act',return_value=True) as invoke:
            result=native_control._focus_stop_and_invoke(NS(hwnd=77),'bound','thread',old)
        return result,focus,invoke,foreground[0]

    def test_exact_focused_stop_is_invoked_once_and_working_focus_restored(self):
        result,focus,invoke,foreground=self.run_stop()
        self.assertTrue(result)
        focus.assert_called_once()
        invoke.assert_called_once()
        self.assertEqual(invoke.call_args.kwargs,{'patterns_only':True})
        self.assertEqual(foreground,88)

    def test_changed_or_unavailable_scope_never_invokes_stop(self):
        for change in ['identity','thread','mode','finished','unreadable','button',
                       'disabled','offscreen','unfocused','ambiguous']:
            with self.subTest(change=change):
                result,focus,invoke,foreground=self.run_stop(change)
                self.assertFalse(result)
                focus.assert_called_once()
                invoke.assert_not_called()
                self.assertEqual(foreground,88)


if __name__=='__main__':
    unittest.main()
