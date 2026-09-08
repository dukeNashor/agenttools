# Definitions only: safe to dot-source without changing machine state.
function Get-RuntimeDirectory { Join-Path $env:ProgramData 'CodexSecureLogonOverride' }
function Get-OverrideTaskName { 'Local-SecureLogonOverride' }
function Get-SettingId($setting) { ($setting.path + '\' + $setting.name).ToLowerInvariant() }

function Read-OverrideProfile($path) {
    $profile = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    if ($profile.version -ne 1) { throw 'Expected profile version 1.' }
    Assert-OverrideSettings $profile.settings
    return $profile
}

function Assert-OverrideSettings($settings) {
    if ($null -eq $settings -or @($settings).Count -eq 0) { throw 'Expected at least one setting.' }
    $seen = @{}
    foreach ($setting in $settings) {
        if ($setting.path -isnot [string] -or $setting.path -notmatch '^(SOFTWARE|SYSTEM)\\[^\r\n]+$' -or $setting.path.EndsWith('\') -or $setting.path.Contains('..')) { throw 'Expected an HKLM-relative SOFTWARE or SYSTEM path.' }
        if ($setting.name -isnot [string] -or [string]::IsNullOrWhiteSpace($setting.name) -or $setting.name -match '[\\\r\n]') { throw 'Expected a named registry value.' }
        if ($setting.value -is [bool] -or $setting.value -is [string] -or $null -eq $setting.value -or [double]$setting.value -lt 0 -or [double]$setting.value -gt [uint32]::MaxValue -or [double]$setting.value -ne [math]::Truncate([double]$setting.value)) { throw 'Profile values must be unsigned DWORD integers.' }
        $id = Get-SettingId $setting
        if ($seen.ContainsKey($id)) { throw "Duplicate setting: $id" }
        $seen[$id] = $true
    }
}

function Read-OverrideValue($setting) {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64')
    $key = $null
    try {
        $key = $base.OpenSubKey($setting.path)
        if ($null -eq $key -or $key.GetValueNames() -notcontains $setting.name) { throw "Selected value does not exist: $(Get-SettingId $setting)" }
        if ($key.GetValueKind($setting.name) -ne [Microsoft.Win32.RegistryValueKind]::DWord) { throw "Selected value is not DWORD: $(Get-SettingId $setting)" }
        # Registry APIs expose DWORD as signed Int32; preserve all 32 bits.
        return [BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$key.GetValue($setting.name)), 0)
    } finally { if ($key) { $key.Dispose() }; $base.Dispose() }
}

function Write-OverrideValue($setting, $value) {
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey('LocalMachine', 'Registry64')
    $key = $null
    try {
        $key = $base.OpenSubKey($setting.path, $true)
        if ($null -eq $key) { throw "Selected key unavailable: $($setting.path)" }
        $signed = [BitConverter]::ToInt32([BitConverter]::GetBytes([uint32]$value), 0)
        $key.SetValue($setting.name, $signed, [Microsoft.Win32.RegistryValueKind]::DWord)
    } finally { if ($key) { $key.Dispose() }; $base.Dispose() }
}

function Get-OverrideSnapshot($settings) {
    foreach ($setting in $settings) {
        [pscustomobject]@{ path=$setting.path; name=$setting.name; value=(Read-OverrideValue $setting) }
    }
}

function Invoke-OverrideProfile($profile) {
    # Preflight the complete selection before any writes. Registry access functions
    # are separately replaceable by the isolated tests, not by task configuration.
    $null = @(Get-OverrideSnapshot $profile.settings)
    foreach ($setting in $profile.settings) {
        $before = Read-OverrideValue $setting
        if ($before -ne $setting.value) { Write-OverrideValue $setting $setting.value }
        $after = Read-OverrideValue $setting
        if ($after -ne $setting.value) { throw "Write verification failed: $(Get-SettingId $setting)" }
        [pscustomobject]@{ name=$setting.name; path=$setting.path; before=$before; after=$after; desired=$setting.value; changed=($before -ne $after) }
    }
}

function Convert-OverrideState($state) {
    if ($state.marker -eq 'agenttools-domain-policy-overrider-v1') {
        Assert-OverrideSettings $state.originals
        if ($state.eventLogWasEnabled -isnot [bool]) { throw 'Missing original event-log state.' }
        return $state
    }
    if ($state.Marker -ne 'CodexSecureLogonOverride-v1' -or -not $state.ValueExisted) { throw 'Unrecognized installation state.' }
    $originals = @([pscustomobject]@{path='SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System';name='DisableCAD';value=$state.OriginalValue})
    foreach ($item in @($state.AdditionalOriginalValues)) {
        if ($null -eq $item) { continue }
        if (-not $item.Existed) { throw 'Legacy migration requires existing DWORD originals.' }
        $originals += [pscustomobject]@{path=$item.Path;name=$item.Name;value=$item.Value}
    }
    Assert-OverrideSettings $originals
    if ($state.EventLogWasEnabled -isnot [bool]) { throw 'Missing original event-log state.' }
    [pscustomobject]@{marker='agenttools-domain-policy-overrider-v1';originals=$originals;eventLogWasEnabled=$state.EventLogWasEnabled;installedAt=$state.InstalledAt}
}

function Merge-OverrideOriginals($existing, $snapshot) {
    $map = @{}
    foreach ($item in @($existing)) { if ($null -ne $item) { $map[(Get-SettingId $item)] = $item } }
    foreach ($item in $snapshot) { if (-not $map.ContainsKey((Get-SettingId $item))) { $map[(Get-SettingId $item)] = $item } }
    @($map.Keys | Sort-Object | ForEach-Object { $map[$_] })
}

function Get-LegacyOverrideProfile($rawState, $preset) {
    # Modern backups retain removed selections, so they cannot reconstruct a
    # missing active profile. Only the original fixed-profile worker permits it.
    if ($rawState.Marker -ne 'CodexSecureLogonOverride-v1') { throw 'Installed profile is missing; inspect and recover it before updating.' }
    $state=Convert-OverrideState $rawState
    $ids=@($state.originals | ForEach-Object { Get-SettingId $_ })
    $selected=@($preset.settings | Where-Object {(Get-SettingId $_) -in $ids})
    if ($selected.Count -ne $ids.Count) { throw 'Legacy selection is not recognized; inspect before migrating.' }
    [pscustomobject]@{version=1;settings=$selected}
}

function Assert-OverrideAdmin {
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this operation in an elevated Windows session.' }
}

function Set-OverrideDirectoryAcl($path) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))
    Set-Acl -LiteralPath $path -AclObject $acl
}

function Assert-OverrideProtected($path) {
    if ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point: $path" }
    $acl = Get-Acl -LiteralPath $path
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-18','S-1-5-32-544')) { throw "Unexpected owner: $path" }
    $mask = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin @('S-1-5-18','S-1-5-32-544') -and ($rule.FileSystemRights -band $mask)) { throw "Untrusted write permission: $path" }
    }
}

function New-OverrideTaskXml($directory, $executable) {
    $command = [Security.SecurityElement]::Escape($executable)
    $arguments = [Security.SecurityElement]::Escape('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+(Join-Path $directory 'Apply.ps1')+'"')
    $query = [Security.SecurityElement]::Escape('<QueryList><Query Id="0" Path="Microsoft-Windows-GroupPolicy/Operational"><Select Path="Microsoft-Windows-GroupPolicy/Operational">*[System[Provider[@Name="Microsoft-Windows-GroupPolicy"] and (EventID=8000 or EventID=8002 or EventID=8004 or EventID=8006)]]</Select></Query></QueryList>')
    @"
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Maintain explicitly selected local HKLM DWORD overrides after computer Group Policy completes. Managed by agenttools domain-policy-overrider.</Description></RegistrationInfo>
<Triggers><EventTrigger><Enabled>true</Enabled><Subscription>$query</Subscription><Delay>PT5S</Delay></EventTrigger></Triggers>
<Principals><Principal id="System"><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>Queue</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings><Enabled>true</Enabled><Hidden>false</Hidden><WakeToRun>false</WakeToRun><ExecutionTimeLimit>PT1M</ExecutionTimeLimit></Settings>
<Actions Context="System"><Exec><Command>$command</Command><Arguments>$arguments</Arguments></Exec></Actions>
</Task>
"@
}

function Stop-OverrideTask {
    $name = Get-OverrideTaskName
    Disable-ScheduledTask -TaskName $name -TaskPath '\' | Out-Null
    Stop-ScheduledTask -TaskName $name -TaskPath '\'
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-ScheduledTask -TaskName $name -TaskPath '\').State -eq 'Running') {
        if ((Get-Date) -gt $deadline) { throw 'Task did not stop; aborting management operation.' }
        Start-Sleep -Milliseconds 200
    }
}

function Test-OverrideTask($profile, $directory) {
    foreach ($run in 1..2) {
        $requested = [DateTimeOffset]::Now
        Start-ScheduledTask -TaskName (Get-OverrideTaskName) -TaskPath '\'
        $deadline = (Get-Date).AddSeconds(30)
        do {
            Start-Sleep -Milliseconds 500
            $task = Get-ScheduledTask -TaskName (Get-OverrideTaskName) -TaskPath '\'
            $info = Get-ScheduledTaskInfo -InputObject $task
            $logFile = Join-Path $directory 'activity.jsonl'
            $last = if (Test-Path -LiteralPath $logFile) { Get-Content -LiteralPath $logFile -Tail 1 | ConvertFrom-Json } else { $null }
            $fresh = $last -and $last.runId -and ([DateTimeOffset]$last.time -ge $requested)
        } while (($task.State -eq 'Running' -or -not $fresh) -and (Get-Date) -lt $deadline)
        if (-not $fresh -or $task.State -ne 'Ready' -or $info.LastTaskResult -ne 0 -or $last.error) { throw "Task verification run $run failed or timed out." }
        $values = @(Get-OverrideSnapshot $profile.settings)
        foreach ($setting in $profile.settings) {
            if ((Read-OverrideValue $setting) -ne $setting.value) { throw 'Task finished but registry does not match profile.' }
        }
        [pscustomobject]@{run=$run;exitCode=$info.LastTaskResult;values=$values;log=$last}
    }
}
