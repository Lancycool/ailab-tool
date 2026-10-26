import urllib.error
from unittest.mock import Mock

import pytest

from campus_runner import launcher
from campus_runner.store import Store


def test_ready_service_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "service_ready", lambda *args: True)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    launcher.ensure_service(Store(tmp_path), 8765, Mock())
    spawn.assert_not_called()


def test_start_detached_once_and_wait_for_ready(tmp_path, monkeypatch):
    states = iter([False, False, True, True])
    monkeypatch.setattr(launcher, "service_ready", lambda *args: next(states))
    monkeypatch.setattr(launcher.time, "sleep", lambda delay: None)
    spawn = Mock(return_value=Mock(pid=1234, poll=lambda: None))
    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    store = Store(tmp_path)
    launcher.ensure_service(store, 8765, Mock())
    launcher.ensure_service(store, 8765, Mock())
    spawn.assert_called_once()
    args, kwargs = spawn.call_args
    assert str(store.home) in args[0]
    assert kwargs['stdin'] == launcher.subprocess.DEVNULL
    assert kwargs.get('creationflags') or kwargs.get('start_new_session')
    assert (tmp_path / 'service.pid').read_text() == '1234'


def test_port_conflict_never_starts_process(tmp_path, monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.HTTPError('url', 401, 'Unauthorized', {}, None)
    spawn = Mock()
    monkeypatch.setattr(launcher.subprocess, 'Popen', spawn)
    with pytest.raises(RuntimeError, match='端口'):
        launcher.ensure_service(Store(tmp_path), 8765, opener)
    spawn.assert_not_called()


def test_failed_start_preserves_error(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'service_ready', lambda *args: False)
    monkeypatch.setattr(launcher.subprocess, 'Popen', Mock(return_value=Mock(pid=1234, poll=lambda: 1)))
    with pytest.raises(RuntimeError, match='service.stderr.log'):
        launcher.ensure_service(Store(tmp_path), 8765, Mock())


@pytest.mark.parametrize('occupied', [False, True])
def test_windows_timeout_checks_port_before_starting(tmp_path, monkeypatch, occupied):
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError(TimeoutError('timed out'))
    probe = Mock()
    if occupied:
        probe.bind.side_effect = OSError('Address in use')
    socket_factory = Mock()
    socket_factory.return_value.__enter__ = Mock(return_value=probe)
    socket_factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(launcher.socket, 'socket', socket_factory)
    if occupied:
        with pytest.raises(RuntimeError, match='未响应'):
            launcher.service_ready(Store(tmp_path), 8765, opener)
    else:
        assert launcher.service_ready(Store(tmp_path), 8765, opener) is False
