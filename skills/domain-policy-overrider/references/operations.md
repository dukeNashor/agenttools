# Operations and verification

## Inspect before selecting overrides

Read current values with `Manage-Overrides.ps1 -Action Status`. For attribution, export `gpresult /scope computer /x <scratch-file> /f` from an elevated process; user scope can usually be exported without elevation. Inspect the winning GPO per setting and compare with the live registry. `secedit /export` and `/mergedpolicy` can distinguish a local baseline from domain settings. Keep full reports outside the repository; they can include account names, scripts, certificates, and internal paths.

The bundled preset maintains `DisableCAD=1`, `ConsentPromptBehaviorAdmin=0`, `PromptOnSecureDesktop=0`, and `PasswordExpiryWarning=3`. The last setting is in **Windows NT\CurrentVersion\Winlogon**, while the first three are in **Windows\CurrentVersion\Policies\System**. A three-day expiry warning does not change the domain password lifetime.

For an approved new selection, write a profile matching `profiles/logon-preferences.json`: version 1 and a non-empty settings array of HKLM-relative `path`, named `name`, and unsigned DWORD `value`. Validate the value's meaning and application semantics; some settings require restart or are obsolete for the installed Windows version. Profile updates are explicit; source-only updates preserve the installed selection.

## Install/update

The management script requires administrator elevation for mutation and verification. Example commands from an elevated shell:

```powershell
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Install -ProfilePath '<approved-profile.json>' -ResultPath '<scratch-result.json>'
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Update -WhatIf
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Update -ResultPath '<scratch-result.json>'
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Update -ProfilePath '<approved-profile.json>'
```

To request UAC from an ordinary shell, use `Start-Process` with `-Verb RunAs`, `-WindowStyle Hidden`, an absolute script path and an absolute result path. Read the result file and require `success=true`. Avoid an elevated job that waits indefinitely for interactive input. Tool denial and UAC cancellation are stopping conditions, not permission to route management through the installed SYSTEM task.

Runtime identity is intentionally compatible with the original deployment: `%ProgramData%\CodexSecureLogonOverride` and `\Local-SecureLogonOverride`. `Update` recognizes the original `CodexSecureLogonOverride-v1` state, including its later `AdditionalOriginalValues`, and preserves those original values. No personal computer/domain names or snapshots belong in this skill.

Only the legacy fixed-profile installation can reconstruct its selection from original values. A missing modern `profile.json` is an error: restore the intended profile from a reviewed backup before updating, because saved originals may include removed selections.

SYSTEM/Administrators own and can modify the runtime; ordinary Users have read/execute only. The worker, common code, profile, state, and uninstaller all inherit this protection. The task uses a local PowerShell process with `RemoteSigned`; it does not alter global execution policy. Domain-enforced execution policy still takes precedence.

Installation and update back up the exact prior files, task XML, and affected values. Update stops the task before replacing runtime files. A failure after mutation attempts rollback and reports rollback errors. Initial-install failure leaves protected diagnostic backups for inspection; no recursive directory cleanup is automatic.

## Verify and troubleshoot

`Verify` runs the actual task twice, requiring fresh run IDs, a ready task state, exit code zero, and matching values. The second run should avoid writes when nothing drifted. `Update` retains a disabled task's disabled state and explicitly skips execution verification in that case. Logs rotate at 256 KiB and retain one previous file.

The legacy worker predates run IDs. Read its status directly, then migrate with `Update` before using the new `Verify` action.

The event subscription covers computer-policy completion events 8000 (boot), 8002 (network change), 8004 (manual), and 8006 (periodic), with a scheduler-managed five-second delay and serial queuing. There is no custom resident listener. Only actual matching events trigger the task; writes by other management agents might not emit these events. Event delay is not debounce.

Inspect `DisableBkGndGroupPolicy` when periodic events do not occur. A value of 1 configures background refresh off; the setting requires restart to take effect. Do not alter this or other Group Policy processing settings as a side effect of deploying a selected-value override. Avoid a full `gpupdate /force` merely to test the task: it may apply unrelated pending policies. Report whether real refresh/event/UI behavior was actually tested.

## Restore

```powershell
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Uninstall
```

Uninstall uses the protected installed uninstaller. It stops the task, restores original DWORD values, removes the task, and restores the event-log enabled flag if installation changed it. Files and backups remain for audit. Disabling a task alone does not restore values. Removing a setting through a profile update restores its first-install baseline and keeps that baseline for full uninstall. Later unrelated manual changes to a managed value do not replace the saved baseline.

After successful uninstall, `Status` reports retained files rather than an active installation. A fresh install requires an unused runtime directory; inspect and archive retained files before reusing that directory.

## Primary references

- [Group Policy scope and precedence](https://learn.microsoft.com/en-us/windows-server/identity/ad-ds/manage/group-policy/group-policy-scope)
- [UAC settings and value meanings](https://learn.microsoft.com/en-us/windows/security/application-security/application-control/user-account-control/settings-and-configuration)
- [Task event delay](https://learn.microsoft.com/en-us/windows/win32/taskschd/eventtrigger-delay)
- [Task security contexts](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)
- [Disable background policy refresh](https://learn.microsoft.com/en-us/windows/client-management/mdm/policy-csp-admx-grouppolicy#disablebackgroundpolicy)
