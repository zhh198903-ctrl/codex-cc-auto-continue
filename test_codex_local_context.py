"""Native context survives multiple GUIs and private pipe protocol changes."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from codex_activity import RecentActivities, TAIL_BYTES


def write(path, event):
    with path.open('ab') as file:
        file.write((json.dumps(event, ensure_ascii=False)+'\n').encode())


def message(path, role, text, channel=None):
    write(path, {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
          'channel': channel, 'content': [{'type': 'text', 'text': text}]}})


@pytest.fixture
def history(tmp_path):
    path = tmp_path/'rollout.jsonl'
    write(path, {'type': 'session_meta', 'payload': {'id': 'full-id'}})
    reader = RecentActivities()
    reader.thread = lambda tid: {'id': 'full-id', 'rollout_path': str(path)} if tid == 'full-id' else None
    return reader, path


def test_only_user_and_final_assistant_messages_are_read_without_pipe(history):
    reader, path = history
    message(path, 'user', '真人语音')
    message(path, 'assistant', 'INTERNAL_ANALYSIS', 'analysis')
    message(path, 'tool', 'PRIVATE_TOOL_DATA')
    message(path, 'developer', 'PRIVATE_INSTRUCTIONS')
    message(path, 'assistant', '语音验收成功', 'final')
    page = reader.context('full-id')
    assert [i['x'] for i in page['items']] == ['真人语音', '语音验收成功']
    assert not page['more'] and not page['newer']
    assert not reader.context('other-id')['items']


def test_wrong_header_and_partial_tail_never_supply_other_thread_context(history):
    reader, path = history
    message(path, 'assistant', 'complete', 'final')
    with path.open('ab') as file: file.write(b'{"type":"response_item"')
    assert [i['x'] for i in reader.context('full-id')['items']] == ['complete']
    path.write_text(json.dumps({'type':'session_meta','payload':{'id':'other-id'}})+'\n',encoding='utf-8')
    message(path, 'assistant', 'WRONG_THREAD')
    assert not reader.context('full-id')['items']


def test_cursors_are_stable_across_new_messages_and_page_in_both_directions(history):
    reader, path = history
    for n in range(20): message(path, 'assistant', str(n), 'final')
    latest = reader.context('full-id')
    assert [i['x'] for i in latest['items']] == [str(n) for n in range(12,20)]
    older = reader.context('full-id', before=latest['items'][0]['s'])
    assert [i['x'] for i in older['items']] == [str(n) for n in range(4,12)]
    forward = reader.context('full-id', after=older['items'][-1]['s'])
    assert forward['items'] == latest['items']
    message(path, 'assistant', '20', 'final')
    assert reader.context('full-id', before=latest['items'][0]['s'])['items'] == older['items']


def test_large_history_reads_bounded_tail_and_truncates_display_text(history):
    reader, path = history
    write(path, {'type':'irrelevant','padding':'z'*(TAIL_BYTES+20)})
    message(path, 'assistant', 'last reply'+'中'*2000, 'final')
    page = reader.context('full-id')
    assert len(page['items']) == 1 and len(page['items'][0]['x']) == 1000


def test_service_context_requires_binding_and_never_calls_legacy_pipe(history):
    from control_service import CodexService
    reader, path = history
    message(path, 'assistant', 'CURRENT_REPLY', 'final')
    service = SimpleNamespace(bound=set(), app_watcher=SimpleNamespace(activities=reader),
                              reader=SimpleNamespace(read=Mock(side_effect=AssertionError('legacy pipe forbidden'))))
    assert not CodexService.context(service, 'codex:desktop:full-id')['items']
    service.bound.add('full-id')
    assert CodexService.context(service, 'codex:desktop:full-id')['items'][0]['x'] == 'CURRENT_REPLY'
    service.reader.read.assert_not_called()
