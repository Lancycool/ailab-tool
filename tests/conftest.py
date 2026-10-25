import contextlib
import hashlib
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from campus_runner import remote
from campus_runner.service import Service


class MemoryCredentials:
    def __init__(self):
        self.values = {}
    def set(self, key, password):
        self.values[key] = password
    def get(self, key):
        return self.values[key]


@pytest.fixture
def local_lock(monkeypatch):
    @contextlib.contextmanager
    def noop(*args, **kwargs):
        yield True
    monkeypatch.setattr(remote, "lock", noop)


@pytest.fixture
def service(tmp_path, local_lock, monkeypatch):
    roots = {}
    class FakeSSH:
        fail = False
        drop_ack = False
        def __init__(self, instance, password):
            self.instance = instance
            self.runner = remote.Runner(roots.setdefault(instance["id"], tmp_path / ("remote-" + instance["id"])))
        def __enter__(self):
            if self.fail:
                raise ConnectionError("network interrupted")
            return self
        def __exit__(self, *args):
            pass
        def deploy(self):
            pass
        def rpc(self, op, **kwargs):
            if op == "inspect":
                return {"environment_exists": True, "data_exists": True, "gpus": [{"index": 0}, {"index": 1}]}
            if op == "gpus":
                return [{"index": 0, "busy": False}, {"index": 1, "busy": False}]
            if op == "activate":
                remote.atomic(self.runner.root / "batches" / (kwargs["batch_id"] + ".active"), True)
                if self.drop_ack:
                    raise ConnectionError("ack lost")
                return {"accepted": True}
            return self.runner.rpc({"op": op, **kwargs})
        def upload_snapshot(self, snapshot, blob_dir):
            for f in snapshot["manifest"]["files"]:
                shutil.copyfile(blob_dir / f["sha256"], self.runner.root / "blobs" / f["sha256"])
            return self.runner.snapshot(snapshot["id"], snapshot["manifest"])
    svc = Service(tmp_path / "state", transport=FakeSSH, credentials=MemoryCredentials())
    svc.test_transport = FakeSSH
    svc.test_roots = roots
    for key in ("a", "b"):
        instance = svc.save_instance({"id": key, "name": key, "host": "127.0.0.1", "username": "user", "root": "/workspace", "conda": "/opt/conda/bin/conda", "expires_at": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()}, "test-secret")
        instance.update(ready=True, host_key={"type": "ssh-ed25519", "base64": "fake"})
        svc.store.put("instance", key, instance)
    project = tmp_path / "project"
    project.mkdir()
    (project / "train.py").write_text("print('training')")
    svc.test_snapshot = svc.prepare_snapshot(str(project))
    yield svc
    svc.close()


def batch(svc, jobs=None):
    return {"name": "test", "jobs": jobs or [job(svc)]}


def job(svc, **changes):
    return {"id": "first", "instance_id": "a", "snapshot_id": svc.test_snapshot["id"], "argv": ["python", "train.py"], "environment": "train", "gpus": [0], **changes}


def approved(svc, value=None):
    result = svc.create_batch(value or batch(svc))
    svc.approve(result["batch_id"], result["digest"])
    return result["batch_id"]
