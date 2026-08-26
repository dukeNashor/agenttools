[CmdletBinding()]
param(
    [switch]$SkipSkillInstall,
    [switch]$SkipTask,
    [switch]$NoOpen
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not $SkipSkillInstall) {
    & (Join-Path $PSScriptRoot 'Install-Skills.ps1') -Apply
}
if (-not $SkipTask) {
    & (Join-Path $PSScriptRoot 'Install-ScheduledTask.ps1')
}

$reportArguments = @{}
if (-not $NoOpen) { $reportArguments.Open = $true }
& (Join-Path $PSScriptRoot 'New-SkillUpdateReport.ps1') @reportArguments
