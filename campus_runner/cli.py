import argparse
import os
import json
import subprocess
import webbrowser
from pathlib import Path

from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Campus Runner 通用 Agent 校园训练管理工具")
    parser.add_argument("--home", default=os.environ.get("CAMPUS_RUNNER_HOME", str(Path.home() / ".campus-runner")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("command", choices=["serve", "mcp", "open", "init", "mcp-config", "mcp-check"])
    from .client_config import CLIENTS
    parser.add_argument("--client", choices=("all", *CLIENTS), default="all", help="导出配置的客户端")
    parser.add_argument("--output", help="配置导出目录，默认位于状态目录的 client-configs 中")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port 必须为 1024–65535")
    if args.command in ("mcp-config", "mcp-check"):
        from .client_config import export_configs, check_connection
        try:
            result = export_configs(args.home, args.port, args.output or Path(args.home) / "client-configs", args.client) if args.command == "mcp-config" else check_connection(args.home, args.port)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            parser.exit(1, str(exc) + "\n")
        return
    store = Store(args.home)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        print("管理服务：http://127.0.0.1:{}；请使用 campus-runner open 打开面板。".format(args.port))
        uvicorn.run(create_app(args.home, port=args.port), host="127.0.0.1", port=args.port, access_log=False)
    elif args.command == "mcp":
        from .mcp_server import serve
        serve(args.home, args.port)
    elif args.command == "open":
        webbrowser.open(f"http://127.0.0.1:{args.port}/#token={store.token('panel')}")
    else:
        print("本地状态目录已创建：" + str(store.home))


if __name__ == "__main__":
    main()
