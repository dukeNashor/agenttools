Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-SkillUpdaterRoot {
    return (Split-Path -Parent $PSScriptRoot)
}

function Remove-UpdaterTemporaryDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    $temporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd('\', '/')
    $item = Get-Item -LiteralPath $resolved
    if ((Split-Path -Parent $resolved) -ne $temporaryRoot -or
        $item.Name -notmatch '^agenttools-(skill-updater|git-wrapper)-[0-9a-f]{32}$' -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing unsafe updater temporary directory cleanup: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Get-SkillUpdaterConfig {
    $path = Join-Path (Get-SkillUpdaterRoot) 'config.json'
    $config = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    # An explicit process override keeps one-off transport policy out of machine settings.
    if ($env:AGENTTOOLS_GIT_PROXY_MODE) { $config.gitProxyMode = $env:AGENTTOOLS_GIT_PROXY_MODE }
    $localPath = Join-Path (Get-SkillUpdaterRoot) 'config.local.json'
    if (-not (Test-Path -LiteralPath $localPath)) {
        if ([string]$config.gitProxyMode -notin @('direct', 'git-config', 'windows-user-proxy')) {
            throw "Unsupported gitProxyMode: $($config.gitProxyMode)"
        }
        return $config
    }

    $local = Get-Content -LiteralPath $localPath -Raw | ConvertFrom-Json
    $allowedTopLevel = @('gitProxyMode', 'tooling', 'localRepositories')
    foreach ($property in $local.PSObject.Properties) {
        if ($allowedTopLevel -notcontains $property.Name) {
            throw "Unsupported machine-local setting: $($property.Name)"
        }
    }

    if ($local.PSObject.Properties['gitProxyMode']) {
        $config.gitProxyMode = [string]$local.gitProxyMode
    }
    if ($env:AGENTTOOLS_GIT_PROXY_MODE) { $config.gitProxyMode = $env:AGENTTOOLS_GIT_PROXY_MODE }
    if ($local.PSObject.Properties['localRepositories']) {
        foreach ($property in $local.localRepositories.PSObject.Properties) {
            $sources = @($config.sources | Where-Object { $_.id -eq $property.Name -and (Get-SourceType -Source $_) -eq 'local' })
            if ($sources.Count -ne 1) { throw "Unknown local source mapping: $($property.Name)" }
            $sources[0] | Add-Member -MemberType NoteProperty -Name repositoryPath -Value ([string]$property.Value) -Force
        }
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
    if ([string]$config.gitProxyMode -notin @('direct', 'git-config', 'windows-user-proxy')) {
        throw "Unsupported gitProxyMode: $($config.gitProxyMode)"
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

function Assert-CanonicalAgentsRoot {
    param([Parameter(Mandatory = $true)]$Config)

    if ([string]$Config.agentsRoot -ne '~/.agents') {
        throw 'agentsRoot must remain the canonical ~/.agents user skill root.'
    }
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
    $gitDiagnostics = Assert-GitEnvironment -Config $config
    $transport = $gitDiagnostics.Transport
    $runner = $diagnostics.Runner
    $arguments = @($runner.Prefix) + $CliArguments
    Write-Host ('=> ' + $runner.DisplayName + ' ' + ($CliArguments -join ' '))

    $originalPath = $env:PATH
    $gitWrapperRoot = $null
    $proxyEnvironmentNames = @('AGENTTOOLS_GIT_HTTP_PROXY', 'AGENTTOOLS_GIT_HTTPS_PROXY')
    $proxyEnvironmentWasSet = @{}
    $proxyEnvironmentValues = @{}
    foreach ($name in $proxyEnvironmentNames) {
        $proxyEnvironmentWasSet[$name] = Test-Path -LiteralPath "Env:$name"
        if ($proxyEnvironmentWasSet[$name]) {
            $proxyEnvironmentValues[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
    }

    try {
        if ($transport.RequiresWrapper) {
            $realGit = Resolve-GitExecutable
            $gitWrapperRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('agenttools-git-wrapper-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $gitWrapperRoot | Out-Null
            $wrapperPath = Join-Path $gitWrapperRoot 'git.cmd'
            if ($transport.Mode -eq 'windows-user-proxy') {
                [Environment]::SetEnvironmentVariable('AGENTTOOLS_GIT_HTTP_PROXY', [string]$transport.HttpProxy, 'Process')
                [Environment]::SetEnvironmentVariable('AGENTTOOLS_GIT_HTTPS_PROXY', [string]$transport.HttpsProxy, 'Process')
            }
            $wrapper = Get-GitWrapperContent -Transport $transport -RealGit $realGit
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
        foreach ($name in $proxyEnvironmentNames) {
            if ($proxyEnvironmentWasSet[$name]) {
                [Environment]::SetEnvironmentVariable($name, $proxyEnvironmentValues[$name], 'Process')
            }
            else {
                [Environment]::SetEnvironmentVariable($name, $null, 'Process')
            }
        }
        if ($gitWrapperRoot -and (Test-Path -LiteralPath $gitWrapperRoot)) {
            Remove-UpdaterTemporaryDirectory -Path $gitWrapperRoot
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

function ConvertTo-GitProxyValues {
    param([Parameter(Mandatory = $true)][string]$ProxyServer)

    if ([string]::IsNullOrWhiteSpace($ProxyServer)) {
        throw 'Windows Internet Settings proxy is empty.'
    }
    if ($ProxyServer.Contains("`r") -or $ProxyServer.Contains("`n")) {
        throw 'Windows Internet Settings proxy contains an invalid line break.'
    }

    $defaultProxy = $null
    $schemeProxies = @{}
    foreach ($entry in @($ProxyServer -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $parts = $entry -split '=', 2
        if ($parts.Count -eq 2) {
            $scheme = $parts[0].Trim().ToLowerInvariant()
            $value = $parts[1].Trim()
            if ($scheme -in @('http', 'https')) {
                $schemeProxies[$scheme] = $value
            }
            continue
        }
        if (-not $defaultProxy) { $defaultProxy = $entry }
    }

    $httpProxy = if ($schemeProxies.ContainsKey('http')) { $schemeProxies['http'] } else { $defaultProxy }
    $httpsProxy = if ($schemeProxies.ContainsKey('https')) { $schemeProxies['https'] } else { $defaultProxy }
    if (-not $httpProxy) { $httpProxy = $httpsProxy }
    if (-not $httpsProxy) { $httpsProxy = $httpProxy }
    if ([string]::IsNullOrWhiteSpace($httpProxy) -or [string]::IsNullOrWhiteSpace($httpsProxy)) {
        throw 'Windows Internet Settings proxy has no usable HTTP or HTTPS proxy entry.'
    }

    foreach ($value in @($httpProxy, $httpsProxy)) {
        $uriText = if ($value -match '^[a-zA-Z][a-zA-Z0-9+.-]*://') { $value } else { 'http://' + $value }
        $uri = $null
        if (-not [uri]::TryCreate($uriText, [System.UriKind]::Absolute, [ref]$uri) -or -not $uri.Host) {
            throw 'Windows Internet Settings proxy is not a valid proxy endpoint.'
        }
    }

    return [pscustomobject]@{
        HttpProxy = [string]$httpProxy
        HttpsProxy = [string]$httpsProxy
    }
}

function Get-ManagedGitTransport {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [AllowNull()]$WindowsProxySettings
    )

    $mode = [string]$Config.gitProxyMode
    switch ($mode) {
        'direct' {
            return [pscustomobject]@{
                Mode = $mode
                Description = 'Direct Git connection (proxy explicitly disabled)'
                Arguments = @('-c', 'http.proxy=', '-c', 'https.proxy=')
                RequiresWrapper = $true
                HttpProxy = $null
                HttpsProxy = $null
            }
        }
        'git-config' {
            return [pscustomobject]@{
                Mode = $mode
                Description = 'Git configuration and process environment'
                Arguments = @()
                RequiresWrapper = $false
                HttpProxy = $null
                HttpsProxy = $null
            }
        }
        'windows-user-proxy' {
            $settings = if ($PSBoundParameters.ContainsKey('WindowsProxySettings')) {
                $WindowsProxySettings
            }
            else {
                Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
            }
            if (-not $settings -or [int]$settings.ProxyEnable -ne 1) {
                throw 'Windows Internet Settings proxy is not enabled for the current user.'
            }
            $proxies = ConvertTo-GitProxyValues -ProxyServer ([string]$settings.ProxyServer)
            return [pscustomobject]@{
                Mode = $mode
                Description = 'Windows Internet Settings proxy for the current user'
                Arguments = @(
                    '-c', "http.proxy=$($proxies.HttpProxy)",
                    '-c', "https.proxy=$($proxies.HttpsProxy)",
                    '-c', 'http.noProxy='
                )
                RequiresWrapper = $true
                HttpProxy = $proxies.HttpProxy
                HttpsProxy = $proxies.HttpsProxy
            }
        }
        default {
            throw "Unsupported gitProxyMode: $mode"
        }
    }
}

function Get-GitWrapperContent {
    param(
        [Parameter(Mandatory = $true)]$Transport,
        [Parameter(Mandatory = $true)][string]$RealGit
    )

    if ($Transport.Mode -eq 'windows-user-proxy') {
        return "@echo off`r`n`"$RealGit`" -c `"http.proxy=%AGENTTOOLS_GIT_HTTP_PROXY%`" -c `"https.proxy=%AGENTTOOLS_GIT_HTTPS_PROXY%`" -c `"http.noProxy=`" %*`r`n"
    }
    return "@echo off`r`n`"$RealGit`" -c `"http.proxy=`" -c `"https.proxy=`" %*`r`n"
}

function Get-GitSafetyDiagnostics {
    param([Parameter(Mandatory = $true)]$Config)

    $errors = New-Object System.Collections.Generic.List[string]
    $warnings = New-Object System.Collections.Generic.List[string]
    $transport = Get-ManagedGitTransport -Config $Config
    $git = Resolve-GitExecutable
    $arguments = @($transport.Arguments) + @('config', '--get-regexp', '^(url\..*\.insteadof|http(\..*)?\.(sslverify|extraheader))$')
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

    return [pscustomobject]@{ Errors = @($errors); Warnings = @($warnings); Git = $git; Transport = $transport }
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

    $transport = Get-ManagedGitTransport -Config $Config
    $arguments = @($transport.Arguments) + $GitArguments

    $output = & (Resolve-GitExecutable) @arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine)
    }
    return (($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine).Trim()
}

function Test-ManagedGitHubConnection {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Source
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            $output = Invoke-ManagedGit -Config $Config -GitArguments @(
                '-c', 'http.connectTimeout=10',
                '-c', 'http.lowSpeedLimit=1',
                '-c', 'http.lowSpeedTime=10',
                'ls-remote', '--quiet', (Get-SourceCloneUrl -Source $Source), 'HEAD'
            )
            if ([string]::IsNullOrWhiteSpace($output)) {
                throw 'GitHub connectivity probe returned no HEAD result.'
            }
            return $true
        }
        catch {
            $lastError = $_
            if ($attempt -lt 2) { Start-Sleep -Seconds 1 }
        }
    }
    throw $lastError
}

function Get-SourceType {
    param([Parameter(Mandatory = $true)]$Source)

    $type = if ($Source.PSObject.Properties['sourceType']) { [string]$Source.sourceType } else { 'github' }
    if ($type -notin @('github', 'local')) { throw "Unsupported sourceType: $type" }
    return $type
}

function Get-LocalRepositoryPath {
    param([Parameter(Mandatory = $true)]$Source)

    if (-not $Source.PSObject.Properties['repositoryPath'] -or [string]$Source.repositoryPath -notmatch '^[A-Za-z]:[\\/]') {
        throw "Local source $($Source.id) requires an absolute local drive path in config.local.json localRepositories."
    }
    $path = [System.IO.Path]::GetFullPath([string]$Source.repositoryPath).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { throw "Local repository is unavailable: $path" }
    $cursor = Get-Item -LiteralPath $path
    while ($cursor) {
        if (($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Local repository uses a symlink/Junction/reparse point; leave it untouched: $($cursor.FullName)"
        }
        $cursor = $cursor.Parent
    }
    return $path
}

function Get-SourceIdentifier {
    param([Parameter(Mandatory = $true)]$Source)

    if ((Get-SourceType -Source $Source) -eq 'local') { return (Get-LocalRepositoryPath -Source $Source).Replace('\', '/') }
    return [string]$Source.repositorySlug
}

function Get-SourceCloneUrl {
    param([Parameter(Mandatory = $true)]$Source)

    if ((Get-SourceType -Source $Source) -eq 'local') { return Get-LocalRepositoryPath -Source $Source }
    return ('https://github.com/{0}.git' -f (Get-SourceIdentifier -Source $Source))
}

function Get-SourceInstallSpec {
    param(
        [Parameter(Mandatory = $true)]$Source,
        [Parameter(Mandatory = $true)][string]$SkillRoot
    )

    if ((Get-SourceType -Source $Source) -eq 'local') {
        return Resolve-PathUnderRoot -Root (Get-LocalRepositoryPath -Source $Source) -RelativePath $SkillRoot
    }
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
    if ($commit -notmatch '^[0-9a-fA-F]{40}$') { throw 'sourceCommit must be a full Git commit SHA.' }
    $localSource = (Get-SourceType -Source $Source) -eq 'local'
    if ($localSource) {
        $top = Invoke-ManagedGit -Config $Config -GitArguments @('-C', $repository, 'rev-parse', '--show-toplevel')
        if ([System.IO.Path]::GetFullPath($top).TrimEnd('\', '/') -ne $repository) { throw 'Local repository path must name its Git worktree root.' }
        Invoke-ManagedGit -Config $Config -GitArguments @('-C', $repository, 'cat-file', '-e', ($commit + '^{commit}')) | Out-Null
    }
    $lastError = $null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $path = Join-Path ([System.IO.Path]::GetTempPath()) ('agenttools-skill-updater-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $path | Out-Null
        try {
            $cloneArguments = @('clone', '--quiet', '--no-checkout')
            if ($localSource) { $cloneArguments += '--no-hardlinks' }
            Invoke-ManagedGit -Config $Config -GitArguments ($cloneArguments + @('--', $repository, $path)) | Out-Null
            Invoke-ManagedGit -Config $Config -GitArguments @('-C', $path, 'checkout', '--detach', '--quiet', $commit) | Out-Null
            $checkedOut = Invoke-ManagedGit -Config $Config -GitArguments @('-C', $path, 'rev-parse', 'HEAD')
            if ($checkedOut -ne $commit) {
                throw "Repository checkout did not reach configured sourceCommit $commit (got $checkedOut)."
            }
            return $path
        }
        catch {
            $lastError = $_
            Remove-UpdaterTemporaryDirectory -Path $path
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
    foreach ($file in Get-ChildItem -LiteralPath $rootPath -File -Recurse -Force | Sort-Object FullName) {
        $relative = $file.FullName.Substring($rootPath.Length).TrimStart('\').Replace('\', '/')
        $manifest[$relative] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
    return $manifest
}

function Get-SkillFolderHash {
    param([Parameter(Mandatory = $true)][string]$Root)

    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Skill directory does not exist: $Root"
    }

    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $files = @(Get-ChildItem -LiteralPath $rootPath -File -Recurse -Force | Sort-Object FullName)
        foreach ($file in $files) {
            $relativePath = $file.FullName.Substring($rootPath.Length).TrimStart('\').Replace('\', '/')
            $pathBytes = [System.Text.Encoding]::UTF8.GetBytes($relativePath)
            if ($pathBytes.Length -gt 0) {
                $algorithm.TransformBlock($pathBytes, 0, $pathBytes.Length, $pathBytes, 0) | Out-Null
            }
            $contentBytes = [System.IO.File]::ReadAllBytes($file.FullName)
            if ($contentBytes.Length -gt 0) {
                $algorithm.TransformBlock($contentBytes, 0, $contentBytes.Length, $contentBytes, 0) | Out-Null
            }
        }
        $algorithm.TransformFinalBlock([byte[]]::new(0), 0, 0) | Out-Null
        return (($algorithm.Hash | ForEach-Object { $_.ToString('x2') }) -join '')
    }
    finally {
        $algorithm.Dispose()
    }
}

function Copy-LocalSourceSkills {
    param(
        [Parameter(Mandatory = $true)]$SourcePlan,
        [Parameter(Mandatory = $true)][string]$AgentsRoot,
        [switch]$Overwrite
    )

    if ((Get-SourceType -Source $SourcePlan.Source) -ne 'local') { throw 'Native copy is only for configured local sources.' }
    $skillsRoot = Resolve-PathUnderRoot -Root $AgentsRoot -RelativePath 'skills'
    foreach ($root in @($AgentsRoot, $skillsRoot)) {
        if ((Test-Path -LiteralPath $root) -and ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Selected user root is a symlink/Junction/reparse point; leave it untouched: $root"
        }
    }
    New-Item -ItemType Directory -Path $skillsRoot -Force | Out-Null
    foreach ($skillFile in $SourcePlan.SkillFiles) {
        $upstream = $skillFile.Directory.FullName
        $destination = Resolve-PathUnderRoot -Root $skillsRoot -RelativePath $skillFile.Directory.Name
        foreach ($root in @($upstream, $destination)) {
            if (-not (Test-Path -LiteralPath $root)) { continue }
            $items = @(Get-Item -LiteralPath $root) + @(Get-ChildItem -LiteralPath $root -Recurse -Force)
            if (@($items | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) {
                throw "Skill tree contains a symlink/Junction/reparse point; leave it untouched: $root"
            }
        }
        Assert-SkillMetadata -SkillFile $skillFile.FullName -ExpectedName $skillFile.Directory.Name | Out-Null
        if (Test-Path -LiteralPath $destination) {
            if ((Compare-DirectoryContent -Upstream $upstream -Installed $destination).Equal) { continue }
            if (-not $Overwrite) { throw 'Different local skill content requires -Overwrite.' }
            $resolved = (Resolve-Path -LiteralPath $destination).Path
            if ((Split-Path -Parent $resolved) -ne [IO.Path]::GetFullPath($skillsRoot)) { throw "Unsafe local skill replacement: $resolved" }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
        # Metadata was validated as strict UTF-8 without BOM. Copy all source
        # bytes intact; the same pinned-content and canonical-lock gates follow.
        Copy-Item -LiteralPath $upstream -Destination $destination -Recurse -Force
        if (-not (Compare-DirectoryContent -Upstream $upstream -Installed $destination).Equal) {
            throw "Local source copy differs from pinned content: $destination"
        }
        Write-Host "Copied verified local skill: $destination"
    }
}

function Set-CanonicalLockEntries {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Lock,
        [Parameter(Mandatory = $true)]$SourcePlan,
        [Parameter(Mandatory = $true)][string]$AgentsRoot,
        [Parameter(Mandatory = $true)][string]$LockPath,
        [AllowNull()]$PreviousLock
    )

    if (-not $Lock) {
        $Lock = [pscustomobject]@{
            version = 3
            skills = [pscustomobject]@{}
            dismissed = [pscustomobject]@{}
        }
    }
    if (-not $Lock.skills) { throw "Global skill lock has no skills object: $LockPath" }

    $now = [DateTime]::UtcNow.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
    foreach ($name in $SourcePlan.DesiredNames) {
        $installedPath = Resolve-PathUnderRoot -Root $AgentsRoot -RelativePath ('skills/' + $name)
        if (-not (Test-Path -LiteralPath $installedPath -PathType Container)) {
            throw "$($SourcePlan.Source.label) did not install the expected skill directory: $installedPath"
        }
        $installedItem = Get-Item -LiteralPath $installedPath
        if (($installedItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or [bool]$installedItem.LinkType) {
            throw "Refusing to record a symlink/Junction/reparse point as an installed skill: $installedPath"
        }

        $upstreamPath = $SourcePlan.SkillFiles |
            Where-Object { $_.Directory.Name -eq $name } |
            Select-Object -ExpandProperty Directory -First 1
        if (-not $upstreamPath) { throw "Pinned source did not contain the expected skill: $name" }
        $comparison = Compare-DirectoryContent -Upstream $upstreamPath.FullName -Installed $installedPath
        if (-not $comparison.Equal) {
            throw "$($SourcePlan.Source.label) installed content differs from pinned sourceCommit for $name."
        }

        $oldProperty = $Lock.skills.PSObject.Properties[$name]
        $oldEntry = if ($oldProperty) { $oldProperty.Value } else { $null }
        $previousProperty = if ($PreviousLock) { $PreviousLock.skills.PSObject.Properties[$name] } else { $null }
        # The CLI may replace an entry. Retain its new fields and restore any
        # previous fields before overriding the updater's canonical identity.
        $fields = [ordered]@{}
        foreach ($existing in @($oldEntry, $(if ($previousProperty) { $previousProperty.Value }))) {
            if ($existing) {
                foreach ($property in $existing.PSObject.Properties) { $fields[$property.Name] = $property.Value }
            }
        }
        $installedAt = if ($fields.Contains('installedAt')) {
            [string]$fields['installedAt']
        }
        else { $now }
        $canonical = [ordered]@{
            source = Get-SourceIdentifier -Source $SourcePlan.Source
            sourceType = Get-SourceType -Source $SourcePlan.Source
            sourceUrl = Get-SourceCloneUrl -Source $SourcePlan.Source
            ref = [string]$SourcePlan.Source.sourceCommit
            skillPath = [string]$SourcePlan.SkillPaths[$name]
            skillFolderHash = Get-SkillFolderHash -Root $installedPath
            installedAt = $installedAt
            updatedAt = $now
        }
        foreach ($key in $canonical.Keys) { $fields[$key] = $canonical[$key] }
        $entry = [pscustomobject]$fields
        $Lock.skills.PSObject.Properties.Remove($name)
        $Lock.skills | Add-Member -MemberType NoteProperty -Name $name -Value $entry
    }

    $json = $Lock | ConvertTo-Json -Depth 20
    [System.IO.File]::WriteAllText($LockPath, $json + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    return $Lock
}

function Format-SkillLockIdentity {
    param([AllowNull()]$Entry)

    if (-not $Entry) { return '<no lock entry>' }
    $parts = foreach ($name in @('source', 'sourceType', 'sourceUrl', 'ref', 'skillPath')) {
        $property = $Entry.PSObject.Properties[$name]
        $value = if ($property) { [string]$property.Value } else { '<missing>' }
        "${name}=$value"
    }
    return ($parts -join ', ')
}

function Get-SkillLockStatus {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Entry,
        [Parameter(Mandatory = $true)]$Source,
        [Parameter(Mandatory = $true)][string]$SkillPath
    )

    if (-not $Entry) { return 'Missing lock entry' }
    $expected = @{
        source = Get-SourceIdentifier -Source $Source
        sourceType = Get-SourceType -Source $Source
        sourceUrl = Get-SourceCloneUrl -Source $Source
        skillPath = $SkillPath
    }
    foreach ($key in $expected.Keys) {
        if (-not $Entry.PSObject.Properties[$key] -or [string]$Entry.$key -cne [string]$expected[$key]) { return 'Lock identity mismatch' }
    }
    if (-not $Entry.PSObject.Properties['ref'] -or [string]::IsNullOrWhiteSpace([string]$Entry.ref)) { return 'Missing lock ref' }
    if ([string]$Entry.ref -cne [string]$Source.sourceCommit) { return 'Lock revision mismatch' }
    return 'Current'
}

function Test-CanonicalLockEntry {
    param(
        [Parameter(Mandatory = $true)][AllowNull()]$Entry,
        [Parameter(Mandatory = $true)]$Source,
        [Parameter(Mandatory = $true)][string]$SkillPath
    )

    return (Get-SkillLockStatus -Entry $Entry -Source $Source -SkillPath $SkillPath) -eq 'Current'
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

function Get-StrictUtf8TextWithoutBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $bytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "$Label must use UTF-8 without BOM: $Path"
    }
    try {
        return [System.Text.UTF8Encoding]::new($false, $true).GetString($bytes)
    }
    catch {
        throw "$Label is not valid UTF-8: $Path"
    }
}

function Assert-SkillMetadata {
    param(
        [Parameter(Mandatory = $true)][string]$SkillFile,
        [Parameter(Mandatory = $true)][string]$ExpectedName
    )

    $text = Get-StrictUtf8TextWithoutBom -Path $SkillFile -Label 'SKILL.md'
    $lines = @([regex]::Split($text, '\r?\n'))
    if ($lines.Count -lt 3 -or $lines[0].Trim() -ne '---') {
        throw "SKILL.md is missing YAML frontmatter: $SkillFile"
    }
    $closingIndex = -1
    for ($index = 1; $index -lt $lines.Count; $index++) {
        if ($lines[$index].Trim() -eq '---') { $closingIndex = $index; break }
    }
    if ($closingIndex -lt 0) { throw "SKILL.md frontmatter is not closed: $SkillFile" }

    $name = $null
    $description = $null
    foreach ($line in @($lines | Select-Object -Skip 1 -First ($closingIndex - 1))) {
        if ($line -match '^\s*name\s*:\s*(?<value>.*?)\s*$') {
            $name = [string]$Matches.value
            $name = $name.Trim()
            if ($name.Length -ge 2 -and (($name[0] -eq '"' -and $name[$name.Length - 1] -eq '"') -or ($name[0] -eq "'" -and $name[$name.Length - 1] -eq "'"))) { $name = $name.Substring(1, $name.Length - 2) }
        }
        elseif ($line -match '^\s*description\s*:\s*(?<value>.*?)\s*$') {
            $description = [string]$Matches.value
            $description = $description.Trim()
            if ($description.Length -ge 2 -and (($description[0] -eq '"' -and $description[$description.Length - 1] -eq '"') -or ($description[0] -eq "'" -and $description[$description.Length - 1] -eq "'"))) { $description = $description.Substring(1, $description.Length - 2) }
        }
    }
    if ([string]::IsNullOrWhiteSpace($name)) { throw "SKILL.md frontmatter has no name: $SkillFile" }
    if ([string]::IsNullOrWhiteSpace($description)) { throw "SKILL.md frontmatter has no description: $SkillFile" }
    if ($name -ne $ExpectedName) { throw "SKILL.md name '$name' does not match directory '$ExpectedName': $SkillFile" }
    $openAiYaml = Join-Path $([System.IO.Path]::GetDirectoryName($SkillFile)) 'agents\openai.yaml'
    if (Test-Path -LiteralPath $openAiYaml -PathType Leaf) {
        Get-StrictUtf8TextWithoutBom -Path $openAiYaml -Label 'agents/openai.yaml' | Out-Null
    }
    return [pscustomobject]@{ Name = $name; Description = $description }
}

function Get-ConfiguredSkillFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Snapshot,
        [Parameter(Mandatory = $true)]$Source
    )

    $files = @()
    $selectedNames = @([string[]]$Source.selectedSkills)
    if ($selectedNames.Count -eq 0) {
        throw "Source $($Source.id) has no selectedSkills allowlist."
    }
    foreach ($relativeRoot in @($Source.skillRoots)) {
        $root = Resolve-PathUnderRoot -Root $Snapshot -RelativePath ([string]$relativeRoot)
        if (-not (Test-Path -LiteralPath $root)) {
            throw "Configured skill root is missing upstream: $relativeRoot"
        }
        $files += @(Get-ChildItem -LiteralPath $root -Filter 'SKILL.md' -File -Recurse |
            Where-Object { $selectedNames -contains $_.Directory.Name })
    }
    $result = @($files | Sort-Object FullName -Unique)
    foreach ($skillFile in $result) {
        Assert-SkillMetadata -SkillFile $skillFile.FullName -ExpectedName $skillFile.Directory.Name | Out-Null
    }
    $foundNames = @($result | ForEach-Object { $_.Directory.Name } | Sort-Object -Unique)
    if ($result.Count -ne $foundNames.Count) {
        throw "A selected skill name appears in more than one configured root for source $($Source.id)."
    }
    $missingNames = @($selectedNames | Where-Object { $foundNames -notcontains $_ })
    if ($missingNames.Count -gt 0) {
        throw "Selected skill(s) are missing from the configured upstream roots: $($missingNames -join ', ')"
    }
    return $result
}

function Get-LegacySkillsRoot {
    return (Join-Path $env:USERPROFILE '.codex\skills')
}

function Get-ReadonlySkillScopeInventory {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$AgentsRoot
    )

    $candidates = @(
        [pscustomobject]@{ Scope = 'Repository'; Path = Join-Path $ProjectRoot '.agents\skills' },
        [pscustomobject]@{ Scope = 'Legacy Codex user'; Path = (Get-LegacySkillsRoot) },
        [pscustomobject]@{ Scope = 'System'; Path = Join-Path (Get-LegacySkillsRoot) '.system' },
        [pscustomobject]@{ Scope = 'Plugin cache'; Path = Join-Path $env:USERPROFILE '.codex\plugins' }
    )
    foreach ($candidate in $candidates) {
        $resolved = [System.IO.Path]::GetFullPath($candidate.Path)
        $sameAsWritable = $resolved.TrimEnd('\', '/') -eq ([System.IO.Path]::GetFullPath($AgentsRoot).TrimEnd('\', '/'))
        if ($sameAsWritable) { continue }
        $exists = Test-Path -LiteralPath $resolved
        $skillCount = 0
        $reparseCount = 0
        if ($exists -and (Get-Item -LiteralPath $resolved).PSIsContainer) {
            $skillDirs = if ($candidate.Scope -eq 'Plugin cache') {
                @(Get-ChildItem -LiteralPath $resolved -Force -Directory -Recurse -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
            }
            else {
                @(Get-ChildItem -LiteralPath $resolved -Force -Directory -ErrorAction SilentlyContinue |
                    Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'SKILL.md') })
            }
            $skillCount = @($skillDirs).Count
            $reparseCount = @($skillDirs | Where-Object { ($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or [bool]$_.LinkType }).Count
        }
        [pscustomobject]@{
            Scope = $candidate.Scope
            Path = $resolved
            Exists = $exists
            SkillCount = $skillCount
            ReparseCount = $reparseCount
            WritableByUpdater = $false
        }
    }
}

function Get-LegacySkillInventory {
    param(
        [Parameter(Mandatory = $true)][string]$LegacyRoot,
        [Parameter(Mandatory = $true)]$SourcePlans
    )

    if (-not (Test-Path -LiteralPath $LegacyRoot)) { return @() }
    $rootItem = Get-Item -LiteralPath $LegacyRoot
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to inspect a reparse-point legacy skills root: $LegacyRoot"
    }

    $known = @{}
    foreach ($plan in $SourcePlans) {
        foreach ($skillFile in @($plan.SkillFiles)) {
            $known[$skillFile.Directory.Name] = [pscustomobject]@{
                Source = $plan.Source
                Upstream = $skillFile.Directory.FullName
            }
        }
    }

    $items = New-Object System.Collections.Generic.List[object]
    foreach ($item in @(Get-ChildItem -LiteralPath $LegacyRoot -Force -Directory | Sort-Object Name)) {
        $isReparse = (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -or [bool]$item.LinkType
        $isProtected = $isReparse -or $item.Name -in @('.system', 'codex-primary-runtime')
        $skillFile = Get-ChildItem -LiteralPath $item.FullName -Filter 'SKILL.md' -File -Recurse -ErrorAction SilentlyContinue |
            Select-Object -First 1
        $knownSkill = if ($known.ContainsKey($item.Name)) { $known[$item.Name] } else { $null }
        $state = if ($isProtected) { 'Protected' }
            elseif (-not $skillFile) { 'Unknown' }
            elseif (-not $knownSkill) { 'Unmanaged' }
            else {
                $comparison = Compare-DirectoryContent -Upstream $knownSkill.Upstream -Installed $item.FullName
                if ($comparison.Equal) { 'Legacy match' } else { 'Divergent legacy' }
            }
        $items.Add([pscustomobject]@{
            Name = $item.Name
            Path = $item.FullName
            State = $state
            Protected = $isProtected
            HasSkillFile = [bool]$skillFile
            ReparsePoint = $isReparse
            LinkType = if ($item.LinkType) { [string]$item.LinkType } elseif ($isReparse) { 'reparse point' } else { $null }
            Source = if ($knownSkill) { [string]$knownSkill.Source.id } else { $null }
            SkillPath = if ($knownSkill) { [string]$knownSkill.Upstream } else { $null }
        })
    }
    return $items.ToArray()
}
