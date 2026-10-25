"""Standalone Linux runner. Only Python >=3.8 standard library is required.

RPC input and output are JSON. No remote TCP listener is opened.
"""
import argparse
import base64
import contextlib
import csv
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "blocked", "unknown"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def safe(root, relative):
    root = Path(root).resolve()
    if not relative or "\\" in relative or ":" in relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("Unsafe relative path")
    path = root / relative
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks are not allowed")
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Path escapes root")
    return path


@contextlib.contextmanager
def lock(path, blocking=False):
    import fcntl
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        f.close()


def identity(pid):
    try:
        stat = Path("/proc/{}/stat".format(pid)).read_text()
        tail = stat[stat.rfind(")") + 2:].split()
        if tail[0] == "Z":
            return None
        return {"pid": pid, "start": tail[19], "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def alive(value):
    return bool(value and identity(value["pid"]) == value)


def group_alive(value):
    """Validate session membership even when the original group leader has exited."""
    if not value:
        return False
    try:
        if value["boot"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            return False
        leader = identity(value["pid"])
        if leader and leader != value:
            return False
        for p in Path("/proc").glob("[0-9]*/stat"):
            try:
                stat = p.read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                if fields[0] != "Z" and int(fields[2]) == value["pid"] and int(fields[3]) == value["pid"] and int(fields[19]) >= int(value["start"]):
                    return True
            except (OSError, ValueError, IndexError):
                continue
    except OSError:
        return False
    return False


def gpu_info():
    result = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,memory.total,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True)
    gpus = []
    for line in result.stdout.splitlines():
        index, uuid, name, total, used = [x.strip() for x in line.split(",", 4)]
        gpus.append({"index": int(index), "uuid": uuid, "name": name, "memory_total_mb": int(total), "memory_used_mb": int(used)})
    result = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=15, check=True)
    occupied = {line.split(",")[0].strip() for line in result.stdout.splitlines() if "," in line}
    for gpu in gpus:
        gpu["busy"] = gpu["uuid"] in occupied
    return gpus


def resource_info(proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
    """One non-blocking CPU/memory snapshot and a bounded GPU query."""
    started = time.monotonic()
    counters = [int(v) for v in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
    cpu = {"scope": "system", "total": sum(counters), "idle": counters[3] + counters[4],
           "logical_cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count(),
           "model": "Unknown"}
    for line in (proc / "cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            cpu["model"] = line.split(":", 1)[1].strip()
            break
    memory_values = {}
    for line in (proc / "meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory_values[key] = int(value.split()[0]) * 1024
    memory = {"scope": "system", "total_bytes": memory_values["MemTotal"],
              "used_bytes": max(0, memory_values["MemTotal"] - memory_values.get("MemAvailable", memory_values.get("MemFree", 0)))}
    # Resolve a cgroup v2 namespace mount. Unavailable limits fall back to labelled system values.
    candidates = [cgroup]
    try:
        relative = next(line.split("::", 1)[1] for line in (proc / "self/cgroup").read_text().splitlines() if line.startswith("0::"))
        nested = (cgroup / relative.lstrip("/")).resolve()
        if cgroup.resolve() in nested.parents:
            candidates.insert(0, nested)
    except (OSError, StopIteration):
        pass
    for directory in candidates:
        try:
            quota, period = (directory / "cpu.max").read_text().split()
            if quota != "max":
                capacity = min(float(cpu["logical_cpus"]), int(quota) / int(period))
                usage = dict(line.split() for line in (directory / "cpu.stat").read_text().splitlines())
                total = time.monotonic() * capacity * 1_000_000
                cpu.update(scope="cgroup", capacity=capacity, total=total, idle=total - int(usage["usage_usec"]))
            maximum = (directory / "memory.max").read_text().strip()
            if maximum != "max":
                memory.update(scope="cgroup", total_bytes=int(maximum), used_bytes=int((directory / "memory.current").read_text()))
            break
        except (OSError, ValueError, KeyError):
            continue
    gpus, warnings = [], []
    try:
        query = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,name,utilization.gpu,memory.total,memory.used,temperature.gpu,power.draw", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5, check=True)
        def number(value):
            try:
                return float(value.strip())
            except ValueError:
                return None
        for row in csv.reader(query.stdout.splitlines(), skipinitialspace=True):
            index, uid, name, utilization, total, used, temperature, power = row
            gpus.append({"index": int(index), "uuid": uid, "name": name, "utilization_percent": number(utilization),
                         "memory_total_mb": number(total), "memory_used_mb": number(used),
                         "temperature_c": number(temperature), "power_w": number(power)})
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        warnings.append("GPU 资源读取不可用：" + type(exc).__name__)
    return {"cpu": cpu, "memory": memory, "gpus": gpus, "warnings": warnings,
            "boot_id": (proc / "sys/kernel/random/boot_id").read_text().strip(),
            "sampled_at": time.time(), "collection_ms": round((time.monotonic() - started) * 1000)}


def detach(root, args):
    log = open(Path(root) / "runner.log", "ab", buffering=0)
    try:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root)] + args,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
        return p.pid
    finally:
        log.close()


def resolve_conda(value):
    path = Path(value)
    if path.is_dir():
        candidates = [path / "bin" / "conda", path / "condabin" / "conda"]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            if (path / "conda-meta").is_dir() or (path / "bin" / "python").is_file():
                raise ValueError("这里是训练环境目录，请把它填写到训练环境字段；Conda 程序应填写安装目录中的 bin/conda，例如 /opt/conda/bin/conda")
            raise ValueError("此目录中没有 bin/conda 或 condabin/conda，请填写 Conda 可执行文件路径")
        path = found
    if not path.is_file():
        raise ValueError("Conda 可执行文件不存在：" + str(path))
    if not os.access(str(path), os.X_OK):
        raise PermissionError("Conda 文件没有执行权限：" + str(path))
    return str(path.absolute())


class Runner:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("batches", "jobs", "blobs", "snapshots", "probes"):
            (self.root / name).mkdir(exist_ok=True)

    def jobdir(self, key):
        if not key or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in key):
            raise ValueError("Invalid identifier")
        return self.root / "jobs" / key

    def states(self):
        states = {}
        for p in (self.root / "jobs").glob("*/state.json"):
            value = read(p)
            if value:
                states[p.parent.name] = value
        return states

    def state(self, key, status, **kwargs):
        directory = self.jobdir(key)
        previous = read(directory / "state.json", {})
        previous.update(kwargs)
        previous.update(status=status, updated_at=time.time())
        atomic(directory / "state.json", previous)
        return previous

    def inspect(self, conda, environment=None, data_path=None):
        if sys.version_info < (3, 8) or sys.platform != "linux":
            raise ValueError("Remote runner requires Linux and Python >=3.8")
        check = self.root / ".write-test"
        check.write_text("ok")
        check.unlink()
        conda = resolve_conda(conda)
        result = subprocess.run([conda, "env", "list", "--json"], capture_output=True, text=True, check=True, timeout=30)
        envs = json.loads(result.stdout)["envs"]
        resolved_env = next((str(Path(p).resolve()) for p in envs if Path(p).name == environment or p == environment), None)
        return {"python": sys.version, "root": str(self.root), "free_bytes": shutil.disk_usage(self.root).free,
                "conda_executable": conda,
                "gpus": gpu_info(), "environments": envs,
                "environment_path": resolved_env,
                "environment_exists": environment is None or any(Path(p).name == environment or p == environment for p in envs),
                "data_exists": not data_path or Path(data_path).is_dir(),
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}

    def snapshot(self, snapshot_id, manifest):
        if sha(manifest) != snapshot_id:
            raise ValueError("Snapshot digest mismatch")
        dest = safe(self.root / "snapshots", snapshot_id)
        if (dest / ".complete").exists():
            return {"ready": True}
        missing = []
        for item in manifest["files"]:
            blob = safe(self.root / "blobs", item["sha256"])
            if not blob.exists() or blob.stat().st_size != item["size"] or hashlib.sha256(blob.read_bytes()).hexdigest() != item["sha256"]:
                missing.append(item["sha256"])
        if missing:
            return {"ready": False, "missing": sorted(set(missing))}
        total = sum(f["size"] for f in manifest["files"])
        if shutil.disk_usage(self.root).free < total + 10 * 1024 * 1024:
            raise ValueError("Insufficient disk space")
        tmp = dest.with_name(dest.name + ".tmp-" + str(os.getpid()))
        tmp.mkdir(exist_ok=True)
        for item in manifest["files"]:
            target = safe(tmp, item["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(str(self.root / "blobs" / item["sha256"]), str(target))
        atomic(tmp / ".complete", manifest)
        if dest.exists():
            # Only our incomplete snapshot directory is removed, never user files.
            shutil.rmtree(str(tmp))
            raise ValueError("Snapshot directory exists without completion marker")
        os.rename(str(tmp), str(dest))
        return {"ready": True}

    def finalize_blob(self, name, expected):
        source = safe(self.root / "blobs", name)
        if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Uploaded file checksum mismatch")
        os.replace(str(source), str(safe(self.root / "blobs", expected)))
        return {"verified": True}

    def stage(self, batch):
        body = batch["body"]
        if sha(body) != batch["digest"]:
            raise ValueError("Batch digest mismatch")
        batch_id = batch["id"]
        path = safe(self.root / "batches", batch_id + ".json")
        with lock(self.root / "submission.lock", True):
            old = read(path)
            if old and old != batch:
                raise ValueError("Batch identifier already used")
            for job in body["jobs"]:
                directory = self.jobdir(job["run_id"])
                directory.mkdir(exist_ok=True)
                spec = read(directory / "spec.json")
                if spec and spec != job:
                    raise ValueError("Task identifier already used")
                source = safe(self.root / "snapshots", job["snapshot_id"])
                if not (source / ".complete").exists():
                    raise ValueError("Snapshot not ready")
                code = directory / "code"
                if not code.exists():
                    tmp = directory / "code.pending"
                    if tmp.exists():
                        shutil.rmtree(str(tmp))
                    shutil.copytree(str(source), str(tmp))
                    os.rename(str(tmp), str(code))
                (directory / "output").mkdir(exist_ok=True)
                atomic(directory / "spec.json", job)
                if not (directory / "state.json").exists():
                    self.state(job["run_id"], "queued")
            atomic(path, batch)
        return {"staged": True}

    def activate(self, batch_id):
        if not safe(self.root / "batches", batch_id + ".json").exists():
            raise ValueError("Batch not staged")
        atomic(safe(self.root / "batches", batch_id + ".active"), True)
        detach(self.root, ["daemon"])
        until = time.monotonic() + 8
        while time.monotonic() < until:
            heartbeat = read(self.root / "heartbeat.json")
            if heartbeat and time.time() - heartbeat["time"] < 10 and alive(heartbeat.get("identity")):
                return {"accepted": True}
            time.sleep(.2)
        raise RuntimeError("Queue staged but daemon heartbeat unconfirmed; retry the same batch")

    def cancel(self, run_ids):
        for key in run_ids:
            directory = self.jobdir(key)
            if not (directory / "spec.json").exists():
                raise ValueError("Unknown job")
            atomic(directory / "cancel.json", {"time": time.time()})
        return {"requested": run_ids}

    def tick(self, gpus=None, launch=True):
        jobs = []
        for path in sorted((self.root / "batches").glob("*.active")):
            batch = read(path.with_suffix(".json"))
            if batch:
                jobs.extend(batch["body"]["jobs"])
        states = self.states()
        # Workers hold locks for their whole lifetime. A missing worker is not retried.
        for job in jobs:
            key = job["run_id"]
            st = states[key]
            if st["status"] in ("starting", "running"):
                with lock(self.jobdir(key) / "worker.lock") as acquired:
                    if acquired and time.time() - st["updated_at"] > 10:
                        reserved = group_alive(st.get("process")) if st.get("process") else st.get("launch_boot") == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
                        self.state(key, "unknown", resources_reserved=reserved, reason="Worker disappeared; reconcile before approving a new task")
        states = self.states()
        for job in jobs:
            key = job["run_id"]
            st = states[key]
            if st["status"] == "unknown" and st.get("process"):
                present = group_alive(st["process"])
                cancelled = (self.jobdir(key) / "cancel.json").exists()
                if present and cancelled:
                    if not st.get("cancel_sent_at"):
                        os.killpg(st["process"]["pid"], signal.SIGTERM)
                        states[key] = self.state(key, "unknown", cancel_sent_at=time.time())
                    elif time.time() - st["cancel_sent_at"] >= 30:
                        os.killpg(st["process"]["pid"], signal.SIGKILL)
                elif not present:
                    states[key] = self.state(key, "cancelled" if cancelled else "unknown", resources_reserved=False)
        active = [j for j in jobs if states[j["run_id"]]["status"] in ("starting", "running") or (states[j["run_id"]]["status"] == "unknown" and states[j["run_id"]].get("resources_reserved", True))]
        # Unknown tasks reserve resources until an operator resolves their process status.
        held = {g for j in active for g in j["gpus"]}
        try:
            need_gpu = any(states[j["run_id"]]["status"] == "queued" and j["gpus"] for j in jobs)
            gpus = (gpu_info() if need_gpu else []) if gpus is None else gpus
            available = {g["index"] for g in gpus if not g["busy"]}
        except Exception:
            available = set()
        for job in jobs:
            key = job["run_id"]
            if states[key]["status"] != "queued":
                continue
            directory = self.jobdir(key)
            if (directory / "cancel.json").exists():
                states[key] = self.state(key, "cancelled")
                continue
            deps = [states.get(d, {"status": "unknown"})["status"] for d in job["dependency_runs"]]
            if any(s in TERMINAL - {"succeeded"} for s in deps):
                states[key] = self.state(key, "blocked", reason="Dependency did not succeed")
                continue
            if any(s != "succeeded" for s in deps):
                continue
            if time.time() + job["timeout_seconds"] + 1800 > job["expires_epoch"]:
                states[key] = self.state(key, "blocked", reason="Instance lifetime insufficient; approve a new batch after renewal")
                continue
            same_env = [j for j in active if j.get("environment_key", j["environment"]) == job.get("environment_key", job["environment"])]
            if same_env and (job["kind"] == "environment" or any(j["kind"] == "environment" for j in same_env)):
                continue
            if len(active) >= min([job["concurrency"]] + [j["concurrency"] for j in active]):
                continue
            requested = set(job["gpus"])
            if requested & held or not requested <= available:
                continue
            boot_path = Path("/proc/sys/kernel/random/boot_id")
            self.state(key, "starting", launch_at=time.time(), launch_boot=boot_path.read_text().strip() if boot_path.exists() else "test")
            held.update(requested)
            active.append(job)
            if launch:
                detach(self.root, ["worker", key])

    def daemon(self):
        with lock(self.root / "daemon.lock") as acquired:
            if not acquired:
                return
            while True:
                try:
                    self.tick()
                    atomic(self.root / "heartbeat.json", {"time": time.time(), "identity": identity(os.getpid())})
                except Exception as exc:
                    print(type(exc).__name__ + ": " + str(exc), flush=True)
                time.sleep(2)

    def worker(self, key):
        directory = self.jobdir(key)
        with lock(directory / "worker.lock") as acquired:
            if not acquired:
                return
            spec = read(directory / "spec.json")
            st = read(directory / "state.json")
            if st["status"] != "starting":
                return
            if (directory / "cancel.json").exists():
                self.state(key, "cancelled")
                return
            output = directory / "output"
            env = os.environ.copy()
            env.update(spec["env"])
            env.update(CUDA_VISIBLE_DEVICES=",".join(str(i) for i in spec["gpus"]),
                       CAMPUS_RUN_ID=key, CAMPUS_OUTPUT_DIR=str(output), CAMPUS_DATA_DIR=spec["data_path"],
                       CAMPUS_METRICS_PATH=str(safe(output, spec["metrics_path"])), PYTHONUNBUFFERED="1")
            command = [spec["conda"], "run", "--no-capture-output", "-n", spec["environment"]] + spec["argv"]
            if spec["environment"].startswith("/"):
                command[3] = "-p"
            if spec["kind"] == "environment":
                command = spec["argv"]
            started = time.time()
            try:
                with (output / "train.log").open("ab", buffering=0) as log:
                    child = subprocess.Popen(command, cwd=str(safe(directory / "code", spec["cwd"])), env=env,
                                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                    process = identity(child.pid)
                    self.state(key, "running", process=process, worker=identity(os.getpid()), started_at=started)
                    stopping, stop_time = None, None
                    while child.poll() is None or group_alive(process):
                        if stopping is None:
                            if (directory / "cancel.json").exists():
                                stopping = "cancelled"
                            elif time.time() - started > spec["timeout_seconds"]:
                                stopping = "timeout"
                            elif child.returncode is not None and group_alive(process):
                                stopping = "failed"
                            if stopping:
                                stop_time = time.time()
                                if group_alive(process):
                                    os.killpg(child.pid, signal.SIGTERM)
                        elif time.time() - stop_time >= 30 and group_alive(process):
                            os.killpg(child.pid, signal.SIGKILL)
                        time.sleep(0.3)
                    self.state(key, stopping or ("succeeded" if child.returncode == 0 else "failed"),
                               exit_code=child.returncode, finished_at=time.time())
            except Exception as exc:
                # If a child was created, uncertain state must retain its resource reservation.
                self.state(key, "unknown" if "child" in locals() and child.poll() is None else "failed", reason=str(exc))

    def files(self, key):
        root = self.jobdir(key) / "output"
        result = []
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in names:
                p = Path(base) / name
                if not p.is_symlink() and p.is_file():
                    result.append({"path": p.relative_to(root).as_posix(), "size": p.stat().st_size})
        return result

    def chunk(self, key, path, offset=0, limit=262144):
        p = safe(self.jobdir(key) / "output", path)
        if offset < 0 or limit < 1 or limit > 1048576:
            raise ValueError("Invalid range")
        size = p.stat().st_size
        if offset > size:
            offset = 0
        with p.open("rb") as f:
            f.seek(offset)
            data = f.read(limit)
        return {"offset": offset, "next_offset": offset + len(data), "size": size,
                "data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}

    def file_info(self, key, path):
        p = safe(self.jobdir(key) / "output", path)
        before = p.stat()
        checksum = hashlib.sha256()
        with p.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                checksum.update(block)
        after = p.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ValueError("File is changing; download after it is finalized")
        return {"size": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": checksum.hexdigest()}

    def rpc(self, request):
        op = request.pop("op")
        allowed = {"inspect", "snapshot", "finalize_blob", "stage", "activate", "cancel", "files", "chunk", "file_info"}
        if op in allowed:
            return getattr(self, op)(**request)
        if op == "status":
            return {"jobs": self.states(), "heartbeat": read(self.root / "heartbeat.json")}
        if op == "gpus":
            return gpu_info()
        if op == "resources":
            return resource_info()
        if op == "probe_start":
            probe = request["probe_id"]
            safe(self.root / "probes", probe + ".json")
            detach(self.root, ["probe", probe])
            return {"started": True}
        if op == "probe_check":
            return read(safe(self.root / "probes", request["probe_id"] + ".json"), {"passed": False})
        raise ValueError("Unknown operation")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("command", choices=["rpc", "daemon", "worker", "probe"])
    parser.add_argument("key", nargs="?")
    args = parser.parse_args()
    runner = Runner(args.root)
    if args.command == "rpc":
        try:
            print(json.dumps({"ok": True, "result": runner.rpc(json.load(sys.stdin))}))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": type(exc).__name__, "message": str(exc)}))
            sys.exit(1)
    elif args.command == "probe":
        time.sleep(8)
        atomic(safe(runner.root / "probes", args.key + ".json"), {"passed": True, "time": time.time()})
    elif args.command == "daemon":
        runner.daemon()
    else:
        runner.worker(args.key)


if __name__ == "__main__":
    main()
