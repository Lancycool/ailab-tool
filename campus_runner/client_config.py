"""Export local MCP configurations without changing any client's settings."""
import json
import os
import subprocess
import sys
from pathlib import Path

CLIENTS = ("generic", "codex", "claude-code", "cursor", "workbuddy", "deepseek-harness")
INSTRUCTIONS = (
    "Campus Runner 用于管理已配置的 SSH 训练实例。先调用 list_instances，必要时用 inspect_instance 检查。"
    "prepare_snapshot 等耗时操作返回 operation_id，请用 get_status 查询。"
    "工具会按需自动启动本地后台。create_batch 成功后会自动打开实验批准页；请用户点击批准并提交，服务自动提交，无需回聊天二次确认。"
    "若 panel_opened 为 false，请提示用户用启动脚本打开面板；不要为此重复创建清单。"
    "Agent 不得读取面板令牌、SSH 密码或代替用户批准。环境安装必须独立批准。"
    "重试提交保持原批次编号；网络断开不等于训练失败。用 read_logs 增量读日志、get_metrics 汇总指标、fetch_artifacts 按需下载。"
    "多客户端共享实例、批准和队列，请先查询已有任务，避免为同一实验重复创建批次。"
)


def launch_config(home, port, python=None):
    if not 1024 <= port <= 65535:
        raise ValueError("port 必须为 1024–65535")
    # Do not resolve a venv's symlink to the base interpreter.
    command = os.path.abspath(python or sys.executable)
    return {"command": command,
            "args": ["-X", "utf8", "-m", "campus_runner.cli", "--home", str(Path(home).absolute()), "--port", str(port), "mcp"],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}}


def render_config(client, launch):
    if client not in CLIENTS:
        raise ValueError("未知客户端：" + client)
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    if client == "codex":
        text = ('[mcp_servers.campus-runner]\ncommand = ' + quote(launch["command"]) +
                '\nargs = ' + quote(launch["args"]) + '\nstartup_timeout_sec = 15\ntool_timeout_sec = 60\nenabled = true\n' +
                '\n[mcp_servers.campus-runner.env]\n' + ''.join(k + ' = ' + quote(v) + '\n' for k, v in launch["env"].items()))
        return "codex.toml", text
    if client == "deepseek-harness":
        # A new row needs insert; an id-only override would silently miss it.
        text = ('- insert:\n    - id: campus-runner-mcp\n      name: "@deepseek-ai/dsh-mcp-client"\n      config:\n' +
                '        serverName: campus-runner\n        transport: stdio\n        command: ' + quote(launch["command"]) +
                '\n        args: ' + quote(launch["args"]) + '\n        env: ' + quote(launch["env"]) +
                '\n        toolCallTimeoutMs: 60000\n')
        return "deepseek-harness.patch.yml", text
    entry = dict(launch)
    if client in ("claude-code", "workbuddy"):
        entry = {"type": "stdio", **entry}
    return client + ".mcp.json", json.dumps({"mcpServers": {"campus-runner": entry}}, ensure_ascii=False, indent=2) + "\n"


def export_configs(home, port, output, client="all", python=None):
    launch = launch_config(home, port, python)
    selected = CLIENTS if client == "all" else (client,)
    # Render before writing so invalid choices do not leave a partial export.
    documents = [render_config(name, launch) for name in selected]
    root = Path(output).absolute()
    root.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, content in documents:
        target = root / name
        target.write_text(content, encoding="utf-8", newline="\n")
        paths.append(str(target))
    return {"files": paths, "state_directory": str(Path(home).absolute()), "port": port,
            "message": "配置已导出；请按客户端说明合并或导入。客户端原有设置未修改。"}


def check_connection(home, port, python=None, cwd=None):
    launch = launch_config(home, port, python)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "campus-mcp-check", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_instances", "arguments": {}}},
    ]
    result = subprocess.run([launch["command"], *launch["args"]], cwd=cwd,
                            env={**os.environ, **launch["env"]},
                            input="".join(json.dumps(r) + "\n" for r in requests), encoding="utf-8",
                            capture_output=True, timeout=75)
    if result.returncode:
        raise ValueError("MCP 子进程启动失败，请检查 Python 路径和依赖安装。")
    try:
        responses = {r["id"]: r for r in (json.loads(line) for line in result.stdout.splitlines())}
        initialized, tools, call = [responses[i]["result"] for i in (1, 2, 3)]
        if call.get("isError"):
            raise ValueError("MCP 协议连接成功，但后台未能就绪；请检查 service.stderr.log，并核对 --home、--port 和端口占用。")
        instances = json.loads(call["content"][0]["text"])
        return {"status": "ok", "protocol": initialized["protocolVersion"],
                "tools": [tool["name"] for tool in tools["tools"]], "instance_count": len(instances)}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP 输出格式不正确，stdout 应只输出协议消息。") from exc
