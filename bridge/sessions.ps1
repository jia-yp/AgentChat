param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{24}$')][string]$Request,
    [Parameter(Mandatory=$true)][ValidateSet('status','apply')][string]$Action,
    [string]$Context = $env:AGENTCHAT_CONTEXT
)
$ErrorActionPreference = 'Stop'
try {
    if (-not $Context) { throw 'Missing AGENTCHAT_CONTEXT' }
    $identity = Get-Content -LiteralPath $Context -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($identity.session_type -ne 'manager') { throw 'Only manager sessions can use this command' }
    $base = $identity.url.TrimEnd('/') + '/api/session-changes/' + $Request
    $headers = @{Authorization = 'Bearer ' + $identity.token; 'User-Agent' = 'AgentChat-Bridge/1'}
    if ($Action -eq 'apply') {
        Invoke-RestMethod -Method Post -Uri ($base + '/apply') -Headers $headers -ContentType 'application/json' -Body '{}' -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 20
    } else {
        Invoke-RestMethod -Uri $base -Headers $headers -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 20
    }
} catch {
    $safeError = $_.Exception.Message
    if ($identity.token) { $safeError = $safeError.Replace($identity.token,'[redacted]') }
    [Console]::Error.WriteLine('AgentChat: ' + $safeError)
    exit 1
}
