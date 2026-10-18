"""Recent native metadata is a read-only catalog, separate from control leases."""
import copy
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import Mock

import pytest
from PyQt6.QtCore import QCoreApplication
from control_service import CodexService
from local_api import LocalApi


@pytest.fixture
def service():
    app = QCoreApplication.instance() or QCoreApplication([])
    items = [dict(thread_id='full-thread-'+str(i), title='same title', cwd='D:/same') for i in range(9)]
    activities = SimpleNamespace(read=Mock(side_effect=lambda n: (copy.deepcopy(items[:n]), '')),
                                 thread=lambda tid: dict(title='older bound', cwd='D:/bound'))
    watcher = SimpleNamespace(config={'recent_limit': 5}, activities=activities, states={})
    backend = SimpleNamespace(snapshot=lambda: ([], {}), connected=False, error='', close=lambda: None)
    instance = CodexService(SimpleNamespace(), watcher, backend)
    instance.refresh()
    yield instance
    instance.close()


def test_count_changes_expand_catalog_on_worker_without_changing_automation_or_claims(service):
    original = copy.deepcopy(service.app_watcher.config)
    assert len(service.snapshot(5)['sessions']) == 5
    service.snapshot(8)
    service.app_watcher.activities.read.reset_mock()
    service.refresh()
    service.app_watcher.activities.read.assert_called_once_with(8)
    assert [r['thread_id'] for r in service.snapshot(8)['sessions']] == ['full-thread-'+str(i) for i in range(8)]
    assert len(service.snapshot(2)['sessions']) == 2
    assert service.app_watcher.config == original
    assert not service.bound and not service.leases and not service.native
    assert all(r['identity'] == '' and r['capabilities'] == [] for r in service.snapshot()['sessions'])


def test_older_bound_session_survives_count_reduction(service):
    service.bound.add('older-full-id')
    service.native['older-full-id'] = (12, 'identity', 'hint')
    service._visible = Mock(side_effect=RuntimeError('not visible'))
    service.refresh()
    page = service.snapshot(1)
    assert [r['thread_id'] for r in page['sessions']] == ['full-thread-0', 'older-full-id']
    assert page['sessions'][1]['bound'] and page['sessions'][1]['identity'] == 'identity'
    assert page['recent_desktop_ids'] == ['codex:desktop:full-thread-0']
    assert not service.leases


def test_http_recent_count_is_authenticated_bounded_and_separate_from_content_consent(service):
    api = LocalApi(dict(token='test-token', enabled=lambda: True,
                        text_enabled=lambda: False, codex_service=service))
    assert api.start(0)[0]
    def get(query, token='test-token'):
        req = Request('http://127.0.0.1:'+str(api.port)+'/v1/codex/sessions'+query,
                      headers={'X-AC-Token': token})
        try:
            with urlopen(req, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)
    try:
        assert get('?recent_limit=3')[0] == 200
        assert len(get('?recent_limit=3')[1]['sessions']) == 3
        assert get('?recent_limit=3', '')[0] == 401
        for query in ('?recent_limit=', '?recent_limit=0', '?recent_limit=101',
                      '?recent_limit=abc', '?recent_limit=1&recent_limit=2'):
            assert get(query) == (400, {'ok': False, 'reason': 'bad_recent_limit'})
        assert not service.leases
    finally:
        api.stop()
