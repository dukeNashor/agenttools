[CmdletBinding()]
param(
    [switch]$Open,
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')

function Encode-Html {
    param([AllowNull()][object]$Value)
    return [System.Net.WebUtility]::HtmlEncode([string]$Value)
}

$config = Get-SkillUpdaterConfig
$projectRoot = Get-SkillUpdaterRoot
$agentsRoot = Resolve-PortablePath $config.agentsRoot
$skillsDirectory = Join-Path $agentsRoot 'skills'
$lockPath = Join-Path $agentsRoot '.skill-lock.json'
$lock = $null
if (Test-Path -LiteralPath $lockPath) {
    $lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
}

if (-not $OutputDirectory) { $OutputDirectory = Join-Path $projectRoot 'reports' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$rows = New-Object System.Collections.Generic.List[object]
$sourceSummaries = New-Object System.Collections.Generic.List[object]
$skillNameOwners = @{}

try {
    $tooling = Get-ToolingDiagnostics -Config $config
    if (@($tooling.Errors).Count -gt 0) {
        $rows.Add([pscustomobject]@{
            Source = 'Tooling'
            Item = [string]$config.tooling.skillsCli
            Status = 'Configuration blocked'
            Summary = (@($tooling.Errors) -join ' ')
            Files = @($tooling.Warnings)
        })
    }
    else {
        $latestToolVersion = Get-LatestToolVersion -Config $config -Diagnostics $tooling
        $toolStatus = if ($latestToolVersion -eq $tooling.PinnedVersion) { 'Current' } else { 'Different' }
        $toolSummary = "Pinned $($tooling.PinnedVersion); registry latest $latestToolVersion; runner $($tooling.RunnerMode); $($tooling.Runner.Manager) $($tooling.ManagerVersion); Node $($tooling.NodeVersion)"
        $rows.Add([pscustomobject]@{
            Source = 'Tooling'
            Item = $tooling.PackageName
            Status = $toolStatus
            Summary = $toolSummary
            Files = @($tooling.Warnings)
        })
    }
}
catch {
    $rows.Add([pscustomobject]@{ Source = 'Tooling'; Item = 'skills CLI'; Status = 'Source unavailable'; Summary = $_.Exception.Message; Files = @() })
}

foreach ($source in $config.sources) {
    $snapshot = $null
    $upstreamNames = @()
    try {
        $snapshot = New-RepositorySnapshot -Config $config -Source $source
        $commit = Invoke-ManagedGit -Config $config -GitArguments @('-C', $snapshot, 'rev-parse', 'HEAD')
        $skillFiles = @(Get-ConfiguredSkillFiles -Snapshot $snapshot -Source $source)
        $sourceSummaries.Add([pscustomobject]@{ Label = $source.label; Commit = $commit; Roots = (@($source.skillRoots) -join ', '); State = 'Pinned' })

        foreach ($skillFile in $skillFiles) {
            $name = $skillFile.Directory.Name
            $upstreamNames += $name
            if ($skillNameOwners.ContainsKey($name)) {
                $rows.Add([pscustomobject]@{
                    Source = $source.label
                    Item = $name
                    Status = 'Name collision'
                    Summary = "Also provided by $($skillNameOwners[$name]); installation is blocked"
                    Files = @()
                })
                continue
            }
            $skillNameOwners[$name] = [string]$source.label
            $installedPath = Join-Path $skillsDirectory $name
            if (-not (Test-Path -LiteralPath $installedPath)) {
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $name; Status = 'Not installed'; Summary = 'Install from upstream'; Files = @() })
                continue
            }

            $comparison = Compare-DirectoryContent -Upstream $skillFile.Directory.FullName -Installed $installedPath
            if ($comparison.Equal) {
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $name; Status = 'Current'; Summary = 'Matches upstream'; Files = @() })
            }
            else {
                $summary = "$($comparison.Modified.Count) modified, $($comparison.Added.Count) added upstream, $($comparison.Removed.Count) local-only"
                $files = @($comparison.Modified | ForEach-Object { "Modified: $_" }) +
                    @($comparison.Added | ForEach-Object { "Added upstream: $_" }) +
                    @($comparison.Removed | ForEach-Object { "Local-only: $_" })
                    $rows.Add([pscustomobject]@{ Source = $source.label; Item = $name; Status = 'Different'; Summary = "$summary; pinned commit $commit"; Files = $files })
            }
        }

        foreach ($sharedFile in $source.sharedFiles) {
            $upstreamPath = Resolve-PathUnderRoot -Root $snapshot -RelativePath ([string]$sharedFile.sourcePath)
            $installedPath = Resolve-PathUnderRoot -Root $agentsRoot -RelativePath ([string]$sharedFile.destinationRelativeToAgentsRoot)
            $itemName = 'shared:' + [System.IO.Path]::GetFileName([string]$sharedFile.sourcePath)
            if (-not (Test-Path -LiteralPath $installedPath)) {
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $itemName; Status = 'Not installed'; Summary = 'Shared dependency is missing'; Files = @() })
            }
            elseif ((Get-FileHash -LiteralPath $upstreamPath -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath $installedPath -Algorithm SHA256).Hash) {
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $itemName; Status = 'Current'; Summary = 'Matches upstream'; Files = @() })
            }
            else {
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $itemName; Status = 'Different'; Summary = 'Shared dependency differs from upstream'; Files = @([string]$sharedFile.sourcePath) })
            }
        }

        if ($lock) {
            $sourceIdentifier = Get-SourceIdentifier -Source $source
            $tracked = @($lock.skills.PSObject.Properties | Where-Object { $_.Value.source -eq $sourceIdentifier })
            foreach ($entry in $tracked) {
                if ($upstreamNames -notcontains $entry.Name) {
                    $rows.Add([pscustomobject]@{ Source = $source.label; Item = $entry.Name; Status = 'Outside subset'; Summary = 'Tracked from this source but excluded by config'; Files = @() })
                }
            }
        }
        foreach ($skillFile in $skillFiles) {
            $name = $skillFile.Directory.Name
            $entry = if ($lock) { $lock.skills.PSObject.Properties[$name] } else { $null }
            $expectedPath = $skillFile.FullName.Substring($snapshot.Length).TrimStart('\').Replace('\', '/')
            $installedPath = Join-Path $skillsDirectory $name
            if ((Test-Path -LiteralPath $installedPath) -and (-not $entry -or [string]$entry.Value.source -ne (Get-SourceIdentifier -Source $source) -or [string]$entry.Value.skillPath -ne $expectedPath)) {
                $actual = if ($entry) { "source=$($entry.Value.source), path=$($entry.Value.skillPath)" } else { 'no lock entry' }
                $rows.Add([pscustomobject]@{ Source = $source.label; Item = $name; Status = 'Lock mismatch'; Summary = "Expected path=$expectedPath; actual $actual"; Files = @() })
            }
        }
    }
    catch {
        $sourceSummaries.Add([pscustomobject]@{ Label = $source.label; Commit = '-'; Roots = '-'; State = 'Unavailable' })
        $rows.Add([pscustomobject]@{ Source = $source.label; Item = 'source'; Status = 'Source unavailable'; Summary = $_.Exception.Message; Files = @() })
    }
    finally {
        if ($snapshot -and (Test-Path -LiteralPath $snapshot)) { Remove-Item -LiteralPath $snapshot -Recurse -Force }
    }
}

$generatedAt = Get-Date
$differentCount = @($rows | Where-Object { $_.Status -ne 'Current' }).Count
$currentCount = @($rows | Where-Object { $_.Status -eq 'Current' }).Count

$sourceHtml = ($sourceSummaries | ForEach-Object {
    '<li><strong>' + (Encode-Html $_.Label) + '</strong><span>' + (Encode-Html $_.State) + ' · ' + (Encode-Html $_.Commit) + '<br><code>' + (Encode-Html $_.Roots) + '</code></span></li>'
}) -join [Environment]::NewLine

$rowHtml = ($rows | Sort-Object Source, Item | ForEach-Object {
    $className = switch ($_.Status) {
        'Current' { 'current' }
        'Different' { 'different' }
        default { 'attention' }
    }
    $filesHtml = ''
    if (@($_.Files).Count -gt 0) {
        $items = (@($_.Files) | Select-Object -First 20 | ForEach-Object { '<li><code>' + (Encode-Html $_) + '</code></li>' }) -join ''
        $filesHtml = '<details><summary>Files</summary><ul>' + $items + '</ul></details>'
    }
    '<tr><td>' + (Encode-Html $_.Source) + '</td><td><code>' + (Encode-Html $_.Item) + '</code></td><td><span class="status ' + $className + '">' + (Encode-Html $_.Status) + '</span></td><td>' + (Encode-Html $_.Summary) + $filesHtml + '</td></tr>'
}) -join [Environment]::NewLine

$installCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1'
$html = @"
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent skill update report</title>
<style>
:root{color-scheme:light dark;--bg:#f5f4ef;--panel:#fff;--text:#20201d;--muted:#6b6a63;--line:#deddd5;--ok:#256c3b;--warn:#9a5a00;--bad:#a33a32}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 ui-sans-serif,Segoe UI,sans-serif}.page{max-width:1120px;margin:0 auto;padding:48px 24px 72px}h1{font-size:32px;margin:0 0 8px}.lede{color:var(--muted);margin:0 0 28px}.metrics{display:grid;grid-template-columns:repeat(2,minmax(0,180px));gap:12px;margin:0 0 24px}.metric,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px}.metric{padding:18px}.metric strong{display:block;font-size:28px}.metric span{color:var(--muted)}.panel{padding:20px;margin-top:16px}ul.sources{list-style:none;padding:0;margin:0}.sources li{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line)}.sources li:last-child{border:0}.sources span{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}.status{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700}.current{color:var(--ok);background:color-mix(in srgb,var(--ok) 12%,transparent)}.different{color:var(--warn);background:color-mix(in srgb,var(--warn) 12%,transparent)}.attention{color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}details{margin-top:6px;color:var(--muted)}.command{overflow:auto;padding:14px;border-radius:8px;background:var(--bg)}@media(max-width:760px){.page{padding:28px 14px}.panel{overflow:auto}th,td{min-width:130px}.metrics{grid-template-columns:1fr 1fr}}@media(prefers-color-scheme:dark){:root{--bg:#171816;--panel:#20211f;--text:#eeeeea;--muted:#aaa99f;--line:#3b3c38;--ok:#7cc88e;--warn:#efb45f;--bad:#ef8c84}}
</style>
</head>
<body><main class="page">
<h1>Agent skill update report</h1>
<p class="lede">Read-only comparison generated $(Encode-Html ($generatedAt.ToString('yyyy-MM-dd HH:mm:ss zzz'))) on $(Encode-Html $env:COMPUTERNAME). No skill was changed.</p>
<section class="metrics"><div class="metric"><strong>$currentCount</strong><span>current items</span></div><div class="metric"><strong>$differentCount</strong><span>items needing attention</span></div></section>
<section class="panel"><h2>Sources</h2><ul class="sources">$sourceHtml</ul></section>
<section class="panel"><h2>Comparison</h2><table><thead><tr><th>Source</th><th>Item</th><th>Status</th><th>Details</th></tr></thead><tbody>$rowHtml</tbody></table></section>
<section class="panel"><h2>Apply reviewed updates</h2><p>Run from the <code>skill-updater</code> directory:</p><div class="command"><code>$(Encode-Html $installCommand)</code></div></section>
</main></body></html>
"@

$stamp = $generatedAt.ToString('yyyyMMdd-HHmmss')
$reportPath = Join-Path $OutputDirectory "skill-update-report-$stamp.html"
$latestPath = Join-Path $OutputDirectory 'latest.html'
[System.IO.File]::WriteAllText($reportPath, $html, [System.Text.UTF8Encoding]::new($false))
Copy-Item -LiteralPath $reportPath -Destination $latestPath -Force

$retention = [int]$config.reportRetention
Get-ChildItem -LiteralPath $OutputDirectory -Filter 'skill-update-report-*.html' -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $retention |
    Remove-Item -Force

Write-Output $latestPath
if ($Open) { Start-Process -FilePath $latestPath }
