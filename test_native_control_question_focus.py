"""A first-focus UIA remount must not lose a safe question answer or retarget it."""
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import native_control


class QuestionFocusTests(unittest.TestCase):
    def run_answer(self, transition='missing'):
        clock=[0.0]
        pattern=NS(Value='placeholder')
        pattern.SetValue=Mock(side_effect=lambda text:setattr(pattern,'Value',text))
        entry=NS(Name='placeholder',GetValuePattern=lambda:pattern,SetFocus=Mock())
        submit=object()
        view=NS(identity='bound',thread_hint='thread',question_card=object(),
                screen=NS(identified=True,running=False))
        value=dict(sig='question',identity='bound',kind='ask',choices=[(entry,'text',None)],
                   entry=entry,submit=submit,back=None)
        reads=[value,None,value,value,None]
        if transition=='exception':reads=[value,RuntimeError('UIA control remounted'),value,value,None]
        if transition=='prompt_change':reads=[value,dict(value,sig='next-question')]
        calls=[0]
        def read(_):
            calls[0]+=1
            if calls[0]>2:
                if transition=='unreadable':return None
                if transition=='thread_change':return NS(identity='bound',thread_hint='other',screen=view.screen)
            return view
        user=NS(GetForegroundWindow=Mock(return_value=88),SetForegroundWindow=Mock())
        with patch.object(native_control.app,'read_text',side_effect=read), \
             patch.object(native_control,'prompt',side_effect=reads), \
             patch.object(native_control.app,'ctypes',NS(windll=NS(user32=user))), \
             patch.object(native_control.app,'_act',return_value=True) as click, \
             patch.object(native_control.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(native_control.time,'sleep',side_effect=lambda s:clock.__setitem__(0,clock[0]+s)):
            result=native_control.act(NS(hwnd=77),'bound','thread','pick',
                                     prompt_sig='question',n=1,text='complete answer')
        entry.SetFocus.assert_called_once()
        return result,pattern.SetValue,click,clock[0]

    def test_missing_first_focus_read_recovers_before_one_input_and_submit(self):
        result,write,click,elapsed=self.run_answer()
        self.assertEqual(result,{'ok':True})
        write.assert_called_once_with('complete answer');click.assert_called_once()
        self.assertGreater(elapsed,0)

    def test_disappearing_uia_control_recovers_without_repeating_writes(self):
        result,write,click,_=self.run_answer('exception')
        self.assertEqual(result,{'ok':True})
        write.assert_called_once();click.assert_called_once()

    def test_known_new_question_rejects_without_input(self):
        result,write,click,elapsed=self.run_answer('prompt_change')
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        write.assert_not_called();click.assert_not_called();self.assertEqual(elapsed,0)

    def test_known_changed_thread_rejects_without_input(self):
        result,write,click,elapsed=self.run_answer('thread_change')
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        write.assert_not_called();click.assert_not_called();self.assertEqual(elapsed,0)

    def test_permanently_unreadable_card_times_out_without_input(self):
        result,write,click,elapsed=self.run_answer('unreadable')
        self.assertEqual(result,{'ok':False,'reason':'changed'})
        write.assert_not_called();click.assert_not_called()
        self.assertGreaterEqual(elapsed,1);self.assertLess(elapsed,1.06)


if __name__=='__main__':unittest.main()
