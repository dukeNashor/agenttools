# Windows deployment procedure

Use this procedure when a user asks to deploy, redeploy, move, or repair `skills/skill-updater` on a Windows PC.

## Inspect

1. Resolve the current checkout path; every scheduled-task path must come from it.
2. Inspect `config.json`, optional `config.local.json`, `~/.agents/.skill-lock.json`, `~/.agents/skills/`, `~/.codex/skills/`, and the exact task named in `config.json`.
3. Run `scripts/Test-Project.ps1`, then `scripts/Install-Skills.ps1 -PreflightOnly`. They must accept the effective registry, Node.js version, explicitly selected runner, package-manager settings, Git configuration, pinned source commits, upstream source paths, shared destinations, and skill names before any skill is changed. If the selected runner is unavailable, explain the missing prerequisite before installing system software.
4. Tell the user that `-Apply` assists only the configured user-level subset without a backup, `-Overwrite` is separately required for different local content or lock metadata, `-Prune` is separately required to remove source-owned entries outside the configured allowlist, and `-PurgeLegacy` is separately required to remove recognized skill directories containing `SKILL.md` from the deprecated `~/.codex/skills` root. Unknown directories are retained. Before purge, show exact candidates, retained Unknown directories, and protected paths; report any symlink/Junction and leave it untouched. Repository, admin/system, legacy, and plugin scopes are read-only inventory surfaces. The report and task-only repair paths do not change skills.

For a machine-specific registry or Git proxy policy, copy `config.local.example.json` to the Git-ignored `config.local.json`. Add only explicitly trusted HTTPS registries. Keep proxy credentials and certificate paths in machine configuration; the project reads their presence without copying or printing their values.

## Deploy

Run `scripts/Deploy.ps1` from Windows PowerShell. Add `-PurgeLegacy` when the reviewed run should also remove recognized skill directories from the deprecated `~/.codex/skills` root. Unknown directories are retained. The scripts resolve the clone path and current user at runtime, so copying a task definition from another PC is invalid. Register the task for the current interactive user with limited privileges; it should start when available and should not wake the PC.

If Git must use the current user's Windows proxy, set `gitProxyMode` to `windows-user-proxy` in `config.local.json`. The updater reads the fixed proxy from Windows Internet Settings and passes it explicitly to each Git invocation, including Git processes started by the skills CLI. PAC-only settings and disabled proxies fail fast. Keep proxy behavior command-scoped; do not rewrite global Git configuration as part of deployment.

## Validate

Deployment is complete only when every item below is true:

- the global lock contains entries from both configured `repositorySlug` values;
- every selected skill under the configured `skillRoots` exists under `~/.agents/skills/`, has valid `SKILL.md` frontmatter (`name` and non-empty `description`), and same-source entries outside the selected layout remain untouched unless `-Prune` was explicitly requested;
- no configured or installed skills from different sources share a name, and every shared destination remains under `~/.agents`;
- no divergent same-name entry remains under `~/.codex/skills`; recognized legacy skill entries are absent after `-PurgeLegacy`, while Unknown directories without `SKILL.md`, `.system`, `codex-primary-runtime`, and reparse-point entries remain protected;
- `~/.agents/references/evidence-map.md` exists;
- `reports/latest.html` opens, states that the comparison is read-only, and shows the pinned and registry-latest CLI versions;
- Task Scheduler shows the configured task enabled, weekly on Monday at 09:00, interactive-only, and start-when-available;
- the task action points to this checkout's `scripts/New-SkillUpdateReport.ps1` rather than a path copied from another PC.

When only the checkout path changed, run `scripts/Install-ScheduledTask.ps1`; reinstalling skills is unnecessary.
