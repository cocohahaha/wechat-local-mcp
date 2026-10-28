param(
    [ValidateSet("claude", "codex", "config", "none")]
    [string]$Client = "config",
    [switch]$Register,
    [switch]$WithUI,
    [switch]$WithMedia
)
$ErrorActionPreference = "Stop"
if ($Register -and $Client -notin @("claude", "codex")) {
    throw "-Register requires -Client claude or -Client codex"
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "未找到 uv。请先按照 https://docs.astral.sh/uv/ 安装 uv。"
}

$ProjectDir = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectDir
try {
    $PythonRequest = "3.12"
    $ExistingPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path $ExistingPython) { $PythonRequest = $ExistingPython }
    $SyncArgs = @("sync", "--locked", "--inexact", "--python", $PythonRequest)
    if ($WithUI) { $SyncArgs += @("--extra", "windows") }
    if ($WithMedia) { $SyncArgs += @("--extra", "media") }
    & uv @SyncArgs
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed ($LASTEXITCODE)" }
    $BootstrapArgs = @("-m", "wechat_local_mcp.setup", "bootstrap", "--client", $Client)
    if ($Register) { $BootstrapArgs += "--register" }
    & (Join-Path $ProjectDir ".venv\Scripts\python.exe") @BootstrapArgs
    if ($LASTEXITCODE -ne 0) { throw "MCP bootstrap failed ($LASTEXITCODE); read the JSON result above." }
} finally {
    Pop-Location
}
