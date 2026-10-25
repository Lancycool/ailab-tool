import base64
import concurrent.futures
import fnmatch
import hashlib
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import metrics, snapshots
from .models import Batch, Instance, connection_identity, digest, relative, utcnow
from .store import Store
from .transport import Credentials, HostKeyRequired, RemoteError, SSH


def job_update_time(job, batch_created=None):
    """Return a real state/lifecycle timestamp, never the latest polling time."""
    for value in (job.get("updated_at"), job.get("finished_at"), job.get("started_at"), job.get("created_at"), batch_created):
        try:
            if value is None or isinstance(value, bool):
                continue
            epoch = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(value).timestamp()
            if math.isfinite(epoch) and 0 <= epoch < 253402300800:
                return epoch
        except (ValueError, TypeError, OverflowError, OSError):
            continue
    return None


class Service:
    def __init__(self, home, transport=SSH, credentials=None):
        self.store = Store(home)
        self.credentials = credentials or Credentials(self.store.home)
        self.transport = transport
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.resource_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        self.locks = {}
        self.lock = threading.RLock()
        self.registry_lock = threading.RLock()
        self.resource_lock = threading.RLock()
        self.resource_pending = {}
        self.resource_due = {}
        self.resource_failures = {}
        self.stop = threading.Event()
        self.poller = None
        from .portal import Portal
        self.portal = Portal()
        for op in self.store.all("operation"):
            if op["status"] in ("queued", "running"):
                op.update(status="interrupted", message="本地服务重启；请重试查询或提交，任务编号保持不变")
                self.store.put("operation", op["id"], op)

    def start(self):
        self.poller = threading.Thread(target=self.poll, daemon=True)
        self.poller.start()

    def close(self):
        self.stop.set()
        self.portal.close()
        if self.poller:
            self.poller.join(timeout=2)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.resource_pool.shutdown(wait=False, cancel_futures=True)

    def instance_lock(self, key):
        with self.lock:
            return self.locks.setdefault(key, threading.RLock())

    def save_instance(self, value, password=None, *, portal_import=False):
        instance = Instance.model_validate(value).model_dump(mode="json")
        key = instance["id"]
        with self.registry_lock, self.instance_lock(key):
            try:
                old = self.store.get("instance", key)
            except KeyError:
                old = {}
            old_source = dict(old.get("portal") or {})
            # Older releases stored the observation but not its independent expiry.
            if old_source and "expires_at" not in old_source:
                if old_source.get("expiry_estimated") and old_source.get("remaining_text"):
                    from .portal import expiry
                    old_source["expires_at"] = expiry(old_source["remaining_text"], datetime.fromisoformat(old_source["observed_at"]))["expires_at"]
                elif not old_source.get("expiry_estimated"):
                    old_source["expires_at"] = old.get("expires_at")
            if not portal_import:
                # Manual forms (including stale ones) cannot replace platform time.
                instance["portal"] = old_source or None
            else:
                source = instance.get("portal") or {}
                same = old_source and all(source.get(k) == old_source.get(k) for k in ("origin", "account", "platform_id"))
                if same and (not source.get("expires_at") or datetime.fromisoformat(source["observed_at"]) < datetime.fromisoformat(old_source["observed_at"])):
                    instance["portal"] = old_source
            authoritative = (instance.get("portal") or {}).get("expires_at")
            if authoritative:
                instance["expires_at"] = authoritative
            if any(j.get("instance_id") == key and (j.get("status") in ("queued", "starting", "running") or (j.get("status") == "unknown" and j.get("resources_reserved", True))) for j in self.store.all("job")):
                raise ValueError("该实例仍有运行中或状态待核实任务；请先核实任务，再更新连接")
            # Any edit creates a new revision and requires a new readiness probe.
            instance.update(revision=uuid.uuid4().hex, connection="unchecked", ready=False,
                            host_key=old.get("host_key") if all(instance.get(k) == old.get(k) for k in ("host", "port", "username")) and
                            all((instance.get("portal") or {}).get(k) == (old.get("portal") or {}).get(k) for k in ("origin", "account", "platform_id")) else None)
            if password:
                self.credentials.set(key, password)
            self.store.put("instance", key, instance)
            return instance

    def merge_unused_duplicate(self, keep_id, duplicate_id):
        """Maintenance repair: preserve the historical ID; archive an unused duplicate.

        Call with the local service stopped. Referenced duplicates are never removed.
        """
        if keep_id == duplicate_id:
            raise ValueError("合并需要两个不同实例编号")
        with self.registry_lock, self.instance_lock(keep_id), self.instance_lock(duplicate_id), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            records = {(kind, key): json.loads(value) for kind, key, value in db.execute("SELECT kind,id,value FROM records")}
            keep = records[("instance", keep_id)]
            duplicate = records[("instance", duplicate_id)]
            if connection_identity(keep) != connection_identity(duplicate) or keep["root"] != duplicate["root"]:
                raise ValueError("连接或工作目录不同，不能自动合并")
            if not keep.get("host_key") or keep.get("host_key") != duplicate.get("host_key"):
                raise ValueError("两条记录必须已确认相同的服务器指纹")
            source = duplicate.get("portal")
            if keep.get("portal") or not source or not source.get("expires_at"):
                raise ValueError("此修复仅支持把平台记录合并到未绑定的手动实例")
            for (kind, _), record in records.items():
                if kind == "job":
                    if record.get("instance_id") == duplicate_id:
                        raise ValueError("重复记录已有任务历史，不能自动移除")
                    if record.get("instance_id") == keep_id and record.get("status") in ("queued", "starting", "running", "unknown"):
                        raise ValueError("原实例仍有未结束任务，不能合并")
                if kind == "batch":
                    body = record.get("body", {})
                    if duplicate_id in record.get("instances", {}) or duplicate_id in body.get("bindings", {}) or any(j.get("instance_id") == duplicate_id for j in body.get("spec", {}).get("jobs", [])):
                        raise ValueError("重复记录已有实验清单，不能自动移除")
            merged = {**keep, "portal": source, "expires_at": source["expires_at"],
                      "revision": uuid.uuid4().hex, "ready": False, "connection": "unchecked"}
            # Validate configuration before changing either record; credentials stay under keep_id.
            Instance.model_validate({k: v for k, v in merged.items() if k in Instance.model_fields})
            self.credentials.get(keep_id)
            archive_id = uuid.uuid4().hex
            archive = {"keep_before": keep, "duplicate_before": duplicate, "merged_id": keep_id, "time": utcnow()}
            db.execute("INSERT INTO records VALUES(?,?,?)", ("instance_merge_archive", archive_id, json.dumps(archive)))
            db.execute("UPDATE records SET value=? WHERE kind='instance' AND id=?", (json.dumps(merged), keep_id))
            db.execute("DELETE FROM records WHERE kind='instance' AND id=?", (duplicate_id,))
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (utcnow(), "instances-merged", keep_id, json.dumps({"duplicate_id": duplicate_id, "archive_id": archive_id})))
            return {"instance_id": keep_id, "archive_id": archive_id}

    def list_instances(self):
        rows = self.store.all("instance")
        for row in rows:
            try:
                cached = self.store.get("resources", row["id"])
                if cached.get("revision") == row["revision"]:
                    row["resources"] = cached
            except KeyError:
                pass
            remain = datetime.fromisoformat(row["expires_at"]).timestamp() - time.time()
            row["expiry_warning"] = "expired" if remain <= 0 else "6h" if remain <= 21600 else "24h" if remain <= 86400 else None
        return rows

    def connect(self, instance):
        return self.transport(instance, self.credentials.get(instance["id"]))

    def failure(self, instance, exc):
        connection = "identity_required" if isinstance(exc, HostKeyRequired) else "online" if isinstance(exc, RemoteError) else "offline"
        instance.update(connection=connection, last_error=str(exc))
        if isinstance(exc, RemoteError):
            instance["last_seen"] = utcnow()
        if isinstance(exc, HostKeyRequired):
            instance.update(pending_key=exc.key, pending_fingerprint=exc.fingerprint, ready=False)
        self.store.put("instance", instance["id"], instance)

    def confirm_key(self, key, fingerprint):
        with self.instance_lock(key):
            instance = self.store.get("instance", key)
            if fingerprint != instance.get("pending_fingerprint") or not instance.get("pending_key"):
                raise ValueError("指纹已变化，请重新检查")
            instance.update(host_key=instance.pop("pending_key"), ready=False, connection="unchecked", revision=uuid.uuid4().hex)
            instance.pop("pending_fingerprint", None)
            self.store.put("instance", key, instance)
            self.store.event("host-key-confirmed", key, {"fingerprint": fingerprint})
            return instance

    def operation(self, action, callback):
        key = uuid.uuid4().hex
        item = {"id": key, "action": action, "status": "queued", "created_at": utcnow()}
        self.store.put("operation", key, item)
        def work():
            self.store.put("operation", key, {**item, "status": "running"})
            try:
                result = callback()
                self.store.put("operation", key, {**item, "status": "succeeded", "result": result})
            except Exception as exc:
                self.store.put("operation", key, {**item, "status": "failed", "error": type(exc).__name__, "message": str(exc)})
        self.pool.submit(work)
        return {"operation_id": key, "status": "queued"}

    def inspect_instance(self, key, environment=None, data_path=None):
        with self.instance_lock(key):
            instance = self.store.get("instance", key)
            environment = environment or instance.get("default_environment") or None
            instance["ready"] = False
            try:
                with self.connect(instance) as remote:
                    remote.deploy()
                    info = remote.rpc("inspect", conda=instance["conda"], environment=environment, data_path=data_path)
                    probe = uuid.uuid4().hex
                    remote.rpc("probe_start", probe_id=probe)
                # The transport really closes before waiting and reconnecting.
                if self.stop.wait(10):
                    raise RuntimeError("服务正在关闭")
                with self.connect(instance) as remote:
                    remote.deploy()
                    result = remote.rpc("probe_check", probe_id=probe)
                instance.update(connection="online", last_seen=utcnow(), inspection=info,
                                ready=bool(result.get("passed") and info["environment_exists"] and info["data_exists"]), last_error=None)
                self.store.put("instance", key, instance)
                return instance
            except Exception as exc:
                self.failure(instance, exc)
                raise

    def prepare_snapshot(self, project_path, excludes=None):
        return snapshots.prepare(self.store, project_path, excludes)

    def create_batch(self, value):
        spec = Batch.model_validate(value).model_dump(mode="json")
        bindings = {}
        for job in spec["jobs"]:
            self.store.get("snapshot", job["snapshot_id"])
            inst = self.store.get("instance", job["instance_id"])
            bindings[inst["id"]] = {k: inst[k] for k in ("revision", "host", "port", "username", "root", "conda", "expires_at")}
        body = {"spec": spec, "bindings": bindings}
        key = uuid.uuid4().hex
        batch = {"id": key, "body": body, "digest": digest(body), "approved_digest": None, "created_at": utcnow(), "instances": {}}
        self.store.put("batch", key, batch)
        return {"batch_id": key, "digest": batch["digest"], "review_path": "/#approvals", "next_step": "请用户在面板点击批准并提交；本地服务自动提交，无需返回聊天再次确认。", "batch": batch}

    def approve(self, key, expected_digest):
        with self.lock:
            batch = self.store.get("batch", key)
            if expected_digest != digest(batch["body"]) or expected_digest != batch["digest"]:
                raise ValueError("清单已变化，请重新查看")
            batch.update(approved_digest=expected_digest, approved_at=utcnow())
            self.store.put("batch", key, batch)
            self.store.event("batch-approved", key, {"digest": expected_digest})
            return batch

    def approve_and_submit(self, key, expected_digest):
        with self.instance_lock("dispatch:" + key):
            self.approve(key, expected_digest)
            return self.queue_submission(key)

    def queue_submission(self, key):
        with self.instance_lock("dispatch:" + key):
            batch = self.store.get("batch", key)
            if batch["approved_digest"] != digest(batch["body"]):
                raise ValueError("实验清单尚未批准或内容发生变化")
            previous = batch.get("submission_operation_id")
            if previous:
                operation = self.store.get("operation", previous)
                if operation["status"] in ("queued", "running") or batch.get("all_accepted"):
                    return {"operation_id": previous, "status": operation["status"], "batch_id": key}
            operation = self.operation("submit_batch", lambda: self.submit_batch(key))
            batch["submission_operation_id"] = operation["operation_id"]
            self.store.put("batch", key, batch)
            return {**operation, "batch_id": key}

    def request_resources(self, refresh=True):
        """Panel-driven, bounded collection; cache reads never contact a server."""
        instances = self.list_instances()
        with self.resource_lock:
            for iid, future in list(self.resource_pending.items()):
                if future.done():
                    del self.resource_pending[iid]
            if refresh:
                for instance in instances:
                    iid = instance["id"]
                    if instance.get("ready") and instance.get("host_key") and iid not in self.resource_pending and time.monotonic() >= self.resource_due.get(iid, 0):
                        self.resource_due[iid] = time.monotonic() + 5
                        self.resource_pending[iid] = self.resource_pool.submit(self.collect_resources, iid)
            return {"instances": [{"id": i["id"], "resources": i.get("resources"), "pending": i["id"] in self.resource_pending} for i in instances]}

    def collect_resources(self, iid):
        # Avoid queuing SSH probes behind uploads or other long operations.
        guard = self.instance_lock(iid)
        if not guard.acquire(blocking=False):
            return
        try:
            instance = self.store.get("instance", iid)
            if not instance.get("ready") or not instance.get("host_key"):
                return
            try:
                started = time.monotonic()
                with self.connect(instance) as remote:
                    remote.deploy()
                    sample = remote.rpc("resources")
                try:
                    previous = self.store.get("resources", iid)
                except KeyError:
                    previous = {}
                cpu, old_cpu = sample["cpu"], previous.get("cpu", {})
                cpu["percent"] = None
                if sample.get("boot_id") == previous.get("boot_id") and instance["revision"] == previous.get("revision") and cpu["scope"] == old_cpu.get("scope") and cpu.get("capacity") == old_cpu.get("capacity"):
                    total = cpu["total"] - old_cpu.get("total", cpu["total"])
                    idle = cpu["idle"] - old_cpu.get("idle", cpu["idle"])
                    if total > 0 and 0 <= idle <= total:
                        cpu["percent"] = round(100 * (total - idle) / total, 1)
                sample.update(revision=instance["revision"], received_at=utcnow(), rpc_elapsed_ms=round((time.monotonic() - started) * 1000), error=None)
                self.store.put("resources", iid, sample)
                self.resource_failures[iid] = 0
                self.resource_due[iid] = time.monotonic() + 5
            except Exception as exc:
                try:
                    previous = self.store.get("resources", iid)
                except KeyError:
                    previous = {}
                if previous.get("revision") != instance["revision"]:
                    previous = {}
                previous.update(error=type(exc).__name__ + ": " + str(exc), failed_at=utcnow(), revision=instance["revision"])
                self.store.put("resources", iid, previous)
                failures = self.resource_failures.get(iid, 0) + 1
                self.resource_failures[iid] = failures
                self.resource_due[iid] = time.monotonic() + min(300, 10 * 2 ** min(failures, 5))
                if isinstance(exc, HostKeyRequired):
                    self.failure(instance, exc)
        finally:
            guard.release()

    def submit_batch(self, key):
        # Per-batch lock avoids concurrent replay while keeping different batches independent.
        with self.instance_lock("batch:" + key):
            # Read only after queue_submission has persisted the operation reference.
            with self.instance_lock("dispatch:" + key):
                batch = self.store.get("batch", key)
            if batch["approved_digest"] != digest(batch["body"]):
                raise ValueError("实验清单尚未批准或内容发生变化")
            spec = batch["body"]["spec"]
            outcomes = {}
            def submit_one(iid):
                with self.instance_lock(iid):
                    inst = self.store.get("instance", iid)
                    if batch["body"]["bindings"][iid]["revision"] != inst["revision"]:
                        raise ValueError("实例信息发生变化，请创建并批准新清单")
                    if not inst.get("ready"):
                        raise ValueError("实例尚未通过环境与离线运行检查")
                    try:
                        with self.connect(inst) as remote:
                            remote.deploy()
                            jobs = [j for j in spec["jobs"] if j["instance_id"] == iid]
                            environment_keys = {}
                            conda_programs = {}
                            for job in jobs:
                                check = remote.rpc("inspect", conda=inst["conda"], environment=job["environment"], data_path=job["data_path"])
                                conda_programs[job["id"]] = check.get("conda_executable", inst["conda"])
                                environment_keys[job["id"]] = check.get("environment_path") or inst["conda"] + ":" + job["environment"]
                                if job["kind"] == "train" and (not check["environment_exists"] or not check["data_exists"]):
                                    raise ValueError("训练环境或数据集不存在")
                                if not set(job["gpus"]) <= {g["index"] for g in check["gpus"]}:
                                    raise ValueError("指定 GPU 不存在")
                                remote.upload_snapshot(self.store.get("snapshot", job["snapshot_id"]), self.store.home / "blobs")
                            remote_jobs = []
                            for job in jobs:
                                run_id = key + "_" + job["id"]
                                remote_jobs.append({**job, "run_id": run_id, "dependency_runs": [key + "_" + d for d in job["depends_on"]],
                                                    "environment_key": environment_keys[job["id"]],
                                                    "conda": conda_programs[job["id"]], "expires_epoch": datetime.fromisoformat(inst["expires_at"]).timestamp(),
                                                    "concurrency": spec["concurrency"].get(iid, 2)})
                                try:
                                    self.store.get("job", run_id)
                                except KeyError:
                                    self.store.put("job", run_id, {"id": run_id, "batch_id": key, "instance_id": iid,
                                                                   "instance_revision": inst["revision"], "status": "queued", "spec": job,
                                                                   "created_at": utcnow(), "updated_at": time.time()})
                            body = {"jobs": remote_jobs}
                            remote.rpc("stage", batch={"id": key, "digest": digest(body), "body": body})
                            remote.rpc("activate", batch_id=key)
                            inst.update(connection="online", last_seen=utcnow(), last_error=None)
                            self.store.put("instance", iid, inst)
                            return {"status": "accepted"}
                    except Exception as exc:
                        self.failure(inst, exc)
                        raise
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(submit_one, iid): iid for iid in batch["body"]["bindings"]}
                for future, iid in futures.items():
                    try:
                        outcomes[iid] = future.result()
                    except Exception as exc:
                        outcomes[iid] = {"status": "unconfirmed", "error": str(exc)}
            batch["instances"] = outcomes
            batch["all_accepted"] = all(v["status"] == "accepted" for v in outcomes.values())
            self.store.put("batch", key, batch)
            return batch

    def download(self, remote, run_id, path, append=False, budget=None):
        relative(path)
        root = self.store.home / "results" / run_id
        target = root / path
        if not target.resolve().is_relative_to(root.resolve()):
            raise ValueError("结果路径越界")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target if append else self.store.home / "transfers" / run_id / (path + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        expected = None
        if not append:
            expected = remote.rpc("file_info", key=run_id, path=path)
            meta = part.with_name(part.name + ".json")
            old = json.loads(meta.read_text()) if meta.exists() else None
            if old != expected:
                part.write_bytes(b"")
                meta.write_text(json.dumps(expected), encoding="utf-8")
        offset = part.stat().st_size if part.exists() else 0
        downloaded = 0
        while True:
            limit = min(262144, budget - downloaded) if budget is not None else 262144
            if limit <= 0:
                break
            chunk = remote.rpc("chunk", key=run_id, path=path, offset=offset, limit=limit)
            data = base64.b64decode(chunk["data"])
            if hashlib.sha256(data).hexdigest() != chunk["sha256"]:
                raise ValueError("结果下载校验失败")
            if chunk["offset"] != offset:
                offset = 0
                part.write_bytes(b"")
            with part.open("ab") as f:
                f.write(data)
            downloaded += len(data)
            offset = chunk["next_offset"]
            if offset >= chunk["size"] or not data:
                if not append:
                    verify = hashlib.sha256()
                    with part.open("rb") as f:
                        for block in iter(lambda: f.read(1024 * 1024), b""):
                            verify.update(block)
                    if part.stat().st_size != expected["size"] or verify.hexdigest() != expected["sha256"]:
                        part.unlink(missing_ok=True)
                        raise ValueError("完整文件校验失败，源文件可能在下载期间发生变化")
                    os.replace(part, target)
                break
        return {"path": str(target), "bytes_received": downloaded, "offset": offset}

    def sync_instance(self, iid):
        with self.instance_lock(iid):
            inst = self.store.get("instance", iid)
            jobs = [j for j in self.store.all("job") if j["instance_id"] == iid and j["instance_revision"] == inst["revision"]]
            if not jobs:
                return
            try:
                with self.connect(inst) as remote:
                    remote.deploy()
                    status = remote.rpc("status")
                    heartbeat = status.get("heartbeat")
                    if not heartbeat or time.time() - heartbeat["time"] > 15:
                        for bid in {j["batch_id"] for j in jobs}:
                            batch = self.store.get("batch", bid)
                            if batch.get("instances", {}).get(iid, {}).get("status") == "accepted":
                                remote.rpc("activate", batch_id=bid)
                                break
                    inst.update(connection="online", last_seen=utcnow(), heartbeat=status.get("heartbeat"), last_error=None)
                    if time.time() - inst.get("gpu_checked_at", 0) >= 60:
                        inst.update(gpus=remote.rpc("gpus"), gpu_checked_at=time.time())
                    for job in jobs:
                        state = status["jobs"].get(job["id"])
                        if state:
                            job.update(state, last_seen=utcnow())
                            entries = remote.rpc("files", key=job["id"])
                            retained, used = [], 0
                            for entry in entries:
                                path, size = entry["path"], entry["size"]
                                if path in ("train.log", job["spec"]["metrics_path"]):
                                    self.download(remote, job["id"], path, append=True, budget=2 * 1024 * 1024)
                                elif any(fnmatch.fnmatch(path, pat) for pat in job["spec"]["artifacts"]):
                                    if size > 20 * 1024 * 1024 or used + size > 200 * 1024 * 1024 or Path(path).suffix in (".pt", ".pth", ".ckpt", ".safetensors"):
                                        retained.append(entry)
                                    else:
                                        used += size
                                        # Download replaceable artifacts after completion to avoid mixed versions.
                                        if job["status"] in ("succeeded", "failed", "cancelled", "timeout", "blocked"):
                                            target = self.store.home / "results" / job["id"] / path
                                            if not target.exists() or target.stat().st_size != size:
                                                self.download(remote, job["id"], path)
                                else:
                                    retained.append(entry)
                            job["remote_files"] = retained
                            job["files"] = entries
                        else:
                            reason = "远程缺少任务记录；请核对实例身份"
                            if job.get("status") != "unknown" or job.get("reason") != reason:
                                job["updated_at"] = time.time()
                            job.update(status="unknown", reason=reason)
                        self.store.put("job", job["id"], job)
                        self.persist_config(job)
                self.store.put("instance", iid, inst)
            except Exception as exc:
                self.failure(inst, exc)
                raise

    def persist_config(self, job):
        root = self.store.home / "results" / job["id"]
        root.mkdir(parents=True, exist_ok=True)
        (root / "approved-config.json").write_text(json.dumps(job["spec"], ensure_ascii=False, indent=2), encoding="utf-8")

    def poll(self):
        due, failures, running = {}, {}, {}
        while not self.stop.wait(1):
            for iid, future in list(running.items()):
                if future.done():
                    try:
                        future.result()
                        failures[iid] = 0
                    except Exception:
                        failures[iid] = failures.get(iid, 0) + 1
                    due[iid] = time.monotonic() + min(300, 30 * 2 ** min(failures[iid], 4))
                    del running[iid]
            for inst in self.list_instances():
                iid = inst["id"]
                if inst.get("ready") and iid not in running and time.monotonic() >= due.get(iid, 0):
                    running[iid] = self.pool.submit(self.sync_instance, iid)

    def get_status(self, batch_id=None, operation_id=None):
        if operation_id:
            return self.store.get("operation", operation_id)
        batches = self.store.all("batch")
        batch_created = {b["id"]: b.get("created_at") for b in batches}
        jobs = []
        for job in self.store.all("job"):
            if batch_id and job["batch_id"] != batch_id:
                continue
            # A sync timestamp says when we read a record, not when its state changed.
            epoch = job_update_time(job, batch_created.get(job.get("batch_id")))
            job["status_updated_at"] = datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch is not None else None
            jobs.append(job)
        jobs.sort(key=lambda job: (job_update_time(job, batch_created.get(job.get("batch_id"))) or 0, job["id"]), reverse=True)
        return {"instances": self.list_instances(), "batches": [b for b in batches if not batch_id or b["id"] == batch_id],
                "jobs": jobs, "operations": self.store.all("operation")[:30]}

    def read_logs(self, run_id, offset=0, limit=32768, tail=False):
        self.store.get("job", run_id)
        if offset < 0 or not 1 <= limit <= 262144:
            raise ValueError("日志范围无效")
        path = self.store.home / "results" / run_id / "train.log"
        if not path.exists():
            return {"text": "", "next_offset": 0, "warning": "日志尚未同步"}
        size = path.stat().st_size
        if tail:
            offset = max(0, size - limit)
        elif offset > size:
            offset = 0
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(limit)
        return {"text": data.decode("utf-8", errors="replace"), "next_offset": offset + len(data), "local_size": size}

    def get_metrics(self, run_id=None, batch_id=None):
        jobs = [j for j in self.store.all("job") if (not run_id or j["id"] == run_id) and (not batch_id or j["batch_id"] == batch_id)]
        result, rows = {}, []
        for job in jobs:
            spec = job["spec"]
            path = self.store.home / "results" / job["id"] / spec["metrics_path"]
            parsed = metrics.parse_metrics(path.read_text(encoding="utf-8", errors="replace") if path.exists() else "")
            records = parsed["records"]
            stride = max(1, (len(records) + 1999) // 2000)
            sampled = records[::stride]
            if records and sampled[-1] != records[-1]:
                sampled.append(records[-1])
            result[job["id"]] = {**parsed, "records": sampled, "total_records": len(records), "sampled": stride > 1}
            values = [r["metrics"][spec["primary_metric"]] for r in parsed["records"] if r["split"] == spec["metric_split"] and spec["primary_metric"] in r["metrics"]]
            rows.append({"job": job["id"], "instance": job["instance_id"], "snapshot": spec["snapshot_id"], "dataset": spec["dataset"],
                         "evaluation": spec["evaluation"], "metric": spec["primary_metric"], "split": spec["metric_split"], "direction": spec["direction"],
                         "last": values[-1] if values else "", "best": (min(values) if spec["direction"] == "min" else max(values)) if values else "",
                         "status": job["status"], "warnings": "; ".join(parsed["warnings"])})
        csv_text, markdown = metrics.reports(rows)
        report_dir = self.store.home / "reports"
        report_dir.mkdir(exist_ok=True)
        report_id = digest({"run_id": run_id, "batch_id": batch_id})[:16]
        (report_dir / (report_id + ".csv")).write_text(csv_text, encoding="utf-8-sig")
        (report_dir / (report_id + ".md")).write_text(markdown, encoding="utf-8")
        return {"series": result, "rows": rows, "markdown": markdown, "csv": csv_text, "report_path": str(report_dir / (report_id + ".md"))}

    def fetch_artifacts(self, run_id, paths):
        job = self.store.get("job", run_id)
        with self.instance_lock(job["instance_id"]):
            inst = self.store.get("instance", job["instance_id"])
            if job["instance_revision"] != inst["revision"]:
                raise ValueError("实例已变更，请核对结果来源")
            with self.connect(inst) as remote:
                remote.deploy()
                return [self.download(remote, run_id, relative(path)) for path in paths]

    def cancel_jobs(self, run_ids):
        grouped = {}
        for key in run_ids:
            job = self.store.get("job", key)
            grouped.setdefault(job["instance_id"], []).append(job)
        result = {}
        for iid, jobs in grouped.items():
            with self.instance_lock(iid):
                inst = self.store.get("instance", iid)
                if any(j["instance_revision"] != inst["revision"] for j in jobs):
                    raise ValueError("实例已变化，不能向新实例发送旧任务的停止指令")
                with self.connect(inst) as remote:
                    remote.deploy()
                    result[iid] = remote.rpc("cancel", run_ids=[j["id"] for j in jobs])
        return result
