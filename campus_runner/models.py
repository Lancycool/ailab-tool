import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def relative(value):
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or "\x00" in value or ":" in value:
        raise ValueError("必须使用不含 .. 的相对路径")
    return value


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Instance(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1)
    root: str
    python: str = "python3"
    conda: str
    default_environment: str = ""
    expires_at: datetime

    @field_validator("root", "conda")
    @classmethod
    def absolute_path(cls, value):
        if not value.startswith("/") or ".." in PurePosixPath(value).parts or "\x00" in value:
            raise ValueError("远程目录和 Conda 程序必须使用绝对路径")
        return value.rstrip("/") or "/"

    @field_validator("expires_at")
    @classmethod
    def aware(cls, value):
        if value.tzinfo is None:
            raise ValueError("到期时间必须包含时区")
        return value.astimezone(timezone.utc)


class Job(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    instance_id: str
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: Literal["train", "environment"] = "train"
    argv: list[str] = Field(min_length=1)
    environment: str = Field(min_length=1)
    gpus: list[int] = Field(default_factory=list)
    data_path: str = ""
    dataset: str = "default"
    evaluation: str = "default"
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=3600, ge=1, le=432000)
    depends_on: list[str] = Field(default_factory=list)
    metrics_path: str = "metrics.jsonl"
    artifacts: list[str] = Field(default_factory=lambda: ["*.json", "*.txt", "*.csv", "*.png"])
    primary_metric: str = "loss"
    metric_split: str = "validation"
    direction: Literal["min", "max"] = "min"

    @field_validator("cwd", "metrics_path")
    @classmethod
    def rel(cls, value):
        return relative(value)

    @field_validator("artifacts")
    @classmethod
    def patterns(cls, values):
        return [relative(v) for v in values]

    @model_validator(mode="after")
    def valid(self):
        if len(self.gpus) != len(set(self.gpus)) or any(g < 0 for g in self.gpus):
            raise ValueError("GPU 编号必须非负且不能重复")
        if self.kind == "train" and not self.gpus:
            raise ValueError("训练任务至少指定一张 GPU")
        if self.kind == "environment" and self.gpus:
            raise ValueError("环境维护任务不能申请 GPU")
        if any("\x00" in s for s in self.argv + list(self.env.values())):
            raise ValueError("命令不能包含 NUL")
        for key in self.env:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key.startswith("CAMPUS_") or key == "CUDA_VISIBLE_DEVICES":
                raise ValueError("环境变量名称无效或由程序保留")
        if self.data_path and not self.data_path.startswith("/"):
            raise ValueError("数据集路径必须为绝对路径")
        return self


class Batch(Strict):
    name: str = Field(min_length=1, max_length=200)
    jobs: list[Job] = Field(min_length=1, max_length=200)
    concurrency: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def graph(self):
        jobs = {j.id: j for j in self.jobs}
        if len(jobs) != len(self.jobs):
            raise ValueError("任务编号重复")
        if len({j.kind for j in self.jobs}) != 1:
            raise ValueError("环境安装必须独立成批批准")
        for key, value in self.concurrency.items():
            if key not in {j.instance_id for j in self.jobs} or not 1 <= value <= 32:
                raise ValueError("并发数必须为 1–32，并对应本批实例")
        visiting, visited = set(), set()
        def visit(key):
            if key in visiting:
                raise ValueError("任务依赖存在循环")
            if key in visited:
                return
            visiting.add(key)
            for dep in jobs[key].depends_on:
                if dep not in jobs or jobs[dep].instance_id != jobs[key].instance_id:
                    raise ValueError("依赖必须是同批次、同实例的任务")
                visit(dep)
            visiting.remove(key)
            visited.add(key)
        for key in jobs:
            visit(key)
        return self
