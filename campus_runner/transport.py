import base64
import hashlib
import json
import posixpath
import shlex
import uuid
from pathlib import Path


class HostKeyRequired(Exception):
    def __init__(self, key):
        self.key = key
        self.fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(key["base64"])).digest()).decode().rstrip("=")
        super().__init__("服务器身份需要确认：" + self.fingerprint)


class RemoteError(RuntimeError):
    """Remote RPC answered successfully but the requested operation failed."""


class Credentials:
    def __init__(self, namespace):
        self.service = "campus-runner:" + hashlib.sha256(str(namespace).encode()).hexdigest()[:16]

    def backend(self):
        # Explicit Windows backend prevents silently falling back to plaintext storage.
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()

    def set(self, key, password):
        self.backend().set_password(self.service, key, password)

    def get(self, key):
        value = self.backend().get_password(self.service, key)
        if not value:
            raise ValueError("请在面板保存 SSH 密码")
        return value


class SSH:
    def __init__(self, instance, password):
        self.instance, self.password = instance, password
        self.client = None
        self.root = instance["root"] + "/.campus-runner"
        self.script = self.root + "/runner.py"

    def __enter__(self):
        import paramiko
        expected = self.instance.get("host_key")
        class Policy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, client, hostname, key):
                actual = {"type": key.get_name(), "base64": key.get_base64()}
                if actual != expected:
                    raise HostKeyRequired(actual)
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(Policy())
        try:
            self.client.connect(self.instance["host"], port=self.instance["port"], username=self.instance["username"],
                                password=self.password, look_for_keys=False, allow_agent=False,
                                timeout=15, banner_timeout=15, auth_timeout=15)
            self.client.get_transport().set_keepalive(30)
            self.sftp = self.client.open_sftp()
            return self
        except Exception:
            self.client.close()
            raise

    def __exit__(self, *args):
        self.client.close()

    def command(self, argv, input_data="", timeout=90):
        stdin, stdout, stderr = self.client.exec_command(shlex.join(argv), timeout=timeout)
        stdin.write(input_data)
        stdin.flush()
        stdin.channel.shutdown_write()
        # RPC emits bounded JSON; stderr is only used on interpreter/boot errors.
        data = stdout.read().decode("utf-8")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        if code and not data:
            raise RuntimeError("远程命令失败：" + err[-2000:])
        return data

    def mkdir(self, path):
        import stat
        current = "/"
        for part in path.strip("/").split("/"):
            current = posixpath.join(current, part)
            try:
                mode = self.sftp.lstat(current).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ValueError("远程管理目录不能包含符号链接")
            except FileNotFoundError:
                self.sftp.mkdir(current, mode=0o700)

    def deploy(self):
        self.mkdir(self.root)
        self.mkdir(self.root + "/blobs")
        data = Path(__file__).with_name("remote.py").read_bytes()
        # Use a versioned path so a new client never replaces code underneath active workers.
        self.script = self.root + "/runner-" + hashlib.sha256(data).hexdigest()[:16] + ".py"
        try:
            with self.sftp.open(self.script, "rb") as f:
                if hashlib.sha256(f.read()).digest() == hashlib.sha256(data).digest():
                    return
        except FileNotFoundError:
            pass
        temp = self.script + "." + uuid.uuid4().hex + ".tmp"
        with self.sftp.open(temp, "wb") as f:
            f.write(data)
        with self.sftp.open(temp, "rb") as f:
            if f.read() != data:
                raise ValueError("远程程序上传校验失败")
        self.sftp.rename(temp, self.script)

    def rpc(self, op, **kwargs):
        data = self.command([self.instance["python"], self.script, "--root", self.root, "rpc"], json.dumps({"op": op, **kwargs}))
        value = json.loads(data)
        if not value.get("ok"):
            raise RemoteError(value.get("error", "RemoteError") + ": " + value.get("message", ""))
        return value["result"]

    def upload_snapshot(self, snapshot, blob_dir):
        manifest = snapshot["manifest"]
        check = self.rpc("snapshot", snapshot_id=snapshot["id"], manifest=manifest)
        for sha in check.get("missing", []):
            temp = sha + "." + uuid.uuid4().hex + ".part"
            self.sftp.put(str(Path(blob_dir) / sha), self.root + "/blobs/" + temp)
            self.rpc("finalize_blob", name=temp, expected=sha)
        result = self.rpc("snapshot", snapshot_id=snapshot["id"], manifest=manifest)
        if not result["ready"]:
            raise RuntimeError("远程代码快照未准备完成")
