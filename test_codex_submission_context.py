"""Native receipts distinguish captured host context from real user input."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import codex_app as app


class ContextReceiptTests(unittest.TestCase):
    def receipt(self,messages):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rollout.jsonl'
            header=json.dumps(dict(type='session_meta',payload=dict(id='owned-full-thread')))+'\n'
            records=[dict(type='response_item',payload=dict(type='message',role='user',
                content=[dict(type='input_text',text=text)],
                internal_chat_message_metadata_passthrough=dict(content_item_kinds=kinds)))
                for text,kinds in messages]
            path.write_bytes((header+''.join(json.dumps(r)+'\n' for r in records)).encode())
            state=app.Submission('bound','continue','previous',invoked_at=1,
                thread_id='owned-full-thread',rollout_path=str(path),rollout_offset=len(header.encode()))
            with patch.object(app,'RecentActivities') as activities:
                activities.return_value.thread.return_value=dict(rollout_path=str(path))
                return app._rollout_submitted(state)

    def test_injected_environment_before_actual_user_confirms_submission(self):
        self.assertTrue(self.receipt([
            ('<environment_context>new sandbox</environment_context>',['environments.environment_context']),
            ('continue',['user.text'])]))

    def test_environment_record_alone_cannot_ack_even_matching_text(self):
        self.assertFalse(self.receipt([('continue',['environments.environment_context'])]))

    def test_different_real_user_still_revokes_confirmation(self):
        self.assertFalse(self.receipt([
            ('context',['environments.environment_context']),
            ('different actual message',['user.text']),('continue',['user.text'])]))

    def test_page_context_before_actual_user_confirms_submission(self):
        self.assertTrue(self.receipt([
            ('<external_codex_apps_open_page>{"page_id":null}</external_codex_apps_open_page>',
             ['additional_content.codex_apps_open_page']), ('continue',['user.text'])]))

    def test_page_context_alone_cannot_ack_even_matching_text(self):
        self.assertFalse(self.receipt([('continue',['additional_content.codex_apps_open_page'])]))

    def test_page_context_does_not_skip_real_user_or_unknown_host_input(self):
        for kinds in [['user.text'], ['additional_content.codex_apps_open_page','user.text'],
                      ['additional_content.unknown'], []]:
            with self.subTest(kinds=kinds):
                self.assertFalse(self.receipt([
                    ('<external_codex_apps_open_page>literal</external_codex_apps_open_page>',kinds),
                    ('continue',['user.text'])]))

    def test_literal_context_tags_or_mixed_user_kinds_are_not_ignored(self):
        for kinds in [['user.text'],['environments.environment_context','user.text'],[]]:
            with self.subTest(kinds=kinds):
                self.assertFalse(self.receipt([
                    ('<environment_context>human text</environment_context>',kinds),
                    ('continue',['user.text'])]))


if __name__=='__main__':unittest.main()
