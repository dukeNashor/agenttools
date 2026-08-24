# Skill update reporter

A small, Windows-only project that manages two trusted skill sources:

- skills under `mattpocock/skills/skills/engineering` and `skills/productivity` only;
- all 13 skills from Cognitive Bias Lab's `critical-thinking-tools`, plus its shared `references/evidence-map.md` dependency.

The scheduled job is read-only. Every Monday at 09:00 it compares installed files with the current upstream repositories, checks whether the pinned `skills` CLI has a newer registry release, writes a self-contained HTML report under `reports/`, and opens `latest.html` in the default browser. If the PC is unavailable, Task Scheduler starts it when the signed-in user is next available.

## Deploy

Open Windows PowerShell in this directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1
```

Deployment directly overwrites the configured subset with current upstream files, removes same-source skills outside that subset, and creates no backup. It then installs or replaces the current-user scheduled task named `AgentTools Skill Update Report`.

The scripts prefer `npx`; when it is unavailable, they use `pnpm dlx` from Codex's bundled runtime. Both execute the exact CLI version in `config.json`. Before installation, the scripts verify Node.js, the effective package registry, SSL settings, Git URL rewriting, shared paths, and skill-name ownership. They inspect machine configuration without rewriting npm, pnpm, or Git settings.

## Machine-local settings

Keep portable policy in `config.json`. For a PC that needs a trusted enterprise registry or must use its Git proxy, copy `config.local.example.json` to `config.local.json` and edit only the required values. The local file is ignored by Git and may override only `gitBypassProxy` and `tooling.allowedRegistries`; proxy credentials and certificate paths remain in the machine's package-manager configuration.

Run `scripts/Test-Project.ps1` after creating or editing the local file. An unknown registry, non-HTTPS registry, disabled SSL verification, unsafe Git rewrite, unsupported local key, or insufficient Node.js version blocks installation.

## Commands

Generate and open a report without changing skills:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\New-SkillUpdateReport.ps1 -Open
```

Apply reviewed updates:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1
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
