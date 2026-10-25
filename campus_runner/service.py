import base64
import concurrent.futures
import fnmatch
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import metrics, snapshots
from .models import Batch, Instance, digest, relative, utcnow
from .store import Store
from .transport import Credentials, HostKeyRequired, RemoteError, SSH


class Service:
    def __init__(self, home, transport=SSH, credentials=None):
        self.store = Store(home)
        self.credentials = credentials or Credentials(self.store.home)
        self.transport = transport
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.locks = {}
        self.lock = threading.RLock()
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

    def instance_lock(self, key):
        with self.lock:
            return self.locks.setdefault(key, threading.RLock())

    def save_instance(self, value, password=None):
        instance = Instance.model_validate(value).model_dump(mode="json")
        key = instance["id"]
        with self.instance_lock(key):
            try:
                old = self.store.get("instance", key)
            except KeyError:
                old = {}
            if "portal" not in value and old.get("portal") and instance["username"] == old.get("username"):
                instance["portal"] = old["portal"]
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

    def list_instances(self):
        rows = self.store.all("instance")
        for row in rows:
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
        return {"batch_id": key, "digest": batch["digest"], "review_path": "/#approvals", "batch": batch}

    def approve(self, key, expected_digest):
        with self.lock:
            batch = self.store.get("batch", key)
            if expected_digest != digest(batch["body"]) or expected_digest != batch["digest"]:
                raise ValueError("清单已变化，请重新查看")
            batch.update(approved_digest=expected_digest, approved_at=utcnow())
            self.store.put("batch", key, batch)
            self.store.event("batch-approved", key, {"digest": expected_digest})
            return batch

    def submit_batch(self, key):
        # Per-batch lock avoids concurrent replay while keeping different batches independent.
        with self.instance_lock("batch:" + key):
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
                                                                   "instance_revision": inst["revision"], "status": "queued", "spec": job})
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
                            job.update(status="unknown", reason="远程缺少任务记录；请核对实例身份")
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
        jobs = self.store.all("job")
        return {"instances": self.list_instances(), "batches": [b for b in self.store.all("batch") if not batch_id or b["id"] == batch_id],
                "jobs": [j for j in jobs if not batch_id or j["batch_id"] == batch_id], "operations": self.store.all("operation")[:30]}

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
