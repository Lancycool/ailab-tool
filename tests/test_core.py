import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from campus_runner import metrics, remote
from campus_runner.models import Batch
from campus_runner.snapshots import prepare
from campus_runner.store import Store
from conftest import approved, batch, job


def test_snapshot_immutable_and_excludes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "train.py").write_text("v1")
    (source / ".env").write_text("SECRET")
    (source / "data").mkdir()
    (source / "data" / "large").write_text("dataset")
    store = Store(tmp_path / "state")
    first = prepare(store, source)
    assert [f["path"] for f in first["manifest"]["files"]] == ["train.py"]
    (source / "train.py").write_text("v2")
    second = prepare(store, source)
    assert first["id"] != second["id"]
    assert (store.home / "blobs" / first["manifest"]["files"][0]["sha256"]).read_text() == "v1"


@pytest.mark.parametrize("mode", ["cycle", "cross-instance", "duplicate", "gpu", "mixed"])
def test_batch_rejects_invalid_graph(service, mode):
    jobs = [job(service), job(service, id="second", depends_on=["first"])]
    if mode == "cycle": jobs[0]["depends_on"] = ["second"]
    if mode == "cross-instance": jobs[1]["instance_id"] = "b"
    if mode == "duplicate": jobs[1]["id"] = "first"
    if mode == "gpu": jobs[0]["gpus"] = [0, 0]
    if mode == "mixed": jobs[1].update(kind="environment", gpus=[])
    with pytest.raises(ValueError): Batch.model_validate(batch(service, jobs))


def test_approval_tampering_rejected(service):
    key = approved(service)
    stored = service.store.get("batch", key)
    stored["body"]["spec"]["jobs"][0]["argv"] = ["malicious-change"]
    service.store.put("batch", key, stored)
    with pytest.raises(ValueError, match="变化"): service.submit_batch(key)


def test_unapproved_not_submitted(service):
    key = service.create_batch(batch(service))["batch_id"]
    with pytest.raises(ValueError, match="批准"): service.submit_batch(key)


def test_changed_instance_invalidates_approval(service):
    key = approved(service)
    inst = service.store.get("instance", "a")
    inst["revision"] = "new-lease"
    service.store.put("instance", "a", inst)
    result = service.submit_batch(key)
    assert not result["all_accepted"]
    assert "变化" in result["instances"]["a"]["error"]


def test_multihost_idempotent_lost_ack(service):
    key = approved(service, batch(service, [job(service), job(service, id="second", instance_id="b")]))
    service.test_transport.drop_ack = True
    assert not service.submit_batch(key)["all_accepted"]
    service.test_transport.drop_ack = False
    assert service.submit_batch(key)["all_accepted"]
    assert service.submit_batch(key)["all_accepted"]
    assert len(service.store.all("job")) == 2
    assert all(len(list((root / "jobs").iterdir())) == 1 for root in service.test_roots.values())


def test_disconnect_preserves_running_state(service):
    key = approved(service)
    service.submit_batch(key)
    run_id = key + "_first"
    remote.Runner(service.test_roots["a"]).state(run_id, "running")
    service.sync_instance("a")
    service.test_transport.fail = True
    with pytest.raises(ConnectionError): service.sync_instance("a")
    assert service.store.get("job", run_id)["status"] == "running"
    assert service.store.get("instance", "a")["connection"] == "offline"


def test_gpu_allocation_and_dependencies(service):
    jobs = [job(service), job(service, id="same_gpu"), job(service, id="other", gpus=[1]), job(service, id="dependent", depends_on=["first"])]
    key = approved(service, batch(service, jobs))
    service.submit_batch(key)
    runner = remote.Runner(service.test_roots["a"])
    runner.tick(gpus=[{"index": 0, "busy": False}, {"index": 1, "busy": False}], launch=False)
    states = runner.states()
    assert states[key + "_first"]["status"] == "starting"
    assert states[key + "_same_gpu"]["status"] == "queued"
    assert states[key + "_other"]["status"] == "starting"
    runner.state(key + "_first", "failed")
    runner.tick(gpus=[{"index": 0, "busy": False}, {"index": 1, "busy": True}], launch=False)
    assert runner.states()[key + "_dependent"]["status"] == "blocked"
    assert runner.states()[key + "_same_gpu"]["status"] == "starting"


def test_multigpu_waits_for_all_and_external_busy(service):
    key = approved(service, batch(service, [job(service, gpus=[0, 1])]))
    service.submit_batch(key)
    runner = remote.Runner(service.test_roots["a"])
    runner.tick(gpus=[{"index": 0, "busy": False}, {"index": 1, "busy": True}], launch=False)
    assert runner.states()[key + "_first"]["status"] == "queued"


def test_expiry_blocks_new_task(service):
    inst = service.store.get("instance", "a")
    inst["expires_at"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    service.store.put("instance", "a", inst)
    key = approved(service)
    service.submit_batch(key)
    runner = remote.Runner(service.test_roots["a"])
    runner.tick(gpus=[{"index": 0, "busy": False}], launch=False)
    assert runner.states()[key + "_first"]["status"] == "blocked"


def test_environment_maintenance_waits(service):
    training = approved(service)
    service.submit_batch(training)
    runner = remote.Runner(service.test_roots["a"])
    runner.state(training + "_first", "running")
    install = approved(service, batch(service, [job(service, id="install", kind="environment", gpus=[], argv=["pip", "install", "x"])]))
    service.submit_batch(install)
    runner.tick(gpus=[{"index": 0, "busy": True}], launch=False)
    assert runner.states()[install + "_install"]["status"] == "queued"


def test_incomplete_upload_cannot_stage(tmp_path, local_lock):
    runner = remote.Runner(tmp_path / "remote")
    manifest = {"files": [{"path": "a.py", "sha256": hashlib.sha256(b"ok").hexdigest(), "size": 2}]}
    result = runner.snapshot(remote.sha(manifest), manifest)
    assert not result["ready"]
    assert not (runner.root / "snapshots" / remote.sha(manifest)).exists()
    bad = runner.root / "blobs" / "bad.part"
    bad.write_bytes(b"wrong")
    with pytest.raises(ValueError): runner.finalize_blob("bad.part", manifest["files"][0]["sha256"])


def test_low_disk_does_not_publish(tmp_path, monkeypatch):
    runner = remote.Runner(tmp_path)
    sha = hashlib.sha256(b"ok").hexdigest()
    (runner.root / "blobs" / sha).write_bytes(b"ok")
    manifest = {"files": [{"path": "a.py", "sha256": sha, "size": 2}]}
    monkeypatch.setattr(remote.shutil, "disk_usage", lambda p: type("Disk", (), {"free": 0})())
    with pytest.raises(ValueError, match="space"): runner.snapshot(remote.sha(manifest), manifest)


def test_metrics_corruption_and_partial(tmp_path):
    path = tmp_path / "metrics.jsonl"
    metrics.write_metrics(1, {"loss": 0.5}, path=path)
    data = path.read_text() + '{"incomplete":' + "\n" + '{"partial"'
    parsed = metrics.parse_metrics(data)
    assert len(parsed["records"]) == 1
    assert len(parsed["warnings"]) == 2
    with pytest.raises(ValueError): metrics.write_metrics(2, {"loss": float("nan")}, path=path)


def test_sync_reports_artifacts_and_metrics(service):
    key = approved(service)
    service.submit_batch(key)
    run = key + "_first"
    runner = remote.Runner(service.test_roots["a"])
    output = runner.jobdir(run) / "output"
    metrics.write_metrics(1, {"loss": 0.7}, path=output / "metrics.jsonl")
    metrics.write_metrics(2, {"loss": 0.3}, path=output / "metrics.jsonl")
    (output / "train.log").write_bytes(b"hello\n")
    (output / "weights.pt").write_bytes(b"weights")
    runner.state(run, "succeeded")
    service.sync_instance("a")
    result = service.get_metrics(run_id=run)
    assert result["rows"][0]["best"] == 0.3
    assert service.read_logs(run)["text"] == "hello\n"
    assert not (service.store.home / "results" / run / "weights.pt").exists()
    service.fetch_artifacts(run, ["weights.pt"])
    assert (service.store.home / "results" / run / "weights.pt").read_bytes() == b"weights"


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../../b", "C:/secret", "a\\b"])
def test_artifact_traversal(tmp_path, path):
    with pytest.raises(ValueError): remote.safe(tmp_path, path)
