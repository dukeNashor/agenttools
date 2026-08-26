---
name: skill-updater
description: Review or apply this repository's pinned global Codex skill set across a Windows node.
disable-model-invocation: true
---

# Skill updater

Use this skill only when the user explicitly asks to inspect, preflight, apply, overwrite, prune, deploy, or repair the skill set managed by this repository.

Before any write, read the repository's `config.json`, optional `config.local.json`, and the target `~/.agents/.skill-lock.json`. Report the expected layout from `config.json` and the actual layout from the lock and filesystem, including:

- each source's exact `sourceCommit` and `skillRoots`;
- missing, different, extra, and lock-mismatch items;
- the selected runner (`codex-bundled-pnpm` or explicitly overridden `user-npx`), its resolved executable, Node.js, registry, proxy, and SSL diagnostics;
- the target `agentsRoot` and the fact that `.skill-lock.json` is the only local state protocol.

Use the deterministic PowerShell scripts in this folder. Generate the read-only report or run `Install-Skills.ps1 -PreflightOnly` first. Ask the user to confirm before running `Install-Skills.ps1 -Apply`; require a separate explicit choice of `-Overwrite` for existing different content or lock metadata and `-Prune` for same-source entries outside the configured layout. Never infer those choices from context.

The repository commit is the release identity. Do not invent a second local lock or silently replace the configured runner, source commit, skill layout, registry, or proxy policy.
