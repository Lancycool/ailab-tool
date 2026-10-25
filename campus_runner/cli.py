import argparse
import os
import webbrowser
from pathlib import Path

from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Codex 校园训练管理工具")
    parser.add_argument("--home", default=os.environ.get("CAMPUS_RUNNER_HOME", str(Path.home() / ".campus-runner")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("command", choices=["serve", "mcp", "open", "init"])
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port 必须为 1024–65535")
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
