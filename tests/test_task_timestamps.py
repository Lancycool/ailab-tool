import pytest

from campus_runner import remote
from campus_runner.service import job_update_time
from conftest import approved


@pytest.mark.parametrize('record,expected', [
    ({'updated_at':200,'last_seen':'2040-01-01T00:00:00Z'},200),
    ({'updated_at':'2026-10-25T08:00:00Z'},1792915200),
    ({'updated_at':'bad','finished_at':300,'started_at':200},300),
    ({'started_at':200,'created_at':'2026-01-01T00:00:00Z'},200),
    ({'updated_at':float('nan'),'finished_at':400},400),
    ({'last_seen':'2040-01-01T00:00:00Z'},None),
])
def test_actual_update_time_ignores_poll_time(record,expected):
    assert job_update_time(record)==expected


def test_api_jobs_sorted_by_state_change_with_creation_fallback(service):
    service.store.put('batch','legacy',{'id':'legacy','created_at':'2026-10-25T10:00:00Z'})
    records=[
        {'id':'old','updated_at':100,'last_seen':'2040-01-01T00:00:00Z'},
        {'id':'new','updated_at':200,'last_seen':'2020-01-01T00:00:00Z'},
        {'id':'created','batch_id':'legacy'},
        {'id':'unknown','last_seen':'2050-01-01T00:00:00Z'},
    ]
    for record in records:
        service.store.put('job',record['id'],record)
    rows=service.get_status()['jobs']
    assert [r['id'] for r in rows]==['created','new','old','unknown']
    assert rows[-1]['status_updated_at'] is None
    assert rows[1]['status_updated_at']=='1970-01-01T00:03:20+00:00'
    # Reading status produces display metadata without rewriting stored history.
    assert 'status_updated_at' not in service.store.get('job','new')


def test_sync_does_not_change_finished_task_update_time(service):
    key=approved(service)
    service.submit_batch(key)
    run_id=key+'_first'
    runner=remote.Runner(service.test_roots['a'])
    remote.atomic(runner.jobdir(run_id)/'state.json',{'status':'succeeded','updated_at':1000,'finished_at':1000,'exit_code':0})
    service.sync_instance('a')
    before=service.get_status()['jobs'][0]
    service.sync_instance('a')
    after=service.get_status()['jobs'][0]
    assert before['status_updated_at']==after['status_updated_at']
    assert before['updated_at']==after['updated_at']==1000
    assert before['last_seen']!=after['last_seen']


def test_new_queued_jobs_have_time_before_first_sync(service):
    key=approved(service)
    service.submit_batch(key)
    record=service.store.get('job',key+'_first')
    assert record['created_at'] and record['updated_at']>0
    assert service.get_status()['jobs'][0]['status_updated_at']


def test_missing_remote_record_changes_state_time_only_once(service):
    key=approved(service)
    service.submit_batch(key)
    run_id=key+'_first'
    runner=remote.Runner(service.test_roots['a'])
    (runner.jobdir(run_id)/'state.json').unlink()
    service.sync_instance('a')
    before=service.store.get('job',run_id)
    service.sync_instance('a')
    after=service.store.get('job',run_id)
    assert before['status']==after['status']=='unknown'
    assert before['updated_at']==after['updated_at']
