"""Captured native question cards can coexist with the general composer."""
import json
import unittest
from test_codex_app import FIXTURES, Node, replay
import codex_app as app
import native_control


class CoexistingQuestionTests(unittest.TestCase):
    def question(self):
        window=replay('idle')
        data=json.loads((FIXTURES/'question_composer_mounted_20261017.json').read_text(encoding='utf-8'))
        card=Node(data,window.control)
        window.control.children.append(card)
        return window,card

    def test_real_card_blocks_general_send_and_preserves_full_question_and_options(self):
        window,card=self.question()
        view=app.read_text(window)
        self.assertIsNotNone(view.composer)
        self.assertIs(view.question_card,card)
        self.assertTrue(view.screen.blocked)
        app.prepare(window,view.identity)
        self.assertFalse(app.send_prompt(window,'continue',dry_run=True))
        value=native_control.public_prompt(native_control.prompt(window))
        self.assertEqual(value['kind'],'ask')
        self.assertEqual(value['text'],'Choose an isolated test option')
        self.assertEqual([o['label'] for o in value['options']],['First answer','Second complete answer','语音回答'])

    def test_header_is_not_the_question_even_when_the_real_question_is_named_question(self):
        window,card=self.question()
        card.children[2].Name='问题'
        value=native_control.public_prompt(native_control.prompt(window))
        self.assertIsNotNone(value)
        self.assertEqual(value['text'],'问题')

    def test_two_pending_cards_hold_general_send_without_guessing_an_answer_target(self):
        window,card=self.question()
        second=Node(json.loads((FIXTURES/'question_composer_mounted_20261017.json').read_text(encoding='utf-8')),window.control)
        window.control.children.append(second)
        view=app.read_text(window)
        self.assertTrue(view.screen.blocked)
        self.assertIsNone(view.question_card)
        self.assertIsNone(native_control.prompt(window))
        app.prepare(window,view.identity)
        self.assertFalse(app.send_prompt(window,'continue',dry_run=True))

    def test_non_codex_modes_never_expose_the_same_card_as_an_actionable_question(self):
        window,_=self.question()
        mode=next(c for c in app.walk(window.control) if '当前模式：Codex' in c.Name)
        mode.Name=mode.Name.replace('Codex','ChatGPT')
        self.assertFalse(app.read_text(window).screen.identified)
        self.assertIsNone(native_control.prompt(window))


if __name__=='__main__':unittest.main()
