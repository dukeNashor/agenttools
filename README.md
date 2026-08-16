# Agent Tools

Reusable Codex skills and scripts. This repository is packaged as a Codex plugin and currently contains one explicitly invoked skill: `$visualize-codex-tokens`.

## Visualize Codex Tokens

Generate a self-contained Chinese interactive HTML report for one local Codex thread, one local date, or an inclusive local date range. The report includes:

- per-turn mutually exclusive token composition;
- total and cumulative token usage;
- a responsive session-list drawer that switches between total statistics and individual sessions;
- top-level task totals with subagent usage folded into the parent task;
- daily trends for date ranges and unified main/subagent turn timelines;
- exact in-scope context-window snapshots placed at their turn-local Token offsets;
- one token-progress dual-ring chart per session: a near-closed outer ring maps Token consumption, while sampled inner step bands and a prominent 100% capacity boundary map Context occupancy;
- connected Compaction markers that split the inner profile and point from the before state to the after state;
- linear and logarithmic charts;
- a dual-handle turn-range slider;
- Excel-like conditional formatting in the details table;
- zero-delay pointer-following dual-ring tooltips with initial-message previews plus distinct Context-occupancy and per-turn Token-share cards;
- status filters, full-text search, and automatically dismissing turn details.

The parser uses cumulative `total_token_usage` deltas between `task_started` and matching `task_complete` or `turn_aborted` boundaries. It does not sum `last_token_usage`, because rollback-related records can repeat snapshots. Instead, it records each `last_token_usage.total_tokens` value as instantaneous context occupancy at that snapshot's turn-local cumulative Token offset. Date reports assign each consumption delta to the local date of its `token_count` snapshot, scan active and archived rollouts, and remove the inherited parent preamble from subagent rollouts before aggregation. Context occupancy remains independent per rollout and is never added across turns or agents.

### Install

Ask Codex to install the skill from this repository with `$skill-installer`, or copy `skills/visualize-codex-tokens` into your user skill directory. The repository also includes `.codex-plugin/plugin.json` for plugin-compatible installation flows.

### Use

Invoke the skill explicitly:

```text
$visualize-codex-tokens generate a report for <thread-id>
$visualize-codex-tokens generate today's report
$visualize-codex-tokens generate a report from 2026-08-01 through 2026-08-16
```

Run the bundled script directly with Python 3.10 or newer:

```powershell
py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  <thread-id> `
  --strict

py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  --today `
  --strict

py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  --from 2026-08-01 `
  --to 2026-08-16 `
  --strict
```

Use `--date YYYY-MM-DD` for one historical local date. The scope selectors and the original thread/JSONL positional input are mutually exclusive. Without `--output`, the report is written under the stable system temporary `agenttools` directory with a thread- or date-based filename. Use `--open` to open it after generation.

### Privacy

Reports embed complete in-scope user messages by default. Date reports omit messages and turns outside the selected range. Use `--exclude-messages` to omit all messages and prompt-derived session titles. Reports can also contain local source metadata, so treat generated HTML as sensitive and do not publish it without explicit authorization.

## Development

```powershell
python -m unittest discover -s tests -v
```

The repository contains only synthetic rollout test data. Generated HTML, real rollout JSONL files, local sessions, and workspace outputs are ignored.

## License

MIT

---

# Agent Tools（中文）

可复用的 Codex Skill 与脚本集合。仓库已按 Codex Plugin 格式打包，目前包含一个仅支持显式调用的 Skill：`$visualize-codex-tokens`。

## Codex Token 可视化

为一个本地 Codex 线程、一个本地日期或一个首尾均包含的本地日期范围生成自包含的中文交互式 HTML 报告，包括：

- 每轮互不重叠的 Token 构成；
- 总消耗与累计消耗；
- 可折叠并在移动端覆盖显示的会话列表抽屉，用于切换总统计与各会话；
- 将子代理消耗汇入父任务的顶层会话统计；
- 日期趋势，以及主会话／子代理统一轮次时间线；
- 按轮内累计 Token 位置记录的全部范围内 Context 快照；
- 每个会话一张近乎闭合的累计 Token 进度双环图：外环弧长映射 Token 消耗，内环采样阶梯及醒目的 100% 容量线映射 Context 占用率；
- 在精确 Token 位置切分内环、从压缩前状态指向压缩后状态的连续 Compaction 标记；
- 线性／对数图表；
- 单轨双手柄轮次范围滑块；
- 类似 Excel 条件格式的轮次明细表；
- 包含初始消息预览、零延迟跟随指针的双环扇面浮窗，以及独立的 Context 占用／本轮 Token 占比卡片；
- 状态筛选、全文搜索与自动收起的轮次详情。

解析器使用 `task_started` 与匹配的 `task_complete` 或 `turn_aborted` 边界之间，累计 `total_token_usage` 的差值。它不会累加 `last_token_usage`，因为回滚相关记录可能重复旧快照；每个 `last_token_usage.total_tokens` 都作为瞬时 Context 占用，记录在该快照对应的轮内累计 Token 位置。日期报告按 `token_count` 快照的本地日期归属消耗增量，同时扫描活动与归档 rollout，并在汇总前排除子代理 rollout 中复制的父会话前导。不同 rollout 的上下文占用彼此独立，绝不跨 turn 或代理求和。

### 安装

可让 Codex 使用 `$skill-installer` 从本仓库安装，也可以将 `skills/visualize-codex-tokens` 复制到用户 Skill 目录。仓库还提供 `.codex-plugin/plugin.json`，供支持 Plugin 的安装流程使用。

### 使用

显式调用 Skill：

```text
$visualize-codex-tokens 为 <线程ID> 生成报告
$visualize-codex-tokens 生成今天的报告
$visualize-codex-tokens 生成 2026-08-01 到 2026-08-16 的报告
```

也可以使用 Python 3.10 或更高版本直接运行脚本：

```powershell
py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  <线程ID> `
  --strict

py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  --today `
  --strict

py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  --from 2026-08-01 `
  --to 2026-08-16 `
  --strict
```

单个历史日期可使用 `--date YYYY-MM-DD`。日期选择参数与原有线程／JSONL 位置参数互斥。未指定 `--output` 时，报告以线程或日期命名，写入系统临时目录下稳定的 `agenttools` 目录。需要生成后打开时使用 `--open`。

### 隐私

报告默认嵌入统计范围内的完整用户消息；日期报告不会复制范围外的消息和轮次。使用 `--exclude-messages` 可排除全部消息及由消息生成的会话标题。报告还可能包含本地来源元数据，因此应将生成的 HTML 视为敏感文件，未经明确授权不要公开。

## 开发

```powershell
python -m unittest discover -s tests -v
```

仓库只包含合成 rollout 测试数据；生成的 HTML、真实 rollout JSONL、本地会话和工作区输出均已忽略。

## 许可证

MIT
