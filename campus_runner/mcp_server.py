"""Small MCP stdio adapter: JSON-RPC, tool discovery, no shell or credentials tools."""
import json
import sys
import urllib.error
import urllib.request

from .models import Batch
from .store import Store


def obj(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
ARRAY = {"type": "array", "items": S}
TOOLS = [
    ("list_instances", "列出实例及连接状态。密码不通过工具提供。", obj()),
    ("inspect_instance", "检查实例并验证断开 SSH 后的后台运行能力。返回操作编号。", obj({"key": S, "environment": S, "data_path": S}, ["key"])),
    ("prepare_snapshot", "创建不可变代码快照；排除数据、密钥和输出。返回操作编号。", obj({"project_path": S, "excludes": ARRAY}, ["project_path"])),
    ("create_batch", "创建实验草案，用户必须在本地面板批准。环境安装必须独立成批。", obj({"batch": Batch.model_json_schema()}, ["batch"])),
    ("submit_batch", "提交已批准清单。重试保持任务编号，不会重复启动。返回操作编号。", obj({"key": S}, ["key"])),
    ("get_status", "查询操作进度、批次和任务最后已知状态。离线不代表失败。", obj({"batch_id": S, "operation_id": S})),
    ("read_logs", "增量读取已同步日志，offset 为字节位置；tail=true 读取末尾。", obj({"run_id": S, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 262144}, "tail": {"type": "boolean"}}, ["run_id"])),
    ("get_metrics", "读取统一指标，生成 CSV 和 Markdown 报告。缺失数据会单独标记。", obj({"run_id": S, "batch_id": S})),
    ("fetch_artifacts", "按需下载指定结果文件，路径相对于任务输出目录。返回操作编号。", obj({"run_id": S, "paths": ARRAY}, ["run_id", "paths"])),
    ("cancel_jobs", "请求停止指定任务，先等待 30 秒正常退出。返回操作编号。", obj({"run_ids": ARRAY}, ["run_ids"])),
]

# Pydantic references point at the schema root, not at properties.batch.
for name, description, schema in TOOLS:
    if name == "create_batch":
        schema["$defs"] = schema["properties"]["batch"].pop("$defs", {})


def handle(message, call):
    method = message.get("method")
    if "id" not in message:
        return None
    response = {"jsonrpc": "2.0", "id": message["id"]}
    if method == "initialize":
        requested = message.get("params", {}).get("protocolVersion", "2024-11-05")
        version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25"
        response["result"] = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                              "serverInfo": {"name": "campus-runner", "version": "0.1.0"}}
    elif method == "ping":
        response["result"] = {}
    elif method == "tools/list":
        response["result"] = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
    elif method == "tools/call":
        params = message.get("params", {})
        name = params.get("name")
        if name not in {t[0] for t in TOOLS}:
            response["error"] = {"code": -32602, "message": "Unknown tool"}
        else:
            try:
                result = call(name, params.get("arguments", {}))
                response["result"] = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}
            except Exception as exc:
                response["result"] = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
    return response


def serve(home, port):
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    store = Store(home)
    def call(name, args):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/tools/{name}", data=json.dumps(args).encode(),
                                         headers={"Authorization": "Bearer " + store.token(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(exc.read().decode()) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError("本地管理服务未启动。请运行 campus-runner serve。") from exc
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Expected JSON object")
            response = handle(message, call)
        except (ValueError, TypeError) as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
