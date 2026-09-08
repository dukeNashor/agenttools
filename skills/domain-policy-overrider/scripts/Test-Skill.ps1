[CmdletBinding()]
param([string]$ScratchDirectory = (Join-Path ([IO.Path]::GetTempPath()) ('domain-policy-overrider-tests-'+[guid]::NewGuid().ToString('N'))))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Common.ps1')
New-Item -ItemType Directory -Path $ScratchDirectory -Force | Out-Null
$script:checks = 0
function Assert($condition, $message) {
    if (-not $condition) { throw "FAILED: $message" }
    $script:checks++
}
function Assert-Throws([scriptblock]$body, $message) {
    $threw=$false
    try { & $body | Out-Null } catch { $threw=$true }
    Assert $threw $message
}
function Save-TestProfile($object) {
    $path=Join-Path $ScratchDirectory 'profile.json'
    $object | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path -Encoding UTF8
    $path
}

# Parse every entrypoint without executing installation, tasks, or registry APIs.
foreach ($file in Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.ps1') {
    $parseErrors=$null; $tokens=$null
    $null=[Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$parseErrors)
    Assert (@($parseErrors).Count -eq 0) ('PowerShell syntax: '+$file.Name)
}
$profile=Read-OverrideProfile (Join-Path $PSScriptRoot '..\profiles\logon-preferences.json')
Assert ($profile.settings.Count -eq 4) 'Preset selection contains four settings'
foreach ($bad in @(
    @{version=1;settings=@()},
    @{version=2;settings=$profile.settings},
    @{version=1;settings=@($profile.settings[0],$profile.settings[0])},
    @{version=1;settings=@(@{path='HKEY_LOCAL_MACHINE\SOFTWARE\Example';name='Value';value=1})},
    @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value=-1})},
    @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value=1.5})},
    @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value=$true})},
    @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value='1'})},
    @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value=4294967296})}
)) {
    $path=Save-TestProfile $bad
    Assert-Throws { Read-OverrideProfile $path } 'Reject invalid profile before any writes'
}
$maxProfile=Read-OverrideProfile (Save-TestProfile @{version=1;settings=@(@{path='SOFTWARE\Example';name='Value';value=4294967295})})
$signed=[BitConverter]::ToInt32([BitConverter]::GetBytes([uint32]$maxProfile.settings[0].value),0)
Assert ($signed -eq -1 -and [BitConverter]::ToUInt32([BitConverter]::GetBytes($signed),0) -eq 4294967295) 'DWORD high bits survive registry conversion'

# Replace only the registry boundary. Production task/ACL functions are never called.
$script:values=@{}
$script:writes=0
$script:failWrites=$false
function Read-OverrideValue($setting) {
    $id=Get-SettingId $setting
    if (-not $script:values.ContainsKey($id)) { throw 'Missing test value' }
    $script:values[$id]
}
function Write-OverrideValue($setting,$value) {
    if ($script:failWrites) { throw 'Simulated access denied' }
    $script:writes++
    $script:values[(Get-SettingId $setting)]=$value
}
$baseline=@(0,5,1,14)
for ($i=0;$i -lt 4;$i++) { $script:values[(Get-SettingId $profile.settings[$i])]=$baseline[$i] }
$before=@(Get-OverrideSnapshot $profile.settings)
$first=@(Invoke-OverrideProfile $profile)
Assert ($script:writes -eq 4 -and @($first | Where-Object changed).Count -eq 4) 'Domain drift is corrected for all selected values'
$second=@(Invoke-OverrideProfile $profile)
Assert ($script:writes -eq 4 -and @($second | Where-Object changed).Count -eq 0) 'Repeated execution writes nothing'
$script:values[(Get-SettingId $profile.settings[0])]=0
$script:values.Remove((Get-SettingId $profile.settings[3]))
Assert-Throws { Invoke-OverrideProfile $profile } 'Missing final value fails complete preflight'
Assert ($script:writes -eq 4) 'Preflight failure causes no partial writes'
$script:values[(Get-SettingId $profile.settings[3])]=3
$script:failWrites=$true
Assert-Throws { Invoke-OverrideProfile $profile } 'Registry access failure is surfaced'
$script:failWrites=$false
foreach ($original in $before) { Write-OverrideValue $original $original.value }
Assert ((Read-OverrideValue $profile.settings[0]) -eq 0 -and (Read-OverrideValue $profile.settings[3]) -eq 14) 'Saved values can be restored'

$legacy=[pscustomobject]@{Marker='CodexSecureLogonOverride-v1';ValueExisted=$true;OriginalValue=0;EventLogWasEnabled=$true;InstalledAt='2026-01-01'}
$v1=Convert-OverrideState $legacy
Assert (@($v1.originals).Count -eq 1 -and $v1.originals[0].value -eq 0) 'Original CAD-only installation migrates'
$legacyProfile=Get-LegacyOverrideProfile $legacy $profile
Assert ($legacyProfile.settings.Count -eq 1 -and $legacyProfile.settings[0].name -eq 'DisableCAD') 'Legacy profile reconstruction preserves the old selection'
$additional=@()
foreach ($i in 1..3) { $additional += [pscustomobject]@{Path=$profile.settings[$i].path;Name=$profile.settings[$i].name;Value=@(0,0,0,14)[$i];Existed=$true} }
$legacy | Add-Member -NotePropertyName AdditionalOriginalValues -NotePropertyValue $additional
$v2=Convert-OverrideState $legacy
Assert-Throws { Get-LegacyOverrideProfile $v2 $profile } 'Missing modern profile cannot re-enable removed selections from saved originals'
$updated=@(Merge-OverrideOriginals $v2.originals $profile.settings)
Assert ($updated.Count -eq 4) 'Migration does not duplicate originals'
Assert (@($updated | Where-Object {$_.name -eq 'PasswordExpiryWarning'})[0].value -eq 14) 'Update preserves original expiry warning'
Assert (@($updated | Where-Object {$_.name -eq 'ConsentPromptBehaviorAdmin'})[0].value -eq 0) 'Update preserves pre-existing manual UAC baseline'
Assert-Throws { Convert-OverrideState ([pscustomobject]@{marker='agenttools-domain-policy-overrider-v1';originals=@();eventLogWasEnabled=$true}) } 'Damaged backup fails closed'
Assert-Throws { Convert-OverrideState ([pscustomobject]@{Marker='Unknown'}) } 'Unrecognized installation fails closed'

$xml=[xml](New-OverrideTaskXml 'C:\Example & Space\Runtime' 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
$query=[xml]$xml.Task.Triggers.EventTrigger.Subscription
Assert ($xml.Task.Principals.Principal.UserId -eq 'S-1-5-18' -and $null -eq $xml.Task.Principals.Principal.LogonType) 'SYSTEM principal avoids unsupported task XML logon type'
Assert ($xml.Task.Settings.MultipleInstancesPolicy -eq 'Queue' -and $xml.Task.Triggers.EventTrigger.Delay -eq 'PT5S') 'Event runs queue with a five-second delay'
Assert ($query.QueryList.Query.Select.InnerText -match 'EventID=8006') 'Event subscription survives XML escaping'
Assert ($xml.Task.Actions.Exec.Arguments.Contains('"C:\Example & Space\Runtime\Apply.ps1"')) 'Action path round-trips XML escaping and quoting'

# Run the actual worker in a child PowerShell with a file-backed fake registry.
# The isolated Common.ps1 replaces both registry functions before Apply executes.
$workerDirectory=Join-Path $ScratchDirectory 'worker'
New-Item -ItemType Directory -Path $workerDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Apply.ps1') -Destination $workerDirectory
$adapter=@'
function Read-OverrideValue($setting) {
    $values=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'fake-values.json') -Raw | ConvertFrom-Json
    $match=@($values | Where-Object {(Get-SettingId $_) -eq (Get-SettingId $setting)})
    if ($match.Count -ne 1) { throw 'Missing fake value' }
    $match[0].value
}
function Write-OverrideValue($setting,$value) {
    $path=Join-Path $PSScriptRoot 'fake-values.json'
    $values=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    foreach ($item in $values) { if ((Get-SettingId $item) -eq (Get-SettingId $setting)) { $item.value=$value } }
    $values | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $path -Encoding UTF8
}
'@
((Get-Content -LiteralPath (Join-Path $PSScriptRoot 'Common.ps1') -Raw)+[Environment]::NewLine+$adapter) | Set-Content -LiteralPath (Join-Path $workerDirectory 'Common.ps1') -Encoding UTF8
$profile | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $workerDirectory 'profile.json') -Encoding UTF8
$before | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $workerDirectory 'fake-values.json') -Encoding UTF8
$shellExecutable=(Get-Process -Id $PID).Path
foreach ($run in 1..2) {
    & $shellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $workerDirectory 'Apply.ps1')
    Assert ($LASTEXITCODE -eq 0) 'Worker process reports successful execution'
}
$entries=@(Get-Content -LiteralPath (Join-Path $workerDirectory 'activity.jsonl') | ForEach-Object { $_ | ConvertFrom-Json })
Assert ($entries.Count -eq 2 -and $entries[0].runId -ne $entries[1].runId) 'Worker writes distinct run records'
Assert (@($entries[1].settings | Where-Object changed).Count -eq 0) 'Second worker process records no writes'
'{"version":1,"settings":[]}' | Set-Content -LiteralPath (Join-Path $workerDirectory 'profile.json') -Encoding UTF8
& $shellExecutable -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $workerDirectory 'Apply.ps1')
Assert ($LASTEXITCODE -eq 1) 'Invalid worker input returns failure exit code'
$failed=Get-Content -LiteralPath (Join-Path $workerDirectory 'activity.jsonl') -Tail 1 | ConvertFrom-Json
Assert (-not [string]::IsNullOrEmpty($failed.error)) 'Worker logs the failed run'
[pscustomobject]@{success=$true;checks=$script:checks;powershell=$PSVersionTable.PSVersion.ToString();scratchDirectory=$ScratchDirectory;machineStateChanged=$false} | ConvertTo-Json
