# Skill updater

A small, Windows-only project that compares and applies two Git-pinned skill sources:

- skills under `mattpocock/skills/skills/engineering` and `skills/productivity` only;
- all 13 skills from Cognitive Bias Lab's `critical-thinking-tools`, plus its shared `references/evidence-map.md` dependency.

The scheduled job is read-only. Every Monday at 09:00 it compares installed files with the pinned upstream commits, checks whether the pinned `skills` CLI has a newer registry release, writes a self-contained HTML report under `reports/`, and opens `latest.html` in the default browser. If the PC is unavailable, Task Scheduler starts it when the signed-in user is next available.

`config.json` is the committed expected state. Each source names an exact 40-character `sourceCommit` and its `skillRoots`; changing either is a normal Git change and therefore changes the `agenttools` release identity. The only local state protocol is the global `~/.agents/.skill-lock.json` written by the `skills` CLI. This project compares the two; it does not create another lock file.

## Deploy

Open Windows PowerShell in this directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1
```

Deployment applies the committed source commits and creates no backup. It preserves same-source skills outside the configured subset unless `-Prune` is explicitly requested. Existing different skill content or lock metadata requires the separate `-Overwrite` choice. It then installs or replaces the current-user scheduled task named `AgentTools Skill Update Report`.

The committed runner policy is `codex-bundled-pnpm`. A machine-local `config.local.json` may explicitly select `user-npx`; there is no silent fallback between them. Both execute the exact CLI version in `config.json`. Before installation, the scripts verify Node.js, the effective package registry, SSL settings, Git URL rewriting, shared paths, and skill-name ownership. They inspect machine configuration without rewriting npm, pnpm, or Git settings.

## Machine-local settings

Keep portable policy in `config.json`. For a PC that needs a trusted enterprise registry, must use its Git proxy, or must explicitly use user-provided `npx`, copy `config.local.example.json` to `config.local.json` and edit only the required values. The local file is ignored by Git and may override only `gitBypassProxy`, `tooling.runner`, and `tooling.allowedRegistries`; proxy credentials and certificate paths remain in the machine's package-manager configuration.

Run `scripts/Test-Project.ps1` after creating or editing the local file. An unknown registry, non-HTTPS registry, disabled SSL verification, unsafe Git rewrite, unsupported local key, or insufficient Node.js version blocks installation.

## Commands

Generate and open a report without changing skills:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\New-SkillUpdateReport.ps1 -Open
```

Apply reviewed updates:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -Apply
```

Apply reviewed updates while allowing replacement of different local content or lock metadata:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -Apply -Overwrite
```

Apply and explicitly remove same-source entries outside the configured layout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -Apply -Prune
```

Run the complete network, source, collision, and tooling preflight without changing skills:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -PreflightOnly
```

Retry one source independently after a transient network failure:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -SourceId critical-thinking-tools
```

Repair only the scheduled task after moving the checkout:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-ScheduledTask.ps1
```

Remove the task:

```powershell
Unregister-ScheduledTask -TaskName 'AgentTools Skill Update Report' -Confirm:$false
```

Generated reports are ignored by Git. The newest 12 timestamped reports are retained alongside `latest.html`.
