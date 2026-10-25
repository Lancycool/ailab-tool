# 校园训练台 · Campus Runner

本地 GPU 太烂？想把任务交给学校集群？每次改好代码后，都需要手动上传到平台，再手动输入训练命令？实验结束后，还要逐个下载日志和数据，才能交给 AI 批量分析？

这些重复操作让本地 AI 与远程训练之间多了一道障碍。Campus Runner 把代码上传、任务执行和结果回传接在一起，让 AI 可以管理学校集群上的训练任务。它本质上就是个MCP，在本地提出实验目标并确认清单，程序负责把任务送到服务器，再把结果交回本地分析。

**Campus Runner 让本地 Agent 通过 SSH 管理校园服务器上的训练。**

在本地写代码，让 Agent 准备实验，再在浏览器面板点击一次“批准并提交”。程序会上传代码、在服务器运行训练，并把日志和指标同步回来。不需要为每次实验手动打开 FinalShell、上传文件，再逐条输入训练命令。

本项目适合已经拥有学校 GPU 实例，希望让 Agent 管理训练流程的用户。当前版本面向 **Windows 本地电脑 + Linux 服务器 + Conda 环境**。学校平台自动导入目前只适配陕西师范大学的 `ailab.snnu.edu.cn`；其他可通过 SSH 访问的服务器可以手动配置，但需要自行验证兼容性。（默认你为SNNU学生，已连接校园网）

## 这个工具能做什么？

| 你要完成的工作 | 程序如何帮助你 |
|---|---|
| 连接学校服务器 | 程序支持手动填写 SSH 信息，也支持你登录学校平台后导入实例。 |
| 上传本地代码 | 程序生成固定代码快照，记录文件摘要，并上传远程缺少的内容。 |
| 安排多组实验 | Agent 准备实例、GPU、命令、环境和时间限制，你在面板批准后执行。 |
| 关闭本地电脑后继续训练 | 服务器继续执行已完整提交的队列，本地重新上线后恢复同步。 |
| 跟踪进度和失败原因 | 面板显示任务状态、日志、指标曲线和退出原因。 |
| 汇总实验 | Agent 读取结构化指标，程序生成 Markdown 报告和 CSV 对比表。 |
| 获取模型权重 | 你选择需要的文件后，程序下载并校验完整性。 |

你仍需在学校平台申请、续期实例，并准备服务器上的数据集和训练环境。第一版不支持自动申请实例、自动迁移、自动断点续训，也不提供 FinalShell 式的完整终端和文件管理器。服务器不会自行运行 AI 修改代码。

```text
本地代码 → Agent 准备实验 → 你在面板批准 → SSH 上传并提交
                                              ↓
                                  服务器队列 → GPU 训练
                                              ↓
                         本地面板 / Agent ← 日志、指标、结果
```

## 开始前，你需要准备什么？

| 位置 | 需要具备的条件 |
|---|---|
| 本地电脑 | Windows、Git、Python 3.11 或以上版本。 |
| AI Agent | 支持本地 MCP STDIO 的客户端，例如 Claude Code、Cursor、WorkBuddy、DeepSeek Harness 或 Codex。 |
| 网络 | 本地电脑能访问服务器 SSH 地址；学校内网实例需要在可访问校园内网的环境使用。 |
| 学校平台导入 | 本地已安装 Microsoft Edge；程序通过 Playwright 打开独立登录窗口。 |
| 远程服务器 | Linux、SSH/SFTP、Python 3.8 或以上版本、Conda，以及可用的 GPU 和 `nvidia-smi`。 |
| 训练环境 | 你需要已有的 Conda 环境、训练依赖和服务器数据集路径。 |

本地管理程序使用独立的 Python 环境。远程任务管理脚本只使用 Python 标准库，不需要管理员权限。你的训练脚本需要哪些依赖，仍由对应的 Conda 环境提供。

## 快速开始

### 1. 下载并安装

你在 PowerShell 中执行以下命令。你可以把仓库放到自己常用的项目目录。

```powershell
git clone https://github.com/Lancycool/ailab-tool.git
cd ailab-tool
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e .
```

`requirements.lock` 保存了已验证的依赖版本。程序直接使用系统 Edge，无需另外执行 `playwright install`。

### 2. 启动本地面板

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

脚本会在后台启动服务，并打开带本地访问凭据的面板。默认地址是 `http://127.0.0.1:8765`。程序只监听本机地址。

如果你关闭了浏览器，可以在项目目录重新打开面板：

```powershell
.\.venv\Scripts\python.exe -m campus_runner.cli --home .\.campus-runner open
```

你直接打开地址时可能看到“请使用程序打开面板”的提示。此时请使用上述命令，不需要手动查找或复制令牌。

### 3. 让你的 Agent 连接本地服务

Campus Runner 使用通用 **MCP STDIO**，可以由不同 Agent 调用。你在项目目录生成客户端配置：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\configure-mcp.ps1 -Client all
```

程序会把配置导出到 `.campus-runner/client-configs/`。生成文件已经包含本机 Python、状态目录和端口的实际路径。程序不会覆盖客户端已有设置。

| 你使用的客户端 | 生成文件 | 接入方式 |
|---|---|---|
| Claude Code | `claude-code.mcp.json` | 你通过 `--mcp-config` 加载，或合并到项目 `.mcp.json`。 |
| Cursor | `cursor.mcp.json` | 你合并到项目或个人的 `.cursor/mcp.json`。 |
| WorkBuddy | `workbuddy.mcp.json` | 你通过客户端的自定义本地 MCP 配置入口导入，选择 STDIO。 |
| DeepSeek Harness | `deepseek-harness.patch.yml` | 你通过 `--patch` 加载，或合并到所用 profile 的补丁文件。 |
| Codex | `codex.toml` | 你合并 TOML 配置，也可以继续运行原来的 `scripts/configure-codex.ps1`。 |
| 其他本地 MCP Agent | `generic.mcp.json` | 你按客户端格式填写其中的启动程序、参数和环境变量。 |

你可以用 `-Client cursor` 等参数只生成某个客户端的配置。各客户端的操作步骤、官方资料和配置差异见 [MCP 客户端接入](docs/MCP_CLIENTS.md)。仓库中的 [配置示例](configs/generic.mcp.json) 使用占位路径；实际使用时优先让脚本生成本机配置。

你先启动本地管理服务，再在客户端加载配置。你可以在项目目录运行协议自检：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m campus_runner.cli --home .\.campus-runner --port 8765 mcp-check
```

成功时，程序返回 `status: ok`、10 个工具名称和实例数量。你随后可以让 Agent 验证连接：

> 请调用 Campus Runner 的 list_instances，确认你能访问本地训练管理服务。请先读取 docs/AGENT_GUIDE.md。

多个 Agent 可以共用一个本地管理服务、同一个 `--home` 与 `--port`。每个客户端会启动自己的 STDIO 适配进程，实例和任务记录由管理服务统一保存。你切换客户端后先查询已有任务，避免为同一实验重复建批。项目移动后，你需要重新生成配置。

服务会通过 MCP 初始化响应提供通用操作指引。你也可以让 Agent 读取 [通用操作说明](docs/AGENT_GUIDE.md)；支持 Skill 的客户端还可以按其方式加载 [操作技能](skills/campus-training/SKILL.md)。本项目的通用性指客户端接入，当前本地运行环境仍以 Windows 为目标。

### 4. 添加服务器实例

你先在学校平台申请可用实例，再选择下面的一种方式添加。

**方式 A：从学校平台导入**

1. 你点击“从学校平台导入 / 刷新”。程序会打开独立 Edge 窗口。
2. 你在学校网页自行登录，并进入实例列表。
3. 你回到本地面板，点击“我已登录，读取实例列表”。
4. 你在目标实例旁点击“读取连接信息”。程序读取对应弹窗中的 TCP/22 外部地址。
5. 你核对用户名、环境和路径。首次导入时，你在本地面板填写一次 SSH 密码；已有记录时，你可以复用同账号凭据。
6. 你点击“确认并保存连接信息”。

程序只读取学校列表当前页。你需要其他分页时，可以在学校窗口切换，再重新读取。网页登录密码不会被程序采集；登录成功也不会自动提供 SSH 密码。

**方式 B：手动填写**

你点击“手动填写”，按下面的含义录入信息。表中的值只是示例。

| 字段 | 示例与填写方式 |
|---|---|
| 实例编号 | `gpu-a`。你和 Agent 会用这个编号指定服务器。 |
| 显示名称 | `我的训练实例`。名称用于面板展示。 |
| 主机与 SSH 端口 | 你填写平台分配的外部地址，例如 `192.0.2.10`、`24060`；不要照抄示例地址。 |
| 用户名、密码 | 你填写能够登录该实例的 SSH 账号和密码。 |
| 远程工作目录 | 你输入用户名后，程序默认填写 `/home/用户名`；实际目录不同时，你可以修改。 |
| Conda 程序或安装目录 | 程序默认填写 `/opt/conda/bin/conda`，也接受安装目录 `/opt/conda`。 |
| 默认训练环境 | 你填写已有环境名称，例如 `radar`，或环境的绝对路径。 |
| Python 命令 | 程序默认使用 `python3`，用于运行远程任务管理脚本。 |
| 到期时间 | 你填写实例的到期时间，面板按北京时间显示。 |

**Conda 程序路径和训练环境是两个字段。** `/home/user/.conda/envs/radar` 是训练环境目录，不应填到 Conda 程序字段。两种添加方式都会提供默认路径，并保留你修改过的自定义值。

**两种方式都需要完成连接检查。** 你保存后点击“检查连接与后台运行”。首次连接时，程序会要求你核对服务器指纹。检查通过后，实例会显示“在线”和“已通过离线检查”。

### 5. 运行第一次短时训练

你可以先用仓库中的 [短时 GPU 示例](examples/acceptance/train.py) 检查完整流程。示例使用合成数据，不需要下载数据集；远程 Conda 环境必须已经安装支持 CUDA 的 PyTorch。

你可以把下面这段话发给 Agent，并替换实例编号、环境名和 GPU 编号：

> 请先读取 docs/AGENT_GUIDE.md。请用 Campus Runner，为当前仓库的 examples/acceptance 目录生成快照。请在实例 gpu-a 的 GPU 0 上，使用 radar 环境运行 python train.py --steps 12 --seed 7。最长运行时间设为 180 秒。示例使用合成数据，不传数据集路径；数据集标识设为 demo-synthetic，评价方式设为 smoke-v1。主要指标是 train split 的 loss，数值越小越好。请先创建实验清单，告诉我面板批准入口。你不需要等待我回聊天确认；我点击“批准并提交”后，本地服务会自动提交。请不要安装依赖。

你接着完成下面的流程：

1. 你在“实验批准”页面核对实例、GPU、环境、命令和代码快照，并点击“批准并提交”。
2. 程序在后台自动上传并提交。你不需要返回 Agent 再说“已批准，请继续”。面板会显示提交进度；失败时，你可以重试同一清单。
3. 你在“任务详情”查看日志和状态。只有面板显示“整批已提交，可离线”后，本地电脑才可以离线。
4. 你在“结果对比”点击“汇总结果”，或让 Agent读取指标并生成总结。

示例正常完成时，任务状态应为“成功”，退出码应为 0。每个任务会产生 12 条指标、`summary.json` 和 `model.pt`。日志和小型结果会自动回传，模型权重由你按需下载。

## 如何接入自己的训练项目？

你需要告诉 Agent 本地项目目录、服务器数据集路径、Conda 环境、训练命令、GPU、时间上限，以及主要评价指标。你可以这样描述：

> 请把 E:\Projects\my-training 生成代码快照，在 gpu-a 的 GPU 0 上使用 radar 环境训练。数据集已经位于服务器 /data/radar。请使用 python train.py --epochs 20，最长运行 4 小时，以 validation split 的 loss 作为主要指标，越小越好。请先检查脚本能否使用工具提供的数据和输出目录，再创建清单供我批准。

训练脚本需要把结果写入程序指定的目录，并输出统一指标。你可以把 [metrics.py](campus_runner/metrics.py) 复制到训练项目，然后在已有指标计算之后调用它：

```python
import os
from pathlib import Path
from metrics import write_metrics

# 你让数据加载逻辑使用 data_dir，让保存逻辑使用 output_dir。
data_dir = Path(os.environ["CAMPUS_DATA_DIR"])
output_dir = Path(os.environ["CAMPUS_OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)

# 你用实际计算出的数值替换下面的示例值。
write_metrics(step=100, metrics={"loss": 0.25}, split="validation", epoch=2)
```

程序会提供 `CAMPUS_RUN_ID`、`CAMPUS_DATA_DIR`、`CAMPUS_OUTPUT_DIR` 和 `CAMPUS_METRICS_PATH`。程序也会设置 `CUDA_VISIBLE_DEVICES`；例如任务被分配服务器 GPU 2 时，训练脚本通常仍通过 `cuda:0` 使用该卡。

多进程训练应只由主进程写统一指标。没有接入统一指标的脚本仍可输出训练日志，但面板无法自动识别任意日志中的曲线和评价结果。你应使用 `python train.py` 或 `bash train.sh` 明确调用脚本，因为代码快照不保留可执行权限。

## 日常使用中，你需要知道的规则

- **你批准的是一份固定清单。** 代码、命令、环境或资源范围变化后，程序需要新的批准。依赖安装也需要单独批准。
- **同一实例不会因重复导入而新建记录。** 程序先按平台身份匹配，再按主机、SSH 端口和用户名匹配手动记录。多个候选需要你明确选择，显示名称不作为合并依据。
- **平台到期时间优先。** 手动编辑不会覆盖已保存的平台时间。新的有效导入会更新旧值；读取失败时，程序保留此前的平台时间。估算时间会明确标记。
- **本地离线与训练失败是两回事。** 关闭 Agent 或本地电脑后，已完整提交的远程队列继续运行；结果同步会等待本地服务恢复。
- **实例续期需要刷新配置。** 你可以再次从平台导入时间。运行中或状态待核实的任务会阻止连接配置更新；旧批准也会因配置变化而失效。
- **任务必须留出到期余量。** 剩余期限不足“任务最长运行时间 + 30 分钟”时，服务器不会启动该任务。程序不会强制停止已经运行的训练。

你可以在面板中停止任务，也可以让 Agent 调用 `cancel_jobs`。网络断开时，停止请求可能无法立即送达。多实例队列、文件限制、失败处理和下载规则详见 [运行规则](docs/OPERATIONS.md)。

## 如何查看 CPU、GPU 和内存使用情况？

实例通过连接检查后，实例卡片会显示 CPU 使用率、CPU 型号和可用逻辑核心、内存用量、GPU 利用率、显存、温度和功耗。CPU 百分比根据两次采样差值计算，首次显示需要等待下一次采样。设备不支持的字段会显示“不可用”。

面板默认每 **10 秒**刷新一次。你可以选择 5 秒、30 秒或暂停，也可以点击“刷新资源”。程序只在实例页面可见时请求采集；你切到其他页面、最小化浏览器或关闭面板后，资源刷新会停止。任务和日志仍按原有后台流程同步。

采集只读取少量 Linux 统计文件，并执行一次有超时限制的 `nvidia-smi` 查询，不扫描全部训练进程，也不安装监控服务。同一实例不会重叠采集，多个面板请求会共享缓存；连接失败后，程序逐步延长重试间隔。面板显示实际采集耗时和时间戳，旧数据不会被标成实时值。具体开销取决于 SSH 延迟和驱动响应，10 秒是默认折中，5 秒更适合短时间观察波动。

程序能读取容器配额时，CPU 和内存会标注“实例”；其余情况会标注“系统可见”。系统可见统计可能包含宿主机上的其他任务，不能当作学校给你的独占配额。

## 结果保存在哪里？

程序默认把本地状态保存在项目目录的 `.campus-runner/` 中：

| 路径 | 保存的内容 |
|---|---|
| `results/<run_id>/` | 该任务的日志、指标、批准配置和已下载结果。 |
| `reports/` | 程序生成的 Markdown 汇总和 CSV 对比表。 |
| `service.stdout.log`、`service.stderr.log` | 本地服务的运行日志和错误信息。 |
| `state.sqlite3` | 实例、任务、批准和同步记录。 |

密码由 Windows 凭据管理器保存。数据库不保存 SSH 明文密码。`.campus-runner/` 还包含本地访问令牌，已经被 Git 忽略，你不要把它作为源码分享。

学校网页登录窗口使用临时会话。你关闭窗口、重启服务，或会话达到 30 分钟后，需要重新登录。

## 启动、停止和更新

你在仓库目录运行以下命令：

```powershell
# 你启动后台服务并打开面板。
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1

# 你停止本地服务；服务器上的训练队列会继续运行。
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
```

你需要更新程序时，先保存自己的代码改动，再执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop.ps1
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install -e .
powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1
```

如果 `git pull` 提示冲突或无法快进，你应先处理本地改动，再继续更新。

默认端口是 `8765`。你需要更换端口时，应给 `start.ps1` 与 `configure-mcp.ps1` 传入相同的 `-Port`，再重新加载导出配置；使用 Codex 注册脚本时也需传入相同端口。CLI 默认状态目录是用户主目录中的 `.campus-runner`；本仓库脚本明确使用项目内目录。你手动运行 CLI 时，需要让服务、面板和 MCP 使用相同的 `--home` 与 `--port`。

## 常见问题

| 你遇到的现象 | 你可以先检查什么 |
|---|---|
| 面板提示没有访问凭据 | 你通过启动脚本或 `campus_runner.cli --home .\.campus-runner open` 重新打开面板。 |
| Agent 找不到工具或连不上服务 | 你先启动本地服务，再运行 `mcp-check`。你检查客户端是否支持本地 STDIO、是否加载了正确配置，以及绝对路径、状态目录和端口是否一致。 |
| Python 环境没有 pip | 你可以执行 `.\.venv\Scripts\python.exe -m ensurepip --upgrade`，然后重新安装依赖。 |
| 学校登录窗口打不开 | 你检查系统 Edge 是否安装，以及本地网络能否打开学校平台。你也可以先用手动配置。 |
| 登录后没有读到实例 | 你进入学校“实例”列表再读取；程序只读取当前分页。连接图标无法识别时，你可以打开目标弹窗后使用“读取我已打开的连接弹窗”。 |
| Conda 报 `Permission denied` | 你检查是否把训练环境目录填到了 Conda 程序字段。程序路径默认是 `/opt/conda/bin/conda`。 |
| 实例仍显示“尚未通过离线检查” | 你查看连接检查结果，核对指纹、Conda 环境、目录和后台进程支持情况。 |
| 日志不再更新 | 你先检查本地网络、服务状态和最后确认时间。日志停止同步不等于远程训练失败。 |
| 结果页面没有曲线 | 你检查脚本是否写入统一指标，是否使用正确的输出路径，以及指标文件是否已同步。 |
| 权重没有自动下载 | 你在任务详情选择对应文件下载，或让 Agent 调用 `fetch_artifacts`。 |

## 开发与验证

你安装依赖后，可以运行本地测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

你需要运行真实 Edge 测试时，可以执行：

```powershell
$env:CAMPUS_BROWSER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q
```

浏览器测试使用本地测试页面和隔离数据库，不需要学校账号。Linux 进程测试位于 `tests/linux_runner_checks.py`，你可以在 Linux 或 WSL 中用 Python 运行它。

项目已经完成单个真实校园实例上的代码上传、短时 GPU 训练、离线队列、结果回传和重复提交检查，也完成了学校平台导入读取。多实例真实并行、学校到期回收和长时间训练仍需进一步验证。开发测试中的 GPU 和 Conda 替身不能代替真实训练验收。

| 你想了解的内容 | 对应文件 |
|---|---|
| Agent 应如何操作实验 | [通用操作说明](docs/AGENT_GUIDE.md) |
| 各种 Agent 的 MCP 配置 | [客户端接入](docs/MCP_CLIENTS.md) |
| 清单、队列和结果下载规则 | [运行规则](docs/OPERATIONS.md) |
| 一键批准、资源采集规则与耗时 | [资源与审批验收](docs/RESOURCE_MONITOR.md) |
| 单实例真实 GPU 验收 | [实例验收记录](docs/INSTANCE_ACCEPTANCE.md) |
| 平台导入、到期时间和去重验证 | [平台导入记录](docs/PORTAL_IMPORT.md) |
| 早期开发验证与 Linux 测试 | [开发验证记录](docs/VALIDATION.md) |
| 双实例实验清单示例 | [batch.json](examples/batch.json)，使用前需替换其中的占位值。 |

本项目用 FastAPI 提供本地接口和面板，用 SQLite 保存记录，用 Paramiko 建立 SSH/SFTP 连接，用 Playwright 读取学校平台。远程队列使用 Python 标准库。MCP 接口提供实例查询、检查、快照、建批、提交、状态、日志、指标、下载和停止十类工具。
