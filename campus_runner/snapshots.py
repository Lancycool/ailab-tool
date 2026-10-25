import fnmatch
import hashlib
import json
import os
from pathlib import Path

from .models import digest

EXCLUDES = {".git", ".venv", "venv", "env", "envs", "node_modules", "__pycache__", ".pytest_cache", ".cache", "build", "dist", ".campus-runner", "data", "datasets", "outputs", "runs", "checkpoints", ".ssh", ".aws", ".codex"}
SECRET_PATTERNS = [".env*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*credentials*", "*.pth", "*.pt", "*.ckpt", "*.safetensors"]


def prepare(store, project_path, excludes=None):
    root = Path(project_path).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("项目路径不是目录")
    patterns = list(excludes or [])
    ignore = root / ".campusignore"
    if ignore.exists():
        patterns += [s.strip() for s in ignore.read_text(encoding="utf-8").splitlines() if s.strip() and not s.startswith("#")]
    blobs = store.home / "blobs"
    blobs.mkdir(exist_ok=True)
    files, excluded = [], []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDES and not (Path(base) / d).is_symlink() and not getattr(Path(base) / d, "is_junction", lambda: False)() and not (Path(base) / d).resolve().is_relative_to(store.home))
        for name in sorted(names):
            p = Path(base) / name
            rel = p.relative_to(root).as_posix()
            if rel == ".complete":
                raise ValueError("项目根目录的 .complete 是程序保留文件名")
            if p.is_symlink() or any(fnmatch.fnmatch(name, pat) for pat in SECRET_PATTERNS) or any(fnmatch.fnmatch(rel, pat) for pat in patterns):
                excluded.append(rel)
                continue
            if p.stat().st_size > 20 * 1024 * 1024:
                raise ValueError(f"代码文件超过 20 MB，请通过 .campusignore 排除：{rel}")
            content = p.read_bytes()
            sha = hashlib.sha256(content).hexdigest()
            target = blobs / sha
            if not target.exists():
                temp = blobs / (sha + "." + os.urandom(6).hex() + ".tmp")
                temp.write_bytes(content)
                os.replace(temp, target)
            files.append({"path": rel, "sha256": sha, "size": len(content)})
    if not files:
        raise ValueError("项目没有可上传的文件")
    manifest = {"files": files}
    key = digest(manifest)
    value = {"id": key, "project": root.name, "source": str(root), "manifest": manifest, "excluded": excluded}
    store.put("snapshot", key, value)
    return value
