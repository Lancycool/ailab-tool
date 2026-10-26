"""Start the local service on demand, independently of the MCP client."""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def startup_lock(home):
    with (home / "service-start.lock").open("a+b") as lock:
        lock.seek(0)
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 20
        while True:
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("后台启动锁等待超时，请检查服务日志。")
                time.sleep(0.2)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def service_ready(store, port, opener):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tools/list_instances", data=b"{}",
        headers={"Authorization": "Bearer " + store.token(), "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=2):
            return True
    except urllib.error.HTTPError as exc:
        raise RuntimeError("本地端口已被占用，或服务的状态目录与 MCP 配置不一致。") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return False
        # Windows may time out rather than refuse an unused loopback port.
        # Only start after verifying that no listener owns that address.
        if isinstance(exc.reason, TimeoutError):
            try:
                with socket.socket() as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind(("127.0.0.1", port))
                return False
            except OSError:
                pass
        raise RuntimeError("本地服务未响应，请检查端口和后台日志。") from exc


def ensure_service(store, port, opener):
    with startup_lock(store.home):
        if service_ready(store, port, opener):
            return
        options = {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (store.home / "service.stdout.log").open("ab") as stdout, (store.home / "service.stderr.log").open("ab") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-m", "campus_runner.cli", "--home", str(store.home), "--port", str(port), "serve"],
                cwd=Path(__file__).resolve().parent.parent, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, **options)
        (store.home / "service.pid").write_text(str(process.pid), encoding="ascii")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if service_ready(store, port, opener):
                    return
            except RuntimeError:
                # A just-launched server can bind while its first probe times out.
                pass
            if process.poll() is not None:
                raise RuntimeError("后台启动失败，请查看状态目录中的 service.stderr.log。")
            time.sleep(0.2)
        raise RuntimeError("后台启动尚未完成，请查看 service.stderr.log，稍后重试。")
