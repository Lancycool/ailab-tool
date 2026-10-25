# MCP 客户端接入

Campus Runner 提供本地 MCP STDIO 服务。客户端启动一个轻量适配进程，适配进程再访问已经运行的本地管理服务。多个客户端共用同一个实例库和远程队列。

## 生成配置

你先按 [README](../README.md) 安装项目并启动本地服务，然后在仓库目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure-mcp.ps1 -Client all
```

程序会在 `.campus-runner/client-configs/` 生成六份配置。配置使用当前虚拟环境 Python 的绝对路径、项目状态目录和指定端口，包含 UTF-8 环境设置，不包含 SSH 密码或面板令牌。客户端从其他项目目录启动时，也能导入已安装的 Campus Runner。

你可以用 `-Client claude-code`、`cursor`、`workbuddy`、`deepseek-harness`、`codex` 或 `generic` 只生成某一种配置。你也可以通过 `-Port 8899` 和 `-OutputDirectory <目录>` 指定端口及输出位置。端口必须与启动脚本一致。

配置导出不会安装客户端，也不会修改客户端设置。你更新生成文件后，应把 `campus-runner` 对应项合并进目标配置，保留其他 MCP 服务器。项目移动后，你需要重新生成配置。

## Claude Code

你可以直接用生成文件启动：

```powershell
claude --mcp-config "E:\你的项目目录\.campus-runner\client-configs\claude-code.mcp.json"
```

你将示例路径替换为本次生成文件的实际绝对路径。你也可以把文件中的 `mcpServers.campus-runner` 合并进训练项目根目录的 `.mcp.json`。客户端提示信任或启用时，你按客户端流程确认，然后用 `/mcp` 检查状态。[Claude Code 官方说明](https://code.claude.com/docs/en/mcp)

## Cursor

你把 `cursor.mcp.json` 中的服务器项合并到训练项目的 `.cursor/mcp.json`，或个人目录下的 `.cursor/mcp.json`。你保存后在 Cursor MCP 设置中启用，并重新加载客户端。[Cursor 官方说明](https://prod.cursor.com/help/customization/mcp)

## WorkBuddy

你在当前版本的自定义 MCP 连接器或 MCP 配置编辑入口中，导入 `workbuddy.mcp.json` 的服务器项。传输方式是 `stdio`，启动程序、参数数组和环境变量都以生成文件为准。你不要把整个参数数组拼成一个字符串。

WorkBuddy 的官方配置使用 `mcpServers`，本地服务可以通过 `command`、`args` 和 `env` 启动。[WorkBuddy 官方连接器说明](https://open.workbuddy.cn/docs/connector)

生成文件适用于本地配置；它不是可直接提交到连接器市场的完整插件包。市场包还需要平台要求的元信息与图标。各版本设置入口可能不同，程序不自动猜测或改写 WorkBuddy 的内部配置路径。

## DeepSeek Harness

DeepSeek Harness 的官方 MCP 客户端使用插件配置。你应使用 `deepseek-harness.patch.yml`，而不是 JSON 配置。该补丁通过 `insert` 添加一个 `@deepseek-ai/dsh-mcp-client` 条目。

你可以给原有 Harness 启动命令追加以下参数：

```text
--patch "E:\你的项目目录\.campus-runner\client-configs\deepseek-harness.patch.yml"
```

你也可以把补丁合并到所用 profile 的 `cordis.patch.yml` 中。你只选择一种加载方式，避免同一条目被重复插入。启动后，你应看到类似 `mcp__campus-runner__list_instances` 的工具命名空间。

配置字段依据 [DeepSeek 官方 MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)，补丁装载依据 [官方 profile 实现](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/boot/app-boot/src/profile.ts)。当前服务支持 `2024-11-05`、`2025-03-26` 和 `2025-11-25` 协议；客户端请求更新版本时，双方需要协商回受支持的版本。

## Codex

你仍可使用原来的注册脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure-codex.ps1
```

你也可以把生成的 `codex.toml` 服务器表合并到个人 Codex 配置。这个客户端使用 TOML，不直接导入 `mcpServers` JSON。[OpenAI 官方说明](https://learn.chatgpt.com/docs/extend/mcp)

## 其他支持 STDIO 的 Agent

你可以参考 `generic.mcp.json`。客户端的配置容器名称可能不同，但本地启动需要的 `command`、`args` 和 `env` 相同。服务器名建议使用 `campus-runner`。当前没有提供 MCP HTTP/SSE 入口；本地面板的 `http://127.0.0.1:8765` 是管理服务地址，不能填入客户端的 MCP HTTP URL 字段。

Agent 需要能够在同一台 Windows 机器、同一用户环境中启动配置里的 Python。运行在其他电脑、WSL、容器或远程工作区的 Agent，需要先解决执行环境与本机服务的连通问题。

## 验证与共同规则

你可以先运行项目自检：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m campus_runner.cli --home .\.campus-runner --port 8765 mcp-check
```

自检会启动独立 STDIO 进程，完成握手、工具发现和 `list_instances` 调用。成功时返回 `status: ok`、工具名称和实例数量，不输出连接凭据。

客户端加载后，你可以发送：

> 请先调用 Campus Runner 的 list_instances，然后读取 docs/AGENT_GUIDE.md。请告诉我有哪些可用实例，暂不提交训练。

服务在初始化响应中提供通用操作指引，客户端不加载 Skill 也可以发现并调用工具。详细规则位于 [通用 Agent 操作说明](AGENT_GUIDE.md)。客户端可能增加工具命名空间，以实际工具列表为准。

所有客户端共用面板批准流程。用户点击“批准并提交”后，服务自动上传和提交；Agent 不得代替用户批准。你切换客户端时，应先查询既有批次和任务，而不是新建一份相同实验。

## 验证范围

本次已使用官方 MCP Python SDK 1.30.0，分别从六种生成配置启动适配进程。各配置均完成初始化、10 个工具发现和真实 `list_instances` 调用，并通过工具 JSON Schema 检查；测试工作目录与仓库目录不同。

这项验证覆盖协议和启动配置，没有把各客户端的图形界面、账号登录及所有版本都测试一遍。客户端启用、工作区信任和工具授权仍以对应版本为准。

维护者可以在装有 `mcp` 和 `PyYAML` 的隔离环境中复跑 SDK 检查：

```powershell
python scripts/check-mcp-sdk.py --config-dir .campus-runner/client-configs
```

该检查仅调用实例查询，不提交训练。
