[CmdletBinding()]
param(
    [string[]]$SourceId,
    [switch]$PruneOnly,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

if ($PruneOnly -and $PreflightOnly) { throw 'PruneOnly and PreflightOnly cannot be combined.' }

function Test-SkillPathInsideConfiguredRoots {
    param(
        [Parameter(Mandatory = $true)][string]$SkillPath,
        [Parameter(Mandatory = $true)]$Source
    )

    $normalized = $SkillPath.Replace('\', '/')
    foreach ($root in @($Source.skillRoots)) {
        $prefix = ([string]$root).Replace('\', '/').TrimEnd('/') + '/'
        if ($normalized.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Remove-TrackedSkillEntries {
    param(
        [Parameter(Mandatory = $true)]$Lock,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$Entries,
        [Parameter(Mandatory = $true)][string]$AgentsRoot,
        [Parameter(Mandatory = $true)][string]$LockPath
    )

    $skillsRoot = Join-Path $AgentsRoot 'skills'
    foreach ($entry in $Entries) {
        $skillPath = Join-Path $skillsRoot $entry.Name
        if (Test-Path -LiteralPath $skillPath) {
            $resolvedSkillPath = (Resolve-Path -LiteralPath $skillPath).Path
            $resolvedSkillsRoot = (Resolve-Path -LiteralPath $skillsRoot).Path.TrimEnd('\')
            if (-not $resolvedSkillPath.StartsWith($resolvedSkillsRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Refusing to remove path outside the global skills directory: $resolvedSkillPath"
            }
            Remove-Item -LiteralPath $resolvedSkillPath -Recurse -Force
        }
        $Lock.skills.PSObject.Properties.Remove($entry.Name)
        Write-Host "Removed outside configured subset: $($entry.Name)"
    }
    if ($Entries.Count -gt 0) {
        $json = $Lock | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($LockPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    }
}

$config = Get-SkillUpdaterConfig
$agentsRoot = Resolve-PortablePath $config.agentsRoot
$selectedSources = @($config.sources)
if ($SourceId) {
    $selectedSources = @($config.sources | Where-Object { $SourceId -contains $_.id })
    if ($selectedSources.Count -ne $SourceId.Count) {
        throw 'One or more requested source IDs do not exist in config.json.'
    }
}

if ($PruneOnly) {
    $lockPath = Join-Path $agentsRoot '.skill-lock.json'
    if (-not (Test-Path -LiteralPath $lockPath)) { throw "Global skill lock does not exist: $lockPath" }
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
    foreach ($source in $selectedSources) {
        $sourceIdentifier = Get-SourceIdentifier -Source $source
        $tracked = @($lock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        $excluded = @($tracked | Where-Object { -not (Test-SkillPathInsideConfiguredRoots -SkillPath ([string]$_.Value.skillPath) -Source $source) })
        Remove-TrackedSkillEntries -Lock $lock -Entries $excluded -AgentsRoot $agentsRoot -LockPath $lockPath
        $remaining = @($lock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        Write-Host "$($source.label): $($remaining.Count) skills inside the configured subset."
    }
    Write-Host 'Subset cleanup complete. No backup was created.'
    return
}

Assert-ToolingEnvironment -Config $config | Out-Null
Assert-GitEnvironment -Config $config | Out-Null
$sourcePlans = New-Object System.Collections.Generic.List[object]
$snapshotPaths = New-Object System.Collections.Generic.List[string]
try {
    foreach ($source in $selectedSources) {
        $snapshot = New-RepositorySnapshot -Config $config -Source $source
        $snapshotPaths.Add($snapshot)
        $skillFiles = @(Get-ConfiguredSkillFiles -Snapshot $snapshot -Source $source)
        $desiredNames = @($skillFiles | ForEach-Object { $_.Directory.Name } | Sort-Object -Unique)
        foreach ($sharedFile in $source.sharedFiles) {
            $sourcePath = Resolve-PathUnderRoot -Root $snapshot -RelativePath ([string]$sharedFile.sourcePath)
            if (-not (Test-Path -LiteralPath $sourcePath)) {
                throw "Shared file is missing upstream: $sourcePath"
            }
        }
        $sourcePlans.Add([pscustomobject]@{
            Source = $source
            Snapshot = $snapshot
            SkillFiles = $skillFiles
            DesiredNames = $desiredNames
        })
    }

    $nameOwners = @{}
    foreach ($plan in $sourcePlans) {
        foreach ($skillFile in $plan.SkillFiles) {
            $name = $skillFile.Directory.Name
            if ($nameOwners.ContainsKey($name)) {
                throw "Configured skill name collision: $name ($($nameOwners[$name]) and $($plan.Source.id))."
            }
            $nameOwners[$name] = [string]$plan.Source.id
        }
    }

    $preflightLockPath = Join-Path $agentsRoot '.skill-lock.json'
    if (Test-Path -LiteralPath $preflightLockPath) {
        $preflightLock = Get-Content -LiteralPath $preflightLockPath -Raw | ConvertFrom-Json
        foreach ($plan in $sourcePlans) {
            $sourceIdentifier = Get-SourceIdentifier -Source $plan.Source
            foreach ($name in $plan.DesiredNames) {
                $existing = $preflightLock.skills.PSObject.Properties[$name]
                if ($existing -and [string]$existing.Value.source -ne $sourceIdentifier) {
                    throw "Installed skill name collision: $name is already tracked from $($existing.Value.source)."
                }
            }
        }
    }

    if ($PreflightOnly) {
        $skillCount = @($sourcePlans | ForEach-Object { $_.DesiredNames }).Count
        $sharedCount = @($sourcePlans | ForEach-Object { $_.Source.sharedFiles }).Count
        Write-Host "Installation preflight passed for $skillCount skills and $sharedCount shared files. No skill was changed."
        return
    }

    foreach ($plan in $sourcePlans) {
        $source = $plan.Source
        foreach ($skillRoot in @($source.skillRoots)) {
            $installSpec = Get-SourceInstallSpec -Source $source -SkillRoot ([string]$skillRoot)
            $lastError = $null
            for ($attempt = 1; $attempt -le 3; $attempt++) {
                try {
                    Invoke-SkillCli -CliArguments @(
                        'add',
                        [string]$installSpec,
                        '--skill', '*',
                        '--agent', 'codex',
                        '--global',
                        '--copy',
                        '--yes',
                        '--full-depth'
                    )
                    $lastError = $null
                    break
                }
                catch {
                    $lastError = $_
                    if ($attempt -lt 3) {
                        Write-Warning "$($source.label) install attempt $attempt failed; retrying."
                        Start-Sleep -Seconds (3 * $attempt)
                    }
                }
            }
            if ($lastError) { throw $lastError }
        }

        foreach ($sharedFile in $source.sharedFiles) {
            $sourcePath = Resolve-PathUnderRoot -Root $plan.Snapshot -RelativePath ([string]$sharedFile.sourcePath)
            $destination = Resolve-PathUnderRoot -Root $agentsRoot -RelativePath ([string]$sharedFile.destinationRelativeToAgentsRoot)
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $destination -Force
            Write-Host "Synced shared file: $destination"
        }

        $lockPath = Join-Path $agentsRoot '.skill-lock.json'
        if (-not (Test-Path -LiteralPath $lockPath)) {
            throw "Global skill lock does not exist: $lockPath"
        }
        $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
        $sourceIdentifier = Get-SourceIdentifier -Source $source
        $tracked = @($lock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        $excluded = @($tracked | Where-Object { $plan.DesiredNames -notcontains $_.Name })
        Remove-TrackedSkillEntries -Lock $lock -Entries $excluded -AgentsRoot $agentsRoot -LockPath $lockPath

        $remaining = @($lock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        if ($remaining.Count -ne $plan.DesiredNames.Count) {
            throw "$($source.label) expected $($plan.DesiredNames.Count) tracked skills, found $($remaining.Count)."
        }
        Write-Host "$($source.label): $($remaining.Count) skills inside the configured subset."
    }
}
finally {
    foreach ($snapshotPath in $snapshotPaths) {
        if (Test-Path -LiteralPath $snapshotPath) { Remove-Item -LiteralPath $snapshotPath -Recurse -Force }
    }
}

Write-Host 'Direct reinstall complete. No backup was created.'
