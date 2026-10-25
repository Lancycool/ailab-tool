# 验证记录

本次验证日期为 2026-10-24。项目在 Windows Python 3.11.15 的独立 `.venv` 中运行。依赖版本记录在 `requirements.lock`。远程程序的实际进程测试在已有的 Ubuntu Python 3.14.4 中运行。

## 已通过的检查

- 29 项本地自动测试覆盖清单批准、资源冲突、任务依赖、重复提交、回执丢失、传输校验、错误指标、报告输出和 HTTP 访问控制。
- 本地测试实际启动 SSH 服务端。测试确认了未经信任的指纹会阻止密码认证，匹配的指纹可以建立 SSH/SFTP 会话，变化的指纹会再次阻止认证。
- 本地测试实际启动管理服务和独立 MCP 进程。MCP 完成初始化、发现十个工具，并读取本地服务的实例列表。
- 11 项 Linux 标准库测试覆盖真实后台进程、队列心跳确认、重复管理进程、依赖执行、失败继续、进程组取消、超时、管理进程崩溃、孤立任务取消、PID 身份检查和符号链接拒绝。
- 浏览器检查覆盖了实例管理、实例表单、实验草案入口和结果汇总页面。检查时，浏览器没有报告 JavaScript 错误。
- 操作技能通过 `quick_validate.py` 检查。Python 模块通过编译检查，面板脚本通过 Node 语法检查。

## 后续真实校园实例验证

上述开发测试使用测试用 GPU 查询程序和 Conda 启动器，不能单独证明真实 CUDA、PyTorch 或学校实例策略可用。

2026-10-25，用户已批准并完成单实例真实 GPU 验收。两组短时训练验证了代码上传、CUDA、离线队列、指标回传、权重完整性和重复提交防护。详细证据见 [实例验收记录](INSTANCE_ACCEPTANCE.md)。多实例真实并行和学校实例到期回收仍需单独验收。

## 复跑

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.cache/pytest-local
wsl -d Ubuntu -- python3 /mnt/e/Projects/ailab/tests/linux_runner_checks.py
node --check campus_runner/static/app.js
```

测试只使用本地临时状态和测试进程。测试不会向学校服务器提交训练。
