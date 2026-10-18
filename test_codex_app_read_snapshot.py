"""Discovery caching must never escape into a later action or observation."""
import unittest
from test_codex_app import replay, Node
import codex_app as app


class ReadSnapshotTests(unittest.TestCase):
    def test_views_return_live_controls_and_disabled_send_is_seen_immediately(self):
        view=app.read_text(replay('idle'))
        self.assertFalse(any(isinstance(c,app._ReadControl) for c in view.controls))
        for control in [view.composer,view.send,view.model_button,view.permission_button]:
            self.assertNotIsInstance(control,app._ReadControl)
        view.send.IsEnabled=False
        self.assertFalse(app._send_available(view.send))
        view.send.IsEnabled=True
        view.send.ClassName+=' opacity-50'
        self.assertFalse(app._send_available(view.send))

    def test_new_draft_is_observed_and_prevents_the_next_submission(self):
        window=replay('idle')
        before=app.read_text(window)
        app.prepare(window,before.identity)
        before.composer.value='An unsent customer draft'
        after=app.read_text(window)
        self.assertTrue(after.screen.draft)
        self.assertFalse(app.send_prompt(window,'continue',dry_run=True))
        self.assertEqual(before.composer.value,'An unsent customer draft')

    def test_new_menu_and_changed_chat_revoke_a_previous_ready_view(self):
        window=replay('idle')
        before=app.read_text(window)
        app.prepare(window,before.identity)
        menu=Node(dict(kind='MenuControl',name='Unrelated menu',id='',enabled=True,
                       offscreen=False,runtime=[1,999],value=None,children=[]),window.control)
        window.control.children.append(menu)
        after=app.read_text(window)
        self.assertTrue(after.screen.blocked)
        self.assertFalse(app.send_prompt(window,'continue',dry_run=True))
        window.control.children.remove(menu)
        window.control.Name='Another conversation'
        self.assertNotEqual(app.read_text(window).identity,before.identity)
        self.assertFalse(app.send_prompt(window,'continue',dry_run=True))

    def test_visibility_and_enabled_state_are_live_inside_a_read(self):
        original=Node(dict(kind='ButtonControl',name='Send',id='',enabled=True,
                           offscreen=False,runtime=[1],value=None,children=[]))
        observed=app._ReadControl(original)
        self.assertTrue(observed.IsEnabled)
        self.assertFalse(observed.IsOffscreen)
        original.IsEnabled=False
        original.IsOffscreen=True
        self.assertFalse(observed.IsEnabled)
        self.assertTrue(observed.IsOffscreen)


if __name__=='__main__':unittest.main()
