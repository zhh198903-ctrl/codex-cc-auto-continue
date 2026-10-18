"""A native selected radio needs one guarded Enter, with no keyboard fallback."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import native_control


class ChoiceCommitTests(unittest.TestCase):
    def run_commit(self, change=''):
        selected=NS(IsSelected=change!='not_selected')
        control=NS(Name='Complete answer',IsEnabled=True,IsOffscreen=False,
            GetSelectionItemPattern=lambda:selected,GetRuntimeId=lambda:[1,2],SetFocus=Mock())
        focused=NS(Name='Complete answer',GetRuntimeId=lambda:[1,3] if change=='focus' else [1,2])
        value=dict(sig='prompt',choices=[(control,'Complete answer',False)])
        fresh=None if change=='changed' else value
        foreground=[88]
        def activate(hwnd):
            if change!='refused':foreground[0]=hwnd.value
        user=NS(GetForegroundWindow=lambda:foreground[0],SetForegroundWindow=Mock(side_effect=activate))
        auto=NS(GetFocusedControl=lambda:focused,SendKeys=Mock())
        with patch.object(native_control,'prompt',return_value=value), \
             patch.object(native_control,'_prompt_after_focus',return_value=fresh), \
             patch.object(native_control.app,'ctypes',NS(windll=NS(user32=user))), \
             patch.object(native_control.app,'auto',auto):
            result=native_control._commit_native_choice(NS(hwnd=77),'identity','thread','prompt',1,'Complete answer')
        return result,auto.SendKeys,user,control,foreground[0]

    def test_exact_selected_choice_gets_one_enter_and_foreground_is_restored(self):
        result,keys,user,control,foreground=self.run_commit()
        self.assertTrue(result)
        keys.assert_called_once_with('{Enter}',waitTime=0)
        control.SetFocus.assert_called_once()
        self.assertEqual(foreground,88)
        self.assertEqual(user.SetForegroundWindow.call_count,2)

    def test_refused_foreground_never_types_into_working_window(self):
        result,keys,user,control,foreground=self.run_commit('refused')
        self.assertFalse(result);keys.assert_not_called();control.SetFocus.assert_not_called()
        self.assertEqual(foreground,88)

    def test_changed_prompt_after_focus_never_submits(self):
        result,keys,_,_,foreground=self.run_commit('changed')
        self.assertFalse(result);keys.assert_not_called();self.assertEqual(foreground,88)

    def test_different_focused_element_never_receives_enter(self):
        result,keys,_,_,foreground=self.run_commit('focus')
        self.assertFalse(result);keys.assert_not_called();self.assertEqual(foreground,88)

    def test_unselected_radio_never_activates_window_or_types(self):
        result,keys,user,control,_=self.run_commit('not_selected')
        self.assertFalse(result);keys.assert_not_called();user.SetForegroundWindow.assert_not_called()
        control.SetFocus.assert_not_called()

    def test_selection_that_advances_question_never_commits_next_step(self):
        view=NS(identity='identity',thread_hint='thread',screen=NS(identified=True),question_card=object())
        original=dict(sig='first',choices=[(NS(),'Complete answer',False)],submit=None)
        following=dict(sig='second',choices=[(NS(),'Different answer',False)],submit=None)
        with patch.object(native_control,'_read_stable',return_value=view), \
             patch.object(native_control.app,'read_text',return_value=view), \
             patch.object(native_control,'prompt',side_effect=[original,following,following]), \
             patch.object(native_control.app,'_act',return_value=True) as choose, \
             patch.object(native_control,'_commit_native_choice') as commit:
            result=native_control.act(NS(hwnd=77),'identity','thread','pick','first',1)
        self.assertTrue(result['ok']);choose.assert_called_once();commit.assert_not_called()

    def test_advanced_card_with_changed_binding_stays_unknown(self):
        view=NS(identity='identity',thread_hint='thread',screen=NS(identified=True),question_card=object())
        moved=NS(identity='another identity',thread_hint='another thread')
        original=dict(sig='first',choices=[(NS(),'Complete answer',False)],submit=None)
        following=dict(sig='second',choices=[(NS(),'Different answer',False)],submit=None)
        with patch.object(native_control,'_read_stable',return_value=view), \
             patch.object(native_control.app,'read_text',return_value=moved), \
             patch.object(native_control,'prompt',side_effect=[original,following]), \
             patch.object(native_control.app,'_act',return_value=True), \
             patch.object(native_control,'_commit_native_choice') as commit:
            result=native_control.act(NS(hwnd=77),'identity','thread','pick','first',1)
        self.assertEqual(result,dict(ok=False,reason='unknown'));commit.assert_not_called()


if __name__=='__main__':unittest.main()
