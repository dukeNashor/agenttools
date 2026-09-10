# Skill updater

A small, Windows-only project that assists with comparing and applying Git-pinned skill sources into the canonical user root `~/.agents/skills`:

- skills under `mattpocock/skills/skills/engineering` and `skills/productivity` only;
- `skill-doctor` from Warp's `common-skills`;
- `ad-engineering-context` from the local ADJenkinsContext Git repository.

Only the names listed in each source's `selectedSkills` allowlist are assisted by this updater. The scheduled job is read-only and does not perform continuous updates. Every Monday at 09:00 it compares installed files with the pinned upstream commits, inventories repository/legacy/system/plugin scopes without modifying them, scans the legacy `~/.codex/skills` root for duplicates and conflicts, checks whether the pinned `skills` CLI has a newer registry release, writes a self-contained HTML report under `reports/`, and opens `latest.html` in the default browser. If the PC is unavailable, Task Scheduler starts it when the signed-in user is next available.

`config.json` is the committed expected state. Each source names an exact 40-character `sourceCommit` and its `skillRoots`; changing either is a normal Git change and therefore changes the `agenttools` release identity. The only local state protocol is the global `~/.agents/.skill-lock.json`. Because `skills@1.5.23` treats a remote ref as a branch during clone, GitHub sources give that fixed CLI an exact local Git snapshot. Local sources use the updater's native byte-preserving copy. Both paths first require `SKILL.md` and optional `agents/openai.yaml` to be valid UTF-8 without BOM, then write CLI-compatible canonical source/ref/path entries only after verifying copied content. No second lock file is created.

## Deploy

Open Windows PowerShell in this directory:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1
```

To deploy and then remove unprotected legacy user skills from `~/.codex/skills`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Deploy.ps1 -PurgeLegacy
```

Deployment applies the committed source commits and creates no backup. It preserves source-owned skills outside the configured allowlist unless `-Prune` is explicitly requested. Different skill content, a different lock identity/revision, or a missing lock entry requires the separate `-Overwrite` choice. Run `Install-Skills.ps1 -Apply -Overwrite` for reviewed differences, followed by `Deploy.ps1 -SkipSkillInstall` to update the task and report. Add `-PurgeLegacy` to remove recognized legacy skill directories (those containing `SKILL.md`) from the deprecated `~/.codex/skills` root after successful verification; Unknown directories are retained. Before doing so, show the user the exact candidates and protected entries. It then installs or replaces the current-user scheduled task named `AgentTools Skill Update Report`.

The report compares files and lock provenance separately. `Current` means files match the pinned snapshot. `Missing lock ref` means only the commit record is missing or blank: ordinary `-Apply` fills it after verifying all files, including hidden files, without reinstalling matching skills. `Lock revision mismatch`, `Lock identity mismatch`, and `Missing lock entry` require review and `-Overwrite`. Applying updates preserves extra lock metadata and the original `installedAt`; generating a report never changes the lock.

The updater writes only the selected user-level skills and configured shared files under `~/.agents`. Repository, admin/system, legacy, and plugin scopes are read-only inventory surfaces. A local skill/source is untrusted until the updater verifies its pinned commit, `SKILL.md` metadata, content, shared files, and lock entries; successfully processed pinned sources are updater-trusted for later comparison.

The committed runner policy is `codex-bundled-pnpm`. A machine-local `config.local.json` may explicitly select `user-npx`; there is no silent fallback between them. Both execute the exact CLI version in `config.json`. Before installation, the scripts verify Node.js, the effective package registry, SSL settings, Git URL rewriting, shared paths, and skill-name ownership. They inspect machine configuration without rewriting npm, pnpm, or Git settings.

## Machine-local settings

Keep portable policy in `config.json`. The Git-ignored `config.local.json` may set `gitProxyMode`, `tooling.runner`, `tooling.allowedRegistries`, and `localRepositories` (a mapping from configured local source IDs to absolute local Git worktree paths). Proxy credentials and certificate paths remain in machine configuration. Supported Git proxy modes are `git-config` (default), `windows-user-proxy` (the current user's fixed Windows Internet Settings proxy), and `direct` (explicitly disables Git proxies). An explicit process environment variable `AGENTTOOLS_GIT_PROXY_MODE` overrides the file policy for a single child-process run without changing persistent settings.

For the configured AD source, add `"localRepositories": { "ad-engineering-context": "D:\\dev\\ADJenkinsContext" }` to the local file. `config.local.example.json` shows the supported settings; copy only the values appropriate for this PC. A missing local repository blocks that source instead of silently skipping it.

Local sources use `sourceType: "local"`, with the commit, roots, and selected skill names in `config.json`. The updater clones an independent snapshot and checks out exactly `sourceCommit`; newer commits and uncommitted edits in the original worktree are excluded. It rejects BOM-prefixed or invalid UTF-8 skill metadata, copies valid source bytes unchanged, rejects reparse points, and retains the same explicit overwrite and content-verification gates. The global lock records the resolved repository path, `sourceType: "local"`, commit, skill path, and verified folder hash. Moving the repository changes its lock identity and requires reviewing the mismatch. The fixed skills CLI accepts local lock entries, but its own update command skips local sources; use this updater to review/apply them.

The AD skill points to canonical documents in `D:\dev\ADJenkinsContext`; that checkout must remain available after installation. Cross-repository document links must resolve from the installed skill location. Fix such links in the source and pin the resulting commit; installation never rewrites the copied skill.

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

Shared files listed in `config.json` are explicit source dependencies, not skills. Their source path, destination, and hash are reported separately; a differing destination requires `-Overwrite`, and the updater writes only the configured destination under the user root. An empty `sharedFiles` list requires no shared resources.

Run the complete network, source, collision, and tooling preflight without changing skills:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -PreflightOnly
```

To install only the configured local AD skill, run preflight with `-SourceId ad-engineering-context -PreflightOnly`, then use the same source filter with `-Apply`. Existing skills from other sources are retained.

Regression checks: run `tests/Test-GitProxy.ps1`, `tests/Test-LocalSource.ps1`, and `tests/Test-LockMigration.ps1`. Local-source and lock-migration tests use isolated temporary fixtures without modifying installed skills; the lock test runs the installer and report against fixture snapshots and substitutes external tooling/network boundaries.

Retry one source independently after a transient network failure:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Skills.ps1 -SourceId warp-common-skills -Apply
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
