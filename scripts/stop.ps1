param()
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$dataPath = Join-Path $projectRoot '.campus-runner'
$pidFile = Join-Path $dataPath 'service.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No service PID file.'; exit 0 }
$serviceProcessId = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId = $serviceProcessId"
if (-not $process) { Write-Output 'Local service is already stopped.'; exit 0 }
if ($process.CommandLine -notlike '*campus_runner.cli*' -or $process.CommandLine -notlike ('*' + $dataPath + '*') -or $process.CommandLine -notlike '*serve*') {
    throw 'PID identity changed. Refusing to stop an unrelated process.'
}
# Windows venv launchers may start the actual interpreter as a child process.
$children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $serviceProcessId"
foreach ($child in $children) {
    if ($child.CommandLine -like '*campus_runner.cli*' -and $child.CommandLine -like ('*' + $dataPath + '*') -and $child.CommandLine -like '*serve*') {
        Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
    }
}
Stop-Process -Id $serviceProcessId -ErrorAction SilentlyContinue
Write-Output 'Local service stopped. Remote training queues are not affected.'
