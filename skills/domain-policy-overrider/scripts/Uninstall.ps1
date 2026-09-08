$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-OverrideAdmin
$directory = Get-RuntimeDirectory
if ([IO.Path]::GetFullPath($PSScriptRoot) -ne [IO.Path]::GetFullPath($directory)) { throw 'Run the protected installed copy for uninstall.' }
Assert-OverrideProtected $directory
$state = Convert-OverrideState (Get-Content -LiteralPath (Join-Path $directory 'state.json') -Raw | ConvertFrom-Json)
$task = Get-ScheduledTask -TaskName (Get-OverrideTaskName) -TaskPath '\' -ErrorAction SilentlyContinue
if ($task) { Stop-OverrideTask }
# Leave the disabled task and backups present if restoration fails, so retrying
# uninstall is possible and a partially restored machine is not reported as done.
foreach ($original in $state.originals) {
    Write-OverrideValue $original $original.value
    if ((Read-OverrideValue $original) -ne $original.value) { throw 'Original-value restoration failed.' }
}
if ($task) { Unregister-ScheduledTask -TaskName (Get-OverrideTaskName) -TaskPath '\' -Confirm:$false }
if (-not $state.eventLogWasEnabled) {
    $log = [System.Diagnostics.Eventing.Reader.EventLogConfiguration]::new('Microsoft-Windows-GroupPolicy/Operational')
    try { $log.IsEnabled=$false; $log.SaveChanges() } finally { $log.Dispose() }
}
$result = [ordered]@{uninstalledAt=[DateTimeOffset]::Now.ToString('o');restored=$state.originals;filesRetainedForAudit=$true}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $directory 'uninstalled.json') -Encoding UTF8
$result
