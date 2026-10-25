param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$dataPath = Join-Path $projectRoot '.campus-runner'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw '请先按 README 创建 .venv 并安装依赖。' }
New-Item -ItemType Directory -Force -Path $dataPath | Out-Null
$running = $false
try {
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2
    if ($response.Content -notmatch 'Campus Runner') { throw '端口已经被其他程序占用。' }
    $running = $true
} catch {
    if ($_.Exception.Message -eq '端口已经被其他程序占用。') { throw }
}
if (-not $running) {
    $arguments = @('-m', 'campus_runner.cli', '--home', ('"' + $dataPath + '"'), '--port', "$Port", 'serve')
    $process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $dataPath 'service.stdout.log') -RedirectStandardError (Join-Path $dataPath 'service.stderr.log')
    $process.Id | Set-Content -LiteralPath (Join-Path $dataPath 'service.pid')
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 300
        if ($process.HasExited) { throw '服务未能启动。请检查 .campus-runner/service.stderr.log。' }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 1
            if ($response.Content -match 'Campus Runner') { $ready = $true; break }
        } catch { }
    }
    if (-not $ready) { throw '服务尚未就绪，请检查日志。' }
}
Write-Output "本地服务已启动：http://127.0.0.1:$Port"
if (-not $NoBrowser) { & $pythonPath -m campus_runner.cli --home $dataPath --port $Port open }
