[CmdletBinding()]
param(
    [switch]$SkipSkillInstall,
    [switch]$SkipTask,
    [switch]$NoOpen,
    [switch]$PurgeLegacy
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $SkipSkillInstall) {
    $installArguments = @{ Apply = $true }
    if ($PurgeLegacy) { $installArguments.PurgeLegacy = $true }
    & (Join-Path $PSScriptRoot 'Install-Skills.ps1') @installArguments
}
if (-not $SkipTask) {
    & (Join-Path $PSScriptRoot 'Install-ScheduledTask.ps1')
}

$reportArguments = @{}
if (-not $NoOpen) { $reportArguments.Open = $true }
& (Join-Path $PSScriptRoot 'New-SkillUpdateReport.ps1') @reportArguments
