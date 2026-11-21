param(
    [ValidateSet('members','history','send')][string]$Action = 'members',
    [string]$Context = $env:AGENTCHAT_CONTEXT,
    [string]$Text,
    [ValidateSet('normal','steer')][string]$Mode = 'normal',
    [string]$Task = '',
    [string]$Nonce = [guid]::NewGuid().ToString()
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
if (-not $Context) { throw 'AGENTCHAT_CONTEXT is required' }
$identity = Get-Content -LiteralPath $Context -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = 'Bearer ' + $identity.token }
$endpoint = $identity.url.TrimEnd('/') + '/api/rooms/' + $identity.room_id
$options = @{ Headers = $headers; ContentType = 'application/json'; MaximumRedirection = 0; TimeoutSec = 20 }
if ($Action -eq 'members') {
    $result = Invoke-RestMethod -Uri $endpoint @options
} elseif ($Action -eq 'history') {
    $result = Invoke-RestMethod -Uri ($endpoint + '/messages?before=9007199254740991&limit=100') @options
} else {
    if (-not $Text) { throw 'Text is required' }
    $cause = if ($env:AGENTCHAT_CAUSE) { [int]$env:AGENTCHAT_CAUSE } else { $null }
    $payload = @{ body = $Text; nonce = $Nonce; cause = $cause; runtime = $identity.runtime; delivery_mode = $Mode; steer_task = $Task } | ConvertTo-Json -Depth 8 -Compress
    $result = Invoke-RestMethod -Uri ($endpoint + '/messages') -Method Post -Body ([System.Text.Encoding]::UTF8.GetBytes($payload)) @options
}
$result | ConvertTo-Json -Depth 20
