"""One-click installation, integrity failover, restart environment and resume."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

import updater


def test_update_is_one_action_and_uses_the_full_close_path(monkeypatch):
    import gui
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    install = Mock()
    monkeypatch.setattr(updater, 'stage_and_swap', install)
    monkeypatch.setattr(gui.QMessageBox, 'question', Mock(side_effect=AssertionError('second confirmation')))
    running = dict(claude=False, codex=True, codex_app=False)
    window = SimpleNamespace(_pending_release=dict(version='3.1.1'), close=Mock(), _append_log=Mock(),
        codex_service=SimpleNamespace(monitor_state=lambda: dict(paused=False, running=running)))
    gui.MainWindow._on_update_ready(window, 'update.exe.new')
    install.assert_called_once_with('update.exe.new', resume=running)
    window.close.assert_called_once()


def test_failed_helper_keeps_the_current_gui_and_monitors_running(monkeypatch):
    import gui
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    monkeypatch.setattr(updater, 'stage_and_swap', Mock(side_effect=OSError('cannot start helper')))
    window = SimpleNamespace(_pending_release=dict(version='3.1.1'), close=Mock(), _append_log=Mock(),
        _on_update_available=Mock(),
        codex_service=SimpleNamespace(monitor_state=lambda: dict(paused=False, running=dict(codex=True))))
    gui.MainWindow._on_update_ready(window, 'update.exe.new')
    window.close.assert_not_called()
    window._on_update_available.assert_called_once()


@pytest.mark.parametrize('digest', [None, '', 'wrong'])
def test_missing_digest_stops_before_downloading(tmp_path, monkeypatch, digest):
    import gui
    monkeypatch.setattr(gui.sys, 'executable', str(tmp_path / 'Auto-Continue.exe'))
    download = Mock()
    monkeypatch.setattr(updater, 'download_update', download)
    errors = []
    worker = gui.Updater()
    worker.download_failed.connect(errors.append)
    worker.download_and_stage(dict(version='3.1.1', sha256=digest, asset_url='https://example.invalid/update'))
    assert len(errors) == 1 and 'SHA-256 is missing' in errors[0]
    download.assert_not_called()


def test_bad_mirror_digest_falls_back_to_verified_github(tmp_path, monkeypatch):
    content = b'MZgood github bytes'
    dest = tmp_path / 'update.exe'
    mirror = updater.mirror_zip_url('3.1.1')
    github = 'https://example.invalid/update.exe'
    monkeypatch.setattr(updater, 'choose_source', lambda *a: (mirror, True, 'mirror selected'))
    calls = []
    def download(url, path, **kwargs):
        calls.append(url)
        if url == mirror:
            with zipfile.ZipFile(path, 'w') as archive:
                archive.writestr('Auto-Continue.exe', b'MZbad mirror bytes')
        else:
            Path(path).write_bytes(content)
        return path
    monkeypatch.setattr(updater, 'download_asset', download)
    updater.download_update('3.1.1', github, str(dest), total_hint=len(content),
                            expected_sha256=hashlib.sha256(content).hexdigest())
    assert calls == [mirror, github] and dest.read_bytes() == content


def test_swap_preserves_profile_environment_and_monitor_switches(tmp_path, monkeypatch):
    target, new = tmp_path / 'Auto-Continue.exe', tmp_path / 'Auto-Continue.exe.new'
    target.write_bytes(b'MZold')
    new.write_bytes(b'MZnew')
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    monkeypatch.setenv('AUTO_CONTINUE_SETTINGS_FILE', 'isolated-profile.ini')
    monkeypatch.setenv('AUTO_CONTINUE_DISCOVERY_FILE', 'isolated-api.json')
    monkeypatch.setattr(updater.tempfile, 'tempdir', str(tmp_path))
    start = Mock()
    monkeypatch.setattr(updater.subprocess, 'Popen', start)
    updater.stage_and_swap(str(new), str(target), resume=dict(codex=True))
    env = start.call_args.kwargs['env']
    assert env['AUTO_CONTINUE_SETTINGS_FILE'] == 'isolated-profile.ini'
    assert env['AUTO_CONTINUE_DISCOVERY_FILE'] == 'isolated-api.json'
    assert json.loads(env['AUTO_CONTINUE_UPDATE_RESUME']) == dict(claude=False, codex=True, codex_app=False)
    bat = next(tmp_path.glob('*.bat')).read_text(encoding='utf-8')
    assert 'PYINSTALLER_RESET_ENVIRONMENT=1' in bat and '%AUTO_CONTINUE_UPDATE_EXE%.bak' in bat
    assert env['AUTO_CONTINUE_UPDATE_EXE'] == str(target)


def test_missing_staged_file_does_not_launch_a_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    start = Mock()
    monkeypatch.setattr(updater.subprocess, 'Popen', start)
    with pytest.raises(FileNotFoundError):
        updater.stage_and_swap(str(tmp_path / 'missing.exe'), str(tmp_path / 'target.exe'))
    start.assert_not_called()


def test_resume_is_consumed_once_and_invalid_values_are_rejected(monkeypatch):
    monkeypatch.setenv('AUTO_CONTINUE_UPDATE_RESUME', '{"codex":true}')
    assert updater.take_resume_state() == dict(claude=False, codex=True, codex_app=False)
    assert updater.take_resume_state() is None
    for invalid in (dict(codex='yes'), dict(another=True), [], dict(codex=1)):
        with pytest.raises(ValueError):
            updater.validate_resume_state(invalid)


def test_swap_failure_is_visible_on_the_next_start(tmp_path, monkeypatch):
    target = tmp_path / 'Auto-Continue.exe'
    marker = Path(updater.update_failure_marker_path(str(target)))
    marker.write_text('Update failed; previous version retained', encoding='utf-8')
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    monkeypatch.setattr(updater, 'current_exe_path', lambda: str(target))
    assert updater.take_update_failure() == 'Update failed; previous version retained'
    assert updater.take_update_failure() == ''


def test_swap_quotes_special_paths_and_refuses_command_lines():
    bat = updater.build_swap_bat(r'C:\100% ready!\new.exe', r'C:\100% ready!\app.exe')
    assert 'DisableDelayedExpansion' in bat and '100%% ready!' in bat
    with pytest.raises(ValueError):
        updater.build_swap_bat('new.exe\ncommand', 'app.exe')


@pytest.mark.skipif(os.name != 'nt', reason='Windows detached helper regression')
def test_actual_windowless_helper_handles_unicode_spaces_and_percent(tmp_path, monkeypatch):
    directory = tmp_path / '中文 空格 %AC_NOT_AN_ENV% ! & paths'
    directory.mkdir()
    target, new = directory / 'Auto-Continue.exe', directory / 'Auto-Continue.exe.new'
    target.write_bytes(b'previous executable')
    new.write_bytes(b'verified replacement')
    monkeypatch.setattr(updater, 'is_frozen', lambda: True)
    process = updater.stage_and_swap(str(new), str(target), relaunch=False)
    assert process.wait(timeout=20) == 0
    assert target.read_bytes() == b'verified replacement'
    assert Path(str(target) + '.bak').read_bytes() == b'previous executable'
    assert not new.exists()
