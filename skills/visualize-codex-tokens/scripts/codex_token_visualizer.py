#!/usr/bin/env python3
"""Generate a self-contained interactive token report for a Codex rollout.

The parser attributes cumulative token counter deltas to Codex task turns.  It
uses task_started/task_complete/turn_aborted boundaries and deliberately does
not sum last_token_usage snapshots, because rollbacks can repeat snapshots.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.2.0"
THREAD_ID_RE = re.compile(
    r"(?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
USAGE_KEYS = (
    "input",
    "cached",
    "cache_write",
    "output",
    "reasoning",
    "total",
)


@dataclass(frozen=True)
class Usage:
    input: int = 0
    cached: int = 0
    cache_write: int = 0
    output: int = 0
    reasoning: int = 0
    total: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Usage":
        input_tokens = _as_nonnegative_int(payload.get("input_tokens"))
        output_tokens = _as_nonnegative_int(payload.get("output_tokens"))
        total_value = payload.get("total_tokens")
        total_tokens = (
            input_tokens + output_tokens
            if total_value is None
            else _as_nonnegative_int(total_value)
        )
        return cls(
            input=input_tokens,
            cached=_as_nonnegative_int(payload.get("cached_input_tokens")),
            cache_write=_as_nonnegative_int(
                payload.get("cache_write_input_tokens")
            ),
            output=output_tokens,
            reasoning=_as_nonnegative_int(payload.get("reasoning_output_tokens")),
            total=total_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {key: int(getattr(self, key)) for key in USAGE_KEYS}

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(**{key: getattr(self, key) + getattr(other, key) for key in USAGE_KEYS})

    def __sub__(self, other: "Usage") -> "Usage":
        return Usage(**{key: getattr(self, key) - getattr(other, key) for key in USAGE_KEYS})

    def clamp_nonnegative(self) -> "Usage":
        return Usage(**{key: max(0, getattr(self, key)) for key in USAGE_KEYS})


@dataclass
class WarningRecord:
    severity: str
    code: str
    message: str
    line: int | None = None
    turn_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "turnId": self.turn_id,
        }


@dataclass
class Turn:
    index: int
    turn_id: str
    started_at: str
    started_line: int
    start_usage: Usage
    end_usage: Usage
    status: str = "incomplete"
    ended_at: str | None = None
    duration_ms: int | None = None
    time_to_first_token_ms: int | None = None
    abort_reason: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    context_windows: list[int] = field(default_factory=list)
    token_snapshots: int = 0
    model_responses: int = 0
    compactions: int = 0
    warning_codes: list[str] = field(default_factory=list)

    def add_unique(self, attr: str, value: Any) -> None:
        if value is None or value == "":
            return
        items = getattr(self, attr)
        if value not in items:
            items.append(value)

    def usage_delta(self) -> Usage:
        return (self.end_usage - self.start_usage).clamp_nonnegative()

    def to_dict(self, cache_write_available: bool) -> dict[str, Any]:
        usage = self.usage_delta()
        breakdown, mismatch = _usage_breakdown(usage, cache_write_available)
        return {
            "index": self.index,
            "turnId": self.turn_id,
            "status": self.status,
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "durationMs": self.duration_ms,
            "timeToFirstTokenMs": self.time_to_first_token_ms,
            "abortReason": self.abort_reason,
            "messages": self.messages,
            "models": self.models,
            "efforts": self.efforts,
            "contextWindows": self.context_windows,
            "tokenSnapshots": self.token_snapshots,
            "modelResponses": self.model_responses,
            "compactions": self.compactions,
            "warnings": self.warning_codes,
            "usage": usage.to_dict(),
            "breakdown": breakdown,
            "breakdownMismatch": mismatch,
        }


class ParseFailure(RuntimeError):
    """Raised when --strict rejects an integrity warning."""


class UsageNormalizer:
    def __init__(self, warnings: list[WarningRecord]) -> None:
        self.warnings = warnings
        self.offset = Usage()
        self.previous_raw: Usage | None = None
        self.previous_logical = Usage()
        self.reset_count = 0

    def normalize(self, raw: Usage, line: int) -> tuple[Usage, Usage]:
        offsets = self.offset.to_dict()
        if self.previous_raw is not None:
            reset_fields: list[str] = []
            for key in USAGE_KEYS:
                if getattr(raw, key) < getattr(self.previous_raw, key):
                    offsets[key] += getattr(self.previous_raw, key)
                    reset_fields.append(key)
            if reset_fields:
                self.reset_count += 1
                self.warnings.append(
                    WarningRecord(
                        "error",
                        "counter_reset",
                        "以下累计 Token 计数发生回退："
                        + ", ".join(reset_fields)
                        + "；已开始新的逻辑计数分段。",
                        line=line,
                    )
                )
        self.offset = Usage(**offsets)
        logical = raw + self.offset
        delta = (logical - self.previous_logical).clamp_nonnegative()
        self.previous_raw = raw
        self.previous_logical = logical
        return logical, delta


def _as_nonnegative_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _extract_thread_id(text: str) -> str | None:
    match = THREAD_ID_RE.search(text)
    return match.group("id").lower() if match else None


def _usage_breakdown(
    usage: Usage, cache_write_available: bool
) -> tuple[dict[str, int], int]:
    cached = min(usage.cached, usage.input)
    remaining_input = max(0, usage.input - cached)
    cache_write = (
        min(usage.cache_write, remaining_input) if cache_write_available else 0
    )
    other_input = max(0, remaining_input - cache_write)
    reasoning = min(usage.reasoning, usage.output)
    ordinary_output = max(0, usage.output - reasoning)
    known_sum = cached + cache_write + other_input + ordinary_output + reasoning
    unclassified = max(0, usage.total - known_sum)
    mismatch = known_sum + unclassified - usage.total
    return (
        {
            "cachedInput": cached,
            "cacheWriteInput": cache_write,
            "otherNonCachedInput": other_input,
            "ordinaryOutput": ordinary_output,
            "reasoningOutput": reasoning,
            "unclassified": unclassified,
        },
        mismatch,
    )


def default_session_roots() -> list[Path]:
    codex_root = Path.home() / ".codex"
    roots = [codex_root / "sessions", codex_root / "archived_sessions"]
    return [path for path in roots if path.exists()]


def resolve_rollout(value: str, roots: Iterable[Path] | None = None) -> Path:
    supplied = Path(value).expanduser()
    if supplied.is_file():
        return supplied.resolve()
    if supplied.exists() and not supplied.is_file():
        raise FileNotFoundError(f"输入路径存在，但不是文件：{supplied}")

    thread_id = _extract_thread_id(value)
    if not thread_id:
        raise FileNotFoundError(
            f"输入不是 JSONL 文件，且未找到线程 ID：{value}"
        )

    search_roots = list(roots) if roots is not None else default_session_roots()
    candidates: list[Path] = []
    for root in search_roots:
        if not root.exists():
            continue
        candidates.extend(root.rglob(f"*{thread_id}*.jsonl"))
    exact = [path for path in candidates if path.name.lower().endswith(f"{thread_id}.jsonl")]
    choices = exact or candidates
    if not choices:
        rendered_roots = ", ".join(str(root) for root in search_roots) or "（无）"
        raise FileNotFoundError(
            f"未找到线程 {thread_id} 的 rollout JSONL。已搜索：{rendered_roots}"
        )
    return max(choices, key=lambda path: path.stat().st_mtime).resolve()


def parse_rollout(path: Path, requested_thread_id: str | None = None) -> dict[str, Any]:
    warnings: list[WarningRecord] = []
    normalizer = UsageNormalizer(warnings)
    latest_usage = Usage()
    turns: list[Turn] = []
    turns_by_id: dict[str, Turn] = {}
    current: Turn | None = None
    unattributed = Usage()
    session_meta: list[dict[str, Any]] = []
    orphan_messages: list[dict[str, Any]] = []
    malformed_lines = 0
    blank_lines = 0
    token_events = 0
    duplicate_snapshots = 0
    rollback_count = 0
    cache_write_field_present = False
    reasoning_field_present = False
    total_field_present = True

    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            stripped = raw_line.strip()
            if not stripped:
                blank_lines += 1
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                malformed_lines += 1
                # A line returned without a newline terminator is necessarily
                # the stream's current final line, including while a live
                # rollout is still being appended.
                trailing = not raw_line.endswith(("\n", "\r"))
                warnings.append(
                    WarningRecord(
                        "error",
                        "trailing_partial_line" if trailing else "malformed_json",
                        (
                            "已忽略末尾疑似未写完的 JSON 行。"
                            if trailing
                            else f"已忽略格式错误的 JSON：{exc.msg}。"
                        ),
                        line=line_number,
                    )
                )
                continue

            record_type = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                payload = {}

            if record_type == "session_meta":
                session_meta.append(payload)
                continue

            if record_type == "turn_context":
                turn_id = _coerce_text(payload.get("turn_id"))
                turn = turns_by_id.get(turn_id)
                if turn is None and current is not None and current.turn_id == turn_id:
                    turn = current
                if turn is not None:
                    turn.add_unique("models", payload.get("model"))
                    effort = payload.get("effort")
                    if effort is None:
                        mode = payload.get("collaboration_mode")
                        if isinstance(mode, dict):
                            settings = mode.get("settings")
                            if isinstance(settings, dict):
                                effort = settings.get("reasoning_effort")
                    turn.add_unique("efforts", effort)
                    context_window = payload.get("model_context_window")
                    if context_window is not None:
                        turn.add_unique(
                            "context_windows", _as_nonnegative_int(context_window)
                        )
                continue

            if record_type == "compacted":
                # event_msg/context_compacted is the canonical count; this top-level
                # companion event intentionally does not increment it again.
                continue

            if record_type != "event_msg":
                continue

            event_type = payload.get("type")
            timestamp = _coerce_text(record.get("timestamp"))

            if event_type == "task_started":
                if current is not None:
                    current.status = "incomplete"
                    current.ended_at = timestamp
                    current.warning_codes.append("nested_task_start")
                    warnings.append(
                        WarningRecord(
                            "error",
                            "nested_task_start",
                            "当前轮次尚未结束，又出现了新的 task_started 事件。",
                            line=line_number,
                            turn_id=current.turn_id,
                        )
                    )
                    turns.append(current)
                turn_id = _coerce_text(payload.get("turn_id")) or f"unknown-{len(turns) + 1}"
                current = Turn(
                    index=len(turns) + 1,
                    turn_id=turn_id,
                    started_at=timestamp,
                    started_line=line_number,
                    start_usage=latest_usage,
                    end_usage=latest_usage,
                )
                context_window = payload.get("model_context_window")
                if context_window is not None:
                    current.add_unique(
                        "context_windows", _as_nonnegative_int(context_window)
                    )
                turns_by_id[turn_id] = current
                continue

            if event_type == "user_message":
                message = {
                    "timestamp": timestamp,
                    "text": _coerce_text(payload.get("message")),
                    "clientId": _coerce_text(payload.get("client_id")) or None,
                    "imageCount": len(payload.get("images") or [])
                    + len(payload.get("local_images") or []),
                    "audioCount": len(payload.get("audio") or [])
                    + len(payload.get("local_audio") or []),
                }
                if current is None:
                    orphan_messages.append(message)
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "orphan_user_message",
                            "活动轮次之外出现了一条用户消息。",
                            line=line_number,
                        )
                    )
                else:
                    message["steering"] = bool(current.messages)
                    current.messages.append(message)
                continue

            if event_type == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    warnings.append(
                        WarningRecord(
                            "error",
                            "missing_token_info",
                            "token_count 不包含 info 对象。",
                            line=line_number,
                            turn_id=current.turn_id if current else None,
                        )
                    )
                    continue
                total_payload = info.get("total_token_usage")
                if not isinstance(total_payload, dict):
                    warnings.append(
                        WarningRecord(
                            "error",
                            "missing_total_usage",
                            "token_count 不包含 total_token_usage。",
                            line=line_number,
                            turn_id=current.turn_id if current else None,
                        )
                    )
                    continue
                cache_write_field_present = cache_write_field_present or (
                    "cache_write_input_tokens" in total_payload
                )
                reasoning_field_present = reasoning_field_present or (
                    "reasoning_output_tokens" in total_payload
                )
                total_field_present = total_field_present and ("total_tokens" in total_payload)
                raw_usage = Usage.from_payload(total_payload)
                logical_usage, event_delta = normalizer.normalize(raw_usage, line_number)
                if event_delta.total == 0:
                    duplicate_snapshots += 1
                token_events += 1
                latest_usage = logical_usage
                if current is not None:
                    current.end_usage = logical_usage
                    current.token_snapshots += 1
                    if event_delta.total > 0:
                        current.model_responses += 1
                    context_window = info.get("model_context_window")
                    if context_window is not None:
                        current.add_unique(
                            "context_windows", _as_nonnegative_int(context_window)
                        )
                elif event_delta.total > 0:
                    unattributed = unattributed + event_delta
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "unattributed_usage",
                            f"活动轮次之外增加了 {event_delta.total:,} 个 Token。",
                            line=line_number,
                        )
                    )
                continue

            if event_type in {"task_complete", "turn_aborted"}:
                terminal_id = _coerce_text(payload.get("turn_id"))
                if current is None:
                    warnings.append(
                        WarningRecord(
                            "error",
                            "orphan_task_terminal",
                            f"没有活动轮次时出现了 {event_type}。",
                            line=line_number,
                            turn_id=terminal_id or None,
                        )
                    )
                    continue
                if terminal_id and terminal_id != current.turn_id:
                    current.warning_codes.append("turn_id_mismatch")
                    warnings.append(
                        WarningRecord(
                            "error",
                            "turn_id_mismatch",
                            f"结束事件的轮次 ID {terminal_id} 与活动轮次 {current.turn_id} 不一致。",
                            line=line_number,
                            turn_id=current.turn_id,
                        )
                    )
                current.status = "complete" if event_type == "task_complete" else "aborted"
                current.ended_at = timestamp
                current.duration_ms = (
                    _as_nonnegative_int(payload.get("duration_ms"))
                    if payload.get("duration_ms") is not None
                    else None
                )
                current.time_to_first_token_ms = (
                    _as_nonnegative_int(payload.get("time_to_first_token_ms"))
                    if payload.get("time_to_first_token_ms") is not None
                    else None
                )
                current.abort_reason = (
                    _coerce_text(payload.get("reason")) or None
                    if event_type == "turn_aborted"
                    else None
                )
                turns.append(current)
                current = None
                continue

            if event_type == "context_compacted":
                if current is not None:
                    current.compactions += 1
                else:
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "orphan_compaction",
                            "活动轮次之外出现了上下文压缩事件。",
                            line=line_number,
                        )
                    )
                continue

            if event_type == "thread_rolled_back":
                rollback_count += 1
                if current is not None:
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "rollback_during_turn",
                            "活动轮次期间发生了线程回滚。",
                            line=line_number,
                            turn_id=current.turn_id,
                        )
                    )

    if current is not None:
        current.status = "incomplete"
        current.ended_at = None
        current.warning_codes.append("unclosed_turn")
        warnings.append(
            WarningRecord(
                "error",
                "unclosed_turn",
                "rollout 结束时仍有一个活动轮次未闭合。",
                turn_id=current.turn_id,
            )
        )
        turns.append(current)

    # Context events can precede finalization; make sure indices are stable.
    for index, turn in enumerate(turns, 1):
        turn.index = index

    filename_thread_id = _extract_thread_id(path.name)
    desired_id = (requested_thread_id or filename_thread_id or "").lower() or None
    selected_meta: dict[str, Any] = {}
    if desired_id:
        for candidate in session_meta:
            ids = {
                _coerce_text(candidate.get("id")).lower(),
                _coerce_text(candidate.get("session_id")).lower(),
            }
            if desired_id in ids:
                selected_meta = candidate
                break
    if not selected_meta and session_meta:
        selected_meta = session_meta[0]
    meta_id = _coerce_text(selected_meta.get("id") or selected_meta.get("session_id"))
    thread_id = desired_id or meta_id or path.stem

    turn_dicts = [turn.to_dict(cache_write_field_present) for turn in turns]
    turn_usage_sum = Usage()
    breakdown_mismatch_turns = 0
    for turn, turn_dict in zip(turns, turn_dicts):
        turn_usage_sum = turn_usage_sum + turn.usage_delta()
        if turn_dict["breakdownMismatch"] != 0:
            breakdown_mismatch_turns += 1

    accounted = turn_usage_sum + unattributed
    reconciliation = latest_usage - accounted
    if any(getattr(reconciliation, key) != 0 for key in USAGE_KEYS):
        warnings.append(
            WarningRecord(
                "error",
                "reconciliation_mismatch",
                "逐轮用量加未归属用量与最终累计计数不一致。",
            )
        )
    if breakdown_mismatch_turns:
        warnings.append(
            WarningRecord(
                "warning",
                "breakdown_mismatch",
                f"有 {breakdown_mismatch_turns} 个轮次的用量分项之和与 total_tokens 不完全一致。",
            )
        )
    if not total_field_present:
        warnings.append(
            WarningRecord(
                "warning",
                "derived_total_tokens",
                "至少一个快照缺少 total_tokens；已改用输入加输出计算。",
            )
        )
    if not cache_write_field_present:
        warnings.append(
            WarningRecord(
                "info",
                "cache_write_unavailable",
                "该 rollout 格式不提供 cache_write_input_tokens。",
            )
        )

    final_breakdown, final_breakdown_mismatch = _usage_breakdown(
        latest_usage, cache_write_field_present
    )
    status_counts: dict[str, int] = {"complete": 0, "aborted": 0, "incomplete": 0}
    for turn in turns:
        status_counts[turn.status] = status_counts.get(turn.status, 0) + 1

    source_stat = path.stat()
    integrity_errors = sum(1 for warning in warnings if warning.severity == "error")
    return {
        "schemaVersion": 1,
        "generator": {"name": "codex_token_visualizer", "version": VERSION},
        "metadata": {
            "threadId": thread_id,
            "sourcePath": str(path),
            "sourceName": path.name,
            "sourceBytes": source_stat.st_size,
            "sourceModifiedAt": datetime.fromtimestamp(
                source_stat.st_mtime, tz=timezone.utc
            ).isoformat(),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "sessionMeta": {
                "cwd": selected_meta.get("cwd"),
                "originator": selected_meta.get("originator"),
                "cliVersion": selected_meta.get("cli_version"),
                "forkedFromId": selected_meta.get("forked_from_id"),
                "parentThreadId": selected_meta.get("parent_thread_id"),
            },
            "containsFullUserMessages": True,
            "cacheWriteFieldAvailable": cache_write_field_present,
            "reasoningFieldAvailable": reasoning_field_present,
        },
        "summary": {
            "turnCount": len(turns),
            "statusCounts": status_counts,
            "zeroUsageTurns": sum(1 for turn in turns if turn.usage_delta().total == 0),
            "tokenEvents": token_events,
            "duplicateSnapshots": duplicate_snapshots,
            "rollbacks": rollback_count,
            "contextCompactions": sum(turn.compactions for turn in turns),
            "malformedLines": malformed_lines,
            "blankLines": blank_lines,
            "orphanMessageCount": len(orphan_messages),
            "counterResets": normalizer.reset_count,
            "finalUsage": latest_usage.to_dict(),
            "finalBreakdown": final_breakdown,
            "finalBreakdownMismatch": final_breakdown_mismatch,
            "turnUsageSum": turn_usage_sum.to_dict(),
            "unattributedUsage": unattributed.to_dict(),
            "accountedUsage": accounted.to_dict(),
            "reconciliationDifference": reconciliation.to_dict(),
            "integrityErrorCount": integrity_errors,
            "warningCount": len(warnings),
        },
        "warnings": [warning.to_dict() for warning in warnings],
        "orphanMessages": orphan_messages,
        "turns": turn_dicts,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>__PAGE_TITLE__</title>
<style>
:root {
  --bg: #f5f2ec;
  --panel: #fffefa;
  --panel-2: #f6f0e6;
  --text: #2d2924;
  --muted: #756e64;
  --border: #ddd5c9;
  --accent: #3b8b78;
  --accent-2: #bd7556;
  --danger: #c95561;
  --warning: #b77a26;
  --cached: #4f9d87;
  --cache-write: #8c78bd;
  --uncached: #d9874c;
  --output: #dca83e;
  --reasoning: #cf6f78;
  --unclassified: #928a80;
  --shadow: 0 16px 42px rgba(92, 75, 54, .12);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    radial-gradient(circle at 8% -10%, rgba(87, 166, 141, .16), transparent 35rem),
    radial-gradient(circle at 92% 0%, rgba(221, 167, 107, .13), transparent 30rem),
    var(--bg);
  color: var(--text);
  font: 14px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
button, input, select { font: inherit; }
button, select, input[type="search"] {
  color: var(--text); background: #fffefa; border: 1px solid var(--border);
  border-radius: 9px; padding: 8px 10px;
}
button { cursor: pointer; }
button:hover, button.active { border-color: var(--accent); color: var(--accent); }
main { max-width: 1680px; margin: 0 auto; padding: 30px clamp(16px, 3vw, 44px) 70px; }
.hero { display:flex; justify-content:space-between; gap:24px; align-items:flex-start; margin-bottom:22px; }
.eyebrow { color: var(--accent); text-transform:uppercase; letter-spacing:.14em; font-size:12px; font-weight:800; }
h1 { font-size: clamp(28px, 4vw, 48px); line-height:1.08; margin:7px 0 10px; letter-spacing:-.035em; }
.subline { color:var(--muted); max-width:920px; overflow-wrap:anywhere; }
.sensitive { color:#984b55; background:#fae9e8; border:1px solid #e8c4c2; border-radius:999px; padding:7px 11px; font-size:12px; white-space:nowrap; }
.sensitive.safe { color:#3f765f; background:#e8f3eb; border-color:#c3ddcb; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(172px,1fr)); gap:12px; margin:20px 0; }
.card, .panel { background:linear-gradient(180deg, rgba(255,254,250,.98), rgba(252,249,243,.98)); border:1px solid var(--border); box-shadow:var(--shadow); }
.card { border-radius:14px; padding:16px; min-height:105px; }
.card .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.07em; }
.card .value { margin-top:8px; font-size:clamp(22px,2.3vw,32px); font-variant-numeric:tabular-nums; font-weight:750; letter-spacing:-.025em; }
.card .note { color:var(--muted); font-size:12px; margin-top:2px; }
.panel { border-radius:16px; margin-top:16px; overflow:hidden; }
.panel-head { padding:18px 20px 12px; display:flex; gap:16px; align-items:flex-start; justify-content:space-between; flex-wrap:wrap; }
.panel-head h2 { margin:0; font-size:18px; }
.panel-head p { margin:4px 0 0; color:var(--muted); }
.controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
.controls label { color:var(--muted); font-size:12px; }
.range-row { padding:0 20px 16px; display:flex; gap:14px; align-items:center; }
.range-row > label { color:var(--muted); font-size:12px; white-space:nowrap; }
.dual-range-shell { flex:1; min-width:220px; }
.dual-range-values { display:flex; justify-content:flex-end; align-items:center; gap:7px; min-height:21px; color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
.dual-range-values output { color:var(--text); font-weight:750; min-width:24px; text-align:center; }
.dual-range { position:relative; height:28px; }
.dual-range-track, .dual-range-fill { position:absolute; left:0; right:0; top:13px; height:4px; border-radius:999px; }
.dual-range-track { background:#ddd5c9; }
.dual-range-fill { right:auto; background:linear-gradient(90deg,var(--accent),#69ad93); box-shadow:0 0 0 1px rgba(59,139,120,.12); }
.dual-range input[type="range"] { position:absolute; inset:0; width:100%; height:28px; margin:0; appearance:none; -webkit-appearance:none; background:transparent; pointer-events:none; }
.dual-range input[type="range"]::-webkit-slider-runnable-track { height:4px; background:transparent; }
.dual-range input[type="range"]::-webkit-slider-thumb { width:18px; height:18px; margin-top:-7px; border:3px solid #fffefa; border-radius:50%; appearance:none; -webkit-appearance:none; background:var(--accent); box-shadow:0 1px 5px rgba(65,55,43,.3); pointer-events:auto; cursor:grab; }
.dual-range input[type="range"]::-moz-range-track { height:4px; background:transparent; }
.dual-range input[type="range"]::-moz-range-thumb { width:14px; height:14px; border:3px solid #fffefa; border-radius:50%; background:var(--accent); box-shadow:0 1px 5px rgba(65,55,43,.3); pointer-events:auto; cursor:grab; }
#range-start { z-index:3; }
#range-end { z-index:4; }
.filter-row { padding:0 20px 15px; display:flex; gap:12px; align-items:center; flex-wrap:wrap; color:var(--muted); }
.filter-row input[type="search"] { min-width:min(100%,320px); flex:1; }
.check { display:inline-flex; gap:5px; align-items:center; }
.legend { display:flex; gap:13px; flex-wrap:wrap; color:var(--muted); font-size:12px; }
.legend span::before { content:""; width:9px; height:9px; display:inline-block; margin-right:5px; border-radius:3px; background:var(--swatch); }
.chart-scroll { overflow-x:auto; border-top:1px solid rgba(126,111,91,.25); }
.chart-wrap { position:relative; min-height:430px; padding:8px 12px 4px; }
svg { display:block; width:100%; height:auto; overflow:visible; }
.axis text { fill:var(--muted); font-size:11px; }
.axis line, .grid-line { stroke:rgba(117,110,100,.2); shape-rendering:crispEdges; }
.bar { cursor:pointer; transition:opacity .14s ease, filter .14s ease; }
.bar:hover, .bar.selected { filter:brightness(1.22); }
.bar.dim { opacity:.28; }
.tooltip { position:fixed; z-index:50; pointer-events:none; display:none; max-width:360px; padding:11px 12px; border-radius:10px; background:#fffefa; border:1px solid var(--border); box-shadow:var(--shadow); color:var(--text); font-size:12px; }
.tooltip strong { display:block; margin-bottom:5px; }
.tooltip .row { display:flex; justify-content:space-between; gap:20px; color:var(--muted); }
.tooltip .row b { color:var(--text); font-variant-numeric:tabular-nums; }
.cumulative-wrap { padding:4px 18px 14px; }
.warning-box { margin:16px 0; border-radius:14px; border:1px solid var(--border); overflow:hidden; }
.warning-box summary { cursor:pointer; padding:13px 16px; background:rgba(247,198,107,.08); color:var(--warning); font-weight:700; }
.warning-list { margin:0; padding:8px 16px 14px 36px; max-height:300px; overflow:auto; }
.warning-list li { margin:6px 0; color:var(--muted); }
.warning-list li.error { color:#a74450; }
.warning-list li.info { color:#5b6f59; }
.table-wrap { overflow:auto; max-height:720px; border-top:1px solid var(--border); }
table { border-collapse:separate; border-spacing:0; width:100%; min-width:1320px; }
th { position:sticky; top:0; z-index:2; background:#eee7dc; color:#5f584f; text-align:right; padding:10px 11px; border-bottom:1px solid var(--border); font-size:11px; letter-spacing:.04em; text-transform:uppercase; cursor:pointer; }
th:first-child, th:nth-child(2), th:nth-child(3), th:last-child { text-align:left; }
td { padding:9px 11px; border-bottom:1px solid rgba(126,111,91,.18); text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
td:first-child, td:nth-child(2), td:nth-child(3), td:last-child { text-align:left; }
tr { cursor:pointer; }
tbody tr:hover, tbody tr.selected { outline:1px solid rgba(59,139,120,.3); outline-offset:-1px; }
td.heat-cell { background-clip:padding-box; transition:filter .15s ease; }
tbody tr:hover td.heat-cell { filter:saturate(1.12) brightness(.985); }
.prompt-cell { max-width:440px; overflow:hidden; text-overflow:ellipsis; }
.status { display:inline-flex; align-items:center; gap:6px; text-transform:capitalize; }
.status::before { content:""; width:8px; height:8px; border-radius:50%; background:var(--accent); }
.status.aborted::before { background:var(--danger); }
.status.incomplete::before { background:var(--warning); }
.drawer { position:fixed; z-index:40; top:0; right:0; width:min(620px,94vw); height:100vh; transform:translateX(104%); transition:transform .22s ease; background:#fbf8f2; border-left:1px solid var(--border); box-shadow:-24px 0 60px rgba(92,75,54,.22); display:flex; flex-direction:column; }
.drawer.open { transform:translateX(0); }
.drawer-head { padding:20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; gap:12px; }
.drawer-head h2 { margin:0; }
.drawer-body { overflow:auto; padding:18px 20px 60px; }
.detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
.detail-item { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:10px; }
.detail-item span { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; }
.detail-item b { display:block; margin-top:4px; overflow-wrap:anywhere; }
.message { margin-top:14px; border:1px solid var(--border); border-radius:12px; overflow:hidden; }
.message-head { padding:8px 11px; background:var(--panel-2); color:var(--muted); font-size:12px; }
.message pre { margin:0; padding:13px; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text); font:13px/1.55 ui-monospace, SFMono-Regular, Consolas, monospace; }
.empty { color:var(--muted); padding:40px; text-align:center; }
.footer { color:var(--muted); text-align:center; margin-top:28px; font-size:12px; }
@media (max-width:720px) {
  .hero { flex-direction:column; }
  .range-row { align-items:flex-start; flex-direction:column; gap:4px; }
  .dual-range-shell { width:100%; }
  .detail-grid { grid-template-columns:1fr; }
}
</style>
</head>
<body>
<main>
  <section class="hero">
    <div>
      <div class="eyebrow">Codex Token 使用分析</div>
      <h1>线程逐轮消耗</h1>
      <div class="subline" id="thread-meta"></div>
    </div>
    <div class="sensitive" id="privacy-indicator"></div>
  </section>

  <section class="cards" id="summary-cards"></section>
  <details class="warning-box" id="warning-box">
    <summary id="warning-summary"></summary>
    <ul class="warning-list" id="warning-list"></ul>
  </details>

  <section class="panel">
    <div class="panel-head">
      <div><h2>每轮 Token 构成</h2><p>各色段互不重叠，柱形总高度等于该轮总消耗。</p></div>
      <div class="controls">
        <button id="linear-button" class="active" type="button">线性</button>
        <button id="log-button" type="button">对数</button>
        <button id="reset-button" type="button">重置筛选</button>
      </div>
    </div>
    <div class="range-row">
      <label for="range-start">轮次范围</label>
      <div class="dual-range-shell">
        <div class="dual-range-values"><output id="range-start-value"></output><span>—</span><output id="range-end-value"></output></div>
        <div class="dual-range">
          <div class="dual-range-track"></div><div class="dual-range-fill" id="range-fill"></div>
          <input id="range-start" type="range" aria-label="起始轮次">
          <input id="range-end" type="range" aria-label="结束轮次">
        </div>
      </div>
    </div>
    <div class="filter-row">
      <input id="search" type="search" placeholder="搜索完整用户消息、轮次 ID、模型……">
      <label class="check"><input type="checkbox" data-status="complete" checked> 已完成</label>
      <label class="check"><input type="checkbox" data-status="aborted" checked> 已中止</label>
      <label class="check"><input type="checkbox" data-status="incomplete" checked> 未闭合</label>
      <span id="visible-count"></span>
    </div>
    <div class="filter-row legend" id="legend"></div>
    <div class="chart-scroll"><div class="chart-wrap" id="turn-chart-wrap"><svg id="turn-chart" role="img" aria-label="每轮 Token 使用图表"></svg></div></div>
  </section>

  <section class="panel">
    <div class="panel-head"><div><h2>累计总消耗</h2><p>按任务轮次展示线性累计 Token 使用量。</p></div></div>
    <div class="cumulative-wrap"><svg id="cumulative-chart" role="img" aria-label="累计 Token 使用图表"></svg></div>
  </section>

  <section class="panel">
    <div class="panel-head"><div><h2>轮次明细</h2><p>数值列使用按列计算的条件格式；点击行或柱形可查看完整用户消息。</p></div></div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th data-sort="index">轮次</th><th data-sort="status">状态</th><th data-sort="startedAt">开始时间</th>
          <th data-sort="modelResponses">模型响应</th><th data-sort="cachedInput">缓存读取</th>
          <th data-sort="cacheWriteInput">缓存写入</th><th data-sort="otherNonCachedInput">其他输入</th>
          <th data-sort="ordinaryOutput">普通输出</th><th data-sort="reasoningOutput">推理输出</th>
          <th data-sort="total">总量</th><th data-sort="cacheRate">缓存率</th><th data-sort="prompt">用户消息</th>
        </tr></thead>
        <tbody id="turn-table-body"></tbody>
      </table>
      <div class="empty" id="table-empty" hidden>没有符合当前筛选条件的轮次。</div>
    </div>
  </section>
  <div class="footer" id="footer"></div>
</main>

<aside class="drawer" id="drawer" aria-hidden="true">
  <div class="drawer-head"><div><div class="eyebrow">轮次详情</div><h2 id="drawer-title"></h2></div><button id="drawer-close" type="button">关闭</button></div>
  <div class="drawer-body" id="drawer-body"></div>
</aside>
<div class="tooltip" id="tooltip"></div>
<script id="report-data" type="application/json">__REPORT_JSON__</script>
<script>
(() => {
  "use strict";
  const report = JSON.parse(document.getElementById("report-data").textContent);
  const turns = report.turns;
  const messagesIncluded = report.metadata.messagesIncluded !== false;
  const cacheWriteAvailable = report.metadata.cacheWriteFieldAvailable;
  const colors = {
    cachedInput: css("--cached"), cacheWriteInput: css("--cache-write"),
    otherNonCachedInput: css("--uncached"), ordinaryOutput: css("--output"),
    reasoningOutput: css("--reasoning"), unclassified: css("--unclassified")
  };
  const segmentLabels = {
    cachedInput: "缓存读取", cacheWriteInput: "缓存写入",
    otherNonCachedInput: cacheWriteAvailable ? "其他非缓存输入" : "非缓存输入（日志未提供写入明细）",
    ordinaryOutput: "普通输出", reasoningOutput: "推理输出", unclassified: "未分类调整"
  };
  const statusLabels = { complete: "已完成", aborted: "已中止", incomplete: "未闭合" };
  const segmentKeys = ["cachedInput", ...(cacheWriteAvailable ? ["cacheWriteInput"] : []), "otherNonCachedInput", "ordinaryOutput", "reasoningOutput", "unclassified"];
  const state = { scale: "linear", start: 1, end: Math.max(1, turns.length), search: "", statuses: new Set(["complete", "aborted", "incomplete"]), sort: "index", direction: 1, selected: null };
  const tooltip = byId("tooltip");

  function byId(id) { return document.getElementById(id); }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function esc(value) { return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[ch]); }
  function formatTokens(value) { return Number(value || 0).toLocaleString(); }
  function compact(value) {
    const n = Number(value || 0), abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(abs >= 1e10 ? 1 : 2).replace(/\.0+$/, "") + "B";
    if (abs >= 1e6) return (n / 1e6).toFixed(abs >= 1e7 ? 1 : 2).replace(/\.0+$/, "") + "M";
    if (abs >= 1e3) return (n / 1e3).toFixed(abs >= 1e4 ? 1 : 2).replace(/\.0+$/, "") + "K";
    return String(n);
  }
  function dateText(value) { if (!value) return "—"; const d = new Date(value); return Number.isNaN(d.valueOf()) ? value : d.toLocaleString("zh-CN"); }
  function durationText(ms) {
    if (ms == null) return "—"; const seconds = ms / 1000;
    if (seconds < 60) return seconds.toFixed(seconds < 10 ? 1 : 0) + " 秒";
    if (seconds < 3600) return (seconds / 60).toFixed(1) + " 分钟";
    return (seconds / 3600).toFixed(2) + " 小时";
  }
  function firstPrompt(turn) { return turn.messages.map(m => m.text).filter(Boolean).join("\n\n↳ 追加用户消息\n"); }
  function cacheRate(turn) { return turn.usage.input ? 100 * turn.usage.cached / turn.usage.input : 0; }
  function statusText(value) { return statusLabels[value] || value || "未知"; }
  function reconciliationOk() { return Object.values(report.summary.reconciliationDifference).every(v => Number(v) === 0); }

  function renderHeader() {
    byId("thread-meta").textContent = `${report.metadata.threadId} · ${report.summary.turnCount} 轮 · ${report.metadata.sourceName}`;
    const privacy = byId("privacy-indicator");
    privacy.textContent = messagesIncluded ? "包含完整用户消息" : "未包含用户消息";
    privacy.classList.toggle("safe", !messagesIncluded);
    byId("search").placeholder = messagesIncluded ? "搜索完整用户消息、轮次 ID、模型……" : "搜索轮次 ID、模型……";
    const u = report.summary.finalUsage;
    const cards = [
      ["累计总消耗", formatTokens(u.total), "全部模型响应的输入与输出之和"],
      ["输入", formatTokens(u.input), `${compact(u.cached)} 来自缓存读取`],
      ["非缓存输入", formatTokens(Math.max(0, u.input - u.cached)), `缓存命中率 ${(u.input ? 100*u.cached/u.input : 0).toFixed(2)}%`],
      ["输出", formatTokens(u.output), `${compact(u.reasoning)} 为推理输出`],
      ["轮次", formatTokens(report.summary.turnCount), `${report.summary.statusCounts.aborted || 0} 轮中止 · ${report.summary.zeroUsageTurns} 轮零消耗`],
      ["对账", reconciliationOk() ? "完全一致" : "存在差异", reconciliationOk() ? "逐轮总和与最终计数闭合" : "请查看完整性警告"]
    ];
    byId("summary-cards").innerHTML = cards.map(([label,value,note]) => `<article class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div><div class="note">${esc(note)}</div></article>`).join("");
    byId("footer").textContent = `生成时间：${dateText(report.metadata.generatedAt)} · 工具：${report.generator.name} ${report.generator.version} · 来源：${report.metadata.sourcePath}`;
  }

  function renderWarnings() {
    const list = byId("warning-list"), box = byId("warning-box");
    byId("warning-summary").textContent = `${report.summary.integrityErrorCount} 个完整性错误 · 共 ${report.summary.warningCount} 条提示`;
    if (!report.warnings.length) { box.hidden = true; return; }
    list.innerHTML = report.warnings.map(w => `<li class="${esc(w.severity)}"><b>${esc(w.code)}</b>${w.line ? ` · 第 ${w.line} 行` : ""}${w.turnId ? ` · ${esc(w.turnId)}` : ""}：${esc(w.message)}</li>`).join("");
    box.open = report.summary.integrityErrorCount > 0;
  }

  function configureControls() {
    const start = byId("range-start"), end = byId("range-end");
    start.min = end.min = 1; start.max = end.max = Math.max(1, turns.length); start.value = 1; end.value = Math.max(1, turns.length);
    function rangeChanged(which) {
      let a = Number(start.value), b = Number(end.value);
      if (a > b) { if (which === "start") b = a; else a = b; start.value = a; end.value = b; }
      state.start = a; state.end = b; renderAll();
    }
    start.addEventListener("pointerdown", () => { start.style.zIndex = "6"; end.style.zIndex = "4"; });
    end.addEventListener("pointerdown", () => { end.style.zIndex = "6"; start.style.zIndex = "3"; });
    start.addEventListener("input", () => rangeChanged("start")); end.addEventListener("input", () => rangeChanged("end"));
    byId("linear-button").addEventListener("click", () => setScale("linear"));
    byId("log-button").addEventListener("click", () => setScale("log"));
    byId("search").addEventListener("input", event => { state.search = event.target.value.trim().toLocaleLowerCase(); renderAll(); });
    document.querySelectorAll("[data-status]").forEach(input => input.addEventListener("change", () => {
      if (input.checked) state.statuses.add(input.dataset.status); else state.statuses.delete(input.dataset.status); renderAll();
    }));
    byId("reset-button").addEventListener("click", () => {
      state.start = 1; state.end = Math.max(1, turns.length); state.search = ""; state.statuses = new Set(["complete","aborted","incomplete"]); state.scale = "linear";
      start.value = 1; end.value = Math.max(1, turns.length); byId("search").value = "";
      document.querySelectorAll("[data-status]").forEach(input => input.checked = true); syncScaleButtons(); renderAll();
    });
    document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
      const key = th.dataset.sort; if (state.sort === key) state.direction *= -1; else { state.sort = key; state.direction = key === "index" ? 1 : -1; } renderTable(filteredTurns());
    }));
    byId("drawer-close").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", event => { if (event.key === "Escape") closeDrawer(); });
    renderLegend(); syncScaleButtons();
  }
  function setScale(scale) { state.scale = scale; syncScaleButtons(); renderTurnChart(filteredTurns()); }
  function syncScaleButtons() { byId("linear-button").classList.toggle("active", state.scale === "linear"); byId("log-button").classList.toggle("active", state.scale === "log"); }
  function renderLegend() { byId("legend").innerHTML = segmentKeys.filter(key => key !== "unclassified" || turns.some(t => t.breakdown.unclassified)).map(key => `<span style="--swatch:${colors[key]}">${esc(segmentLabels[key])}</span>`).join(""); }
  function updateRangeFill() {
    const start = byId("range-start"), end = byId("range-end"), fill = byId("range-fill");
    const min = Number(start.min), max = Number(start.max), span = Math.max(1, max-min);
    const left = 100*(Number(start.value)-min)/span, right = 100*(Number(end.value)-min)/span;
    fill.style.left = `${left}%`; fill.style.width = `${Math.max(0,right-left)}%`;
  }

  function filteredTurns() {
    return turns.filter(turn => {
      if (turn.index < state.start || turn.index > state.end || !state.statuses.has(turn.status)) return false;
      if (!state.search) return true;
      const haystack = [turn.turnId, turn.status, turn.models.join(" "), turn.efforts.join(" "), firstPrompt(turn)].join(" ").toLocaleLowerCase();
      return haystack.includes(state.search);
    });
  }
  function renderAll() {
    byId("range-start-value").textContent = state.start; byId("range-end-value").textContent = state.end; updateRangeFill();
    const visible = filteredTurns(); byId("visible-count").textContent = `当前显示 ${visible.length} 轮`;
    renderTurnChart(visible); renderTable(visible); renderCumulative();
  }

  function svgEl(name, attrs = {}) { const el = document.createElementNS("http://www.w3.org/2000/svg", name); Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, v)); return el; }
  function clearSvg(svg) { while (svg.firstChild) svg.removeChild(svg.firstChild); }
  function addText(svg, x, y, text, anchor = "end") { const node = svgEl("text", {x,y,"text-anchor":anchor,fill:css("--muted"),"font-size":"11"}); node.textContent = text; svg.appendChild(node); }
  function niceTicks(max, count = 5) {
    if (max <= 0) return [0]; const rough = max / count; const power = 10 ** Math.floor(Math.log10(rough)); const fraction = rough / power;
    const nice = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power; const result = [];
    for (let n = 0; n <= max + nice * .25; n += nice) result.push(n); return result;
  }
  function showTooltip(event, turn) {
    const b = turn.breakdown;
    tooltip.innerHTML = `<strong>第 ${turn.index} 轮 · ${esc(statusText(turn.status))}</strong>` + segmentKeys.filter(key => b[key]).map(key => `<div class="row"><span>${esc(segmentLabels[key])}</span><b>${formatTokens(b[key])}</b></div>`).join("") + `<div class="row"><span>总量</span><b>${formatTokens(turn.usage.total)}</b></div><div style="margin-top:6px;color:var(--muted);max-height:48px;overflow:hidden">${esc(firstPrompt(turn) || "未记录用户消息")}</div>`;
    tooltip.style.display = "block"; const pad = 14; let x = event.clientX + pad, y = event.clientY + pad;
    if (x + 370 > innerWidth) x = event.clientX - 370; if (y + tooltip.offsetHeight > innerHeight) y = event.clientY - tooltip.offsetHeight - pad;
    tooltip.style.left = `${Math.max(8,x)}px`; tooltip.style.top = `${Math.max(8,y)}px`;
  }
  function hideTooltip() { tooltip.style.display = "none"; }

  function renderTurnChart(visible) {
    const svg = byId("turn-chart"); clearSvg(svg);
    const width = Math.max(940, visible.length * 24 + 100), height = 420, margin = {top:18,right:20,bottom:48,left:76}, innerH = height-margin.top-margin.bottom, innerW = width-margin.left-margin.right;
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.style.minWidth = `${width}px`;
    if (!visible.length) { addText(svg, width/2, height/2, "没有符合当前筛选条件的轮次", "middle"); return; }
    const maxTotal = Math.max(...visible.map(t => t.usage.total), 1);
    const scaled = value => state.scale === "log" ? Math.log10(1 + value) / Math.log10(1 + maxTotal) : value / maxTotal;
    const ticks = state.scale === "log" ? [0, ...Array.from({length:Math.floor(Math.log10(maxTotal))+1}, (_,i) => 10**i).filter(v => v <= maxTotal)] : niceTicks(maxTotal);
    ticks.forEach(tick => { const y = margin.top + innerH * (1-scaled(tick)); svg.appendChild(svgEl("line", {x1:margin.left,x2:width-margin.right,y1:y,y2:y,class:"grid-line"})); addText(svg, margin.left-9, y+4, compact(tick)); });
    const step = innerW / visible.length, barW = Math.max(3, Math.min(18, step*.72));
    visible.forEach((turn, position) => {
      const x = margin.left + position*step + (step-barW)/2, total = Math.max(0,turn.usage.total), totalHeight = innerH*scaled(total);
      let y = margin.top + innerH;
      const group = svgEl("g", {class:`bar${state.selected===turn.turnId?" selected":""}`, tabindex:"0", role:"button", "aria-label":`第 ${turn.index} 轮，${formatTokens(total)} Token`});
      segmentKeys.forEach(key => { const value = Math.max(0,turn.breakdown[key]||0); if (!value || !total) return; const h = totalHeight * value / Math.max(total,1); y -= h; group.appendChild(svgEl("rect", {x,y,width:barW,height:Math.max(.5,h),fill:colors[key],rx:"1"})); });
      group.addEventListener("mousemove", event => showTooltip(event,turn)); group.addEventListener("mouseleave",hideTooltip); group.addEventListener("focus", event => showTooltip(event,turn)); group.addEventListener("blur",hideTooltip); group.addEventListener("click",() => openDrawer(turn));
      svg.appendChild(group);
      if (visible.length <= 45 || position % Math.ceil(visible.length/30) === 0) addText(svg,x+barW/2,height-23,String(turn.index),"middle");
    });
    addText(svg, margin.left+innerW/2, height-5, "轮次序号", "middle");
    const axisLabel = svgEl("text", {x:15,y:margin.top+innerH/2,fill:css("--muted"),"font-size":"11",transform:`rotate(-90 15 ${margin.top+innerH/2})`,"text-anchor":"middle"}); axisLabel.textContent = `Token（${state.scale === "log" ? "对数" : "线性"}）`; svg.appendChild(axisLabel);
  }

  function renderCumulative() {
    const svg = byId("cumulative-chart"); clearSvg(svg); if (!turns.length) return;
    const width=1200,height=260,margin={top:16,right:24,bottom:34,left:76},innerW=width-margin.left-margin.right,innerH=height-margin.top-margin.bottom;
    svg.setAttribute("viewBox",`0 0 ${width} ${height}`); let cumulative=0; const points=turns.map((turn,i)=>{cumulative+=turn.usage.total;return {x:margin.left+(turns.length===1?0:i*innerW/(turns.length-1)),y:margin.top+innerH*(1-cumulative/Math.max(report.summary.turnUsageSum.total,1)),value:cumulative,turn};});
    niceTicks(report.summary.turnUsageSum.total,4).forEach(tick=>{const y=margin.top+innerH*(1-tick/Math.max(report.summary.turnUsageSum.total,1));svg.appendChild(svgEl("line",{x1:margin.left,x2:width-margin.right,y1:y,y2:y,class:"grid-line"}));addText(svg,margin.left-9,y+4,compact(tick));});
    const path=points.map((p,i)=>`${i?"L":"M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" "); svg.appendChild(svgEl("path",{d:path,fill:"none",stroke:css("--accent"),"stroke-width":"2.5"}));
    const area=`${path} L${points.at(-1).x},${margin.top+innerH} L${points[0].x},${margin.top+innerH} Z`; svg.insertBefore(svgEl("path",{d:area,fill:"rgba(59,139,120,.09)"}),svg.firstChild);
    const overlay=svgEl("rect",{x:margin.left,y:margin.top,width:innerW,height:innerH,fill:"transparent",cursor:"crosshair"}); overlay.addEventListener("mousemove",event=>{const rect=svg.getBoundingClientRect(),localX=(event.clientX-rect.left)*width/rect.width,idx=Math.max(0,Math.min(points.length-1,Math.round((localX-margin.left)/innerW*(points.length-1))));const p=points[idx];showTooltip(event,{...p.turn,breakdown:{},usage:{total:p.value},status:`累计至第 ${p.turn.index} 轮`});});overlay.addEventListener("mouseleave",hideTooltip);svg.appendChild(overlay);
  }

  function sortValue(turn,key) {
    if (key in turn.breakdown) return turn.breakdown[key]; if (key in turn.usage) return turn.usage[key];
    if (key === "cacheRate") return cacheRate(turn); if (key === "prompt") return firstPrompt(turn).toLocaleLowerCase(); return turn[key] ?? "";
  }
  function numericMax(items, getter) { return items.reduce((max, item) => Math.max(max, Number(getter(item)) || 0), 1); }
  function heatStyle(value, max, highIsGood = false) {
    const normalized = Math.log1p(Math.max(0, Number(value) || 0)) / Math.log1p(Math.max(1, Number(max) || 1));
    const intensity = Math.max(0, Math.min(1, highIsGood ? 1-normalized : normalized));
    const hue = 122 * (1-intensity), saturation = 58 + 8*intensity, lightness = 95 - 3*intensity;
    return `background-color:hsl(${hue.toFixed(1)} ${saturation.toFixed(1)}% ${lightness.toFixed(1)}%)`;
  }
  function renderTable(visible) {
    const sorted=[...visible].sort((a,b)=>{const av=sortValue(a,state.sort),bv=sortValue(b,state.sort);return (typeof av==="number"&&typeof bv==="number"?av-bv:String(av).localeCompare(String(bv)))*state.direction;});
    const maxima={
      modelResponses:numericMax(visible,t=>t.modelResponses), cachedInput:numericMax(visible,t=>t.breakdown.cachedInput),
      cacheWriteInput:numericMax(visible,t=>t.breakdown.cacheWriteInput), otherNonCachedInput:numericMax(visible,t=>t.breakdown.otherNonCachedInput),
      ordinaryOutput:numericMax(visible,t=>t.breakdown.ordinaryOutput), reasoningOutput:numericMax(visible,t=>t.breakdown.reasoningOutput),
      total:numericMax(visible,t=>t.usage.total)
    };
    byId("table-empty").hidden=sorted.length>0;
    byId("turn-table-body").innerHTML=sorted.map(turn=>{const b=turn.breakdown,prompt=firstPrompt(turn).replace(/\s+/g," ").trim(),rate=cacheRate(turn);return `<tr data-turn-id="${esc(turn.turnId)}" class="${state.selected===turn.turnId?"selected":""}"><td>${turn.index}</td><td><span class="status ${esc(turn.status)}">${esc(statusText(turn.status))}</span></td><td>${esc(dateText(turn.startedAt))}</td><td class="heat-cell" style="${heatStyle(turn.modelResponses,maxima.modelResponses)}">${formatTokens(turn.modelResponses)}</td><td class="heat-cell" style="${heatStyle(b.cachedInput,maxima.cachedInput)}">${formatTokens(b.cachedInput)}</td><td${cacheWriteAvailable?` class="heat-cell" style="${heatStyle(b.cacheWriteInput,maxima.cacheWriteInput)}"`:""}>${cacheWriteAvailable?formatTokens(b.cacheWriteInput):"不适用"}</td><td class="heat-cell" style="${heatStyle(b.otherNonCachedInput,maxima.otherNonCachedInput)}">${formatTokens(b.otherNonCachedInput)}</td><td class="heat-cell" style="${heatStyle(b.ordinaryOutput,maxima.ordinaryOutput)}">${formatTokens(b.ordinaryOutput)}</td><td class="heat-cell" style="${heatStyle(b.reasoningOutput,maxima.reasoningOutput)}">${formatTokens(b.reasoningOutput)}</td><td class="heat-cell" style="${heatStyle(turn.usage.total,maxima.total)}"><b>${formatTokens(turn.usage.total)}</b></td><td class="heat-cell" style="${heatStyle(rate,100,true)}">${rate.toFixed(2)}%</td><td class="prompt-cell" title="${esc(prompt)}">${esc(prompt||"—")}</td></tr>`;}).join("");
    byId("turn-table-body").querySelectorAll("tr").forEach(row=>row.addEventListener("click",()=>openDrawer(turns.find(t=>t.turnId===row.dataset.turnId))));
  }

  function openDrawer(turn) {
    if (!turn) return; state.selected=turn.turnId; byId("drawer-title").textContent=`第 ${turn.index} 轮`; const b=turn.breakdown;
    const details=[["状态",statusText(turn.status)],["轮次 ID",turn.turnId],["开始时间",dateText(turn.startedAt)],["持续时间",durationText(turn.durationMs)],["模型",turn.models.join(", ")||"—"],["推理强度",turn.efforts.join(", ")||"—"],["模型响应",formatTokens(turn.modelResponses)],["Token 快照",formatTokens(turn.tokenSnapshots)],["上下文压缩",formatTokens(turn.compactions)],["总 Token",formatTokens(turn.usage.total)],["输入",formatTokens(turn.usage.input)],["输出",formatTokens(turn.usage.output)]];
    let body=`<div class="detail-grid">${details.map(([k,v])=>`<div class="detail-item"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
    body+=`<div class="message"><div class="message-head">Token 构成</div><pre>${segmentKeys.map(key=>`${segmentLabels[key]}：${formatTokens(b[key]||0)}`).join("\n")}</pre></div>`;
    if (turn.messages.length) body+=turn.messages.map((m,i)=>`<section class="message"><div class="message-head">${i===0?"初始用户消息":"追加用户消息"} · ${esc(dateText(m.timestamp))}${m.imageCount?` · ${m.imageCount} 张图片`:""}${m.audioCount?` · ${m.audioCount} 条音频`:""}</div><pre></pre></section>`).join("");
    else body+=`<div class="message"><div class="message-head">用户消息</div><pre>${messagesIncluded?"该轮未记录用户消息。":"生成报告时已排除用户消息。"}</pre></div>`;
    byId("drawer-body").innerHTML=body;
    byId("drawer-body").querySelectorAll("section.message pre").forEach((pre,i)=>{pre.textContent=turn.messages[i].text;});
    byId("drawer").classList.add("open"); byId("drawer").setAttribute("aria-hidden","false"); renderTurnChart(filteredTurns()); renderTable(filteredTurns());
  }
  function closeDrawer() { state.selected=null; byId("drawer").classList.remove("open"); byId("drawer").setAttribute("aria-hidden","true"); renderTurnChart(filteredTurns()); renderTable(filteredTurns()); }

  try { renderHeader(); renderWarnings(); configureControls(); renderAll(); document.body.dataset.reportReady="true"; }
  catch (error) { document.body.dataset.reportReady="error"; const pre=document.createElement("pre");pre.className="empty";pre.textContent=`报告渲染失败：${error.stack||error}`;document.body.prepend(pre);console.error(error); }
})();
</script>
</body>
</html>
"""


def render_html(report: dict[str, Any], title: str | None = None) -> str:
    thread_id = report.get("metadata", {}).get("threadId", "unknown")
    page_title = title or f"Codex Token 报告 · {thread_id}"
    report_json = json.dumps(
        report, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )
    # Script-data escaping prevents a user message containing </script> from
    # terminating the JSON block. The JS renderer also uses textContent for full
    # messages and HTML-escapes all short previews.
    report_json = (
        report_json.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return HTML_TEMPLATE.replace("__PAGE_TITLE__", html.escape(page_title)).replace(
        "__REPORT_JSON__", report_json
    )


def set_message_policy(report: dict[str, Any], include_messages: bool) -> None:
    """Apply the report's explicit message-embedding policy in place."""
    report.setdefault("metadata", {})["messagesIncluded"] = include_messages
    report["metadata"]["containsFullUserMessages"] = include_messages
    if include_messages:
        return
    for turn in report.get("turns", []):
        turn["messages"] = []
    report["orphanMessages"] = []


def default_output_path(thread_id: str) -> Path:
    safe_id = re.sub(r"[^0-9A-Za-z._-]+", "-", thread_id).strip(".-") or "thread"
    return Path(tempfile.gettempdir()) / "agenttools" / f"codex-token-{safe_id}.html"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为一个 Codex 线程生成自包含的交互式 Token 报告。"
    )
    parser.add_argument(
        "thread",
        help="Codex 线程 ID，或明确的 rollout .jsonl 文件路径。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 HTML 路径（默认：系统临时目录/agenttools/codex-token-<线程ID>.html）。",
    )
    parser.add_argument(
        "--sessions-root",
        action="append",
        type=Path,
        help="新增或替代要搜索的会话根目录；可重复指定。",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="发现完整性错误时失败，且不写入 HTML。",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="在默认浏览器中打开生成的报告。",
    )
    parser.add_argument(
        "--exclude-messages",
        action="store_true",
        help="不在 HTML 中嵌入用户消息。默认会嵌入完整用户消息。",
    )
    parser.add_argument("--title", help="自定义 HTML 页面标题。")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = [path.expanduser().resolve() for path in args.sessions_root] if args.sessions_root else None
    requested_id = _extract_thread_id(args.thread)
    try:
        rollout = resolve_rollout(args.thread, roots)
        report = parse_rollout(rollout, requested_thread_id=requested_id)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    error_count = report["summary"]["integrityErrorCount"]
    if args.strict and error_count:
        print(
            f"错误：严格模式发现 {error_count} 个完整性错误；未写入报告。",
            file=sys.stderr,
        )
        for warning in report["warnings"]:
            if warning["severity"] == "error":
                location = f" 第 {warning['line']} 行" if warning.get("line") else ""
                print(f"  - {warning['code']}{location}: {warning['message']}", file=sys.stderr)
        return 3

    set_message_policy(report, include_messages=not args.exclude_messages)
    thread_id = report["metadata"]["threadId"]
    output = args.output or default_output_path(thread_id)
    output = output.expanduser().resolve()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_html(report, args.title), encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"错误：无法写入 {output}：{exc}", file=sys.stderr)
        return 4

    summary = report["summary"]
    usage = summary["finalUsage"]
    print(f"线程：{thread_id}")
    print(f"轮次：{summary['turnCount']:,}")
    print(f"总 Token：{usage['total']:,}")
    print(f"输入：{usage['input']:,}（缓存读取：{usage['cached']:,}）")
    print(f"输出：{usage['output']:,}（推理输出：{usage['reasoning']:,}）")
    print(f"完整性错误：{summary['integrityErrorCount']:,}")
    print(f"用户消息：{'已嵌入' if report['metadata']['messagesIncluded'] else '已排除'}")
    print(f"报告：{output}")
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
