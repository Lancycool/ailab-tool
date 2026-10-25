param(
    [ValidateSet('all','generic','codex','claude-code','cursor','workbuddy','deepseek-harness')]
    [string]$Client = 'all',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataPath = Join-Path $projectRoot '.campus-runner'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先按 README 安装项目依赖。' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $dataPath 'client-configs' }
& $pythonPath -X utf8 -m campus_runner.cli --home $dataPath --port $Port mcp-config --client $Client --output $OutputDirectory
if ($LASTEXITCODE -ne 0) { throw 'MCP 配置导出失败。' }
