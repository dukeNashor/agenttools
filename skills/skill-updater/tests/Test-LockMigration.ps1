[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Assert-Rejected([scriptblock]$Action, [string]$Pattern) {
    try { & $Action | Out-Null }
    catch {
        if ($_.Exception.Message -notmatch $Pattern) { throw }
        return
    }
    throw "Expected rejection matching: $Pattern"
}

$testRoot = Join-Path $PSScriptRoot ('.lock-migration-' + [guid]::NewGuid().ToString('N'))
$previousTestRoot = $env:AGENTTOOLS_LOCK_TEST_ROOT
$utf8 = [Text.UTF8Encoding]::new($false)
try {
    $scripts = Join-Path $testRoot 'scripts'
    $upstream = Join-Path $testRoot 'snapshot/skills/fixture'
    $installed = Join-Path $testRoot 'agents/skills/fixture'
    New-Item -ItemType Directory -Path $scripts, $upstream, $installed -Force | Out-Null
    foreach ($name in @('Common.ps1', 'Install-Skills.ps1', 'New-SkillUpdateReport.ps1')) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot "../scripts/$name") -Destination $scripts
    }
    $skillText = "---`nname: fixture`ndescription: Lock migration fixture.`n---`nPinned content.`n"
    [IO.File]::WriteAllText((Join-Path $upstream 'SKILL.md'), $skillText, $utf8)
    [IO.File]::WriteAllText((Join-Path $installed 'SKILL.md'), $skillText, $utf8)
    $source = [pscustomobject]@{
        id = 'fixture'; label = 'Fixture'; repositorySlug = 'fixture/skills'
        sourceCommit = 'a' * 40; skillRoots = @('skills'); selectedSkills = @('fixture'); sharedFiles = @()
    }
    $config = [pscustomobject]@{
        agentsRoot = (Join-Path $testRoot 'agents'); sources = @($source)
        gitProxyMode = 'direct'; reportRetention = 2; tooling = @{ skillsCli = 'skills@1.5.23' }
    }
    [IO.File]::WriteAllText((Join-Path $testRoot 'config.json'), ($config | ConvertTo-Json -Depth 10), $utf8)
    # Run the actual installer and report. Substitute only external boundaries:
    # fixture root, verified snapshot, tooling/network and unrelated scope inventory.
    $boundaries = @'

function Get-SkillUpdaterConfig { Get-Content (Join-Path $env:AGENTTOOLS_LOCK_TEST_ROOT 'config.json') -Raw | ConvertFrom-Json }
function Assert-CanonicalAgentsRoot {
    param($Config)
    if ($Config.agentsRoot -ne (Join-Path $env:AGENTTOOLS_LOCK_TEST_ROOT 'agents')) { throw 'Unsafe fixture root' }
}
function Assert-ToolingEnvironment { param($Config) }
function Assert-GitEnvironment { param($Config) }
function New-RepositorySnapshot { param($Config, $Source) Join-Path $env:AGENTTOOLS_LOCK_TEST_ROOT 'snapshot' }
function Remove-UpdaterTemporaryDirectory {
    param($Path)
    if ($Path -ne (Join-Path $env:AGENTTOOLS_LOCK_TEST_ROOT 'snapshot')) { throw 'Unsafe fixture snapshot cleanup' }
}
function Get-LegacySkillsRoot { Join-Path $env:AGENTTOOLS_LOCK_TEST_ROOT 'legacy' }
function Get-LegacySkillInventory { param($LegacyRoot, $SourcePlans) @() }
function Get-ReadonlySkillScopeInventory { param($ProjectRoot, $AgentsRoot) @() }
function Get-ManagedGitTransport { param($Config) [pscustomobject]@{Description='Fixture transport'} }
function Test-ManagedGitHubConnection { param($Config, $Source) }
function Get-ToolingDiagnostics { param($Config) throw 'Tooling lookup excluded from lock fixture' }
function Invoke-ManagedGit {
    param($Config, $GitArguments)
    if (($GitArguments -join ' ') -notmatch 'rev-parse HEAD$') { throw 'Unexpected fixture Git invocation' }
    return 'a' * 40
}
function Invoke-SkillCli {
    param($CliArguments)
    $root = $env:AGENTTOOLS_LOCK_TEST_ROOT
    [IO.File]::AppendAllText((Join-Path $root 'cli-calls.txt'), "install`n")
    Copy-Item -LiteralPath (Join-Path $root 'snapshot/skills/fixture/SKILL.md') -Destination (Join-Path $root 'agents/skills/fixture/SKILL.md') -Force
    $path = Join-Path $root 'agents/.skill-lock.json'
    $lock = Get-Content $path -Raw | ConvertFrom-Json
    # Like a CLI rewrite, replace the entry and omit its previous extra fields.
    $lock.skills.fixture = [pscustomobject]@{ source='fixture/skills'; cliExtra='retained' }
    [IO.File]::WriteAllText($path, ($lock | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
}
'@
    [IO.File]::AppendAllText((Join-Path $scripts 'Common.ps1'), $boundaries, $utf8)
    $env:AGENTTOOLS_LOCK_TEST_ROOT = $testRoot
    . (Join-Path $scripts 'Common.ps1')
    $lockPath = Join-Path $testRoot 'agents/.skill-lock.json'
    $oldLock = [pscustomobject]@{
        version = 3; dismissed = @{ notice = $true }; customTop = 'preserved'
        skills = [pscustomobject]@{
            fixture = [pscustomobject]@{
                source = 'fixture/skills'; sourceType = 'github'; sourceUrl = 'https://github.com/fixture/skills.git'
                skillPath = 'skills/fixture/SKILL.md'; installedAt = '2026-08-24T00:00:00Z'
                pluginName = 'fixture-plugin'; custom = @{ nested = @('one', 'two') }
            }
            untouched = [pscustomobject]@{source='other/source'; skillPath='skills/untouched/SKILL.md'; marker=42}
        }
    }
    $oldJson = $oldLock | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($lockPath, $oldJson, $utf8)
    & (Join-Path $scripts 'New-SkillUpdateReport.ps1') -OutputDirectory (Join-Path $testRoot 'reports') | Out-Null
    $report = [IO.File]::ReadAllText((Join-Path $testRoot 'reports/latest.html'))
    Assert-True ($report.Contains('Missing lock ref')) 'Report must distinguish missing ref from a true revision mismatch.'
    Assert-True ([IO.File]::ReadAllText($lockPath) -ceq $oldJson) 'Reports must leave the lock byte-for-byte unchanged.'

    & (Join-Path $scripts 'Install-Skills.ps1') -Apply
    $migrated = Get-Content $lockPath -Raw | ConvertFrom-Json
    Assert-True (Test-CanonicalLockEntry -Entry $migrated.skills.fixture -Source $source -SkillPath 'skills/fixture/SKILL.md') 'Apply must migrate verified missing-ref entries without Overwrite.'
    Assert-True (-not (Test-Path (Join-Path $testRoot 'cli-calls.txt'))) 'Metadata-only migration must not invoke the CLI.'
    Assert-True ($migrated.skills.fixture.pluginName -eq 'fixture-plugin' -and $migrated.skills.fixture.custom.nested[1] -eq 'two') 'Migration must preserve extra and nested fields.'
    Assert-True ($migrated.skills.fixture.installedAt -eq '2026-08-24T00:00:00Z') 'Migration must preserve installedAt.'
    Assert-True ($migrated.customTop -eq 'preserved' -and $migrated.dismissed.notice -and $migrated.skills.untouched.marker -eq 42) 'Migration must preserve top-level state and unrelated skills.'

    foreach ($difference in @('modified', 'added', 'removed', 'hidden')) {
        [IO.File]::WriteAllText($lockPath, $oldJson, $utf8)
        $extra = Join-Path $installed 'extra.txt'
        switch ($difference) {
            'modified' { [IO.File]::AppendAllText((Join-Path $installed 'SKILL.md'), 'Changed') }
            'added' { [IO.File]::WriteAllText((Join-Path $upstream 'new.txt'), 'New upstream file', $utf8) }
            'removed' { [IO.File]::WriteAllText($extra, 'Local-only file', $utf8) }
            'hidden' {
                [IO.File]::WriteAllText($extra, 'Hidden local-only file', $utf8)
                [IO.File]::SetAttributes($extra, [IO.FileAttributes]::Hidden)
            }
        }
        Assert-Rejected { & (Join-Path $scripts 'Install-Skills.ps1') -Apply } 'Re-run with -Apply -Overwrite'
        Assert-True ([IO.File]::ReadAllText($lockPath) -ceq $oldJson) "$difference content must block lock migration."
        [IO.File]::WriteAllText((Join-Path $installed 'SKILL.md'), $skillText, $utf8)
        foreach ($file in @($extra, (Join-Path $upstream 'new.txt'))) {
            if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
        }
    }

    $entry = $oldLock.skills.fixture
    Assert-True ((Get-SkillLockStatus -Entry $entry -Source $source -SkillPath 'skills/fixture/SKILL.md') -eq 'Missing lock ref') 'Missing ref must be classified.'
    $entry | Add-Member -NotePropertyName ref -NotePropertyValue '  '
    Assert-True ((Get-SkillLockStatus -Entry $entry -Source $source -SkillPath 'skills/fixture/SKILL.md') -eq 'Missing lock ref') 'Blank ref must be classified as missing.'
    $entry.ref = 'b' * 40
    $revisionJson = $oldLock | ConvertTo-Json -Depth 20
    [IO.File]::WriteAllText($lockPath, $revisionJson, $utf8)
    Assert-Rejected { & (Join-Path $scripts 'Install-Skills.ps1') -Apply } 'Re-run with -Apply -Overwrite'
    Assert-True ([IO.File]::ReadAllText($lockPath) -ceq $revisionJson) 'Different ref must still require Overwrite even when files match.'
    & (Join-Path $scripts 'New-SkillUpdateReport.ps1') -OutputDirectory (Join-Path $testRoot 'reports') | Out-Null
    Assert-True ([IO.File]::ReadAllText((Join-Path $testRoot 'reports/latest.html')).Contains('Lock revision mismatch')) 'Report must identify genuine revision differences.'
    $entry.ref = ''
    $entry.sourceType = 'local'
    Assert-True ((Get-SkillLockStatus -Entry $entry -Source $source -SkillPath 'skills/fixture/SKILL.md') -eq 'Lock identity mismatch') 'Wrong identity must never qualify for missing-ref repair.'
    Assert-True ((Get-SkillLockStatus -Entry $null -Source $source -SkillPath 'skills/fixture/SKILL.md') -eq 'Missing lock entry') 'Missing entry must not be treated as missing ref.'
    foreach ($case in @('identity', 'missing-entry')) {
        $conflict = $oldLock | ConvertTo-Json -Depth 20 | ConvertFrom-Json
        if ($case -eq 'missing-entry') { $conflict.skills.PSObject.Properties.Remove('fixture') }
        $conflictJson = $conflict | ConvertTo-Json -Depth 20
        [IO.File]::WriteAllText($lockPath, $conflictJson, $utf8)
        Assert-Rejected { & (Join-Path $scripts 'Install-Skills.ps1') -Apply } 'Re-run with -Apply -Overwrite'
        Assert-True ([IO.File]::ReadAllText($lockPath) -ceq $conflictJson) "$case must block unapproved metadata replacement."
    }

    # An actual reinstall must preserve pre-CLI fields as well as new CLI fields.
    [IO.File]::WriteAllText($lockPath, $oldJson, $utf8)
    [IO.File]::AppendAllText((Join-Path $installed 'SKILL.md'), 'Changed')
    & (Join-Path $scripts 'Install-Skills.ps1') -Apply -Overwrite
    $reinstalled = Get-Content $lockPath -Raw | ConvertFrom-Json
    Assert-True (Test-Path (Join-Path $testRoot 'cli-calls.txt')) 'Different content must actually be reinstalled.'
    Assert-True ($reinstalled.skills.fixture.pluginName -eq 'fixture-plugin' -and $reinstalled.skills.fixture.cliExtra -eq 'retained') 'Reinstall must retain both pre-CLI and new CLI metadata.'
    Assert-True ($reinstalled.skills.fixture.installedAt -eq '2026-08-24T00:00:00Z') 'CLI rewrite must not reset installedAt.'
    Write-Host 'Lock migration regression tests passed (real installer/report, content gates, hidden files, read-only report, metadata preservation).'
}
finally {
    $env:AGENTTOOLS_LOCK_TEST_ROOT = $previousTestRoot
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = (Resolve-Path -LiteralPath $testRoot).Path
        if ((Split-Path -Parent $resolved) -ne [IO.Path]::GetFullPath($PSScriptRoot)) { throw "Unsafe test cleanup path: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
