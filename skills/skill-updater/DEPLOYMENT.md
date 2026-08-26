# Windows deployment procedure

Use this procedure when a user asks to deploy, redeploy, move, or repair `skills/skill-updater` on a Windows PC.

## Inspect

1. Resolve the current checkout path; every scheduled-task path must come from it.
2. Inspect `config.json`, optional `config.local.json`, `~/.agents/.skill-lock.json`, `~/.agents/skills/`, and the exact task named in `config.json`.
3. Run `scripts/Test-Project.ps1`, then `scripts/Install-Skills.ps1 -PreflightOnly`. They must accept the effective registry, Node.js version, explicitly selected runner, package-manager settings, Git configuration, pinned source commits, upstream source paths, shared destinations, and skill names before any skill is changed. If the selected runner is unavailable, explain the missing prerequisite before installing system software.
4. Tell the user that `-Apply` changes the configured subset without a backup, `-Overwrite` is separately required for different local content or lock metadata, and `-Prune` is separately required to remove same-source skills outside the configured layout. The report and task-only repair paths do not change skills.

For a machine-specific registry or Git proxy policy, copy `config.local.example.json` to the Git-ignored `config.local.json`. Add only explicitly trusted HTTPS registries. Keep proxy credentials and certificate paths in machine configuration; the project reads their presence without copying or printing their values.

## Deploy

Run `scripts/Deploy.ps1` from Windows PowerShell. The scripts resolve the clone path and current user at runtime, so copying a task definition from another PC is invalid. Register the task for the current interactive user with limited privileges; it should start when available and should not wake the PC.

If Git must use the machine proxy, set `gitBypassProxy` to `false` in `config.local.json`. Keep proxy behavior command-scoped; do not rewrite global Git configuration as part of deployment.

## Validate

Deployment is complete only when every item below is true:

- the global lock contains entries from both configured `repositorySlug` values;
- every skill under the configured `skillRoots` exists under `~/.agents/skills/`, and same-source skills outside those roots are absent;
- no configured or installed skills from different sources share a name, and every shared destination remains under `~/.agents`;
- `~/.agents/references/evidence-map.md` exists;
- `reports/latest.html` opens, states that the comparison is read-only, and shows the pinned and registry-latest CLI versions;
- Task Scheduler shows the configured task enabled, weekly on Monday at 09:00, interactive-only, and start-when-available;
- the task action points to this checkout's `scripts/New-SkillUpdateReport.ps1` rather than a path copied from another PC.

When only the checkout path changed, run `scripts/Install-ScheduledTask.ps1`; reinstalling skills is unnecessary.
