import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from campus_runner import remote
from campus_runner.api import create_app
from conftest import batch


def wait_for(callback):
    deadline = time.monotonic() + 3
    while not callback():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_panel_approval_submits_once_without_second_codex_call(service, monkeypatch):
    created = service.create_batch(batch(service))
    started, release = threading.Event(), threading.Event()
    calls = []
    original = service.test_transport.upload_snapshot
    def upload(self, snapshot, blob_dir):
        calls.append(self.instance['id'])
        started.set()
        assert release.wait(3)
        return original(self, snapshot, blob_dir)
    monkeypatch.setattr(service.test_transport, 'upload_snapshot', upload)
    client = TestClient(create_app(service.store.home, service, background=False), base_url='http://127.0.0.1:8765')
    payload = {'batch_id':created['batch_id'], 'digest':created['digest']}
    headers = {'Authorization':'Bearer ' + service.store.token('panel')}
    try:
        first = client.post('/api/panel/approve', json=payload, headers=headers)
        assert first.status_code == 200
        assert started.wait(2)
        second = client.post('/api/panel/approve', json=payload, headers=headers)
        assert second.json()['operation_id'] == first.json()['operation_id']
        assert calls == ['a']
    finally:
        release.set()
    wait_for(lambda: service.store.get('operation',first.json()['operation_id'])['status']=='succeeded')
    saved = service.store.get('batch',created['batch_id'])
    assert saved['all_accepted']
    assert saved['submission_operation_id'] == first.json()['operation_id']


def test_changed_approval_and_unapproved_queue_do_not_execute(service, monkeypatch):
    created = service.create_batch(batch(service))
    monkeypatch.setattr(service, 'submit_batch', lambda key: pytest.fail('unapproved execution'))
    with pytest.raises(ValueError):
        service.approve_and_submit(created['batch_id'], '0'*64)
    with pytest.raises(ValueError):
        service.queue_submission(created['batch_id'])
    assert not service.store.all('operation')


def proc_fixture(tmp_path):
    proc = tmp_path/'proc'
    (proc/'self').mkdir(parents=True)
    (proc/'sys/kernel/random').mkdir(parents=True)
    (proc/'stat').write_text('cpu  100 10 20 800 30 10 20 10 99 20\n')
    (proc/'cpuinfo').write_text('processor : 0\nmodel name : Test CPU\n')
    (proc/'meminfo').write_text('MemTotal: 1000000 kB\nMemAvailable: 400000 kB\nMemFree: 100000 kB\n')
    (proc/'self/cgroup').write_text('0::/\n')
    (proc/'sys/kernel/random/boot_id').write_text('boot-1')
    return proc


def test_resource_snapshot_uses_single_gpu_query_and_excludes_guest_ticks(tmp_path, monkeypatch):
    proc = proc_fixture(tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['timeout'] == 5
        return SimpleNamespace(stdout='0, GPU-1, RTX Test, 65, 10240, 5120, 48, 120.5\n')
    monkeypatch.setattr(remote.subprocess,'run',run)
    result = remote.resource_info(proc, tmp_path/'missing-cgroup')
    assert len(calls) == 1
    assert result['cpu']['total'] == 1000
    assert result['cpu']['idle'] == 830
    assert result['memory']['used_bytes'] == 600000*1024
    assert result['gpus'][0]['utilization_percent'] == 65
    assert result['gpus'][0]['power_w'] == 120.5
    assert result['cpu']['scope']=='system'


def test_cgroup_limits_and_missing_gpu_fields(tmp_path,monkeypatch):
    proc=proc_fixture(tmp_path)
    cg=tmp_path/'cgroup'; cg.mkdir()
    (cg/'cpu.max').write_text('200000 100000')
    (cg/'cpu.stat').write_text('usage_usec 40000\nnr_periods 5\n')
    (cg/'memory.max').write_text('1000000')
    (cg/'memory.current').write_text('400000')
    monkeypatch.setattr(remote.subprocess,'run',lambda *a,**k: SimpleNamespace(stdout='0, GPU-1, Test, N/A, 10240, 0, N/A, [Not Supported]\n'))
    result=remote.resource_info(proc,cg)
    assert result['cpu']['scope']=='cgroup'
    assert result['cpu']['capacity']<=2
    assert result['memory']=={'scope':'cgroup','total_bytes':1000000,'used_bytes':400000}
    assert result['gpus'][0]['power_w'] is None


def test_gpu_timeout_does_not_hide_cpu_memory(tmp_path,monkeypatch):
    def fail(*args,**kwargs):
        raise subprocess.TimeoutExpired('nvidia-smi',5)
    monkeypatch.setattr(remote.subprocess,'run',fail)
    result=remote.resource_info(proc_fixture(tmp_path),tmp_path/'none')
    assert result['cpu']['model']=='Test CPU'
    assert result['memory']['total_bytes']>0
    assert not result['gpus'] and result['warnings']


def sample(total=100,idle=60):
    return {'cpu':{'scope':'system','total':total,'idle':idle,'logical_cpus':4,'model':'Test'},
            'memory':{'scope':'system','total_bytes':1000,'used_bytes':400},'gpus':[],
            'boot_id':'boot','collection_ms':1,'sampled_at':time.time(),'warnings':[]}


def test_resource_cache_computes_delta_and_ignores_different_instance_revision(service,monkeypatch):
    snapshots=iter([sample(),sample(200,80),sample(300,100)])
    original=service.test_transport.rpc
    monkeypatch.setattr(service.test_transport,'rpc',lambda self,op,**kwargs: next(snapshots) if op=='resources' else original(self,op,**kwargs))
    service.collect_resources('a')
    assert service.store.get('resources','a')['cpu']['percent'] is None
    service.collect_resources('a')
    assert service.store.get('resources','a')['cpu']['percent']==80
    instance=service.store.get('instance','a'); instance['revision']='new'
    service.store.put('instance','a',instance)
    assert 'resources' not in next(i for i in service.list_instances() if i['id']=='a')
    service.collect_resources('a')
    assert service.store.get('resources','a')['cpu']['percent'] is None


def test_monitor_requests_share_pending_work_and_cached_reads_never_collect(service,monkeypatch):
    calls=[]; release=threading.Event()
    def collect(iid):
        calls.append(iid); release.wait(3)
    monkeypatch.setattr(service,'collect_resources',collect)
    assert service.request_resources(False)['instances']
    assert not calls
    try:
        service.request_resources()
        service.request_resources()
        wait_for(lambda: len(calls)==2)
        assert sorted(calls)==['a','b']
    finally:
        release.set()


def test_monitor_errors_keep_last_sample_and_back_off(service,monkeypatch):
    old={**sample(),'revision':service.store.get('instance','a')['revision'],'received_at':'2026-10-25T00:00:00Z'}
    service.store.put('resources','a',old)
    def fail(*a,**k): raise ConnectionError('offline')
    monkeypatch.setattr(service.test_transport,'rpc',fail)
    service.collect_resources('a')
    saved=service.store.get('resources','a')
    assert saved['received_at']==old['received_at']
    assert saved['error'] and service.resource_due['a']>time.monotonic()+15
    client=TestClient(create_app(service.store.home,service,background=False),base_url='http://127.0.0.1:8765')
    assert client.post('/api/panel/resources',json={},headers={'Authorization':'Bearer '+service.store.token()}).status_code==403
