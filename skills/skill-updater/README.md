# Skill updater

A small, Windows-only project that assists with comparing and applying two Git-pinned skill sources into the canonical user root `~/.agents/skills`:

- skills under `mattpocock/skills/skills/engineering` and `skills/productivity` only;
- all 13 skills from Cognitive Bias Lab's `critical-thinking-tools`, plus its shared `references/evidence-map.md` dependency.

Only the names listed in each source's `selectedSkills` allowlist are assisted by this updater. The scheduled job is read-only and does not perform continuous updates. Every Monday at 09:00 it compares installed files with the pinned upstream commits, inventories repository/legacy/system/plugin scopes without modifying them, scans the legacy `~/.codex/skills` root for duplicates and conflicts, checks whether the pinned `skills` CLI has a newer registry release, writes a self-contained HTML report under `reports/`, and opens `latest.html` in the default browser. If the PC is unavailable, Task Scheduler starts it when the signed-in user is next available.

`config.json` is the committed expected state. Each source names an exact 40-character `sourceCommit` and its `skillRoots`; changing either is a normal Git change and therefore changes the `agenttools` release identity. The only local state protocol is the global `~/.agents/.skill-lock.json` written by the `skills` CLI. This project compares the two; it does not create another lock file.

## Deploy

Open Windows PowerShell in this directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1
```

To deploy and then remove unprotected legacy user skills from `~/.codex/skills`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1 -PurgeLegacy
```

Deployment applies the committed source commits and creates no backup. It preserves source-owned skills outside the configured allowlist unless `-Prune` is explicitly requested. Existing different skill content or lock metadata requires the separate `-Overwrite` choice. Add `-PurgeLegacy` to remove recognized legacy skill directories (those containing `SKILL.md`) from the deprecated `~/.codex/skills` root after successful verification; Unknown directories are retained. Before doing so, show the user the exact candidates and protected entries. It then installs or replaces the current-user scheduled task named `AgentTools Skill Update Report`.

The updater writes only the selected user-level skills and configured shared files under `~/.agents`. Repository, admin/system, legacy, and plugin scopes are read-only inventory surfaces. A local skill/source is untrusted until the updater verifies its pinned commit, `SKILL.md` metadata, content, shared files, and lock entries; successfully processed pinned sources are updater-trusted for later comparison.

The committed runner policy is `codex-bundled-pnpm`. A machine-local `config.local.json` may explicitly select `user-npx`; there is no silent fallback between them. Both execute the exact CLI version in `config.json`. Before installation, the scripts verify Node.js, the effective package registry, SSL settings, Git URL rewriting, shared paths, and skill-name ownership. They inspect machine configuration without rewriting npm, pnpm, or Git settings.

## Machine-local settings

Keep portable policy in `config.json`. For a PC that needs a trusted enterprise registry, a Windows user proxy, or explicitly user-provided `npx`, copy `config.local.example.json` to `config.local.json` and edit only the required values. The local file is ignored by Git and may override only `gitProxyMode`, `tooling.runner`, and `tooling.allowedRegistries`; proxy credentials and certificate paths remain in machine configuration. Supported Git proxy modes are `git-config` (default), `windows-user-proxy` (the current user's fixed Windows Internet Settings proxy), and `direct` (explicitly disables Git proxies).

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

Apply reviewed updates and remove unprotected legacy user skills under `~/.codex/skills` after all selected skills and lock entries are verified:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -Apply -PurgeLegacy
```

`-PurgeLegacy` does not create a backup. Before running it, tell the user the exact paths that will be removed. It removes only top-level legacy directories containing `SKILL.md`; directories without `SKILL.md` are reported as Unknown and retained. It never removes `.system`, `codex-primary-runtime`, or symlink/Junction entries, and it is blocked when a same-name legacy skill differs from the pinned source. Any symlink/Junction is reported to the user because ChatGPT Windows discovery may not follow it reliably.

Shared files such as `references/evidence-map.md` are explicit source dependencies, not skills. Their source path, destination, and hash are reported separately; a differing destination requires `-Overwrite`, and the updater writes only the configured destination under the user root.

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
