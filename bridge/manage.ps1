param(
    [Parameter(Mandatory=$true)][string]$Member,
    [Parameter(Mandatory=$true)][ValidateSet('models','configure','enable','disable','refresh-models')][string]$Action,
    [string]$Model,
    [string]$Effort,
    [int]$Timeout,
    [string]$Context = $env:AGENTCHAT_CONTEXT
)
$ErrorActionPreference = 'Stop'
try {
    if (-not $Context) { throw 'Missing AGENTCHAT_CONTEXT' }
    $identity = Get-Content -LiteralPath $Context -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($identity.session_type -ne 'manager') { throw 'Only manager sessions can use this command' }
    $base = $identity.url.TrimEnd('/') + '/api/rooms/' + $identity.room_id
    $headers = @{ Authorization = 'Bearer ' + $identity.token; 'User-Agent' = 'AgentChat-Bridge/1' }
    $snapshot = Invoke-RestMethod -Uri $base -Headers $headers -MaximumRedirection 0 -TimeoutSec 20
    $target = @($snapshot.members | Where-Object { ($_.id -eq $Member -or $_.name -eq $Member) -and $_.kind -eq 'agent' -and $_.bridge -eq $identity.bridge_id })
    if ($target.Count -ne 1) { throw 'Member must belong to this manager bridge' }
    $target = $target[0]
    $base += '/members/' + $target.id
    if ($Action -eq 'models') { $target.model_settings | ConvertTo-Json -Depth 12; exit 0 }
    if ($Action -eq 'configure') {
        $payload = @{model=$target.model_settings.model;revision=$target.model_settings.revision}
        if ($PSBoundParameters.ContainsKey('Model')) { $payload.model = $Model }
        if ($PSBoundParameters.ContainsKey('Effort')) { $payload.reasoning_effort = $Effort }
        if ($PSBoundParameters.ContainsKey('Timeout')) { $payload.timeout_seconds = $Timeout }
        $base += '/model'
    } elseif ($Action -eq 'refresh-models') {
        $payload = @{}; $base += '/models/refresh'
    } else {
        $payload = @{action=$Action}; $base += '/control'
    }
    $body = [Text.Encoding]::UTF8.GetBytes(($payload | ConvertTo-Json -Compress))
    Invoke-RestMethod -Method Post -Uri $base -Headers $headers -ContentType 'application/json; charset=utf-8' -Body $body -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 12
} catch {
    $safeError = $_.Exception.Message
    if ($identity.token) { $safeError = $safeError.Replace($identity.token,'[redacted]') }
    [Console]::Error.WriteLine('AgentChat: ' + $safeError)
    exit 1
}
