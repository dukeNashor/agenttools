---
name: skill-updater
description: Assist with reviewing or applying this repository's pinned user-level Codex skill set on a Windows node.
disable-model-invocation: true
---

# Skill updater

Use this skill only when the user explicitly asks to inspect, preflight, apply, overwrite, prune, purge legacy skills, deploy, or repair the user-level skill set assisted by this repository. This updater is advisory: it does not dictate the complete local skill environment and it does not continuously update skills merely because its scheduled report exists.

Before any write, read the repository's `config.json`, optional `config.local.json`, and the target `~/.agents/.skill-lock.json`. Report the expected layout from `config.json` and the actual layout from the lock and filesystem, including:

- each source's exact `sourceCommit` and `skillRoots`;
- missing, different, extra, and lock-mismatch items;
- the selected runner (`codex-bundled-pnpm` or explicitly overridden `user-npx`), its resolved executable, Node.js, registry, proxy, and SSL diagnostics;
- the target `agentsRoot` and the fact that `.skill-lock.json` is the only local state protocol; for pinned arbitrary commits, the updater uses the fixed `skills` CLI to copy from its exact verified local snapshot and writes CLI-compatible lock entries only after content verification;
- read-only inventory for repository, legacy Codex, system, and plugin scopes; only the configured user root may be changed.

Use the deterministic PowerShell scripts in this folder. Generate the read-only report or run `Install-Skills.ps1 -PreflightOnly` first. Ask the user to confirm before running `Install-Skills.ps1 -Apply`; require a separate explicit choice of `-Overwrite` for existing different content or lock metadata, `-Prune` for source-owned entries outside the configured layout, and `-PurgeLegacy` for recognized skill directories under `~/.codex/skills`. Before any purge, tell the user the exact candidate paths, protected paths, retained Unknown directories, and that no backup is created. Never infer those choices from context.

The repository commit is the release identity. Do not invent a second local lock or silently replace the configured runner, source commit, skill layout, registry, or proxy policy. The pinned `skills` CLI is not given a raw commit as a branch; its copy step receives the exact local Git snapshot, and the updater records the canonical remote source/ref/path in the one global lock file after verification.

Trust boundary: before this updater successfully processes a source, treat local skill repositories and skill content as untrusted and inspect them without executing their code. A source becomes updater-trusted only after its pinned commit, selected skill metadata, installed content, shared files, and `.skill-lock.json` entries all verify successfully. Other scopes and unselected local skills remain read-only and untrusted.

The writable user root is `~/.agents/skills`; the updater assists only the selected skills named in each source's `selectedSkills` allowlist. All other scopes are inventory-only. The legacy `~/.codex/skills` root is inspected during reports and preflight; only top-level entries containing `SKILL.md` are eligible for removal after a successful `-Apply -PurgeLegacy`. Unknown entries without `SKILL.md` are retained. Never remove `.system`, `codex-primary-runtime`, or reparse-point entries. If a symlink or Junction is found, tell the user explicitly and leave it untouched because the ChatGPT Windows app may not discover it reliably.
