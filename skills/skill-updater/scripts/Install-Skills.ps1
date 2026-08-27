[CmdletBinding()]
param(
    [string[]]$SourceId,
    [switch]$Apply,
    [Alias('PruneOnly')][switch]$Prune,
    [switch]$Overwrite,
    [switch]$PurgeLegacy,
    [switch]$PreflightOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

if ($PreflightOnly -and ($Apply -or $Prune -or $Overwrite -or $PurgeLegacy)) {
    throw 'PreflightOnly cannot be combined with Apply, Prune, Overwrite, or PurgeLegacy.'
}
if (($Prune -or $Overwrite -or $PurgeLegacy) -and -not $Apply -and -not $PreflightOnly) {
    throw 'Prune, Overwrite, and PurgeLegacy require Apply.'
}
if (-not $Apply -and -not $PreflightOnly) {
    throw 'This command is read-only unless -PreflightOnly is specified, or changes are explicitly authorized with -Apply.'
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

function Get-LockObject {
    param([Parameter(Mandatory = $true)][string]$LockPath)

    if (-not (Test-Path -LiteralPath $LockPath)) { return $null }
    $lock = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
    if (-not $lock.skills) { throw "Global skill lock has no skills object: $LockPath" }
    return $lock
}

function Test-SourceOwnedLockEntry {
    param(
        [Parameter(Mandatory = $true)]$Entry,
        [Parameter(Mandatory = $true)]$Source
    )

    if ([string]$Entry.source -ne (Get-SourceIdentifier -Source $Source)) { return $false }
    $path = ([string]$Entry.skillPath).Replace('\', '/').TrimStart('/')
    foreach ($root in @($Source.skillRoots)) {
        $normalizedRoot = ([string]$root).Replace('\', '/').Trim('/')
        if ($path.StartsWith($normalizedRoot + '/', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Add-Finding {
    param(
        [Parameter(Mandatory = $true)][System.Collections.Generic.List[object]]$Findings,
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Item,
        [Parameter(Mandatory = $true)][string]$Status,
        [Parameter(Mandatory = $true)][string]$Summary
    )

    $Findings.Add([pscustomobject]@{ Source = $Source; Item = $Item; Status = $Status; Summary = $Summary })
}

function Get-InstallationFindings {
    param(
        [Parameter(Mandatory = $true)]$SourcePlans,
        [Parameter(Mandatory = $true)][AllowNull()]$Lock,
        [Parameter(Mandatory = $true)][string]$AgentsRoot
    )

    $findings = New-Object System.Collections.Generic.List[object]
    $skillsRoot = Join-Path $AgentsRoot 'skills'
    foreach ($plan in $SourcePlans) {
        $sourceIdentifier = Get-SourceIdentifier -Source $plan.Source
        foreach ($skillFile in $plan.SkillFiles) {
            $name = $skillFile.Directory.Name
            $installedPath = Join-Path $skillsRoot $name
            $lockProperty = if ($Lock) { $Lock.skills.PSObject.Properties[$name] } else { $null }
            $entry = if ($lockProperty) { $lockProperty.Value } else { $null }

            if (-not (Test-Path -LiteralPath $installedPath)) {
                Add-Finding -Findings $findings -Source $plan.Source.label -Item $name -Status 'Missing' -Summary 'Expected skill directory is absent.'
                continue
            }
            $installedItem = Get-Item -LiteralPath $installedPath
            if (($installedItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or [bool]$installedItem.LinkType) {
                $linkKind = if ($installedItem.LinkType) { [string]$installedItem.LinkType } else { 'reparse point' }
                Add-Finding -Findings $findings -Source $plan.Source.label -Item $name -Status 'Unsafe path' -Summary "Detected $linkKind at the selected user skill path; tell the user and leave it untouched."
                continue
            }
            if (-not $entry -or [string]$entry.source -ne $sourceIdentifier -or [string]$entry.skillPath -ne [string]$plan.SkillPaths[$name]) {
                $actualSource = if ($entry) { [string]$entry.source } else { '<no lock entry>' }
                $actualPath = if ($entry) { [string]$entry.skillPath } else { '<no lock entry>' }
                Add-Finding -Findings $findings -Source $plan.Source.label -Item $name -Status 'Lock mismatch' -Summary "Expected source=$sourceIdentifier path=$($plan.SkillPaths[$name]); actual source=$actualSource path=$actualPath."
                continue
            }

            $comparison = Compare-DirectoryContent -Upstream $skillFile.Directory.FullName -Installed $installedPath
            if (-not $comparison.Equal) {
                Add-Finding -Findings $findings -Source $plan.Source.label -Item $name -Status 'Different' -Summary "Installed content differs from pinned sourceCommit $($plan.Source.sourceCommit)."
            }
        }

        $tracked = if ($Lock) { @($Lock.skills.PSObject.Properties | Where-Object { [string]$_.Value.source -eq $sourceIdentifier }) } else { @() }
        $excluded = @($tracked | Where-Object {
            (Test-SourceOwnedLockEntry -Entry $_.Value -Source $plan.Source) -and
            $plan.DesiredNames -notcontains $_.Name
        })
        foreach ($entry in $excluded) {
            Add-Finding -Findings $findings -Source $plan.Source.label -Item $entry.Name -Status 'Unmanaged' -Summary 'Tracked from this source root but not present in the explicit selectedSkills allowlist; retained unless explicitly pruned.'
        }

        foreach ($sharedFile in @($plan.Source.sharedFiles)) {
            $sourcePath = Resolve-PathUnderRoot -Root $plan.Snapshot -RelativePath ([string]$sharedFile.sourcePath)
            $destination = Resolve-PathUnderRoot -Root $AgentsRoot -RelativePath ([string]$sharedFile.destinationRelativeToAgentsRoot)
            if (-not (Test-Path -LiteralPath $destination)) {
                Add-Finding -Findings $findings -Source $plan.Source.label -Item ('shared:' + [System.IO.Path]::GetFileName([string]$sharedFile.sourcePath)) -Status 'Missing' -Summary 'Expected shared file is absent.'
            }
            else {
                $destinationItem = Get-Item -LiteralPath $destination
                if (($destinationItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or [bool]$destinationItem.LinkType) {
                Add-Finding -Findings $findings -Source $plan.Source.label -Item ('shared:' + [System.IO.Path]::GetFileName([string]$sharedFile.sourcePath)) -Status 'Unsafe path' -Summary 'Destination is a symlink/Junction/reparse point; tell the user and leave it untouched.'
                }
                elseif ((Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash) {
                    Add-Finding -Findings $findings -Source $plan.Source.label -Item ('shared:' + [System.IO.Path]::GetFileName([string]$sharedFile.sourcePath)) -Status 'Different' -Summary 'Installed shared file differs from the pinned sourceCommit.'
                }
            }
        }
    }
    return $findings.ToArray()
}

$config = Get-SkillUpdaterConfig
Assert-CanonicalAgentsRoot -Config $config
$agentsRoot = Resolve-PortablePath $config.agentsRoot
$lockPath = Join-Path $agentsRoot '.skill-lock.json'
$selectedSources = @($config.sources)
if ($SourceId) {
    $selectedSources = @($config.sources | Where-Object { $SourceId -contains $_.id })
    if ($selectedSources.Count -ne $SourceId.Count) {
        throw 'One or more requested source IDs do not exist in config.json.'
    }
}
if ($PurgeLegacy -and $selectedSources.Count -ne @($config.sources).Count) {
    throw 'PurgeLegacy requires all configured sources to be selected.'
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
        $skillPaths = @{}
        foreach ($skillFile in $skillFiles) {
            $skillPaths[$skillFile.Directory.Name] = $skillFile.FullName.Substring($snapshot.Length).TrimStart('\').Replace('\', '/')
        }
        $skillNamesByRoot = @{}
        foreach ($skillRoot in @($source.skillRoots)) {
            $rootPath = [System.IO.Path]::GetFullPath((Join-Path $snapshot ([string]$skillRoot))).TrimEnd('\')
            $skillNamesByRoot[[string]$skillRoot] = @($skillFiles |
                Where-Object { $_.FullName.StartsWith($rootPath + '\', [System.StringComparison]::OrdinalIgnoreCase) } |
                ForEach-Object { $_.Directory.Name } | Sort-Object -Unique)
        }
        foreach ($sharedFile in @($source.sharedFiles)) {
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
            SkillPaths = $skillPaths
            SkillNamesByRoot = $skillNamesByRoot
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

    $lock = Get-LockObject -LockPath $lockPath
    foreach ($plan in $sourcePlans) {
        $sourceIdentifier = Get-SourceIdentifier -Source $plan.Source
        foreach ($name in $plan.DesiredNames) {
            $existing = if ($lock) { $lock.skills.PSObject.Properties[$name] } else { $null }
            if ($existing -and [string]$existing.Value.source -ne $sourceIdentifier) {
                throw "Installed skill name collision: $name is already tracked from $($existing.Value.source)."
            }
        }
    }

    $findings = @(Get-InstallationFindings -SourcePlans $sourcePlans -Lock $lock -AgentsRoot $agentsRoot)
    $legacyInventory = @(Get-LegacySkillInventory -LegacyRoot (Get-LegacySkillsRoot) -SourcePlans $sourcePlans)
    foreach ($legacy in $legacyInventory) {
        $summary = switch ($legacy.State) {
            'Protected' {
                if ($legacy.ReparsePoint) { "Detected $($legacy.LinkType); tell the user and leave it untouched because ChatGPT Windows discovery may not follow it reliably." }
                else { 'Protected legacy entry; never removed by the updater.' }
            }
            'Legacy match' { "Matches the configured pinned source for $($legacy.Source)." }
            'Divergent legacy' { "Same-name legacy skill differs from the configured pinned source for $($legacy.Source)." }
            'Unmanaged' { 'No configured source ownership; this recognized skill is removed only with -PurgeLegacy.' }
            'Unknown' { 'No SKILL.md was found; retained and never removed by -PurgeLegacy.' }
            default { 'Not a recognized skill directory; retained unless explicitly reviewed.' }
        }
        $status = switch ($legacy.State) {
            'Legacy match' { 'Legacy duplicate' }
            'Divergent legacy' { 'Legacy conflict' }
            'Protected' { 'Protected' }
            'Unmanaged' { 'Legacy unmanaged' }
            'Unknown' { 'Unknown' }
            default { 'Legacy unmanaged' }
        }
        Add-Finding -Findings $findings -Source 'Legacy .codex/skills' -Item $legacy.Name -Status $status -Summary $summary
    }
    foreach ($finding in $findings) {
        if ($finding.Status -in @('Unmanaged', 'Legacy unmanaged', 'Legacy duplicate', 'Protected', 'Unknown')) { Write-Warning "$($finding.Source) / $($finding.Item): $($finding.Summary)" }
        else { Write-Host "$($finding.Status): $($finding.Source) / $($finding.Item) — $($finding.Summary)" }
    }
    foreach ($legacy in @($legacyInventory | Where-Object { $_.ReparsePoint })) {
        Write-Warning "User notice required: $($legacy.LinkType) skill detected at $($legacy.Path); it will not be followed or removed."
    }

    if ($PreflightOnly) {
        $attention = @($findings | Where-Object { $_.Status -ne 'Extra' }).Count
        $skillCount = @($sourcePlans | ForEach-Object { $_.DesiredNames }).Count
        $sharedCount = @($sourcePlans | ForEach-Object { $_.Source.sharedFiles }).Count
        Write-Host "Installation preflight passed for $skillCount skills and $sharedCount shared files; $attention item(s) need attention. No skill was changed."
        return
    }

    if ($PurgeLegacy -and @($legacyInventory | Where-Object { $_.State -eq 'Divergent legacy' }).Count -gt 0) {
        throw 'A divergent legacy skill has the same name as a configured skill. Resolve the conflict before using -PurgeLegacy.'
    }

    $overwriteRequired = @($findings | Where-Object { $_.Status -in @('Different', 'Lock mismatch') })
    $unsafePaths = @($findings | Where-Object { $_.Status -eq 'Unsafe path' })
    if ($unsafePaths.Count -gt 0) {
        foreach ($finding in $unsafePaths) { Write-Warning "User notice required: $($finding.Source) / $($finding.Item): $($finding.Summary)" }
        throw 'A selected user path is a symlink/Junction/reparse point. Resolve it with the user before applying updates.'
    }
    if ($overwriteRequired.Count -gt 0 -and -not $Overwrite) {
        throw 'Existing installed content or lock metadata differs from the pinned expectation. Re-run with -Apply -Overwrite after reviewing the findings.'
    }

    foreach ($plan in $sourcePlans) {
        $source = $plan.Source
        foreach ($skillRoot in @($source.skillRoots)) {
            $skillNames = @($plan.SkillNamesByRoot[[string]$skillRoot])
            if ($skillNames.Count -eq 0) { continue }
            $installSpec = Get-SourceInstallSpec -Source $source -SkillRoot ([string]$skillRoot)
            $lastError = $null
            for ($attempt = 1; $attempt -le 3; $attempt++) {
                try {
                    $cliArguments = @('add', [string]$installSpec, '--skill') + $skillNames + @(
                        '--agent', 'codex',
                        '--global',
                        '--copy',
                        '--yes',
                        '--full-depth'
                    )
                    Invoke-SkillCli -CliArguments $cliArguments
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

        foreach ($sharedFile in @($source.sharedFiles)) {
            $sourcePath = Resolve-PathUnderRoot -Root $plan.Snapshot -RelativePath ([string]$sharedFile.sourcePath)
            $destination = Resolve-PathUnderRoot -Root $agentsRoot -RelativePath ([string]$sharedFile.destinationRelativeToAgentsRoot)
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $sourcePath -Destination $destination -Force
            Write-Host "Synced shared file: $destination"
        }

        $updatedLock = Get-LockObject -LockPath $lockPath
        if (-not $updatedLock) { throw "Global skill lock does not exist after installation: $lockPath" }
        $sourceIdentifier = Get-SourceIdentifier -Source $source
        $tracked = @($updatedLock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        if ($Prune) {
            $excluded = @($tracked | Where-Object {
                (Test-SourceOwnedLockEntry -Entry $_.Value -Source $source) -and
                $plan.DesiredNames -notcontains $_.Name
            })
            Remove-TrackedSkillEntries -Lock $updatedLock -Entries $excluded -AgentsRoot $agentsRoot -LockPath $lockPath
            $updatedLock = Get-LockObject -LockPath $lockPath
        }

        foreach ($name in $plan.DesiredNames) {
            $entry = $updatedLock.skills.PSObject.Properties[$name]
            if (-not $entry -or [string]$entry.Value.source -ne $sourceIdentifier -or [string]$entry.Value.skillPath -ne [string]$plan.SkillPaths[$name]) {
                throw "$($source.label) did not produce the expected .skill-lock.json entry for $name."
            }
        }
        $remaining = @($updatedLock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
        Write-Host "$($source.label): $($plan.DesiredNames.Count) expected skills verified; $($remaining.Count) same-source entries retained."
    }

    if ($PurgeLegacy) {
        $legacyRoot = Get-LegacySkillsRoot
        if (Test-Path -LiteralPath $legacyRoot) {
            $resolvedLegacyRoot = (Resolve-Path -LiteralPath $legacyRoot).Path.TrimEnd('\')
            foreach ($legacy in $legacyInventory | Where-Object { -not $_.Protected -and $_.HasSkillFile }) {
                $resolvedLegacyPath = (Resolve-Path -LiteralPath $legacy.Path).Path
                if (-not $resolvedLegacyPath.StartsWith($resolvedLegacyRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Refusing to remove a legacy path outside .codex/skills: $resolvedLegacyPath"
                }
                Remove-Item -LiteralPath $resolvedLegacyPath -Recurse -Force
                Write-Host "Removed legacy skill: $resolvedLegacyPath"
            }
        }
    }
}
finally {
    foreach ($snapshotPath in $snapshotPaths) {
        if (Test-Path -LiteralPath $snapshotPath) { Remove-Item -LiteralPath $snapshotPath -Recurse -Force }
    }
}

Write-Host 'Skill installation completed. No backup was created.'
