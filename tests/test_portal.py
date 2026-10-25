import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from campus_runner.api import create_app
from campus_runner.portal import ORIGIN, Observations, Portal, endpoint, expiry, parse_connection


def prepared(service):
    p = service.portal
    p.update(session_id="session", status="ready", selected="123", candidates=[{
        "platform_id": "123", "name": "school gpu", "expires_at": "2030-01-01T00:00:00+00:00",
        "observed_at": "2026-10-25T00:00:00+00:00", "remaining_text": "4天", "expiry_estimated": False,
    }], mappings=[{"host": "192.0.2.4", "port": 24060}])
    payload = {"session_id": "session", "platform_id": "123", "mapping_index": 0,
               "credential_instance_id": "a", "instance": {"name": "school gpu", "username": "user",
               "root": "/workspace", "conda": "/opt/conda", "expires_at": "2031-01-01T00:00:00+00:00"}}
    return p, payload


def test_only_ssh_external_mapping():
    text = '服务 SSH\n类型\ntcp\n实例内部地址\n*:22\n服务外部地址\n192.0.2.4:24060\n[2001:db8::1]:2200\n取消'
    assert parse_connection(text) == [{"host": "192.0.2.4", "port": 24060}, {"host": "2001:db8::1", "port": 2200}]
    assert parse_connection(text.replace('*:22', '*:2222')) == []
    assert parse_connection(text.replace('tcp', 'udp')) == []


@pytest.mark.parametrize('address', ['host:0', 'host:65536', '*:22', '0.0.0.0:22', 'https://host:22', 'host:22;whoami'])
def test_invalid_endpoint(address):
    with pytest.raises(ValueError):
        endpoint(address)


def test_expiry_is_conservative_and_exact_is_utc():
    now = datetime(2026, 10, 25, tzinfo=timezone.utc)
    value = expiry('4天8小时19分', now)
    assert value['expires_at'] == '2026-10-29T08:18:00+00:00'
    assert value['expiry_estimated']
    exact = expiry('4天', now, 1793232000)
    assert not exact['expiry_estimated']
    for text in ['超过4天', '已过期', '永久', 'soon', '']:
        assert expiry(text, now)['expires_at'] is None


def test_observations_never_retain_auth_or_unrelated_data():
    observer = Observations()
    observer.receive(json.dumps({'token': 'secret', 'items': [{'eid': 123, 'name': 'gpu', 'expStopAt': 1793232000,
                                                              'password': 'private'}]}))
    assert observer.apps == {'123': {'eid': 123, 'name': 'gpu', 'expStopAt': 1793232000}}
    observer.receive('not-json')
    observer.receive(b'\xff')
    assert 'secret' not in str(observer.__dict__)
    assert 'private' not in str(observer.__dict__)


def test_repeat_import_reuses_identity_password_and_binds_expiry(service):
    portal, payload = prepared(service)
    first = portal.save(service, payload)
    assert first['expires_at'] == '2030-01-01T00:00:00Z'
    assert first['host'] == '192.0.2.4'
    assert service.credentials.get(first['id']) == 'test-secret'
    payload.pop('credential_instance_id')
    second = portal.save(service, payload)
    assert first['id'] == second['id']
    assert len(service.list_instances()) == 3
    assert first['revision'] != second['revision']


def test_attach_old_instance_clears_key_and_preserves_environment(service):
    portal, payload = prepared(service)
    payload['target_instance_id'] = 'a'
    saved = portal.save(service, payload)
    assert saved['id'] == 'a'
    assert saved['host_key'] is None
    assert not saved['ready']
    assert saved['portal']['platform_id'] == '123'
    assert len(service.list_instances()) == 2


def test_reuse_requires_same_username(service):
    portal, payload = prepared(service)
    payload['instance']['username'] = 'different'
    with pytest.raises(ValueError, match='用户名'):
        portal.save(service, payload)
    assert len(service.list_instances()) == 2


@pytest.mark.parametrize('field,value', [('session_id','stale'), ('platform_id','other'), ('mapping_index',-1), ('mapping_index',900)])
def test_stale_or_mismatched_import_does_not_change_instances(service, field, value):
    portal, payload = prepared(service)
    payload[field] = value
    with pytest.raises(ValueError):
        portal.save(service, payload)
    assert len(service.list_instances()) == 2


def test_import_does_not_override_running_instance(service):
    portal, payload = prepared(service)
    payload['target_instance_id'] = 'a'
    service.store.put('job', 'active', {'instance_id':'a', 'status':'running'})
    with pytest.raises(ValueError, match='运行中'):
        portal.save(service, payload)
    assert service.store.get('instance','a')['host'] == '127.0.0.1'


def test_portal_is_panel_only(service):
    app = create_app(service.store.home, service, background=False)
    with TestClient(app, base_url='http://127.0.0.1:8765') as client:
        for action in ['start', 'status', 'read', 'save', 'close']:
            response = client.post('/api/panel/portal/' + action, json={}, headers={
                'Authorization':'Bearer ' + service.store.token()})
            assert response.status_code == 403
        assert client.get('/static/portal.js').status_code == 200


def test_worker_serializes_browser_calls_and_cleans_session():
    class Fake:
        closed = False
        def open(self): pass
        def tick(self): time.sleep(.005)
        def scan(self): return [{'platform_id':'123'}]
        def connections(self, candidate): return [{'host':'192.0.2.4','port':22}]
        def close(self): self.closed = True
    browser = Fake()
    portal = Portal(lambda: browser)
    session = portal.start()['session_id']
    def wait(status):
        deadline = time.monotonic() + 2
        while portal.status()['status'] != status:
            assert time.monotonic() < deadline
            time.sleep(.01)
    wait('waiting')
    portal.command('scan',session)
    wait('ready')
    portal.command('connections',session,'123')
    wait('ready')
    assert portal.status()['mappings'][0]['port'] == 22
    portal.close()
    assert browser.closed
    assert portal.status()['status'] == 'closed'
    assert portal.status()['candidates'] == []
