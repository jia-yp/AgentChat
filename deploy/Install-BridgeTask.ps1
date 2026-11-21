param(
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [string]$Python
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$configPath = (Resolve-Path -LiteralPath $Config).Path
if (-not $Python) { $Python = Join-Path $projectRoot '.venv\Scripts\python.exe' }
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$scriptPath = Join-Path $projectRoot 'agentchat.py'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists. Choose a new task name.' }
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument ('"' + $scriptPath + '" start --config "' + $configPath + '"') -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'Start the user-configured AgentChat bridge at login.' | Select-Object TaskName,State
