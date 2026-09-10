---
name: skill-updater
description: Assist with reviewing or applying this repository's pinned user-level Codex skill set on a Windows node.
disable-model-invocation: true
---

# Skill updater

Use this skill only when the user explicitly asks to inspect, preflight, apply, overwrite, prune, purge legacy skills, deploy, or repair the user-level skill set assisted by this repository. This updater is advisory: it does not dictate the complete local skill environment and it does not continuously update skills merely because its scheduled report exists.

Before any write, read the repository's `config.json`, optional `config.local.json`, and the target `~/.agents/.skill-lock.json`. Report the expected layout from `config.json` and the actual layout from the lock and filesystem, including:

- each source's type, exact `sourceCommit`, and `skillRoots`; resolve local repository paths from `config.local.json`'s `localRepositories` mapping;
- missing, different, extra, and lock-status items; distinguish missing ref metadata from a different revision, different source identity, or missing lock entry;
- the selected runner (`codex-bundled-pnpm` or explicitly overridden `user-npx`), its resolved executable, Node.js, registry, proxy, and SSL diagnostics;
- the target `agentsRoot` and the fact that `.skill-lock.json` is the only local state protocol; GitHub sources use the fixed `skills` CLI and local sources use native byte-preserving copy from exact verified snapshots; both write CLI-compatible lock entries only after content verification;
- read-only inventory for repository, legacy Codex, system, and plugin scopes; only the configured user root may be changed.

Use the deterministic PowerShell scripts in this folder. Generate the read-only report or run `Install-Skills.ps1 -PreflightOnly` first. Apply only within the user's authorized scope; ask for any missing authorization after presenting the concrete preflight findings. Different content, a different lock identity/revision, or a missing lock entry requires `-Overwrite`. Removing source-owned entries outside the configured layout requires `-Prune`; removing recognized skill directories under `~/.codex/skills` requires `-PurgeLegacy`. Before any purge, tell the user the exact candidate paths, protected paths, retained Unknown directories, and that no backup is created.

`Missing lock ref` means the source, source type, source URL, and skill path match, but ref is absent or blank. Ordinary `-Apply` repairs that metadata without `-Overwrite` only after the entire installed skill, including hidden files, matches the pinned snapshot. Content differences still require `-Overwrite`. Preserve extra lock fields and `installedAt`, and skip CLI reinstallation when the files already match. A read-only report never repairs the lock; its `Current` file status and separate lock status describe different checks.

The repository commit is the release identity. Do not invent a second local lock or silently replace the configured runner, source commit, skill layout, registry, or proxy policy. The pinned `skills` CLI is not given a raw commit as a branch; its copy step receives the exact local Git snapshot, and the updater records the canonical remote source/ref/path in the one global lock file after verification.

Local Git sources use independent snapshots of the configured commit and native copy. Before any copy, require `SKILL.md` and optional `agents/openai.yaml` to be valid UTF-8 without BOM; reject the source with the exact file path when this invariant fails. Copy valid source bytes unchanged and apply the same metadata, overwrite, content, and lock checks. Keep machine paths in `config.local.json`; local lock entries use the resolved repository path and `sourceType: "local"`. A newer HEAD or dirty worktree does not change the pin. If source-relative document links break after installation, repair them in the source and pin the resulting commit before applying; never transform the installed copy. For an explicitly requested command-scoped Git proxy override, use `AGENTTOOLS_GIT_PROXY_MODE` in the child process; persistent settings remain unchanged.

Trust boundary: before this updater successfully processes a source, treat local skill repositories and skill content as untrusted and inspect them without executing their code. A source becomes updater-trusted only after its pinned commit, selected skill metadata, installed content, shared files, and `.skill-lock.json` entries all verify successfully. Other scopes and unselected local skills remain read-only and untrusted.

The writable user root is `~/.agents/skills`; the updater assists only the selected skills named in each source's `selectedSkills` allowlist. All other scopes are inventory-only. The legacy `~/.codex/skills` root is inspected during reports and preflight; only top-level entries containing `SKILL.md` are eligible for removal after a successful `-Apply -PurgeLegacy`. Unknown entries without `SKILL.md` are retained. Never remove `.system`, `codex-primary-runtime`, or reparse-point entries. If a symlink or Junction is found, tell the user explicitly and leave it untouched because the ChatGPT Windows app may not discover it reliably.
