param(
    [ValidatePattern('^[a-f0-9]{24}$')][string]$Request,
    [Parameter(Mandatory=$true)][ValidateSet('status','apply','list','create','leave','rejoin','cancel-leave')][string]$Action,
    [string]$RolesFile,
    [string[]]$Member = @(),
    [string]$Nonce,
    [switch]$Immediate,
    [string]$Context = $env:AGENTCHAT_CONTEXT
)
$ErrorActionPreference = 'Stop'
try {
    if (-not $Context) { throw 'Missing AGENTCHAT_CONTEXT' }
    $identity = Get-Content -LiteralPath $Context -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($identity.session_type -ne 'manager') { throw 'Only manager sessions can use this command' }
    $base = $identity.url.TrimEnd('/')
    $headers = @{Authorization = 'Bearer ' + $identity.token; 'User-Agent' = 'AgentChat-Bridge/1'}
    if ($Action -in @('status','apply')) {
        if (-not $Request) { throw 'Missing Request' }
        $base += '/api/session-changes/' + $Request
    } else { $base += '/api/manager/sessions' }
    if ($Action -eq 'apply') {
        Invoke-RestMethod -Method Post -Uri ($base + '/apply') -Headers $headers -ContentType 'application/json' -Body '{}' -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 20
    } elseif ($Action -in @('status','list')) {
        Invoke-RestMethod -Uri $base -Headers $headers -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 20
    } else {
        if (-not $Nonce -or $env:AGENTCHAT_CAUSE -notmatch '^\d+$') { throw 'A stable Nonce and a current private owner task are required' }
        $roles = @()
        if ($RolesFile) { $roles = @(Get-Content -LiteralPath $RolesFile -Raw -Encoding UTF8 | ConvertFrom-Json) }
        $body = @{action=$Action;cause=[long]$env:AGENTCHAT_CAUSE;nonce=$Nonce;roles=$roles;members=@($Member);immediate=[bool]$Immediate} | ConvertTo-Json -Depth 30
        Invoke-RestMethod -Method Post -Uri $base -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($body)) -MaximumRedirection 0 -TimeoutSec 20 | ConvertTo-Json -Depth 30
    }
} catch {
    $safeError = $_.Exception.Message
    if ($identity.token) { $safeError = $safeError.Replace($identity.token,'[redacted]') }
    [Console]::Error.WriteLine('AgentChat: ' + $safeError)
    exit 1
}
