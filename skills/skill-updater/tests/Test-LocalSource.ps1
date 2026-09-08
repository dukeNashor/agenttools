[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\Common.ps1')

function Assert-True($Condition, [string]$Message) {
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

$testRoot = Join-Path $PSScriptRoot ('.local-source-' + [guid]::NewGuid().ToString('N'))
$snapshot = $null
$config = [pscustomobject]@{ gitProxyMode = 'direct' }
try {
    $repository = Join-Path $testRoot 'repository with spaces'
    $skillDirectory = Join-Path $repository 'skills\fixture'
    $agentsDirectory = Join-Path $skillDirectory 'agents'
    New-Item -ItemType Directory -Path $agentsDirectory -Force | Out-Null
    $skillFile = Join-Path $skillDirectory 'SKILL.md'
    $openAiYaml = Join-Path $agentsDirectory 'openai.yaml'
    $pinnedText = "---`r`nname: fixture`r`ndescription: A fixture skill.`r`n---`r`nPinned content.`r`n"
    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    [IO.File]::WriteAllText($skillFile, $pinnedText, $utf8NoBom)
    [IO.File]::WriteAllText($openAiYaml, "interface:`r`n  display_name: Fixture`r`n", $utf8NoBom)
    Invoke-ManagedGit -Config $config -GitArguments @('init', '--quiet', $repository) | Out-Null
    Invoke-ManagedGit -Config $config -GitArguments @('-C', $repository, 'add', '.') | Out-Null
    $commitArguments = @('-C', $repository, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgSign=false', 'commit', '--quiet', '-m')
    Invoke-ManagedGit -Config $config -GitArguments ($commitArguments + 'pinned') | Out-Null
    $commit = Invoke-ManagedGit -Config $config -GitArguments @('-C', $repository, 'rev-parse', 'HEAD')
    $source = [pscustomobject]@{
        id = 'fixture'; label = 'Local fixture'; sourceType = 'local'; repositoryPath = $repository
        sourceCommit = $commit; skillRoots = @('skills'); selectedSkills = @('fixture'); sharedFiles = @()
    }

    [IO.File]::WriteAllText($skillFile, $pinnedText.Replace('Pinned content.', 'Later commit.'), $utf8NoBom)
    Invoke-ManagedGit -Config $config -GitArguments @('-C', $repository, 'add', '.') | Out-Null
    Invoke-ManagedGit -Config $config -GitArguments ($commitArguments + 'later') | Out-Null
    [IO.File]::AppendAllText($skillFile, 'Dirty working tree.')
    $snapshot = New-RepositorySnapshot -Config $config -Source $source
    $files = @(Get-ConfiguredSkillFiles -Snapshot $snapshot -Source $source)
    Assert-True ($files.Count -eq 1) 'Allowlist must resolve exactly one selected skill.'
    Assert-True ([IO.File]::ReadAllText($files[0].FullName) -eq $pinnedText) 'Snapshot must use the pinned commit, not HEAD or dirty content.'
    Assert-True ([IO.File]::ReadAllText($skillFile).Contains('Dirty working tree.')) 'Original worktree must remain untouched.'
    $agentsRoot = Join-Path $testRoot 'agents'
    $installed = Join-Path $agentsRoot 'skills\fixture'
    $plan = [pscustomobject]@{
        Source = $source; DesiredNames = @('fixture'); SkillFiles = $files
        SkillPaths = @{ fixture = 'skills/fixture/SKILL.md' }
    }
    Copy-LocalSourceSkills -SourcePlan $plan -AgentsRoot $agentsRoot
    Assert-True ([IO.File]::ReadAllBytes((Join-Path $installed 'SKILL.md'))[0] -eq 45) 'Native local copy must preserve BOM-less SKILL.md bytes.'
    Assert-True ([IO.File]::ReadAllBytes((Join-Path $installed 'agents\openai.yaml'))[0] -eq 105) 'Native local copy must preserve BOM-less openai.yaml bytes.'
    $lockPath = Join-Path $agentsRoot '.skill-lock.json'
    $lock = Set-CanonicalLockEntries -Lock $null -SourcePlan $plan -AgentsRoot $agentsRoot -LockPath $lockPath
    Assert-True (Test-CanonicalLockEntry -Entry $lock.skills.fixture -Source $source -SkillPath 'skills/fixture/SKILL.md') 'Local lock identity must verify.'
    Assert-True ($lock.skills.fixture.sourceType -eq 'local') 'Local source must not masquerade as GitHub.'
    $oldStyleEntry = [pscustomobject]@{source = 'fixture'}
    Assert-True (-not (Test-CanonicalLockEntry -Entry $oldStyleEntry -Source $source -SkillPath 'skills/fixture/SKILL.md')) 'Old lock entries without ref must be mismatches.'
    Assert-True ((Format-SkillLockIdentity -Entry $oldStyleEntry).Contains('ref=<missing>')) 'Old lock entries must be reportable under strict mode.'
    $lock.skills.fixture.sourceType = 'github'
    Assert-True (-not (Test-CanonicalLockEntry -Entry $lock.skills.fixture -Source $source -SkillPath 'skills/fixture/SKILL.md')) 'Wrong lock source type must be rejected.'
    $lockBefore = [IO.File]::ReadAllText($lockPath)
    [IO.File]::AppendAllText((Join-Path $installed 'SKILL.md'), 'Different local content.')
    Assert-Rejected { Set-CanonicalLockEntries -Lock $lock -SourcePlan $plan -AgentsRoot $agentsRoot -LockPath $lockPath } 'content differs'
    Assert-True ([IO.File]::ReadAllText($lockPath) -eq $lockBefore) 'Failed content verification must leave the lock untouched.'
    Assert-Rejected { Copy-LocalSourceSkills -SourcePlan $plan -AgentsRoot $agentsRoot } 'requires -Overwrite'
    Copy-LocalSourceSkills -SourcePlan $plan -AgentsRoot $agentsRoot -Overwrite
    Assert-True (Compare-DirectoryContent -Upstream $files[0].Directory.FullName -Installed $installed).Equal 'Explicit overwrite must restore pinned bytes.'
    [IO.File]::WriteAllText($skillFile, $pinnedText, [Text.UTF8Encoding]::new($true))
    Assert-Rejected { Assert-SkillMetadata -SkillFile $skillFile -ExpectedName 'fixture' } 'SKILL.md must use UTF-8 without BOM'
    [IO.File]::WriteAllText($skillFile, $pinnedText, $utf8NoBom)
    [IO.File]::WriteAllText($openAiYaml, "interface:`r`n  display_name: Fixture`r`n", [Text.UTF8Encoding]::new($true))
    Assert-Rejected { Assert-SkillMetadata -SkillFile $skillFile -ExpectedName 'fixture' } 'agents/openai.yaml must use UTF-8 without BOM'
    [IO.File]::WriteAllText($openAiYaml, "interface:`r`n  display_name: Fixture`r`n", $utf8NoBom)
    [IO.File]::WriteAllBytes($skillFile, [byte[]](0xFF, 0xFE, 0x00))
    Assert-Rejected { Assert-SkillMetadata -SkillFile $skillFile -ExpectedName 'fixture' } 'SKILL.md is not valid UTF-8'
    Assert-Rejected { Remove-UpdaterTemporaryDirectory -Path $repository } 'unsafe updater temporary'
    $source.selectedSkills = @('missing')
    Assert-Rejected { Get-ConfiguredSkillFiles -Snapshot $snapshot -Source $source } 'missing'
    $source.skillRoots = @('../outside')
    Assert-Rejected { Get-ConfiguredSkillFiles -Snapshot $snapshot -Source $source } 'Unsafe relative path'
    $source.sourceCommit = '0' * 40
    Assert-Rejected { New-RepositorySnapshot -Config $config -Source $source } 'valid object|Not a valid'
    $source.repositoryPath = 'relative/repository'
    Assert-Rejected { Get-LocalRepositoryPath -Source $source } 'absolute local drive path'
    Write-Host 'Local source regression tests passed (pinned snapshot, dirty worktree, strict UTF-8 metadata, allowlist, lock, content refusal, invalid commit and paths).'
}
finally {
    # Only remove the exact fixture/snapshot directories allocated by this test.
    foreach ($candidate in @($snapshot, $testRoot)) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate)) { continue }
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        $parent = if ($candidate -eq $testRoot) { [IO.Path]::GetFullPath($PSScriptRoot) } else { [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') }
        if ((Split-Path -Parent $resolved) -ne $parent) { throw "Unsafe test cleanup path: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
