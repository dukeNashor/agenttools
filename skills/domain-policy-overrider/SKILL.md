---
name: domain-policy-overrider
description: Inspect applied Windows domain policies and maintain explicitly selected local registry overrides after Group Policy refresh. Use when preserving local preferences, updating an existing override task, verifying policy drift, or restoring original values.
---

# Domain Policy Overrider

Use the Windows scripts in `scripts/`. The current implementation manages **existing HKLM DWORD values**, using computer Group Policy completion events and Task Scheduler. Other policy types require their own implementation; LSA rights, domain-account password rules, certificates, and logon scripts are not registry DWORD overrides.

## Choose the scope

Read the applicable GPO result and current values separately. A winning GPO in RSoP identifies the recorded source; it does not prove the registry still matches or that the domain controller has no newer configuration. For diagnosis and event limitations, read [references/operations.md](references/operations.md).

Before changing machine state, establish the exact paths, value names, and desired values from the user's request. Existing authorization carries forward. An inspection request does not authorize deploying the bundled profile. `profiles/logon-preferences.json` contains this user's chosen four-setting preset, not a recommended security baseline.

## Maintain the task

Run under Windows PowerShell 5.1 or PowerShell 7 on Windows:

```powershell
& '<skill-dir>\scripts\Manage-Overrides.ps1' -Action Status
```

- `Status` is read-only and the default. It reports registry drift, installation state, and task status when accessible.
- `Install` requires elevation and an explicit `-ProfilePath`. It backs up originals, protects the runtime directory, installs the event task, and verifies execution.
- `Update` requires elevation. Without `-ProfilePath`, it preserves the installed profile; an explicit profile changes the selection. Removed selections are restored to their original values. Existing rollback baselines are preserved.
- `Verify` starts the installed task twice and checks its logs, exit codes, and values. It does not run `gpupdate` or lock the computer.
- `Uninstall` stops and removes the task, restores all backed-up originals, and retains protected files/logs for review.

Read [references/operations.md](references/operations.md) before installation, migration, or troubleshooting. Invoke `Manage-Overrides.ps1` directly from an elevated session, or request UAC elevation with `Start-Process -Verb RunAs -WindowStyle Hidden`. Use `-ResultPath` to capture results when launching hidden. If elevation or tool approval is denied, report the denial; the existing SYSTEM task is not an alternative route to perform management operations.

## Runtime invariants

- SYSTEM executes a protected copy under `%ProgramData%\CodexSecureLogonOverride`, never scripts/configuration from the checkout. The existing runtime/task names are retained for compatibility with the original installation.
- The registry profile is the selection boundary. Preflight all selected values before writing, modify only differences, verify each write, and record failures.
- Events `8000`, `8002`, `8004`, and `8006` in `Microsoft-Windows-GroupPolicy/Operational` trigger execution after five seconds. Events queue serially; there is no custom persistent listener or polling loop.
- Preserve original values across updates, including migration of the original `CodexSecureLogonOverride-v1` state. Keep users' pre-existing manual settings as their rollback baseline.
- Treat successful manual task runs and a verified event subscription as distinct evidence. A real domain-refresh test remains untested unless actually performed.

## Development and handoff

Run `scripts/Test-Skill.ps1` for isolated behavior tests with an in-memory registry adapter. These tests do not elevate, register tasks, or write HKLM. Validate the skill frontmatter with the skill-creator validator when available.

After deployment, report selected values, verified runs, unchanged scope, backup location, and any untested event/UI behavior. Source maintenance alone does not redeploy the running task or install this skill into a global skill directory.
