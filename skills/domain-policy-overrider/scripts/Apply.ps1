$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$entry = [ordered]@{time=[DateTimeOffset]::Now.ToString('o');runId=[guid]::NewGuid().ToString();identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name;settings=@()}
$exitCode = 0
try {
    $profile = Read-OverrideProfile (Join-Path $PSScriptRoot 'profile.json')
    Invoke-OverrideProfile $profile | ForEach-Object { $entry.settings += $_ }
} catch { $entry.error=$_.Exception.Message; $exitCode=1 }
finally {
    $logPath = Join-Path $PSScriptRoot 'activity.jsonl'
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 262144) {
        Move-Item -LiteralPath $logPath -Destination (Join-Path $PSScriptRoot 'activity.previous.jsonl') -Force
    }
    $entry | ConvertTo-Json -Depth 6 -Compress | Add-Content -LiteralPath $logPath -Encoding UTF8
}
exit $exitCode
