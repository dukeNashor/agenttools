# Agent Tools

Reusable Codex skills and scripts. This repository is packaged as a Codex plugin and currently contains one explicitly invoked skill: `$visualize-codex-tokens`.

## Visualize Codex Tokens

Generate a self-contained Chinese interactive HTML report for one local Codex thread or rollout JSONL file. The report includes:

- per-turn mutually exclusive token composition;
- total and cumulative token usage;
- linear and logarithmic charts;
- a dual-handle turn-range slider;
- Excel-like conditional formatting in the details table;
- status filters, full-text search, and turn details.

The parser uses cumulative `total_token_usage` deltas between `task_started` and matching `task_complete` or `turn_aborted` boundaries. It does not sum `last_token_usage`, because rollback-related records can repeat snapshots.

### Install

Ask Codex to install the skill from this repository with `$skill-installer`, or copy `skills/visualize-codex-tokens` into your user skill directory. The repository also includes `.codex-plugin/plugin.json` for plugin-compatible installation flows.

### Use

Invoke the skill explicitly:

```text
$visualize-codex-tokens generate a report for <thread-id>
```

Run the bundled script directly with Python 3.10 or newer:

```powershell
py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  <thread-id> `
  --strict
```

Without `--output`, the report is written to the stable system temporary path `agenttools/codex-token-<thread-id>.html`. Use `--open` to open it after generation.

### Privacy

Reports embed complete user messages by default. Use `--exclude-messages` to omit them. Reports can also contain local source metadata, so treat generated HTML as sensitive and do not publish it without explicit authorization.

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

为一个本地 Codex 线程或 rollout JSONL 生成自包含的中文交互式 HTML 报告，包括：

- 每轮互不重叠的 Token 构成；
- 总消耗与累计消耗；
- 线性／对数图表；
- 单轨双手柄轮次范围滑块；
- 类似 Excel 条件格式的轮次明细表；
- 状态筛选、全文搜索与轮次详情。

解析器使用 `task_started` 与匹配的 `task_complete` 或 `turn_aborted` 边界之间，累计 `total_token_usage` 的差值。它不会累加 `last_token_usage`，因为回滚相关记录可能重复旧快照。

### 安装

可让 Codex 使用 `$skill-installer` 从本仓库安装，也可以将 `skills/visualize-codex-tokens` 复制到用户 Skill 目录。仓库还提供 `.codex-plugin/plugin.json`，供支持 Plugin 的安装流程使用。

### 使用

显式调用 Skill：

```text
$visualize-codex-tokens 为 <线程ID> 生成报告
```

也可以使用 Python 3.10 或更高版本直接运行脚本：

```powershell
py -3 .\skills\visualize-codex-tokens\scripts\codex_token_visualizer.py `
  <线程ID> `
  --strict
```

未指定 `--output` 时，报告写入系统临时目录下的稳定路径 `agenttools/codex-token-<线程ID>.html`。需要生成后打开时使用 `--open`。

### 隐私

报告默认嵌入完整用户消息；使用 `--exclude-messages` 可排除消息。报告还可能包含本地来源元数据，因此应将生成的 HTML 视为敏感文件，未经明确授权不要公开。

## 开发

```powershell
python -m unittest discover -s tests -v
```

仓库只包含合成 rollout 测试数据；生成的 HTML、真实 rollout JSONL、本地会话和工作区输出均已忽略。

## 许可证

MIT
