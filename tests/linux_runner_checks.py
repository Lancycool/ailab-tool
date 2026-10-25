"""Run with Linux Python: python3 tests/linux_runner_checks.py (stdlib only)."""
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "campus_runner" / "remote.py"
spec = importlib.util.spec_from_file_location("runner", MODULE)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)


def wait_until(callback, seconds=12):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = callback()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError("Timed out waiting for runner")


class LinuxRunnerChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="campus-runner-tests-")
        self.root = Path(self.tmp.name)
        self.runner = remote.Runner(self.root)
        self.processes = []
        self.conda = self.root / "fake-conda"
        self.conda.write_text("#!" + sys.executable + "\nimport os,sys\nos.execvp(sys.argv[5],sys.argv[5:])\n")
        self.conda.chmod(0o700)
        bindir = self.root / "bin"
        bindir.mkdir()
        smi = bindir / "nvidia-smi"
        smi.write_text("#!" + sys.executable + "\nimport sys\nif '--query-gpu=' in ' '.join(sys.argv): print('0, GPU-0, Test GPU, 10000, 0\\n1, GPU-1, Test GPU, 10000, 0')\n")
        smi.chmod(0o700)
        self.env = {**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]}

    def tearDown(self):
        for state in self.runner.states().values():
            proc = state.get("process")
            if remote.group_alive(proc):
                os.killpg(proc["pid"], signal.SIGKILL)
        heartbeat = remote.read(self.root / "heartbeat.json", {})
        if remote.alive(heartbeat.get("identity")):
            os.kill(heartbeat["identity"]["pid"], signal.SIGKILL)
        for p in self.processes:
            if p.poll() is None:
                p.kill()
            p.wait()
        for path in self.root.glob("runner.log"):
            self.assertNotIn("Traceback", path.read_text())
        self.tmp.cleanup()

    def stage(self, name="batch", jobs=None, script="print('done',flush=True)"):
        data = script.encode()
        blob = hashlib.sha256(data).hexdigest()
        (self.root / "blobs" / blob).write_bytes(data)
        manifest = {"files": [{"path": "train.py", "sha256": blob, "size": len(data)}]}
        snapshot = remote.sha(manifest)
        self.runner.snapshot(snapshot, manifest)
        base = {"id": "one", "run_id": name + "_one", "snapshot_id": snapshot, "kind": "train", "argv": [sys.executable, "train.py"],
                "environment": "test", "gpus": [0], "env": {}, "cwd": ".", "data_path": "/data", "metrics_path": "metrics.jsonl",
                "conda": str(self.conda), "concurrency": 2, "timeout_seconds": 30, "expires_epoch": time.time() + 86400,
                "dependency_runs": []}
        entries = [{**base, **j} for j in (jobs or [{}])]
        body = {"jobs": entries}
        payload = {"id": name, "digest": remote.sha(body), "body": body}
        self.runner.stage(payload)
        remote.atomic(self.root / "batches" / (name + ".active"), True)
        return payload

    def spawn(self, command, key=None):
        p = subprocess.Popen([sys.executable, str(MODULE), "--root", str(self.root), command] + ([key] if key else []),
                             env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        self.processes.append(p)
        return p

    def test_background_queue_and_duplicate_daemon(self):
        self.stage(jobs=[{}, {"id": "two", "run_id": "batch_two", "dependency_runs": ["batch_one"]}])
        daemon = self.spawn("daemon")
        wait_until(lambda: remote.read(self.root / "heartbeat.json"))
        duplicate = self.spawn("daemon")
        self.assertEqual(duplicate.wait(timeout=3), 0)
        wait_until(lambda: all(s["status"] == "succeeded" for s in self.runner.states().values()))
        self.assertIsNone(daemon.poll())
        self.assertEqual((self.root / "jobs/batch_one/output/train.log").read_text().strip(), "done")

    def test_activation_requires_live_daemon_heartbeat(self):
        self.stage()
        old = os.environ["PATH"]
        os.environ["PATH"] = self.env["PATH"]
        try:
            result = self.runner.activate("batch")
        finally:
            os.environ["PATH"] = old
        self.assertTrue(result["accepted"])
        self.assertTrue(remote.alive(remote.read(self.root / "heartbeat.json")["identity"]))
        wait_until(lambda: self.runner.states()["batch_one"]["status"] == "succeeded")

    def test_submission_replay_never_restarts(self):
        batch = self.stage()
        self.spawn("daemon")
        wait_until(lambda: self.runner.states()["batch_one"]["status"] == "succeeded")
        self.runner.stage(batch)
        time.sleep(.3)
        self.assertEqual(self.runner.states()["batch_one"]["status"], "succeeded")
        self.assertEqual((self.root / "jobs/batch_one/output/train.log").read_text().count("done"), 1)

    def test_failure_does_not_block_independent_task(self):
        self.stage(jobs=[{"argv": [sys.executable, "-c", "raise SystemExit(2)"]},
                         {"id": "two", "run_id": "batch_two", "gpus": [1]},
                         {"id": "three", "run_id": "batch_three", "dependency_runs": ["batch_one"]}])
        self.spawn("daemon")
        wait_until(lambda: self.runner.states()["batch_three"]["status"] == "blocked")
        states = self.runner.states()
        self.assertEqual(states["batch_one"]["exit_code"], 2)
        self.assertEqual(states["batch_two"]["status"], "succeeded")

    def test_cancellation_kills_process_group(self):
        self.stage(script="import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\nprint('started',flush=True)\ntime.sleep(120)")
        self.spawn("daemon")
        wait_until(lambda: self.runner.states()["batch_one"].get("process"))
        proc = self.runner.states()["batch_one"]["process"]
        self.runner.cancel(["batch_one"])
        wait_until(lambda: self.runner.states()["batch_one"]["status"] == "cancelled")
        self.assertFalse(remote.group_alive(proc))

    def test_timeout_and_no_automatic_retry(self):
        self.stage(jobs=[{"timeout_seconds": 1}], script="import time;time.sleep(120)")
        self.spawn("daemon")
        wait_until(lambda: self.runner.states()["batch_one"]["status"] == "timeout")
        self.assertFalse(remote.group_alive(self.runner.states()["batch_one"]["process"]))

    def test_worker_loss_retains_reservation(self):
        self.stage(script="import time;time.sleep(120)")
        self.runner.tick(gpus=[{"index": 0, "busy": False}], launch=False)
        worker = self.spawn("worker", "batch_one")
        wait_until(lambda: self.runner.states()["batch_one"].get("process"))
        worker.kill(); worker.wait()
        state = self.runner.states()["batch_one"]
        state["updated_at"] = time.time() - 20
        remote.atomic(self.root / "jobs/batch_one/state.json", state)
        self.runner.tick(gpus=[{"index": 0, "busy": True}], launch=False)
        self.assertEqual(self.runner.states()["batch_one"]["status"], "unknown")
        self.assertTrue(self.runner.states()["batch_one"]["resources_reserved"])
        os.killpg(state["process"]["pid"], signal.SIGKILL)
        wait_until(lambda: not remote.group_alive(state["process"]))
        self.runner.tick(gpus=[{"index": 0, "busy": False}], launch=False)
        self.assertFalse(self.runner.states()["batch_one"]["resources_reserved"])

    def test_probe_survives_rpc_exit(self):
        p = subprocess.run([sys.executable, str(MODULE), "--root", str(self.root), "rpc"],
                           input=json.dumps({"op": "probe_start", "probe_id": "offline"}), capture_output=True, text=True, timeout=5)
        self.assertTrue(json.loads(p.stdout)["ok"])
        wait_until(lambda: remote.read(self.root / "probes/offline.json"), seconds=11)

    def test_orphan_can_be_cancelled_after_worker_crash(self):
        self.stage(script="import time;time.sleep(120)")
        self.runner.tick(gpus=[{"index": 0, "busy": False}], launch=False)
        worker = self.spawn("worker", "batch_one")
        wait_until(lambda: self.runner.states()["batch_one"].get("process"))
        worker.kill(); worker.wait()
        state = self.runner.states()["batch_one"]
        state["updated_at"] = time.time() - 20
        remote.atomic(self.root / "jobs/batch_one/state.json", state)
        self.runner.cancel(["batch_one"])
        self.runner.tick(gpus=[{"index": 0, "busy": True}], launch=False)
        wait_until(lambda: not remote.group_alive(state["process"]))
        self.runner.tick(gpus=[{"index": 0, "busy": False}], launch=False)
        self.assertEqual(self.runner.states()["batch_one"]["status"], "cancelled")

    def test_symlink_outputs_are_not_exposed(self):
        self.stage()
        output = self.root / "jobs/batch_one/output"
        (output / "secret").symlink_to("/etc/passwd")
        self.assertEqual(self.runner.files("batch_one"), [])
        with self.assertRaises(ValueError):
            self.runner.chunk("batch_one", "secret")

    def test_pid_reuse_identity_mismatch(self):
        current = remote.identity(os.getpid())
        current["start"] = str(int(current["start"]) + 1)
        self.assertFalse(remote.alive(current))
        self.assertFalse(remote.group_alive(current))


if __name__ == "__main__":
    unittest.main(verbosity=2)
