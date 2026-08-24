[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scriptFiles = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1' -File)
$failureMessages = New-Object System.Collections.Generic.List[string]
foreach ($file in $scriptFiles) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    foreach ($parseError in @($parseErrors)) {
        $failureMessages.Add("$($file.Name): $($parseError.Message)")
    }
}
if ($failureMessages.Count -gt 0) {
    throw ($failureMessages -join [Environment]::NewLine)
}

. (Join-Path $PSScriptRoot 'Common.ps1')
$config = Get-SkillUpdaterConfig
if ([int]$config.version -ne 4) { throw 'Unsupported config version.' }
if (@($config.sources).Count -ne 2) { throw 'Expected exactly two tracked sources.' }
if ([int]$config.reportRetention -lt 1) { throw 'Report retention must be positive.' }

$package = Get-PackageSpecParts -PackageSpec ([string]$config.tooling.skillsCli)
try { [version]([string]$config.tooling.minimumNodeVersion) | Out-Null }
catch { throw 'minimumNodeVersion must be a numeric dotted version.' }
if (@($config.tooling.allowedRegistries).Count -eq 0) { throw 'At least one trusted package registry is required.' }
$normalizedRegistries = @($config.tooling.allowedRegistries | ForEach-Object { Get-NormalizedRegistryUrl -Url ([string]$_) })
if (@($normalizedRegistries | Sort-Object -Unique).Count -ne $normalizedRegistries.Count) {
    throw 'Trusted package registries must be unique.'
}

if (@('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday') -notcontains [string]$config.task.dayOfWeek) {
    throw 'Scheduled-task dayOfWeek is invalid.'
}
try { [datetime]::ParseExact([string]$config.task.time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture) | Out-Null }
catch { throw 'Scheduled-task time must use HH:mm.' }

$sourceIds = @{}
$repositorySlugs = @{}
$sharedDestinations = @{}
foreach ($source in $config.sources) {
    if ([string]::IsNullOrWhiteSpace([string]$source.id) -or $sourceIds.ContainsKey([string]$source.id)) {
        throw "Source IDs must be non-empty and unique: $($source.id)"
    }
    $sourceIds[[string]$source.id] = $true
    if ([string]$source.repositorySlug -notmatch '^[^/\s]+/[^/\s]+$') { throw "$($source.id) has an invalid repository slug." }
    if ($repositorySlugs.ContainsKey([string]$source.repositorySlug)) { throw "Repository slugs must be unique: $($source.repositorySlug)" }
    $repositorySlugs[[string]$source.repositorySlug] = $true
    if ([string]$source.ref -notmatch '^\S+$') { throw "$($source.id) has an invalid ref." }
    if (@($source.skillRoots).Count -eq 0) { throw "$($source.id) has no skill roots." }
    $roots = @{}
    foreach ($skillRoot in @($source.skillRoots)) {
        if (-not (Test-SafeRelativePath -Path ([string]$skillRoot))) { throw "$($source.id) has an invalid skill root: $skillRoot" }
        if ($roots.ContainsKey([string]$skillRoot)) { throw "$($source.id) has a duplicate skill root: $skillRoot" }
        $roots[[string]$skillRoot] = $true
    }
    foreach ($sharedFile in @($source.sharedFiles)) {
        if (-not (Test-SafeRelativePath -Path ([string]$sharedFile.sourcePath))) {
            throw "$($source.id) has an invalid shared source path: $($sharedFile.sourcePath)"
        }
        $destination = [string]$sharedFile.destinationRelativeToAgentsRoot
        if (-not (Test-SafeRelativePath -Path $destination)) { throw "$($source.id) has an invalid shared destination: $destination" }
        if ($sharedDestinations.ContainsKey($destination)) { throw "Shared destinations must be unique: $destination" }
        $sharedDestinations[$destination] = $true
    }
}

$derivedInstallSpecs = @($config.sources | ForEach-Object {
    $source = $_
    @($source.skillRoots) | ForEach-Object { Get-SourceInstallSpec -Source $source -SkillRoot ([string]$_) }
})
$tooling = Assert-ToolingEnvironment -Config $config
$git = Assert-GitEnvironment -Config $config

Write-Host "Parsed $($scriptFiles.Count) PowerShell scripts."
Write-Host "Derived $($derivedInstallSpecs.Count) install specs from configured skill roots."
Write-Host "Skill runner: $($tooling.Runner.DisplayName) via $($tooling.Runner.Manager) $($tooling.ManagerVersion)"
Write-Host "Node.js: $($tooling.NodeVersion); registry: $($tooling.Registry)"
Write-Host "Git: $($git.Git)"
Write-Host 'Project validation passed.'
