param([int]$Port = 8781)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv (Join-Path $projectRoot '.venv')
    & $pythonPath -m pip install -r (Join-Path $projectRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
}
$env:AGENTCHAT_DATA = Join-Path $projectRoot 'data'
$env:AGENTCHAT_PORT = "$Port"
Set-Location -LiteralPath $projectRoot
& $pythonPath -m server
