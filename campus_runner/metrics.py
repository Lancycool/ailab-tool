"""Copy this dependency-free module into a training project if desired."""
import csv
import io
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path


def write_metrics(step, metrics, split="validation", epoch=None, path=None):
    record = {"schema_version": 1, "step": step, "split": split, "metrics": metrics,
              "timestamp": datetime.now(timezone.utc).isoformat()}
    if epoch is not None:
        record["epoch"] = epoch
    for value in metrics.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("指标必须为有限数值")
    target = Path(path or os.environ.get("CAMPUS_METRICS_PATH", "metrics.jsonl"))
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


def parse_metrics(data):
    records, warnings = [], []
    if not data:
        return {"records": [], "warnings": ["指标文件缺失或为空"]}
    for i, line in enumerate(data.splitlines(keepends=True), 1):
        try:
            if not line.endswith("\n"):
                raise ValueError("末行尚未完整写入")
            row = json.loads(line)
            if row.get("schema_version") != 1 or not isinstance(row.get("step"), (int, float)) or not math.isfinite(row["step"]) or not isinstance(row.get("split"), str) or not isinstance(row.get("timestamp"), str):
                raise ValueError("指标记录格式不正确")
            if not isinstance(row.get("metrics"), dict) or not row["metrics"]:
                raise ValueError("指标为空")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in row["metrics"].values()):
                raise ValueError("指标包含非有限数值")
            records.append(row)
        except (ValueError, TypeError, AttributeError) as exc:
            warnings.append(f"第 {i} 行：{exc}")
    return {"records": records, "warnings": warnings}


def reports(rows):
    out = io.StringIO(newline="")
    columns = ["job", "instance", "snapshot", "dataset", "evaluation", "metric", "split", "direction", "last", "best", "status", "warnings"]
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: ("'" + str(row.get(key, "")) if str(row.get(key, "")).startswith(("=", "+", "-", "@")) else row.get(key, "")) for key in columns})
    md = "# 实验汇总\n\n程序按数据集、评价方式、指标和方向区分结果。数值不代表跨组排名。\n\n"
    md += "| 任务 | 状态 | 数据集 / 评价 | 指标 | 最后值 | 最佳值 | 缺口 |\n|---|---|---|---|---|---|---|\n"
    for row in rows:
        cells = [row["job"], row["status"], row["dataset"] + " / " + row["evaluation"], row["metric"], row["last"], row["best"], row["warnings"]]
        md += "| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in cells) + " |\n"
    return out.getvalue(), md
