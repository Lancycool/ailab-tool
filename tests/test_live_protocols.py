import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import paramiko
import pytest

from campus_runner.transport import HostKeyRequired, SSH


def test_actual_mcp_process_calls_running_http_service(tmp_path,monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    state_home = tmp_path / '状态 with spaces'
    args = [sys.executable, "-m", "campus_runner.cli", "--home", str(state_home), "--port", str(port)]
    env = {**os.environ, "PYTHONUTF8": "1"}
    server = subprocess.Popen(args + ["serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    try:
        end = time.monotonic() + 10
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=.3) as response:
                    assert response.status == 200
                break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail("HTTP service did not start")
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_instances", "arguments": {}}}]
        result = subprocess.run(args + ["mcp"], input="".join(json.dumps(m) + "\n" for m in messages), encoding="utf-8", capture_output=True, env=env, timeout=10)
        assert result.returncode == 0, result.stderr
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(responses) == 3
        assert len(responses[1]["result"]["tools"]) == 10
        assert json.loads(responses[2]["result"]["content"][0]["text"]) == []
        schema = next(t["inputSchema"] for t in responses[1]["result"]["tools"] if t["name"] == "create_batch")
        assert "Job" in schema["$defs"]
        # A different Agent cwd and inherited proxy must not redirect localhost traffic.
        from campus_runner.client_config import check_connection
        with monkeypatch.context() as patch:
            patch.setenv('http_proxy','http://127.0.0.1:1')
            patch.setenv('https_proxy','http://127.0.0.1:1')
            patch.setenv('no_proxy','')
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                checks=list(pool.map(lambda _: check_connection(state_home,port,cwd=tmp_path),range(2)))
        for checked in checks:
            assert checked['status']=='ok'
            assert checked['instance_count']==0
            assert len(checked['tools'])==10
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_real_ssh_host_key_gate_before_password_auth():
    key = paramiko.RSAKey.generate(2048)
    authenticated = []
    stopping = threading.Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(.2)
    transports = []
    class Server(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            authenticated.append(username)
            return paramiko.AUTH_SUCCESSFUL
        def get_allowed_auths(self, username):
            return "password"
        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
    def accept():
        while not stopping.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(conn)
            transports.append(transport)
            transport.add_server_key(key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer)
            try:
                transport.start_server(server=Server())
            except (EOFError, OSError, paramiko.SSHException):
                transport.close()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    instance = {"host": "127.0.0.1", "port": listener.getsockname()[1], "username": "test", "root": "/test"}
    try:
        with pytest.raises(HostKeyRequired) as exc:
            with SSH(instance, "test-password"):
                pass
        assert authenticated == []
        instance["host_key"] = exc.value.key
        with SSH(instance, "test-password"):
            pass
        assert authenticated == ["test"]
        instance["host_key"] = {"type": "ssh-rsa", "base64": "not-the-same"}
        with pytest.raises(HostKeyRequired):
            with SSH(instance, "test-password"):
                pass
        assert authenticated == ["test"]
    finally:
        stopping.set()
        listener.close()
        for transport in transports:
            transport.close()
        thread.join(timeout=3)
