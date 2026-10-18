"""Several native GUIs share one logical chat, never several senders."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
from PyQt6.QtWidgets import QApplication
from codex_cli import Screen
from codex_watcher import CodexWatcher, State
from control_service import CodexService
from codex_bridge.client import CodexError

APP = QApplication.instance() or QApplication([])
TID = 'same-full-thread-id'


def setup_service():
    watcher = CodexWatcher(provider='codex_app')
    watcher.activities = NS(thread=lambda tid: dict(id=tid, title='same chat'),
                            thread_id_for_title=lambda title: TID,
                            completed_turn=lambda tid: 'completion')
    backend = NS(connected=False, close=lambda: None)
    return CodexService(CodexWatcher(), watcher, backend)


def views():
    return {h: NS(identity='identity-'+str(h), thread_hint='hint-'+str(h),
                  title='same chat', screen=Screen(identified=True)) for h in (10, 20)}


def test_duplicate_guis_bind_one_stable_window_and_keep_its_pending_receipt():
    service, data = setup_service(), views()
    with patch('control_service.codex_app.terminal_handles', return_value=[20, 10]), \
         patch('control_service.codex_app.window_from_handle', side_effect=lambda h: NS(hwnd=h)), \
         patch('control_service.codex_app.read_text', side_effect=lambda w: data[w.hwnd]), \
         patch('control_service.codex_app.process_identity', side_effect=lambda h: (100+h, 'codex.exe')):
        first = service.execute(dict(action='bind', thread_id=TID))
        assert first['hwnd'] == 10 and first['window_count'] == 2
        state = service.app_watcher.states[10]
        state.submission = receipt = object()
        second = service.execute(dict(action='bind', thread_id=TID))
        assert second['hwnd'] == 10
        assert service.app_watcher.states[10] is state
        assert state.submission is receipt
        assert service._visible(TID)[0].hwnd == 10
        data[10] = NS(identity='other-chat', thread_hint='other-hint', screen=Screen(identified=True), title='other')
        with pytest.raises(CodexError) as error:
            service.execute(dict(action='bind', thread_id=TID))
        assert error.value.reason == 'changed'
        assert service.native[TID][0] == 10  # Do not silently migrate to GUI 20.
    service.close()


def test_namesake_threads_still_cannot_bind_and_pid_reuse_holds():
    service, data = setup_service(), views()
    with patch('control_service.codex_app.terminal_handles', return_value=[10, 20]), \
         patch('control_service.codex_app.window_from_handle', side_effect=lambda h: NS(hwnd=h)), \
         patch('control_service.codex_app.read_text', side_effect=lambda w: data[w.hwnd]), \
         patch('control_service.codex_app.process_identity', return_value=(123, 'codex.exe')) as owner:
        service.app_watcher.activities.thread_id_for_title = lambda title: ''
        with pytest.raises(CodexError) as error:
            service.execute(dict(action='bind', thread_id=TID))
        assert error.value.reason == 'desktop_not_visible'
        service.app_watcher.activities.thread_id_for_title = lambda title: TID
        service.execute(dict(action='bind', thread_id=TID))
        owner.return_value = (124, 'codex.exe')
        with pytest.raises(CodexError) as error:
            service._visible(TID)
        assert error.value.reason == 'changed'
    service.close()


def watcher_with_mirrors():
    driver = NS(window_exists=lambda h: True)
    watcher = CodexWatcher(driver=driver, provider='codex_app')
    watcher.activities = None
    watcher.running = True
    now = datetime.now(timezone.utc)
    for h in (10, 20):
        watcher.states[h] = State(h, 'same chat', thread_id=TID, identity=str(h), thread_hint=str(h),
                                 screen=Screen(identified=True, ready=True), read_at=now)
    return watcher, [(watcher.states[h], NS(hwnd=h)) for h in (20, 10)]


def test_mirrored_permission_is_approved_once_and_table_has_one_session():
    watcher, members = watcher_with_mirrors()
    watcher.driver.approve_permission = Mock(return_value=True)
    for st, _ in members:
        st.screen = Screen(identified=True, permission=True)
    for st, window in watcher._one_window_per_thread(members):
        watcher._observe(st, window, datetime.now(timezone.utc))
    watcher.driver.approve_permission.assert_called_once()
    assert watcher.driver.approve_permission.call_args.args[0].hwnd == 10
    watcher._publish()
    assert len(watcher.latest_rows) == 1 and watcher.latest_rows[0]['tabs'] == 2
    assert watcher._one_window_per_thread(list(reversed(members)))[0][0].hwnd == 10
    watcher.driver.send_prompt = Mock(return_value=True)
    assert not watcher._send(watcher.states[20], NS(hwnd=20), 'continue', datetime.now(timezone.utc), 'manual')
    watcher.driver.send_prompt.assert_not_called()


def test_window_close_transfers_cooldown_but_never_an_uncertain_submission():
    watcher, members = watcher_with_mirrors()
    primary = watcher._one_window_per_thread(members)[0][0]
    primary.retry_attempts = 4
    primary.last_sent = datetime.now(timezone.utc) - timedelta(seconds=2)
    primary.submission = object()
    watcher.driver.window_exists = lambda h: h == 20
    assert watcher._one_window_per_thread([members[0]]) == []
    primary.submission = None
    chosen = watcher._one_window_per_thread([members[0]])[0][0]
    assert chosen.hwnd == 20 and chosen.retry_attempts == 4 and chosen.last_sent == primary.last_sent


def test_live_primary_switch_does_not_authorize_a_mirror_or_lose_full_id_boundary():
    watcher, members = watcher_with_mirrors()
    watcher._one_window_per_thread(members)
    assert watcher._one_window_per_thread([members[0]]) == []
    watcher.states[20].thread_id = 'different-full-id'
    assert watcher._one_window_per_thread([members[0]])[0][0].thread_id == 'different-full-id'


def test_actual_tick_reads_all_processes_but_dispatches_once_in_any_z_order():
    watcher, members = watcher_with_mirrors()
    watcher.activities = NS(read=lambda n: ([], ''), thread_id_for_title=lambda title: TID)
    data = views()
    watcher.driver.terminal_handles = Mock(return_value=[20, 10])
    watcher.driver.window_from_handle = lambda h: NS(hwnd=h, Name='same chat')
    watcher.driver.read_text = Mock(side_effect=lambda w: data[w.hwnd])
    watcher.driver.inspect_screen = lambda view, now: view.screen
    watcher.driver.session_key = lambda w, view: view.identity
    watcher._observe = Mock()
    watcher.tick()
    assert watcher.driver.read_text.call_count == 2
    assert watcher._observe.call_count == 1
    assert watcher._observe.call_args.args[0].hwnd == 10
    watcher._observe.reset_mock()
    watcher.driver.terminal_handles.return_value = [10, 20]
    watcher.tick()
    assert watcher._observe.call_count == 1 and watcher._observe.call_args.args[0].hwnd == 10
    assert len(watcher.latest_rows) == 1 and watcher.latest_rows[0]['tabs'] == 2
