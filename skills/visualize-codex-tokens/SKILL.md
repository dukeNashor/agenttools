---
name: visualize-codex-tokens
description: Generate a self-contained interactive HTML report of per-turn and total token usage from a local Codex thread or rollout JSONL.
---

# Visualize Codex Tokens

Generate the report with `scripts/codex_token_visualizer.py`.

## Workflow

1. Accept either a Codex thread ID or an explicit rollout `.jsonl` path.
2. Use Python 3.10 or newer. On Windows, prefer `py -3`; elsewhere, prefer `python3`.
3. Run the script with `--strict` first:

   ```text
   <python> <skill-dir>/scripts/codex_token_visualizer.py <thread-or-jsonl> --strict
   ```

4. Pass `--output <path>` when the user chooses a destination. Otherwise let the script write `%TEMP%/agenttools/codex-token-<thread-id>.html` on Windows or the equivalent system temporary directory.
5. Pass `--exclude-messages` only when the user asks to omit user messages. Reports include complete user messages by default.
6. Pass `--open` only when the user explicitly asks to open the report.
7. Return the report path, total Token count, turn count, and integrity-error count.

If strict mode rejects the rollout, report the integrity errors and ask whether to generate a best-effort report. Do not silently rerun without `--strict`.

## Privacy boundary

Treat every default report as sensitive: it embeds complete user messages and local source metadata. Keep generated reports local unless the user explicitly authorizes sharing. Never commit generated `.html` reports or rollout `.jsonl` files to a public repository.
