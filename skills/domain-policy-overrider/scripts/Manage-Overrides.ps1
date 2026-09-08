[CmdletBinding(SupportsShouldProcess=$true)]
param(
    [ValidateSet('Status','Install','Update','Verify','Uninstall')][string]$Action='Status',
    [string]$ProfilePath,
    [string]$ResultPath
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
$directory = Get-RuntimeDirectory
$taskName = Get-OverrideTaskName
$report = [ordered]@{action=$Action;started=[DateTimeOffset]::Now.ToString('o');success=$false}
$mutated = $false
$fresh = $false
$logConfig = $null
$managedFiles = @('Common.ps1','Apply.ps1','Uninstall.ps1','profile.json','state.json','task.xml')
try {
    $statePath = Join-Path $directory 'state.json'
    $uninstalled = Test-Path -LiteralPath (Join-Path $directory 'uninstalled.json')
    $installed = (Test-Path -LiteralPath $statePath) -and -not $uninstalled
    $report.installed = $installed
    $report.uninstalledFilesRetained = $uninstalled
    $rawState = if ($installed) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } else { $null }
    $state = if ($installed) { Convert-OverrideState $rawState } else { $null }
    $installedProfilePath = Join-Path $directory 'profile.json'
    $oldProfile = $null
    if ($installed) {
        if (Test-Path -LiteralPath $installedProfilePath) { $oldProfile = Read-OverrideProfile $installedProfilePath }
        else {
            # Original single-setting/four-setting installation predates profiles.
            $preset = Read-OverrideProfile (Join-Path $PSScriptRoot '..\profiles\logon-preferences.json')
            $oldProfile = Get-LegacyOverrideProfile $rawState $preset
        }
    }
    $profile = if ($ProfilePath) { Read-OverrideProfile $ProfilePath } else { $oldProfile }
    if ($Action -eq 'Status') {
        $report.directory = $directory
        $report.profile = $profile
        if ($profile) {
            $report.values = @($profile.settings | ForEach-Object {
                try { [pscustomobject]@{path=$_.path;name=$_.name;current=(Read-OverrideValue $_);desired=$_.value} }
                catch { [pscustomobject]@{error=$_.Exception.Message} }
            })
        }
        try {
            $task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop
            $info = Get-ScheduledTaskInfo -InputObject $task
            $report.task = [ordered]@{state=[string]$task.State;principal=$task.Principal.UserId;lastResult=$info.LastTaskResult;lastRun=$info.LastRunTime}
        } catch { $report.taskQueryError=$_.Exception.Message }
        $report.success=$true
    } else {
        if ($Action -eq 'Install' -and (-not $ProfilePath -or $installed -or (Test-Path -LiteralPath $directory))) { throw 'Install requires an explicit profile and an unused runtime directory. Use Update for an existing installation.' }
        if ($Action -ne 'Install' -and -not $installed) { throw 'No recognized installation exists.' }
        if ($Action -eq 'Verify' -and $ProfilePath) { throw 'Verify uses the installed selection; change it through Update first.' }
        if ($Action -eq 'Uninstall' -and $ProfilePath) { throw 'Uninstall restores installation backups, not an input profile.' }
        $report.profile=$profile
        if ($PSCmdlet.ShouldProcess("$taskName in $directory", $Action)) {
            Assert-OverrideAdmin
            if ($installed) {
                Assert-OverrideProtected $directory
                # Validate every existing runtime file before trusting/executing it.
                foreach ($file in $managedFiles) {
                    $path=Join-Path $directory $file
                    if (Test-Path -LiteralPath $path) { Assert-OverrideProtected $path }
                }
            }
            if ($Action -eq 'Uninstall') {
                $report.result = & (Join-Path $directory 'Uninstall.ps1')
            } elseif ($Action -eq 'Verify') {
                if (-not (Test-Path -LiteralPath $installedProfilePath)) { throw 'Legacy worker has no run IDs. Migrate with Update before using this verifier.' }
                $report.runs = @(Test-OverrideTask $profile $directory)
            } else {
                $existingTask = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
                if ($Action -eq 'Install' -and $existingTask) { throw 'Task name is already in use.' }
                if ($Action -eq 'Update' -and -not $existingTask) { throw 'Installed task is missing; inspect state before repair.' }
                if ($existingTask -and (@($existingTask.Actions).Count -ne 1 -or $existingTask.Actions.Arguments -notlike "*$directory\Apply.ps1*")) { throw 'Existing task action is not recognized.' }
                $selection = @($profile.settings)
                if ($oldProfile) { $selection += @($oldProfile.settings) }
                $snapshot = @(Get-OverrideSnapshot $selection)
                $logConfig = [System.Diagnostics.Eventing.Reader.EventLogConfiguration]::new('Microsoft-Windows-GroupPolicy/Operational')
                $logEnabledBefore = $logConfig.IsEnabled
                $originalXml = if ($existingTask) { Export-ScheduledTask -TaskName $taskName -TaskPath '\' } else { $null }
                $taskWasDisabled = $existingTask -and $existingTask.State -eq 'Disabled'
                if (-not $installed) {
                    New-Item -ItemType Directory -Path $directory | Out-Null
                    Set-OverrideDirectoryAcl $directory
                    $fresh=$true
                    $state=[pscustomobject]@{marker='agenttools-domain-policy-overrider-v1';originals=@();eventLogWasEnabled=$logEnabledBefore;installedAt=[DateTimeOffset]::Now.ToString('o')}
                }
                $backupDir=Join-Path $directory ('backup-'+[DateTime]::Now.ToString('yyyyMMdd-HHmmss')+'-'+[guid]::NewGuid().ToString('N').Substring(0,8))
                New-Item -ItemType Directory -Path $backupDir | Out-Null
                $present=@()
                foreach ($file in $managedFiles) {
                    $path=Join-Path $directory $file
                    if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination (Join-Path $backupDir $file); $present+=$file }
                }
                $snapshot | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $backupDir 'values.json') -Encoding UTF8
                if ($originalXml) { $originalXml | Set-Content -LiteralPath (Join-Path $backupDir 'registered-task.xml') -Encoding Unicode }
                $report.backupDirectory=$backupDir
                $mutated=$true
                if ($existingTask) { Stop-OverrideTask }
                $state.originals=@(Merge-OverrideOriginals $state.originals $snapshot)
                foreach ($file in @('Common.ps1','Apply.ps1','Uninstall.ps1')) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $directory $file) -Force }
                $profile | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $installedProfilePath -Encoding UTF8
                $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
                foreach ($file in @('Common.ps1','Apply.ps1','Uninstall.ps1','profile.json','state.json')) { Assert-OverrideProtected (Join-Path $directory $file) }
                # Dropping a selection stops enforcing it and restores its baseline.
                $newIds=@($profile.settings | ForEach-Object { Get-SettingId $_ })
                foreach ($removed in @($oldProfile.settings | Where-Object {$null -ne $_ -and (Get-SettingId $_) -notin $newIds})) {
                    $original=@($state.originals | Where-Object {(Get-SettingId $_) -eq (Get-SettingId $removed)})[0]
                    Write-OverrideValue $original $original.value
                    if ((Read-OverrideValue $original) -ne $original.value) { throw 'Removed selection was not restored.' }
                }
                if (-not $logConfig.IsEnabled) { $logConfig.IsEnabled=$true; $logConfig.SaveChanges() }
                $xml = if ($originalXml) { $originalXml } else { New-OverrideTaskXml $directory "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }
                if ($originalXml) {
                    $document = [xml]$xml
                    $document.Task.RegistrationInfo.Description = 'Maintain explicitly selected local HKLM DWORD overrides after computer Group Policy completes. Managed by agenttools domain-policy-overrider.'
                    $xml = $document.OuterXml
                }
                Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $xml -Force | Out-Null
                if ($taskWasDisabled) { $report.verificationSkipped='Existing task remains disabled.' }
                else { $report.runs=@(Test-OverrideTask $profile $directory) }
                Export-ScheduledTask -TaskName $taskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $directory 'task.xml') -Encoding Unicode
            }
            $report.success=$true
        } else { $report.success=$true; $report.previewOnly=$true }
    }
} catch {
    $report.error=$_.Exception.Message
    if ($mutated) {
        try {
            $task=Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
            if ($task) { Stop-OverrideTask }
            foreach ($value in $snapshot) { Write-OverrideValue $value $value.value }
            foreach ($file in $managedFiles) {
                $target=Join-Path $directory $file
                if ($file -in $present) { Copy-Item -LiteralPath (Join-Path $backupDir $file) -Destination $target -Force }
                elseif (Test-Path -LiteralPath $target) {
                    # Fixed leaf filenames, verified inside the protected runtime.
                    if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($target)) -ne [IO.Path]::GetFullPath($directory)) { throw 'Rollback path escaped runtime directory.' }
                    Remove-Item -LiteralPath $target -Force
                }
            }
            if ($originalXml) { Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Xml $originalXml -Force | Out-Null }
            elseif ($task) { Unregister-ScheduledTask -TaskName $taskName -TaskPath '\' -Confirm:$false }
            if ($logConfig.IsEnabled -ne $logEnabledBefore) { $logConfig.IsEnabled=$logEnabledBefore; $logConfig.SaveChanges() }
            $report.rolledBack=$true
            if ($fresh) { $report.cleanupNote='Protected runtime directory and diagnostic backups retained; inspect before retrying Install.' }
        } catch { $report.rollbackError=$_.Exception.Message }
    }
} finally {
    if ($logConfig) { $logConfig.Dispose() }
    $report.finished=[DateTimeOffset]::Now.ToString('o')
    $json=$report | ConvertTo-Json -Depth 9
    if ($ResultPath) { $json | Set-Content -LiteralPath $ResultPath -Encoding UTF8 }
    $json
}
if (-not $report.success) { exit 1 }
