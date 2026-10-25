"""Optional interoperability check: run using an environment with mcp and PyYAML.

Only initialize, tools/list and list_instances are sent. No training is submitted.
"""
import argparse
import asyncio
import json
import tomllib
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import yaml


async def check(root):
    launches = []
    for name in ("generic", "claude-code", "cursor", "workbuddy"):
        path = root / (name + ".mcp.json")
        launches.append((name, json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["campus-runner"]))
    launches.append(("codex", tomllib.loads((root / "codex.toml").read_text(encoding="utf-8"))["mcp_servers"]["campus-runner"]))
    plugin = yaml.safe_load((root / "deepseek-harness.patch.yml").read_text(encoding="utf-8"))[0]["insert"][0]
    assert plugin["name"] == "@deepseek-ai/dsh-mcp-client"
    launches.append(("deepseek-harness", plugin["config"]))
    for client, launch in launches:
        params = StdioServerParameters(command=launch["command"], args=launch["args"], env=launch["env"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 10
                result = await session.call_tool("list_instances", {})
                assert not result.isError
                print(json.dumps({"client_config": client, "protocol": initialized.protocolVersion,
                                  "tools": len(tools.tools), "instance_count": len(json.loads(result.content[0].text))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-dir", required=True)
    asyncio.run(check(Path(parser.parse_args().config_dir)))
