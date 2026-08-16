---
name: visualize-codex-tokens
description: Generate a self-contained interactive HTML report with a single token-progress dual-ring view of Codex consumption and context-window occupancy for one local thread, one local date, or an inclusive local date range.
---

# Visualize Codex Tokens

Generate the report with `scripts/codex_token_visualizer.py`.

## Workflow

1. Choose exactly one report scope:
   - one thread: `<thread-id-or-jsonl>`;
   - today in the machine's local timezone: `--today`;
   - one local date: `--date YYYY-MM-DD`;
   - an inclusive local date range: `--from YYYY-MM-DD --to YYYY-MM-DD`.
2. Use Python 3.10 or newer. On Windows, prefer `py -3`; elsewhere, prefer `python3`.
3. Run the script with `--strict` first:

   ```text
   <python> <skill-dir>/scripts/codex_token_visualizer.py <scope> --strict
   ```

4. Pass `--output <path>` when the user chooses a destination. Otherwise let the script write a stable file under the system temporary `agenttools` directory, named with the thread ID or date range.
5. Pass `--exclude-messages` only when the user asks to omit user messages. Reports include complete user messages by default.
6. Pass `--open` only when the user explicitly asks to open the report.
7. Return the report path, total Token count, turn count, session count for date reports, and integrity-error count.

For date reports, group subagent rollouts under their top-level task and attribute cumulative-counter deltas to the local date of each `token_count` snapshot. The HTML opens on total statistics and lets the user switch to a top-level session's unified main/subagent turn timeline.

Treat cumulative consumption and context occupancy as separate metrics. Attribute consumption from `total_token_usage` deltas. Read every in-scope `last_token_usage.total_tokens` and `model_context_window` snapshot, retain its turn-local cumulative Token offset, and use the ending or latest snapshot for turn summaries. Never sum context occupancy across turns or agents. Date-clipped turns expose only in-range snapshots, live turns expose the current latest snapshot, and missing snapshots remain unknown.

Render one token-progress dual-ring chart for each selected session. Allocate each turn an outer-ring arc proportional to its Token consumption and color it by rollout source. Use a one-degree seam between Token 0% and 100%. Order turns by their ending/latest context snapshot and render the inner ring as sampled step bands placed at the retained turn-local Token offsets. Keep 25%/50%/75% guides subtle and make the labeled Context 100% capacity boundary prominent. Leave source changes and unknown snapshots visibly discontinuous, and keep the ring's angles stable when filters change. Represent zero-Token turns as radial ticks. Split the inner-ring profile at every Compaction offset; connect the outer position tick to the before-state circle with a dashed line, then draw a solid inward arrow to the after-state circle. Keep total date statistics free of aggregated context values because context occupancy is a stock, not an additive total.

Show a zero-delay pointer-following tooltip for every dual-ring turn sector. Put Context occupancy and the turn's share of the selected session's total Token consumption in distinct green and orange KPI cards. Keep the Token-share denominator equal to the complete selected-session denominator used by the ring, independent of filters. Include compact turn metadata, Token composition, Context snapshot details, Compaction count, and an initial-user-message preview; keep the full message in the turn-detail drawer. Let a primary click outside that drawer close it without consuming the clicked control's action, while another turn target switches the drawer directly. In date reports, render the session list as a desktop-collapsible and mobile-overlay drawer without persisting its state across report loads.

Date reports treat a live trailing line and an unclosed active turn as provisional warnings. If strict mode rejects any other integrity error, report it and ask whether to generate a best-effort report. Do not silently rerun without `--strict`.

## Privacy boundary

Treat every default report as sensitive: it embeds complete user messages and local source metadata. Keep generated reports local unless the user explicitly authorizes sharing. Never commit generated `.html` reports or rollout `.jsonl` files to a public repository.
