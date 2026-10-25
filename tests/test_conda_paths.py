import os

import pytest

from campus_runner.remote import resolve_conda
from campus_runner.transport import RemoteError
from conftest import approved


def test_installation_directory_resolves_bin(tmp_path):
    conda = tmp_path / "bin" / "conda"
    conda.parent.mkdir()
    conda.write_text("#!/bin/sh\n")
    conda.chmod(0o755)
    assert resolve_conda(str(tmp_path)) == str(conda.absolute())
    assert resolve_conda(str(conda)) == str(conda.absolute())


def test_condabin_directory_resolves(tmp_path):
    conda = tmp_path / "condabin" / "conda"
    conda.parent.mkdir()
    conda.write_text("#!/bin/sh\n")
    conda.chmod(0o755)
    assert resolve_conda(str(tmp_path)) == str(conda.absolute())


def test_environment_directory_has_actionable_error(tmp_path):
    (tmp_path / "conda-meta").mkdir()
    with pytest.raises(ValueError, match="训练环境目录"):
        resolve_conda(str(tmp_path))


def test_missing_conda_has_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        resolve_conda(str(tmp_path / "missing"))


def test_remote_validation_error_does_not_mark_ssh_offline(service):
    instance = service.store.get("instance", "a")
    service.failure(instance, RemoteError("Conda directory is invalid"))
    saved = service.store.get("instance", "a")
    assert saved["connection"] == "online"
    assert saved["last_error"] == "Conda directory is invalid"


def test_non_executable_file_reports_permission(tmp_path, monkeypatch):
    path = tmp_path / "conda"
    path.write_text("#!/bin/sh\n")
    monkeypatch.setattr(os, "access", lambda *args: False)
    with pytest.raises(PermissionError, match="文件没有执行权限"):
        resolve_conda(str(path))


def test_normalized_conda_is_used_for_training(service, monkeypatch):
    original = service.test_transport.rpc
    def rpc(self, op, **kwargs):
        value = original(self, op, **kwargs)
        if op == "inspect":
            value["conda_executable"] = "/resolved/bin/conda"
        return value
    monkeypatch.setattr(service.test_transport, "rpc", rpc)
    key = approved(service)
    assert service.submit_batch(key)["all_accepted"]
    from campus_runner.remote import read
    spec = read(service.test_roots["a"] / "jobs" / (key + "_first") / "spec.json")
    assert spec["conda"] == "/resolved/bin/conda"


def test_default_environment_is_inspected(service, monkeypatch):
    instance = service.store.get("instance", "a")
    instance["default_environment"] = "radar"
    service.store.put("instance", "a", instance)
    seen = []
    original = service.test_transport.rpc
    def rpc(self, op, **kwargs):
        if op == "probe_start": return {"started": True}
        if op == "probe_check": return {"passed": True}
        if op == "inspect": seen.append(kwargs["environment"])
        return original(self, op, **kwargs)
    monkeypatch.setattr(service.test_transport, "rpc", rpc)
    monkeypatch.setattr(service.stop, "wait", lambda timeout: False)
    assert service.inspect_instance("a")["ready"]
    assert seen == ["radar"]
