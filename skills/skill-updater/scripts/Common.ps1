Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-SkillUpdaterRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Get-SkillUpdaterConfig {
    $path = Join-Path (Get-SkillUpdaterRoot) 'config.json'
    $config = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $localPath = Join-Path (Get-SkillUpdaterRoot) 'config.local.json'
    if (-not (Test-Path -LiteralPath $localPath)) { return $config }

    $local = Get-Content -LiteralPath $localPath -Raw | ConvertFrom-Json
    $allowedTopLevel = @('gitBypassProxy', 'tooling')
    foreach ($property in $local.PSObject.Properties) {
        if ($allowedTopLevel -notcontains $property.Name) {
            throw "Unsupported machine-local setting: $($property.Name)"
        }
    }

    if ($local.PSObject.Properties['gitBypassProxy']) {
        $config.gitBypassProxy = [bool]$local.gitBypassProxy
    }
    if ($local.PSObject.Properties['tooling']) {
        foreach ($property in $local.tooling.PSObject.Properties) {
            if ($property.Name -notin @('allowedRegistries', 'runner')) {
                throw "Unsupported machine-local tooling setting: $($property.Name)"
            }
            if ($property.Name -eq 'runner' -and [string]$property.Value -notin @('codex-bundled-pnpm', 'user-npx')) {
                throw "Unsupported machine-local runner: $($property.Value)"
            }
        }
        if ($local.tooling.PSObject.Properties['runner']) {
            $config.tooling.runner = [string]$local.tooling.runner
        }
        if ($local.tooling.PSObject.Properties['allowedRegistries']) {
            $config.tooling.allowedRegistries = @($local.tooling.allowedRegistries)
        }
    }
    return $config
}

function Resolve-PortablePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($Path -eq '~') {
        return $env:USERPROFILE
    }
    if ($Path.StartsWith('~/') -or $Path.StartsWith('~\')) {
        return (Join-Path $env:USERPROFILE $Path.Substring(2))
    }
    return $Path
}

function Test-SafeRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Contains('\')) { return $false }
    $normalized = $Path.Trim('/')
    if ($normalized -ne $Path) { return $false }
    $parts = @($normalized.Split('/'))
    if ($parts.Count -eq 0 -or @($parts | Where-Object { -not $_ -or $_ -eq '.' -or $_ -eq '..' }).Count -gt 0) {
        return $false
    }
    return -not [System.IO.Path]::IsPathRooted($Path)
}

function Resolve-PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    if (-not (Test-SafeRelativePath -Path $RelativePath)) {
        throw "Unsafe relative path: $RelativePath"
    }
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $rootPath $RelativePath))
    if (-not $candidate.StartsWith($rootPath + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes its configured root: $RelativePath"
    }
    return $candidate
}

function Add-DirectoryToProcessPath {
    param([Parameter(Mandatory = $true)][string]$Directory)

    $parts = @($env:PATH -split ';')
    if ($parts -notcontains $Directory) {
        $env:PATH = $Directory + ';' + $env:PATH
    }
}

function Resolve-SkillRunner {
    $config = Get-SkillUpdaterConfig
    $packageSpec = [string]$config.tooling.skillsCli
    $runnerMode = [string]$config.tooling.runner
    if ($runnerMode -eq 'user-npx') {
        $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
        if (-not $npx) { $npx = Get-Command npx -ErrorAction SilentlyContinue }
        if (-not $npx) { throw 'Configured runner user-npx, but npx was not found on PATH.' }
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
        if (-not $npm) {
            $adjacentNpm = Join-Path (Split-Path -Parent $npx.Source) 'npm.cmd'
            if (Test-Path -LiteralPath $adjacentNpm) { $npm = Get-Item -LiteralPath $adjacentNpm }
        }
        if (-not $npm) { throw 'Configured runner user-npx, but its npm configuration command could not be resolved.' }
        $npmPath = if ($npm.PSObject.Properties['FullName']) { $npm.FullName } else { $npm.Source }
        return [pscustomobject]@{
            FilePath = $npx.Source
            ConfigFilePath = $npmPath
            Manager = 'npm'
            Mode = $runnerMode
            Prefix = @($packageSpec)
            DisplayName = "user npx $packageSpec"
        }
    }

    if ($runnerMode -ne 'codex-bundled-pnpm') {
        throw "Unsupported runner mode: $runnerMode"
    }

    $runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
    $pnpm = $null
    if (Test-Path -LiteralPath $runtimeRoot) {
        $pnpm = Get-ChildItem -LiteralPath $runtimeRoot -Filter 'pnpm.cmd' -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]dependencies[\\/]bin[\\/]fallback[\\/]pnpm\.cmd$' } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
    }

    if (-not $pnpm) {
        throw 'Configured runner codex-bundled-pnpm, but Codex bundled pnpm was not found.'
    }

    if ($pnpm) {
        $pnpmPath = if ($pnpm.PSObject.Properties['FullName']) { $pnpm.FullName } else { $pnpm.Source }
        $dependenciesRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $pnpmPath))
        $bundledNode = Join-Path $dependenciesRoot 'node\bin\node.exe'
        if (Test-Path -LiteralPath $bundledNode) {
            Add-DirectoryToProcessPath (Split-Path -Parent $bundledNode)
        }
        return [pscustomobject]@{
            FilePath = $pnpmPath
            ConfigFilePath = $pnpmPath
            Manager = 'pnpm'
            Mode = $runnerMode
            Prefix = @('dlx', $packageSpec)
            DisplayName = "Codex bundled pnpm dlx $packageSpec"
        }
    }

}

function Resolve-NodeExecutable {
    $config = Get-SkillUpdaterConfig
    $runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
    if ([string]$config.tooling.runner -eq 'codex-bundled-pnpm') {
        if (-not (Test-Path -LiteralPath $runtimeRoot)) {
            throw 'Configured runner codex-bundled-pnpm, but Codex bundled Node.js was not found.'
        }
        $bundledNode = Get-ChildItem -LiteralPath $runtimeRoot -Filter 'node.exe' -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]dependencies[\\/]node[\\/]bin[\\/]node\.exe$' } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($bundledNode) { return $bundledNode.FullName }
        throw 'Configured runner codex-bundled-pnpm, but Codex bundled Node.js was not found.'
    }

    $node = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $node) { $node = Get-Command node -ErrorAction SilentlyContinue }
    if ($node) { return $node.Source }
    throw 'Configured runner user-npx, but user-provided Node.js was not found on PATH.'
}

function Get-NormalizedRegistryUrl {
    param([Parameter(Mandatory = $true)][string]$Url)

    $uri = $null
    if (-not [uri]::TryCreate($Url, [System.UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -ne 'https') {
        throw "Registry must be an absolute HTTPS URL: $Url"
    }
    return $uri.AbsoluteUri.TrimEnd('/') + '/'
}

function Get-PackageSpecParts {
    param([Parameter(Mandatory = $true)][string]$PackageSpec)

    $separator = $PackageSpec.LastIndexOf('@')
    if ($separator -le 0) { throw "Package spec must include an exact version: $PackageSpec" }
    $name = $PackageSpec.Substring(0, $separator)
    $version = $PackageSpec.Substring($separator + 1)
    if ($version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
        throw "Package spec must use an exact semantic version: $PackageSpec"
    }
    return [pscustomobject]@{ Name = $name; Version = $version }
}

function Get-PackageManagerSetting {
    param(
        [Parameter(Mandatory = $true)]$Runner,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $output = & $Runner.ConfigFilePath config get $Name 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $value = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    if (-not $value -or $value -eq 'undefined' -or $value -eq 'null') { return $null }
    return $value
}

function Get-ToolingDiagnostics {
    param([Parameter(Mandatory = $true)]$Config)

    $errors = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $runner = $null
    $nodeVersion = $null
    $managerVersion = $null
    $registry = $null

    try {
        $package = Get-PackageSpecParts -PackageSpec ([string]$Config.tooling.skillsCli)
    }
    catch {
        $errors.Add($_.Exception.Message)
        $package = [pscustomobject]@{ Name = ''; Version = '' }
    }

    try {
        $runner = Resolve-SkillRunner
        $managerVersion = ((& $runner.ConfigFilePath --version 2>$null) -join '').Trim()
        if ($LASTEXITCODE -ne 0) { throw 'Package manager version check failed.' }
    }
    catch {
        $errors.Add($_.Exception.Message)
    }

    try {
        $nodeOutput = ((& (Resolve-NodeExecutable) --version 2>$null) -join '').Trim().TrimStart('v')
        $nodeVersion = [version]($nodeOutput -replace '-.*$', '')
        $minimumNodeVersion = [version]([string]$Config.tooling.minimumNodeVersion)
        if ($nodeVersion -lt $minimumNodeVersion) {
            $errors.Add("Node.js $nodeVersion is below the configured minimum $minimumNodeVersion.")
        }
    }
    catch {
        $errors.Add("Node.js validation failed: $($_.Exception.Message)")
    }

    if ($runner) {
        try {
            $registry = Get-NormalizedRegistryUrl -Url ([string](Get-PackageManagerSetting -Runner $runner -Name 'registry'))
            $allowedRegistries = @($Config.tooling.allowedRegistries | ForEach-Object { Get-NormalizedRegistryUrl -Url ([string]$_) })
            if ($allowedRegistries -notcontains $registry) {
                $errors.Add("Package registry is not trusted by config: $registry")
            }
        }
        catch {
            $errors.Add("Package registry validation failed: $($_.Exception.Message)")
        }

        $strictSsl = Get-PackageManagerSetting -Runner $runner -Name 'strict-ssl'
        if ($strictSsl -and $strictSsl -eq 'false') {
            $errors.Add('Package manager strict SSL validation is disabled.')
        }
        $offline = Get-PackageManagerSetting -Runner $runner -Name 'offline'
        if ($offline -and $offline -eq 'true') {
            $errors.Add('Package manager offline mode is enabled.')
        }
        $ignoreScripts = Get-PackageManagerSetting -Runner $runner -Name 'ignore-scripts'
        if ($ignoreScripts -and $ignoreScripts -eq 'true') {
            $warnings.Add('Package manager lifecycle scripts are disabled; the pinned CLI may fail if it adds a build step.')
        }
        foreach ($setting in @('proxy', 'https-proxy', 'noproxy', 'ca', 'cafile', 'cert', 'certfile', 'key', 'keyfile')) {
            if (Get-PackageManagerSetting -Runner $runner -Name $setting) {
                $warnings.Add("Package manager setting is present: $setting")
            }
        }
    }

    $environmentNames = @(Get-ChildItem Env: | Where-Object {
        $_.Name -match '^(NPM_CONFIG_|PNPM_|HTTP_PROXY$|HTTPS_PROXY$|NO_PROXY$|ALL_PROXY$)'
    } | Select-Object -ExpandProperty Name | Sort-Object)
    foreach ($name in $environmentNames) { $warnings.Add("Process environment setting is present: $name") }

    return [pscustomobject]@{
        Errors = @($errors)
        Warnings = @($warnings)
        Runner = $runner
        PackageName = $package.Name
        PinnedVersion = $package.Version
        NodeVersion = if ($nodeVersion) { $nodeVersion.ToString() } else { $null }
        ManagerVersion = $managerVersion
        Registry = $registry
        RunnerMode = if ($runner) { $runner.Mode } else { [string]$Config.tooling.runner }
    }
}

function Assert-ToolingEnvironment {
    param([Parameter(Mandatory = $true)]$Config)

    $diagnostics = Get-ToolingDiagnostics -Config $Config
    if (@($diagnostics.Errors).Count -gt 0) {
        throw (@($diagnostics.Errors) -join [Environment]::NewLine)
    }
    foreach ($warning in @($diagnostics.Warnings)) { Write-Warning $warning }
    return $diagnostics
}

function Get-LatestToolVersion {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Diagnostics
    )

    $output = & $Diagnostics.Runner.ConfigFilePath view $Diagnostics.PackageName version --json 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
    }
    $value = (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
    try { return [string]($value | ConvertFrom-Json) }
    catch { return $value.Trim('"') }
}

function Invoke-SkillCli {
    param([Parameter(Mandatory = $true)][string[]]$CliArguments)

    $config = Get-SkillUpdaterConfig
    $diagnostics = Assert-ToolingEnvironment -Config $config
    Assert-GitEnvironment -Config $config
    $runner = $diagnostics.Runner
    $arguments = @($runner.Prefix) + $CliArguments
    Write-Host ('=> ' + $runner.DisplayName + ' ' + ($CliArguments -join ' '))

    $originalPath = $env:PATH
    $gitWrapperRoot = $null

    try {
        if ($config.gitBypassProxy) {
            $realGit = Resolve-GitExecutable
            $gitWrapperRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('agenttools-git-wrapper-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $gitWrapperRoot | Out-Null
            $wrapperPath = Join-Path $gitWrapperRoot 'git.cmd'
            $wrapper = "@echo off`r`n`"$realGit`" -c http.proxy= -c https.proxy= %*`r`n"
            [System.IO.File]::WriteAllText($wrapperPath, $wrapper, [System.Text.Encoding]::ASCII)
            $env:PATH = $gitWrapperRoot + ';' + $env:PATH
        }
        & $runner.FilePath @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Skill CLI failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        $env:PATH = $originalPath
        if ($gitWrapperRoot -and (Test-Path -LiteralPath $gitWrapperRoot)) {
            Remove-Item -LiteralPath $gitWrapperRoot -Recurse -Force
        }
    }
}

function Resolve-GitExecutable {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { $git = Get-Command git -ErrorAction SilentlyContinue }
    if ($git) { return $git.Source }

    $runtimeRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
    if (Test-Path -LiteralPath $runtimeRoot) {
        $bundledGit = Get-ChildItem -LiteralPath $runtimeRoot -Filter 'git.exe' -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '[\\/]dependencies[\\/]native[\\/]git[\\/]cmd[\\/]git\.exe$' } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($bundledGit) { return $bundledGit.FullName }
    }

    throw 'Git was not found on PATH or in the Codex bundled runtime.'
}

function Get-GitSafetyDiagnostics {
    param([Parameter(Mandatory = $true)]$Config)

    $errors = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $git = Resolve-GitExecutable
    $arguments = @()
    if ($Config.gitBypassProxy) { $arguments += @('-c', 'http.proxy=', '-c', 'https.proxy=') }
    $arguments += @('config', '--get-regexp', '^(url\..*\.insteadof|http(\..*)?\.(sslverify|extraheader))$')
    $entries = & $git @arguments 2>$null
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
        $errors.Add('Git safety configuration could not be inspected.')
    }

    foreach ($entry in @($entries)) {
        $parts = $entry.ToString() -split '\s+', 2
        if ($parts.Count -ne 2) { continue }
        $key = $parts[0]
        $value = $parts[1]
        if ($key -match '^url\..*\.insteadof$') {
            foreach ($source in $Config.sources) {
                $cloneUrl = Get-SourceCloneUrl -Source $source
                if ($cloneUrl.StartsWith($value, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $errors.Add("Git url.*.insteadOf rewrites the configured source URL prefix: $value")
                }
            }
        }
        elseif ($key -match '\.sslverify$' -and $value -eq 'false') {
            $errors.Add('Git SSL verification is disabled for an applicable HTTP scope.')
        }
        elseif ($key -match '\.extraheader$' -and ($key -eq 'http.extraheader' -or $key -match 'github\.com')) {
            $errors.Add('Git sends an extra HTTP header to GitHub; public source clones are blocked to avoid credential leakage.')
        }
    }

    if ($env:GIT_SSL_NO_VERIFY -and $env:GIT_SSL_NO_VERIFY -ne '0' -and $env:GIT_SSL_NO_VERIFY -ne 'false') {
        $errors.Add('GIT_SSL_NO_VERIFY disables Git certificate verification.')
    }
    foreach ($name in @('GIT_CONFIG_COUNT', 'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM')) {
        if (Test-Path -LiteralPath "Env:$name") { $warnings.Add("Process environment setting is present: $name") }
    }

    return [pscustomobject]@{ Errors = @($errors); Warnings = @($warnings); Git = $git }
}

function Assert-GitEnvironment {
    param([Parameter(Mandatory = $true)]$Config)

    $diagnostics = Get-GitSafetyDiagnostics -Config $Config
    if (@($diagnostics.Errors).Count -gt 0) {
        throw (@($diagnostics.Errors) -join [Environment]::NewLine)
    }
    foreach ($warning in @($diagnostics.Warnings)) { Write-Warning $warning }
    return $diagnostics
}

function Invoke-ManagedGit {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][string[]]$GitArguments
    )

    $arguments = @()
    if ($Config.gitBypassProxy) {
        $arguments += @('-c', 'http.proxy=', '-c', 'https.proxy=')
    }
    $arguments += $GitArguments

    $output = & (Resolve-GitExecutable) @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
    }
    return (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
}

function Get-SourceIdentifier {
    param([Parameter(Mandatory = $true)]$Source)

    return [string]$Source.repositorySlug
}

function Get-SourceCloneUrl {
    param([Parameter(Mandatory = $true)]$Source)

    return ('https://github.com/{0}.git' -f (Get-SourceIdentifier -Source $Source))
}

function Get-SourceInstallSpec {
    param(
        [Parameter(Mandatory = $true)]$Source,
        [Parameter(Mandatory = $true)][string]$SkillRoot
    )

    return ('https://github.com/{0}/tree/{1}/{2}' -f
        (Get-SourceIdentifier -Source $Source),
        ([string]$Source.sourceCommit).Trim('/'),
        $SkillRoot.Replace('\', '/').Trim('/'))
}

function New-RepositorySnapshot {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Source
    )

    Assert-GitEnvironment -Config $Config | Out-Null
    $repository = Get-SourceCloneUrl -Source $Source
    $commit = [string]$Source.sourceCommit
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $path = Join-Path ([System.IO.Path]::GetTempPath()) ('agenttools-skill-updater-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $path | Out-Null
        try {
            Invoke-ManagedGit -Config $Config -GitArguments @(
                'clone', '--quiet', $repository, $path
            ) | Out-Null
            Invoke-ManagedGit -Config $Config -GitArguments @('-C', $path, 'checkout', '--detach', '--quiet', $commit) | Out-Null
            $checkedOut = Invoke-ManagedGit -Config $Config -GitArguments @('-C', $path, 'rev-parse', 'HEAD')
            if ($checkedOut -ne $commit) {
                throw "Repository checkout did not reach configured sourceCommit $commit (got $checkedOut)."
            }
            return $path
        }
        catch {
            $lastError = $_
            if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
            if ($attempt -lt 3) { Start-Sleep -Seconds (3 * $attempt) }
        }
    }
    throw $lastError
}

function Get-FileManifest {
    param([Parameter(Mandatory = $true)][string]$Root)

    $manifest = @{}
    if (-not (Test-Path -LiteralPath $Root)) { return $manifest }
    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    foreach ($file in Get-ChildItem -LiteralPath $rootPath -File -Recurse | Sort-Object FullName) {
        $relative = $file.FullName.Substring($rootPath.Length).TrimStart('\').Replace('\', '/')
        $manifest[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $manifest
}

function Compare-DirectoryContent {
    param(
        [Parameter(Mandatory = $true)][string]$Upstream,
        [Parameter(Mandatory = $true)][string]$Installed
    )

    $upstreamManifest = Get-FileManifest $Upstream
    $installedManifest = Get-FileManifest $Installed
    $allPaths = @($upstreamManifest.Keys + $installedManifest.Keys | Sort-Object -Unique)
    $added = @()
    $removed = @()
    $modified = @()

    foreach ($path in $allPaths) {
        if (-not $installedManifest.ContainsKey($path)) { $added += $path; continue }
        if (-not $upstreamManifest.ContainsKey($path)) { $removed += $path; continue }
        if ($upstreamManifest[$path] -ne $installedManifest[$path]) { $modified += $path }
    }

    return [pscustomobject]@{
        Equal = (($added.Count + $removed.Count + $modified.Count) -eq 0)
        Added = $added
        Removed = $removed
        Modified = $modified
    }
}

function Get-ConfiguredSkillFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Snapshot,
        [Parameter(Mandatory = $true)]$Source
    )

    $files = @()
    foreach ($relativeRoot in @($Source.skillRoots)) {
        $root = Join-Path $Snapshot ([string]$relativeRoot)
        if (-not (Test-Path -LiteralPath $root)) {
            throw "Configured skill root is missing upstream: $relativeRoot"
        }
        $files += @(Get-ChildItem -LiteralPath $root -Filter 'SKILL.md' -File -Recurse)
    }
    return @($files | Sort-Object FullName -Unique)
}
