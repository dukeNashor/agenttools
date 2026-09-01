[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\Common.ps1')

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]$Expected,
        [Parameter(Mandatory = $true)]$Actual,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if ($Expected -ne $Actual) {
        throw "$Message Expected '$Expected', got '$Actual'."
    }
}

function Assert-Contains {
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Needle,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Text.Contains($Needle)) {
        throw "$Message Missing '$Needle'."
    }
}

$uniform = ConvertTo-GitProxyValues -ProxyServer 'proxy.example:8080'
Assert-Equal -Expected 'proxy.example:8080' -Actual $uniform.HttpProxy -Message 'Uniform proxy HTTP value.'
Assert-Equal -Expected 'proxy.example:8080' -Actual $uniform.HttpsProxy -Message 'Uniform proxy HTTPS value.'

$perScheme = ConvertTo-GitProxyValues -ProxyServer 'http=http-proxy.example:8080;https=https-proxy.example:8443'
Assert-Equal -Expected 'http-proxy.example:8080' -Actual $perScheme.HttpProxy -Message 'Per-scheme HTTP value.'
Assert-Equal -Expected 'https-proxy.example:8443' -Actual $perScheme.HttpsProxy -Message 'Per-scheme HTTPS value.'

$direct = Get-ManagedGitTransport -Config ([pscustomobject]@{ gitProxyMode = 'direct' })
Assert-Equal -Expected 'direct' -Actual $direct.Mode -Message 'Direct mode.'
Assert-Equal -Expected 'http.proxy=' -Actual $direct.Arguments[1] -Message 'Direct mode clears HTTP proxy.'
Assert-Equal -Expected 'https.proxy=' -Actual $direct.Arguments[3] -Message 'Direct mode clears HTTPS proxy.'

$gitConfig = Get-ManagedGitTransport -Config ([pscustomobject]@{ gitProxyMode = 'git-config' })
Assert-Equal -Expected 0 -Actual @($gitConfig.Arguments).Count -Message 'Git-config mode does not override Git configuration.'

$windowsSettings = [pscustomobject]@{ ProxyEnable = 1; ProxyServer = 'http=proxy.example:8080;https=secure-proxy.example:8443' }
$windows = Get-ManagedGitTransport -Config ([pscustomobject]@{ gitProxyMode = 'windows-user-proxy' }) -WindowsProxySettings $windowsSettings
Assert-Equal -Expected 'windows-user-proxy' -Actual $windows.Mode -Message 'Windows proxy mode.'
Assert-Equal -Expected 6 -Actual @($windows.Arguments).Count -Message 'Windows proxy argument count.'
Assert-Contains -Text ([string]$windows.Arguments[1]) -Needle 'http.proxy=' -Message 'Windows mode sets HTTP proxy.'
Assert-Contains -Text ([string]$windows.Arguments[3]) -Needle 'https.proxy=' -Message 'Windows mode sets HTTPS proxy.'
Assert-Equal -Expected 'http.noProxy=' -Actual $windows.Arguments[5] -Message 'Windows mode clears Git noProxy.'

$wrapper = Get-GitWrapperContent -Transport $windows -RealGit 'C:\Git\cmd\git.exe'
Assert-Contains -Text $wrapper -Needle '%AGENTTOOLS_GIT_HTTP_PROXY%' -Message 'Git wrapper carries HTTP proxy.'
Assert-Contains -Text $wrapper -Needle '%AGENTTOOLS_GIT_HTTPS_PROXY%' -Message 'Git wrapper carries HTTPS proxy.'

Write-Output 'Git proxy regression tests passed.'
