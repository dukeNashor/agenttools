---
name: visualize-codex-tokens
description: Generate a self-contained interactive HTML report with a token-progress dual-ring view of Codex consumption and context-window occupancy, plus exact per-tool-call usage satellites, for one local thread, an explicit rollout ID list, one or more explicit project directories/current ChatGPT Windows App project, one local date, or an inclusive local date range.
---

# Visualize Codex Tokens

Generate the report with `scripts/codex_token_visualizer.py`.

## Scope contract

Require the caller to provide exactly one explicit scope before running the script:

- a local date segment: `--date YYYY-MM-DD` for one day, or `--from YYYY-MM-DD --to YYYY-MM-DD` for an inclusive range;
- an explicit project/session collection: one or more rollout or thread IDs, using the positional thread argument for one ID or `--ids` for multiple IDs.
- an explicit project collection: repeat `--project <directory>` for one or more directories;
- the current ChatGPT Windows App session project: `--current-project` (alias `--current-repo`), which resolves the current working directory supplied by the app.

Treat an explicit ID collection as the project boundary. For a project scope, discover rollout JSONL files from the default session roots, match `session_meta.cwd` inside the canonical project directory or match the same Git common directory, then include rollouts whose `parent_thread_id` or `forked_from_id` leads back to a directly matched rollout. Explicit project directories may be ordinary directories; `--current-project` requires a recognizable Git repository. Never infer membership from a repository name, message content, or session title. A scope must be reflected accurately in the report title and final summary. If no project, current-project, date segment, thread, or ID collection is supplied, ask the caller to provide one and do not generate a report.

## Fast path

Once the scope is explicit, keep execution tight:

1. Invoke the script once with the selected scope.
2. If it succeeds, perform only a lightweight output-file check when needed to provide the local link.
3. Return the link, the actual scope, and the required statistics.

Repository source inspection, memory lookup, extra directory discovery, and planning are outside this skill's Token-report workflow. Project discovery is limited to the current app project path or the explicit `--project` paths plus the configured session roots; use broader inspection only when the caller separately requests source analysis or historical context.

## Workflow

1. Choose exactly one report scope:
   - one explicit thread or rollout JSONL path: `<thread-id-or-jsonl>`;
   - an explicit rollout ID list: `--ids <id> [<id> ...]`; repeat `--ids` when convenient;
   - an explicit project collection: `--project <directory>`; repeat it for multiple projects;
   - the current ChatGPT Windows App project: `--current-project` or `--current-repo` when the user says current project, current repo, or current repository;
   - one local date: `--date YYYY-MM-DD`;
   - an inclusive local date range: `--from YYYY-MM-DD --to YYYY-MM-DD`.
   Do not select a scope by default. In particular, do not fall back to `--today` or `--current-project` when the caller has not supplied a scope.
2. Use Python 3.10 or newer. On Windows, prefer `py -3`; elsewhere, prefer `python3`.
3. Run the script without `--strict` by default. This is best-effort mode: integrity problems are embedded in the report and surfaced in the CLI summary. Add `--strict` only when the caller explicitly wants report generation to fail on integrity errors:

   ```text
   <python> <skill-dir>/scripts/codex_token_visualizer.py <scope>
   ```

4. For an ID-list scope, aggregate exactly the selected rollout IDs, de-duplicate repeated IDs, do not clip activity by date, and group selected subagent rollouts under their detected root task. For a project scope, scan the default session roots, de-duplicate rollout IDs, select every directly matched rollout, and include all descendants linked by `parent_thread_id` or `forked_from_id`; do not impose a session-count limit and do not clip activity by date. If zero rollouts match, write a zero-session report with the discovery scope and counts. Pass `--output <path>` when the user chooses a destination. Otherwise let the script write a stable file under the system temporary `agenttools` directory, named with the thread ID, ID-list digest, project label/digest, or date range.
5. Pass `--exclude-messages` only when the user asks to omit user messages. Reports include complete user messages by default.
6. Use `--title "..."` (or the explicit alias `--report-title "..."`) when the user wants a memorable report name. It sets the visible total title and the HTML page title; in a multi-session report, switching into an individual session still shows that session's title.
7. Do not pass `--open` during a normal skill invocation, and do not call a browser/open tool after the script finishes. Pass `--open` only when the user explicitly asks to open the report.
8. After a successful run, return only a Markdown link to the local HTML file plus a short text brief. State the actual scope in the brief; for project scopes include project paths plus candidate, direct-match, and selected rollout counts. Include elapsed wall-clock time, session count, turn count, total Token count, input Token count, output Token count, and integrity-error count. Use the script's reported elapsed time when available; for a single-thread report, report the session count as `1`, and for date/project reports use `summary.sessionCount`. Do not embed, preview, or automatically open the HTML.

Reports default to displaying Token values in `M` units with at most one decimal place; the report-level unit control still allows switching to raw, `K`, or `B` units, while underlying usage remains in raw Token counts.

For date reports, group subagent rollouts under their top-level task and attribute cumulative-counter deltas to the local date of each `token_count` snapshot. ID-list reports use the same multi-session HTML and aggregation rules, but preserve each selected rollout's full activity and identify the scope as `指定会话`. The HTML opens on total statistics and lets the user switch to a top-level session's unified main/subagent turn timeline.

Treat cumulative consumption and context occupancy as separate metrics. Attribute consumption from `total_token_usage` deltas. Read every in-scope `last_token_usage.total_tokens` and `model_context_window` snapshot, retain its turn-local cumulative Token offset, and use the ending or latest snapshot for turn summaries. Never sum context occupancy across turns or agents. Date-clipped turns expose only in-range snapshots, live turns expose the current latest snapshot, and missing snapshots remain unknown.

Render one token-progress dual-ring chart for each selected session. Present the dual-ring, per-turn composition, cumulative-consumption, and detail-table views as accessible tabs, with the dual-ring selected by default for a thread or selected date-range session. Keep shared filters above the tabs, and place the report summary metrics in a compact brief at the top right on desktop and a single column on mobile. Allocate each turn an outer-ring arc proportional to its Token consumption and color it by rollout source. Use a one-degree seam between Token 0% and 100%. Order turns by their ending/latest context snapshot and render the inner ring as sampled step bands placed at the retained turn-local Token offsets. Keep 25%/50%/75% guides subtle and make the labeled Context 100% capacity boundary prominent. Leave source changes and unknown snapshots visibly discontinuous, and keep the ring's angles stable when filters change. Represent zero-Token turns as radial ticks. Split the inner-ring profile at every Compaction offset; connect the outer position tick to the before-state circle with a dashed line, then draw a solid inward arrow to the after-state circle. Keep total date statistics free of aggregated context values because context occupancy is a stock, not an additive total. In date-range reports, select total statistics by default and show two dashboard-style model doughnut charts there: raw per-model Token and official-rate-adjusted Sol-equivalent Token. Keep their model colors synchronized, put the total in the center, and leave the session Context dual-ring view unchanged when a session is selected.

When a report contains subagent turns, keep the complete selected-session Token denominator and render main turns in the primary ring. Move subagent turns to satellite arcs outside that ring, bind each satellite to the nearest preceding main turn (falling back to the nearest main turn), and offset siblings onto nearby radial lanes. Preserve satellite Context as a compact radial marker, show zero-Token satellites as ticks, move their Compaction markers with them, and connect satellites to their parent only with an interaction-emphasized guide line. In date-range reports, style the cross-session total as a distinct summary entry and select total statistics by default. Keep one state-aware session-navigation control: the expanded sidebar exposes a left-chevron close control, and the collapsed desktop rail exposes a right-chevron reopen control; on mobile the same controls operate as a drawer. The whole statistics tab bar carries a current-model Label at its upper-left edge: 多模型 for total statistics and the selected session's compact model name for individual sessions. Model list items show a single oversized, high-saturation diagonal Label anchored in the lower-right corner over a model-tinted background, use a compact model label in the sidebar, and expose the session's observed effort types as a compact badge. Total statistics show the two model doughnuts without search, tool, status, or reset controls; each doughnut's outer ring subdivides the matching inner model sector by eligible single-model sessions, uses the corresponding model color, and hides multi-model sessions, while individual sessions retain those filters. Make outer session segments hoverable and keyboard-focusable: show a cursor-following brief with session name, model, effort, Token, turn count, and last activity, and temporarily highlight the matching sidebar item without changing the current view or search query. For Spark-only sessions, display the model as `Spark`, use a dedicated magenta theme color, and add a separate `计划外` badge in session-facing labels; keep mixed sessions labeled by their primary in-plan model. GPT-5.3 Codex Spark remains outside the plan-level model comparison: keep its usage in overall totals, but exclude Spark-only turns from model charts and show the excluded amount explicitly. Keep the rate-card source, effective date, and last verification timestamp in report metadata.

When rollout records expose tool-call events, render one independent tool satellite per observed call outside the dual ring. Group those satellites in a dashed radial envelope for their own parent turn; never merge calls from different turns, even when the raw tool name is the same. Order calls by observed event time, preserve the raw tool name, classify common tools such as Computer Use, Chrome/Browser Use, ImageGen, Exec Reasoning, Shell/Terminal, Code Interpreter, Web Search, File Search, MCP, and Function Calling, and keep an `other` category for unknown tools. Tool usage is an exact projection of reported per-call usage only: input, cached input, output, reasoning output, and total are shown when present. Calls without exact usage remain hollow/unknown and must not be inferred or added to the main turn total. Present tool filtering as a multi-select checkbox list using OR semantics. Default only Computer Use, Chrome/Browser Use, and ImageGen to checked; leave Exec Reasoning and other basic/frequent tools unchecked. An empty selection hides only the tool satellites and envelopes, never the main Token/Context rings, turn order, or totals. Clicking a tool satellite opens its parent-turn detail drawer. Apply the active date window to tool events, and hide the tool layer when the rollout exposes no tool events.

Show a zero-delay pointer-following tooltip for every dual-ring turn sector. Put Context occupancy and the turn's share of the selected session's total Token consumption in distinct green and orange KPI cards. Show the current Context occupancy as the large value followed by a small parenthetical absolute change from the previous turn's ending snapshot, using increase/decrease/no-change wording; keep the Token card current-turn-only without a previous-turn comparison. Keep the Token-share denominator equal to the complete selected-session denominator used by the ring, independent of filters. Include compact turn metadata, Token composition, Context snapshot details, Compaction count, and an initial-user-message preview; keep the full message in the turn-detail drawer. Let a primary click outside that drawer close it without consuming the clicked control's action, while another turn target switches the drawer directly. In date reports, render the session list as a desktop-collapsible and mobile-overlay drawer without persisting its state across report loads.

Date reports treat a live trailing line and an unclosed active turn as provisional warnings. Best-effort mode writes the report while preserving all integrity warnings; strict mode rejects integrity errors and writes nothing. Report the integrity-error count and warning details in either mode.

## Privacy boundary

Treat every default report as sensitive: it embeds complete user messages and local source metadata. Keep generated reports local unless the user explicitly authorizes sharing. Never commit generated `.html` reports or rollout `.jsonl` files to a public repository.
