param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataPath = Join-Path $projectRoot '.campus-runner'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先安装项目依赖。' }
& codex mcp add campus-runner -- $pythonPath -m campus_runner.cli --home $dataPath --port $Port mcp
if ($LASTEXITCODE -ne 0) { throw 'Codex MCP 注册失败。你可以使用 configs/codex.toml 手动配置。' }
Write-Output 'MCP 已注册。请重新加载 Codex 会话，并按 README 安装操作技能。'
