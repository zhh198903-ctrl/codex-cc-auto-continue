"""Context keeps source text; compact previews are a separate API choice."""
import json
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
import local_api
from codex_bridge.backend import CodexBackend


def backend_with(text):
    backend = CodexBackend()
    thread = {}
    backend._item(thread, 'a', text)
    backend.threads['owned'] = thread
    return backend


def test_context_preserves_identifiers_markdown_and_whitespace():
    text = '\n```python\nmy_value = a_b * 2\n  # next_step\n```\n> keep_me\n'
    assert backend_with(text).context('codex:owned')['items'][0]['x'] == text


def test_context_preserves_long_utf8_reply():
    text = '完整内容_路径 C:\\a_b\\file.md\n' * 500
    assert backend_with(text).context('codex:owned')['items'][0]['x'] == text


def test_compact_context_keeps_existing_summary_limit():
    text = '完整内容_路径 C:\\a_b\\file.md\n' * 500
    item = backend_with(text).context('codex:owned', compact=True)['items'][0]
    assert len(item['x'].encode('utf-8')) <= 240
    assert set(item) == {'s', 'r', 'x'}


def test_context_does_not_publish_internal_raw_field():
    assert set(backend_with('hello').context('codex:owned')['items'][0]) == {'s', 'r', 'x'}


def test_retention_and_cursor_order_still_hold():
    backend = backend_with('first')
    for index in range(205):
        backend._item(backend.threads['owned'], 'u', f'item_{index}')
    assert len(backend.threads['owned']['items']) == 200
    page = backend.context('codex:owned', before=200)
    assert page['more'] and page['newer']
    assert [i['s'] for i in page['items']] == list(range(192, 200))


def test_legacy_in_memory_context_remains_readable():
    backend = CodexBackend()
    backend.threads['owned'] = {'items': [{'s': 1, 'r': 'a', 'x': 'legacy'}]}
    assert backend.context('codex:owned')['items'] == [{'s': 1, 'r': 'a', 'x': 'legacy'}]


@pytest.fixture
def endpoint():
    state = {'allowed': True, 'calls': []}
    def context(sid, before=-1, after=-1, compact=False):
        state['calls'].append((sid, before, after, compact))
        return {'items': [{'s': 1, 'r': 'a', 'x': 'short' if compact else 'full_text\n  code'}]}
    token = local_api.new_token()
    api = local_api.LocalApi({'enabled': lambda: True, 'token': token,
        'text_enabled': lambda: state['allowed'], 'codex_service': SimpleNamespace(context=context)})
    assert api.start(0)[0]
    def get(query='', authenticated=True):
        request = urllib.request.Request(f'http://127.0.0.1:{api._port}/v1/codex/sessions/codex%3Aowned/context'+query)
        if authenticated:
            request.add_header('X-AC-Token', token)
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())
    yield state, get
    api.stop()


def test_http_context_defaults_to_original_text(endpoint):
    state, get = endpoint
    code, page = get()
    assert code == 200 and page['items'][0]['x'] == 'full_text\n  code'
    assert state['calls'] == [('codex:owned', -1, -1, False)]


def test_http_compact_context_is_explicit(endpoint):
    state, get = endpoint
    code, page = get('?compact=1&before=8&after=2')
    assert code == 200 and page['items'][0]['x'] == 'short'
    assert state['calls'] == [('codex:owned', 8, 2, True)]


@pytest.mark.parametrize('query', ['?compact=', '?compact=true', '?compact=2', '?compact=1&compact=0'])
def test_http_rejects_invalid_context_mode(endpoint, query):
    state, get = endpoint
    assert get(query) == (400, {'ok': False, 'reason': 'bad_context_mode'})
    assert not state['calls']


@pytest.mark.parametrize('query', ['', '?compact=1'])
def test_both_context_modes_require_separate_reading_permission(endpoint, query):
    state, get = endpoint
    state['allowed'] = False
    assert get(query) == (403, {'ok': False, 'reason': 'text_disabled'})
    assert not state['calls']


def test_context_modes_still_require_token(endpoint):
    state, get = endpoint
    assert get('?compact=1', authenticated=False) == (401, {'ok': False, 'reason': 'unauthorized'})
    assert not state['calls']
