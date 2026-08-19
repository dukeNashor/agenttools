#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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
import time as monotonic_time
import webbrowser
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterable


VERSION = "2.18.0"

# The range report uses the official Codex token-based rate card.  Keep the
# effective date and the last verification timestamp next to the table so a
# future rate-card refresh has an explicit audit point in generated reports.
RATE_CARD_SOURCE = "https://help.openai.com/en/articles/20001106-codex-rate-card"
RATE_CARD_EFFECTIVE_DATE = "2026-07-30"
RATE_CARD_CHECKED_AT = "2026-08-19T02:11:22Z"
SOL_RATE_CARD = {"input": 125.0, "cached": 12.5, "output": 750.0}
CODEX_RATE_CARD = {
    "gpt-5.6-sol": {"label": "GPT-5.6 Sol", **SOL_RATE_CARD},
    "gpt-5.6-terra": {"label": "GPT-5.6 Terra", "input": 50.0, "cached": 5.0, "output": 300.0},
    "gpt-5.6-luna": {"label": "GPT-5.6 Luna", "input": 5.0, "cached": 0.5, "output": 30.0},
    "gpt-5.5": {"label": "GPT-5.5", **SOL_RATE_CARD},
    "gpt-5.5-cyber": {"label": "GPT-5.5 Cyber", "input": 312.5, "cached": 31.25, "output": 1875.0},
    "gpt-5.4": {"label": "GPT-5.4", "input": 62.5, "cached": 6.25, "output": 375.0},
    "gpt-5.4-mini": {"label": "GPT-5.4 mini", "input": 18.75, "cached": 1.875, "output": 113.0},
    "gpt-5.3-codex": {"label": "GPT-5.3 Codex", "input": 43.75, "cached": 4.375, "output": 350.0},
    "gpt-5.2": {"label": "GPT-5.2", "input": 43.75, "cached": 4.375, "output": 350.0},
    "gpt-image-2.0-image": {"label": "GPT-Image-2.0（图像）", "input": 200.0, "cached": 50.0, "output": 750.0},
    "gpt-image-2.0-text": {"label": "GPT-Image-2.0（文本）", "input": 125.0, "cached": 31.25, "output": 250.0},
}
PLAN_EXCLUDED_MODEL_KEYS = frozenset({"gpt-5-3-codex-spark"})

TOOL_SATELLITE_HIT_SCRIPT = """
<style>
.tool-envelope { stroke:var(--muted); stroke-width:2.5; stroke-dasharray:none; opacity:.38; }
.tool-satellite .tool-satellite-unknown { stroke:var(--tool-color,#6f8fb7); stroke-width:2.6; stroke-dasharray:none; stroke-linecap:round; opacity:.82; }
.tool-satellite .tool-satellite-connector { stroke:var(--tool-color,#6f8fb7); stroke-width:1.5; stroke-dasharray:none; stroke-linecap:round; opacity:.44; }
.tool-satellite:hover .tool-satellite-connector, .tool-satellite:focus .tool-satellite-connector, .tool-satellite.selected .tool-satellite-connector { stroke:var(--tool-color,#6f8fb7); stroke-width:2.7; stroke-dasharray:none; opacity:.96; }
</style>
<script>
(() => {
  const TOOL_COLORS = {
    "Computer Use":"#4f78a8", "Chrome Use / Browser Use":"#3b8b78", ImageGen:"#bd7556",
    "Exec Reasoning":"#9a8f84", "Shell / Terminal":"#8c78bd", "Code Interpreter":"#6d8c45",
    "Web Search":"#4f9d87", "File Search":"#d9874c", MCP:"#b35f79",
    "Function Calling":"#a56c3f", "其他工具":"#6f8fb7"
  };
  function decorateToolSatellite(group) {
    const label = (group.getAttribute("aria-label") || "").split(/[，,]/)[0];
    group.style.setProperty("--tool-color", TOOL_COLORS[label] || "#6f8fb7");
  }
  function installToolSatelliteHitTargets() {
    document.querySelectorAll(".tool-satellite").forEach(group => {
      decorateToolSatellite(group);
      if (group.querySelector(".tool-satellite-hit-line, .tool-satellite-hit-band")) return;
      group.querySelectorAll(".tool-satellite-connector, .tool-satellite-unknown, .token-sector").forEach(node => {
        const band = node.classList.contains("token-sector");
        const hit = node.cloneNode(false);
        hit.setAttribute("class", band ? "tool-satellite-hit-band" : "tool-satellite-hit-line");
        hit.setAttribute("aria-hidden", "true");
        hit.setAttribute("pointer-events", band ? "all" : "stroke");
        hit.setAttribute("vector-effect", "non-scaling-stroke");
        hit.setAttribute("stroke-width", "20");
        hit.setAttribute("stroke", "transparent");
        if (band) hit.setAttribute("fill", "transparent");
        group.appendChild(hit);
      });
    });
  }
  installToolSatelliteHitTargets();
  new MutationObserver(installToolSatelliteHitTargets).observe(document.body, {childList:true, subtree:true});
})();
</script>
"""
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

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "Usage":
        payload = payload or {}
        return cls(
            **{
                key: _as_nonnegative_int(
                    payload.get(
                        key,
                        payload.get("cacheWrite" if key == "cache_write" else key),
                    )
                )
                for key in USAGE_KEYS
            }
        )

    def to_dict(self) -> dict[str, int]:
        return {key: int(getattr(self, key)) for key in USAGE_KEYS}

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(**{key: getattr(self, key) + getattr(other, key) for key in USAGE_KEYS})

    def __sub__(self, other: "Usage") -> "Usage":
        return Usage(**{key: getattr(self, key) - getattr(other, key) for key in USAGE_KEYS})

    def clamp_nonnegative(self) -> "Usage":
        return Usage(**{key: max(0, getattr(self, key)) for key in USAGE_KEYS})


@dataclass(frozen=True)
class ContextSnapshot:
    """One Codex-recorded context occupancy snapshot."""

    tokens: int
    window_tokens: int | None
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        rate = (
            round(100 * self.tokens / self.window_tokens, 4)
            if self.window_tokens
            else None
        )
        return {
            "tokens": self.tokens,
            "windowTokens": self.window_tokens,
            "occupancyRate": rate,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ContextTimelinePoint:
    """One context snapshot placed on a turn-local cumulative Token axis."""

    snapshot: ContextSnapshot
    turn_token_offset: int
    range_turn_token_offset: int | None = None

    def to_dict(self, window: DateWindow | None = None) -> dict[str, Any]:
        return {
            **self.snapshot.to_dict(),
            "turnTokenOffset": (
                self.range_turn_token_offset
                if window is not None
                else self.turn_token_offset
            ),
        }


@dataclass
class ContextCompaction:
    timestamp: str
    before: ContextSnapshot | None = None
    after: ContextSnapshot | None = None
    turn_token_offset: int | None = None
    range_turn_token_offset: int | None = None

    def to_dict(self, window: DateWindow | None = None) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
            "turnTokenOffset": (
                self.range_turn_token_offset
                if window is not None
                else self.turn_token_offset
            ),
        }


@dataclass(frozen=True)
class DateWindow:
    """An inclusive local-date range represented as a half-open UTC interval."""

    start_date: date
    end_date: date
    start_utc: datetime
    end_utc: datetime
    timezone_label: str
    local_tz: tzinfo | None = field(default=None, repr=False, compare=False)

    @classmethod
    def for_dates(
        cls,
        start_date: date,
        end_date: date,
        local_tz: tzinfo | None = None,
    ) -> "DateWindow":
        if end_date < start_date:
            raise ValueError("结束日期不能早于开始日期。")
        start_naive = datetime.combine(start_date, time.min)
        end_naive = datetime.combine(end_date + timedelta(days=1), time.min)
        if local_tz is None:
            start_local = start_naive.astimezone()
            end_local = end_naive.astimezone()
        else:
            start_local = start_naive.replace(tzinfo=local_tz)
            end_local = end_naive.replace(tzinfo=local_tz)
        timezone_label = start_local.tzname() or str(start_local.tzinfo) or "本机时区"
        return cls(
            start_date=start_date,
            end_date=end_date,
            start_utc=start_local.astimezone(timezone.utc),
            end_utc=end_local.astimezone(timezone.utc),
            timezone_label=timezone_label,
            local_tz=local_tz,
        )

    def _local(self, value: datetime) -> datetime:
        if self.local_tz is None:
            return value.astimezone()
        return value.astimezone(self.local_tz)

    def contains_datetime(self, value: datetime) -> bool:
        return self.start_utc <= value.astimezone(timezone.utc) < self.end_utc

    def contains(self, timestamp: str) -> bool:
        parsed = _parse_timestamp(timestamp)
        return parsed is not None and self.contains_datetime(parsed)

    def local_date_text(self, timestamp: str) -> str | None:
        parsed = _parse_timestamp(timestamp)
        return self._local(parsed).date().isoformat() if parsed is not None else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "startDate": self.start_date.isoformat(),
            "endDate": self.end_date.isoformat(),
            "startUtc": self.start_utc.isoformat(),
            "endUtcExclusive": self.end_utc.isoformat(),
            "timezone": self.timezone_label,
            "attribution": "token_count_snapshot_time",
        }


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


TOOL_EVENT_TYPES = {
    "tool_call",
    "tool_use",
    "function_call",
    "custom_tool_call",
    "computer_call",
    "browser_call",
    "browser_use",
    "image_generation_call",
    "mcp_tool_call",
    "tool_result",
    "tool_output",
    "function_call_output",
    "custom_tool_call_output",
    "web_search_end",
    "mcp_tool_call_begin",
    "mcp_tool_call_end",
}


@dataclass
class ToolCall:
    """One observed tool invocation attached to a single turn."""

    sequence: int
    call_id: str | None
    name: str
    raw_name: str
    category: str
    timestamp: str
    provider: str | None = None
    semantic_tool: str | None = None
    classification_source: str = "raw"
    transport_wrapper: bool = False
    ended_at: str | None = None
    status: str = "observed"
    usage: Usage = field(default_factory=Usage)
    usage_reported: bool = False
    usage_known: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "callId": self.call_id,
            "name": self.name,
            "rawName": self.raw_name,
            "category": self.category,
            "provider": self.provider,
            "semanticTool": self.semantic_tool,
            "classificationSource": self.classification_source,
            "transportWrapper": self.transport_wrapper,
            "timestamp": self.timestamp,
            "endedAt": self.ended_at,
            "status": self.status,
            "usage": self.usage.to_dict(),
            "usageReported": self.usage_reported,
            "usageKnown": sorted(self.usage_known),
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
    outputs: list[dict[str, Any]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    efforts: list[str] = field(default_factory=list)
    context_windows: list[int] = field(default_factory=list)
    token_snapshots: int = 0
    model_responses: int = 0
    compactions: int = 0
    warning_codes: list[str] = field(default_factory=list)
    range_usage: Usage = field(default_factory=Usage)
    range_relevant: bool = False
    range_first_activity_at: str | None = None
    range_last_activity_at: str | None = None
    message_events: int = 0
    latest_context_snapshot: ContextSnapshot | None = None
    range_latest_context_snapshot: ContextSnapshot | None = None
    context_timeline: list[ContextTimelinePoint] = field(default_factory=list)
    context_compactions: list[ContextCompaction] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)

    def add_unique(self, attr: str, value: Any) -> None:
        if value is None or value == "":
            return
        items = getattr(self, attr)
        if value not in items:
            items.append(value)

    def add_output(self, timestamp: str, text: str, phase: Any = None) -> None:
        text = _coerce_text(text)
        if not text.strip():
            return
        entry = {
            "timestamp": timestamp,
            "text": text,
            "phase": _coerce_text(phase) or None,
        }
        if self.outputs and self.outputs[-1]["text"] == text:
            if entry["phase"] and not self.outputs[-1].get("phase"):
                self.outputs[-1]["phase"] = entry["phase"]
            self.outputs[-1]["timestamp"] = timestamp
            return
        self.outputs.append(entry)

    def usage_delta(self) -> Usage:
        return (self.end_usage - self.start_usage).clamp_nonnegative()

    def note_range_activity(self, timestamp: str) -> None:
        self.range_relevant = True
        if timestamp:
            if self.range_first_activity_at is None:
                self.range_first_activity_at = timestamp
            self.range_last_activity_at = timestamp

    def tool_usage(self) -> Usage:
        result = Usage()
        for call in self.tool_calls:
            if call.usage_reported:
                result = result + call.usage
        return result

    def tool_summary(self) -> dict[str, Any]:
        categories: dict[str, int] = {}
        for call in self.tool_calls:
            categories[call.category] = categories.get(call.category, 0) + 1
        return {
            "callCount": len(self.tool_calls),
            "reportedCallCount": sum(1 for call in self.tool_calls if call.usage_reported),
            "unknownCallCount": sum(1 for call in self.tool_calls if not call.usage_reported),
            "usage": self.tool_usage().to_dict(),
            "categories": categories,
        }

    def to_dict(
        self,
        cache_write_available: bool,
        window: DateWindow | None = None,
    ) -> dict[str, Any]:
        usage = self.range_usage if window is not None else self.usage_delta()
        breakdown, mismatch = _usage_breakdown(usage, cache_write_available)
        range_clipped = False
        if window is not None:
            started = _parse_timestamp(self.started_at)
            ended = _parse_timestamp(self.ended_at or "")
            range_clipped = bool(
                (started is not None and started < window.start_utc)
                or ended is None
                or (ended is not None and ended >= window.end_utc)
            )
        context_snapshot = (
            self.range_latest_context_snapshot
            if window is not None
            else self.latest_context_snapshot
        )
        if context_snapshot is None:
            context_payload = {
                "snapshotType": "unknown",
                "tokens": None,
                "windowTokens": None,
                "occupancyRate": None,
                "timestamp": None,
            }
        else:
            if window is None:
                snapshot_type = (
                    "turn_end"
                    if self.status in {"complete", "aborted"}
                    else "current_latest"
                )
            else:
                ended = _parse_timestamp(self.ended_at or "")
                ended_in_window = ended is not None and window.contains_datetime(ended)
                if self.status in {"complete", "aborted"} and ended_in_window:
                    snapshot_type = "turn_end"
                elif (
                    self.status == "incomplete"
                    and self.latest_context_snapshot == self.range_latest_context_snapshot
                ):
                    snapshot_type = "current_latest"
                else:
                    snapshot_type = "range_latest"
            context_payload = {
                "snapshotType": snapshot_type,
                **context_snapshot.to_dict(),
            }
        context_compactions: list[dict[str, Any]] = []
        for compaction in self.context_compactions:
            payload = compaction.to_dict(window=window)
            if window is not None:
                for side in ("before", "after"):
                    snapshot = payload.get(side)
                    if snapshot is not None and not window.contains(
                        _coerce_text(snapshot.get("timestamp"))
                    ):
                        payload[side] = None
            context_compactions.append(payload)
        context_timeline = []
        for point in self.context_timeline:
            if window is not None and not window.contains(point.snapshot.timestamp):
                continue
            payload = point.to_dict(window=window)
            if payload["turnTokenOffset"] is not None:
                context_timeline.append(payload)
        tool_calls = [call.to_dict() for call in self.tool_calls]
        tool_summary = self.tool_summary()
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
            "outputs": self.outputs,
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
            "rangeClipped": range_clipped,
            "rangeFirstActivityAt": self.range_first_activity_at,
            "rangeLastActivityAt": self.range_last_activity_at,
            "contextSnapshot": context_payload,
            "contextTimeline": context_timeline,
            "contextCompactions": context_compactions,
            "toolCalls": tool_calls,
            "toolSummary": tool_summary,
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


def _normalized_model_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _coerce_text(value).lower()).strip("-")


def _is_plan_excluded_model(value: Any) -> bool:
    return _normalized_model_name(value) in PLAN_EXCLUDED_MODEL_KEYS


def _turn_model_names(turn: dict[str, Any]) -> list[str]:
    names = []
    for value in turn.get("models", []):
        text_value = _coerce_text(value).strip()
        if text_value and text_value not in names:
            names.append(text_value)
    return names


def _model_rate_card(model: Any) -> tuple[str, dict[str, float], str]:
    """Return a display label, rate card, and confidence for one model name."""

    raw = _coerce_text(model).strip()
    normalized = _normalized_model_name(raw)
    aliases = {
        "gpt-5-6": "gpt-5.6-sol",
        "gpt-5-6-sol": "gpt-5.6-sol",
        "gpt-5-6-terra": "gpt-5.6-terra",
        "gpt-5-6-luna": "gpt-5.6-luna",
        "gpt-5-5": "gpt-5.5",
        "gpt-5-5-cyber": "gpt-5.5-cyber",
        "gpt-5-4": "gpt-5.4",
        "gpt-5-4-mini": "gpt-5.4-mini",
        "gpt-5-3-codex": "gpt-5.3-codex",
        "gpt-5-2": "gpt-5.2",
        "gpt-image-2-0-image": "gpt-image-2.0-image",
        "gpt-image-2-0-text": "gpt-image-2.0-text",
    }
    key = aliases.get(normalized)
    if key is None:
        key = next(
            (candidate for candidate in CODEX_RATE_CARD if normalized == _normalized_model_name(candidate)),
            None,
        )
    if key is None:
        return raw or "未知模型", dict(SOL_RATE_CARD), "unconfigured"
    card = CODEX_RATE_CARD[key]
    return _coerce_text(card["label"]), card, "official"


def _model_usage_buckets(turns: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate raw and Sol-equivalent Token usage by the model bucket.

    The parser only knows the set of models observed in a turn, not a model
    assignment for each token delta.  A turn with more than one model is kept
    intact in the explicit 多模型 bucket rather than inventing a split.
    """

    buckets: dict[str, dict[str, Any]] = {}
    for turn in turns:
        names = [name for name in _turn_model_names(turn) if not _is_plan_excluded_model(name)]
        # Spark has a separate ChatGPT subscription quota.  A Spark-only turn
        # remains in the report's overall totals, but is intentionally absent
        # from plan-level model and rate-card comparisons.
        if not names and _turn_model_names(turn):
            continue
        if len(names) > 1:
            label = "多模型"
            card = dict(SOL_RATE_CARD)
            confidence = "fallback"
        elif names:
            label, card, confidence = _model_rate_card(names[0])
        else:
            label = "未知模型"
            card = dict(SOL_RATE_CARD)
            confidence = "unconfigured"

        usage = Usage.from_dict(turn.get("usage"))
        breakdown = turn.get("breakdown") or {}
        cached = _as_nonnegative_int(breakdown.get("cachedInput", usage.cached))
        cache_write = _as_nonnegative_int(breakdown.get("cacheWriteInput", usage.cache_write))
        other_input = _as_nonnegative_int(
            breakdown.get("otherNonCachedInput", max(0, usage.input - cached - cache_write))
        )
        ordinary_output = _as_nonnegative_int(
            breakdown.get("ordinaryOutput", max(0, usage.output - usage.reasoning))
        )
        reasoning_output = _as_nonnegative_int(
            breakdown.get("reasoningOutput", min(usage.reasoning, usage.output))
        )
        unclassified = _as_nonnegative_int(breakdown.get("unclassified", 0))
        output = ordinary_output + reasoning_output + unclassified
        credits = (
            other_input * card["input"]
            + cached * card["cached"]
            + output * card["output"]
        ) / 1_000_000
        sol_equivalent_tokens = (
            other_input * card["input"] / SOL_RATE_CARD["input"]
            + cached * card["cached"] / SOL_RATE_CARD["cached"]
            + output * card["output"] / SOL_RATE_CARD["output"]
        )
        bucket = buckets.setdefault(
            label,
            {
                "model": label,
                "rawTokens": 0,
                "weightedTokens": 0.0,
                "costCredits": 0.0,
                "rateMultiplier": None,
                "rateStatus": confidence,
            },
        )
        bucket["rawTokens"] += usage.total
        bucket["weightedTokens"] += sol_equivalent_tokens
        bucket["costCredits"] += credits
        if bucket["rateStatus"] == "official" and confidence != "official":
            bucket["rateStatus"] = confidence

    result = []
    for bucket in buckets.values():
        label = bucket["model"]
        _, card, confidence = _model_rate_card(label)
        if label in {"多模型", "未知模型"}:
            card = dict(SOL_RATE_CARD)
            confidence = "fallback" if label == "多模型" else "unconfigured"
        ratios = [
            card["input"] / SOL_RATE_CARD["input"],
            card["cached"] / SOL_RATE_CARD["cached"],
            card["output"] / SOL_RATE_CARD["output"],
        ]
        multiplier = ratios[0] if max(ratios) - min(ratios) < 1e-9 else None
        bucket["rateMultiplier"] = round(multiplier, 6) if multiplier is not None else None
        bucket["rateStatus"] = confidence if bucket["rateStatus"] == "official" else bucket["rateStatus"]
        bucket["weightedTokens"] = round(bucket["weightedTokens"], 4)
        bucket["costCredits"] = round(bucket["costCredits"], 6)
        result.append(bucket)
    return sorted(result, key=lambda item: (-item["weightedTokens"], item["model"]))


def _plan_excluded_usage(turns: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize usage omitted from plan-level model comparisons."""

    model_names: set[str] = set()
    raw_tokens = 0
    turn_count = 0
    for turn in turns:
        names = _turn_model_names(turn)
        if not names or any(not _is_plan_excluded_model(name) for name in names):
            continue
        usage = Usage.from_dict(turn.get("usage"))
        raw_tokens += usage.total
        turn_count += 1
        model_names.update(names)
    return {
        "models": sorted("Spark" if _is_plan_excluded_model(name) else name for name in model_names),
        "rawTokens": raw_tokens,
        "turnCount": turn_count,
    }


def _primary_model(
    model_usage: list[dict[str, Any]], plan_excluded_usage: dict[str, Any] | None = None
) -> str:
    if model_usage:
        return model_usage[0]["model"]
    if plan_excluded_usage and plan_excluded_usage.get("rawTokens", 0):
        return "Spark"
    return "未知模型"


def _efforts_from_turns(turns: Iterable[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            text_value
            for turn in turns
            for value in turn.get("efforts", [])
            if (text_value := _coerce_text(value).strip())
        }
    )


def _response_item_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        return "".join(
            _coerce_text(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("text") is not None
        )
    return _coerce_text(payload.get("text"))


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


SKY_UI_METHODS = frozenset(
    {
        "activate_window",
        "click",
        "double_click",
        "drag",
        "get_window",
        "get_window_state",
        "key",
        "launch_app",
        "list_apps",
        "list_windows",
        "press",
        "scroll",
        "type",
    }
)


def _tool_semantics(
    payload: dict[str, Any],
    invocation: dict[str, Any],
    raw_name: str,
    effective_type: str,
) -> tuple[str | None, str | None, str, bool]:
    explicit_provider = _first_text(
        payload.get("provider"),
        payload.get("provider_name"),
        payload.get("providerName"),
        invocation.get("provider"),
        invocation.get("server"),
    ) or None
    explicit_semantic = _first_text(
        payload.get("semantic_tool"),
        payload.get("semanticTool"),
        payload.get("semantic"),
        invocation.get("semantic_tool"),
        invocation.get("semanticTool"),
    ) or None
    is_mcp = "mcp" in effective_type or explicit_provider is not None
    provider = explicit_provider
    semantic_tool = explicit_semantic
    source = "explicit" if explicit_provider or explicit_semantic else "raw"
    if is_mcp:
        semantic_tool = semantic_tool or raw_name or None
        source = "explicit" if provider or semantic_tool else source

    arguments = invocation.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    code = _coerce_text(arguments.get("code"))
    if (
        provider == "node_repl"
        and semantic_tool == "js"
        and any(
            re.search(rf"\bsky\s*\.\s*{re.escape(method)}\s*\(", code)
            for method in SKY_UI_METHODS
        )
    ):
        return "sky", "computer-use", "inferred", False

    input_text = _coerce_text(payload.get("input"))
    transport_wrapper = raw_name in {"exec", "js"} and "mcp__" in input_text
    return provider, semantic_tool, source, transport_wrapper


def _tool_category(
    raw_name: str,
    event_type: str,
    semantic_tool: str | None = None,
    provider: str | None = None,
) -> str:
    rendered = f"{raw_name} {event_type} {semantic_tool or ''} {provider or ''}".lower().replace("_", "-")
    if "image" in rendered or "imagegen" in rendered or "image-generation" in rendered:
        return "imagegen"
    if "computer" in rendered:
        return "computer-use"
    if "chrome" in rendered or "browser" in rendered:
        return "chrome-use"
    if "exec" in rendered and "reason" in rendered:
        return "exec-reasoning"
    if "shell" in rendered or "terminal" in rendered or "command" in rendered:
        return "shell"
    if "code-interpreter" in rendered or "python" in rendered:
        return "code-interpreter"
    if "web-search" in rendered or "search" in rendered:
        return "web-search"
    if "file-search" in rendered or "retrieval" in rendered:
        return "file-search"
    if "mcp" in rendered:
        return "mcp"
    if "function" in rendered or "custom-tool" in rendered:
        return "function-calling"
    return "other"


def _usage_from_mapping(value: Any) -> tuple[Usage, bool, set[str]]:
    if not isinstance(value, dict):
        return Usage(), False
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "input"),
        "cached_input_tokens": ("cached_input_tokens", "cachedInputTokens", "cached"),
        "cache_write_input_tokens": (
            "cache_write_input_tokens",
            "cacheWriteInputTokens",
            "cache_write",
            "cacheWrite",
        ),
        "output_tokens": ("output_tokens", "outputTokens", "output"),
        "reasoning_output_tokens": (
            "reasoning_output_tokens",
            "reasoningOutputTokens",
            "reasoning_tokens",
            "reasoningTokens",
            "reasoning",
        ),
        "total_tokens": ("total_tokens", "totalTokens", "total"),
    }
    normalized: dict[str, Any] = {}
    known: set[str] = set()
    present = False
    known_names = {
        "input_tokens": "input",
        "cached_input_tokens": "cached",
        "cache_write_input_tokens": "cache_write",
        "output_tokens": "output",
        "reasoning_output_tokens": "reasoning",
        "total_tokens": "total",
    }
    for target, candidates in aliases.items():
        for candidate in candidates:
            if candidate in value:
                normalized[target] = value[candidate]
                known.add(known_names[target])
                present = True
                break
    if not present:
        return Usage(), False, set()
    if {"input", "output"}.issubset(known) and "total" not in known:
        known.add("total")
    return Usage.from_payload(normalized), True, known


def _extract_tool_usage(payload: dict[str, Any]) -> tuple[Usage, bool, set[str]]:
    candidates: list[Any] = [payload]
    for key in ("usage", "token_usage", "tokenUsage", "tool_usage", "toolUsage", "info"):
        if key in payload:
            candidates.append(payload.get(key))
    for key in ("item", "tool_call", "call", "result", "output"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
            for usage_key in ("usage", "token_usage", "tokenUsage", "tool_usage", "toolUsage"):
                if usage_key in nested:
                    candidates.append(nested.get(usage_key))
    for candidate in candidates:
        usage, present, known = _usage_from_mapping(candidate)
        if present:
            return usage, True, known
    return Usage(), False, set()


def _extract_tool_event(record_type: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    record_name = _coerce_text(record_type).lower()
    event_type = _coerce_text(payload.get("type")).lower()
    item = payload.get("item")
    item_dict = item if isinstance(item, dict) else {}
    item_type = _coerce_text(item_dict.get("type")).lower()
    effective_type = item_type or event_type or record_name
    if effective_type not in TOOL_EVENT_TYPES and record_name not in {"tool_call", "tool_result", "tool_output"}:
        return None
    nested_tool = item_dict.get("tool")
    nested_function = item_dict.get("function")
    invocation = payload.get("invocation")
    invocation_dict = invocation if isinstance(invocation, dict) else {}
    action = payload.get("action")
    action_dict = action if isinstance(action, dict) else {}
    if not isinstance(nested_tool, dict):
        nested_tool = {}
    if not isinstance(nested_function, dict):
        nested_function = {}
    raw_name = _first_text(
        payload.get("tool_name"),
        payload.get("toolName"),
        payload.get("name"),
        payload.get("tool"),
        payload.get("tool_type"),
        item_dict.get("tool_name"),
        item_dict.get("toolName"),
        item_dict.get("name"),
        nested_tool.get("name"),
        nested_tool.get("type"),
        nested_function.get("name"),
        invocation_dict.get("tool"),
        invocation_dict.get("name"),
        action_dict.get("type"),
    )
    if not raw_name:
        raw_name = effective_type.replace("_", "-") or "unknown-tool"
    call_id = _first_text(
        payload.get("call_id"),
        payload.get("callId"),
        item_dict.get("call_id"),
        item_dict.get("callId"),
        item_dict.get("id"),
        invocation_dict.get("call_id"),
        invocation_dict.get("callId"),
    ) or None
    provider, semantic_tool, classification_source, transport_wrapper = _tool_semantics(
        payload, invocation_dict, raw_name, effective_type
    )
    usage, usage_reported, usage_known = _extract_tool_usage(payload)
    is_result = any(marker in effective_type for marker in ("result", "output", "end"))
    status = _first_text(payload.get("status"), item_dict.get("status"))
    if not status:
        status = "completed" if is_result else "observed"
    return {
        "eventType": effective_type,
        "callId": call_id,
        "rawName": raw_name,
        "name": raw_name,
        "category": _tool_category(
            raw_name, effective_type, semantic_tool, provider
        ),
        "provider": provider,
        "semanticTool": semantic_tool,
        "classificationSource": classification_source,
        "transportWrapper": transport_wrapper,
        "usage": usage,
        "usageReported": usage_reported,
        "usageKnown": usage_known,
        "isResult": is_result,
        "status": status,
    }


def _add_usage_bucket(buckets: dict[str, Usage], key: str | None, value: Usage) -> None:
    if key is None:
        return
    buckets[key] = buckets.get(key, Usage()) + value


def _merge_tool_usage(
    current: Usage,
    current_known: set[str],
    incoming: Usage,
    incoming_known: set[str],
) -> tuple[Usage, set[str]]:
    values = current.to_dict()
    known = set(current_known)
    for key in incoming_known:
        values[key] = getattr(incoming, key)
        known.add(key)
    if {"input", "output"}.issubset(known) and "total" not in known:
        values["total"] = values["input"] + values["output"]
        known.add("total")
    return Usage.from_dict(values), known


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


def parse_rollout(
    path: Path,
    requested_thread_id: str | None = None,
    window: DateWindow | None = None,
    tolerate_live: bool = False,
) -> dict[str, Any]:
    filename_thread_id = _extract_thread_id(path.name)
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
    range_usage = Usage()
    range_unattributed = Usage()
    daily_usage: dict[str, Usage] = {}
    range_activity = False
    range_first_activity_at: str | None = None
    range_last_activity_at: str | None = None
    subagent_preamble = False
    subagent_baseline_applied = False
    latest_context_snapshot: ContextSnapshot | None = None
    previous_context_snapshot: ContextSnapshot | None = None
    pending_compaction: ContextCompaction | None = None
    tool_calls_by_key: dict[tuple[str, str], ToolCall] = {}
    tool_sequence = 0

    def compaction_offsets() -> tuple[int | None, int | None]:
        if current is None:
            return None, None
        turn_offset = (latest_usage - current.start_usage).clamp_nonnegative().total
        return turn_offset, current.range_usage.total

    def note_range_activity(timestamp: str) -> None:
        nonlocal range_activity, range_first_activity_at, range_last_activity_at
        if window is None or not window.contains(timestamp):
            return
        range_activity = True
        if timestamp:
            if range_first_activity_at is None:
                range_first_activity_at = timestamp
            range_last_activity_at = timestamp

    def record_tool_event(
        record_type: Any,
        event_payload: dict[str, Any],
        event_timestamp: str,
    ) -> None:
        nonlocal tool_sequence
        event = _extract_tool_event(record_type, event_payload)
        if (
            event is None
            or current is None
            or (window is not None and not window.contains(event_timestamp))
        ):
            return
        call_id = event["callId"]
        lookup_key = (current.turn_id, call_id) if call_id else None
        existing = tool_calls_by_key.get(lookup_key) if lookup_key is not None else None
        if existing is not None:
            existing.usage, existing.usage_known = _merge_tool_usage(
                existing.usage,
                existing.usage_known,
                event["usage"],
                event["usageKnown"],
            )
            existing.usage_reported = bool(existing.usage_known)
            if event["provider"] is not None:
                existing.provider = event["provider"]
            if event["semanticTool"] is not None:
                existing.semantic_tool = event["semanticTool"]
            if event["classificationSource"] != "raw":
                existing.classification_source = event["classificationSource"]
            existing.transport_wrapper = (
                existing.transport_wrapper or event["transportWrapper"]
            )
            existing.category = _tool_category(
                existing.raw_name,
                event["eventType"],
                existing.semantic_tool,
                existing.provider,
            )
            if event["isResult"] or event["status"] != "observed":
                existing.ended_at = event_timestamp
                existing.status = event["status"]
            return
        tool_sequence += 1
        call = ToolCall(
            sequence=tool_sequence,
            call_id=call_id,
            name=event["name"],
            raw_name=event["rawName"],
            category=event["category"],
            timestamp=event_timestamp,
            provider=event["provider"],
            semantic_tool=event["semanticTool"],
            classification_source=event["classificationSource"],
            transport_wrapper=event["transportWrapper"],
            ended_at=event_timestamp if event["isResult"] else None,
            status=event["status"],
            usage=event["usage"],
            usage_reported=event["usageReported"],
            usage_known=set(event["usageKnown"]),
        )
        current.tool_calls.append(call)
        if lookup_key is not None:
            tool_calls_by_key[lookup_key] = call
        current.note_range_activity(event_timestamp)
        note_range_activity(event_timestamp)

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
                        "warning" if tolerate_live and trailing else "error",
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
            timestamp = _coerce_text(record.get("timestamp"))
            in_window = window is None or window.contains(timestamp)

            if record_type == "session_meta":
                session_meta.append(payload)
                candidate_id = _coerce_text(payload.get("id")).lower()
                source_text = " ".join(
                    _coerce_text(payload.get(key))
                    for key in ("thread_source", "source")
                ).lower()
                if (
                    filename_thread_id
                    and candidate_id == filename_thread_id
                    and "subagent" in source_text
                ):
                    subagent_preamble = True
                note_range_activity(timestamp)
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
                # companion event marks the exact pre-compaction context snapshot.
                if current is not None:
                    turn_offset, range_turn_offset = compaction_offsets()
                    pending_compaction = ContextCompaction(
                        timestamp=timestamp,
                        before=latest_context_snapshot,
                        turn_token_offset=turn_offset,
                        range_turn_token_offset=range_turn_offset,
                    )
                continue

            if record_type != "event_msg":
                if record_type in {"response_item", "tool_call", "tool_result", "tool_output"}:
                    if (
                        record_type == "response_item"
                        and current is not None
                        and payload.get("type") == "message"
                        and payload.get("role") == "assistant"
                        and (window is None or in_window)
                    ):
                        current.add_output(
                            timestamp,
                            _response_item_text(payload),
                            payload.get("phase"),
                        )
                    record_tool_event(record_type, payload, timestamp)
                continue

            event_type = payload.get("type")
            if _extract_tool_event(record_type, payload) is not None:
                record_tool_event(event_type, payload, timestamp)
                continue
            if (
                event_type == "thread_settings_applied"
                and subagent_preamble
                and not subagent_baseline_applied
                and current is not None
            ):
                # Subagent rollouts begin with a copied snapshot of the active
                # parent turn. Keep its cumulative counter as the baseline for
                # subsequent deltas, but never expose or count the copied turn.
                current = None
                turns.clear()
                turns_by_id.clear()
                orphan_messages.clear()
                warnings.clear()
                unattributed = Usage()
                range_unattributed = Usage()
                range_usage = Usage()
                daily_usage.clear()
                malformed_lines = 0
                blank_lines = 0
                token_events = 0
                duplicate_snapshots = 0
                rollback_count = 0
                normalizer.reset_count = 0
                subagent_baseline_applied = True
                latest_context_snapshot = None
                previous_context_snapshot = None
                pending_compaction = None
                tool_calls_by_key.clear()
                continue

            if event_type == "task_started":
                pending_compaction = None
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
                if window is not None and in_window:
                    current.note_range_activity(timestamp)
                    note_range_activity(timestamp)
                continue

            if event_type == "agent_message":
                if current is not None and (window is None or in_window):
                    current.add_output(
                        timestamp,
                        _coerce_text(payload.get("message") or payload.get("text")),
                        payload.get("phase"),
                    )
                    current.note_range_activity(timestamp)
                    note_range_activity(timestamp)
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
                if current is not None:
                    current.message_events += 1
                if window is not None and not in_window:
                    continue
                if current is None:
                    orphan_messages.append(message)
                    if window is not None:
                        note_range_activity(timestamp)
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "orphan_user_message",
                            "活动轮次之外出现了一条用户消息。",
                            line=line_number,
                        )
                    )
                else:
                    message["steering"] = current.message_events > 1
                    current.messages.append(message)
                    if window is not None:
                        current.note_range_activity(timestamp)
                        note_range_activity(timestamp)
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
                last_payload = info.get("last_token_usage")
                context_snapshot: ContextSnapshot | None = None
                if isinstance(last_payload, dict) and "total_tokens" in last_payload:
                    raw_window = info.get("model_context_window")
                    context_window = (
                        _as_nonnegative_int(raw_window)
                        if raw_window is not None
                        else (
                            current.context_windows[-1]
                            if current is not None and current.context_windows
                            else None
                        )
                    )
                    if context_window == 0:
                        context_window = None
                    context_snapshot = ContextSnapshot(
                        tokens=_as_nonnegative_int(last_payload.get("total_tokens")),
                        window_tokens=context_window,
                        timestamp=timestamp,
                    )
                    previous_context_snapshot = latest_context_snapshot
                    latest_context_snapshot = context_snapshot
                    if pending_compaction is not None and pending_compaction.after is None:
                        pending_compaction.after = context_snapshot
                if event_delta.total == 0 and in_window:
                    duplicate_snapshots += 1
                if in_window:
                    token_events += 1
                latest_usage = logical_usage
                if current is not None:
                    current.end_usage = logical_usage
                    if context_snapshot is not None:
                        current.latest_context_snapshot = context_snapshot
                        turn_offset = (
                            logical_usage - current.start_usage
                        ).clamp_nonnegative().total
                        range_turn_offset = (
                            current.range_usage.total + event_delta.total
                            if window is not None and in_window
                            else None
                        )
                        current.context_timeline.append(
                            ContextTimelinePoint(
                                snapshot=context_snapshot,
                                turn_token_offset=turn_offset,
                                range_turn_token_offset=range_turn_offset,
                            )
                        )
                        if in_window:
                            current.range_latest_context_snapshot = context_snapshot
                    if in_window:
                        current.token_snapshots += 1
                        if event_delta.total > 0:
                            current.model_responses += 1
                        if window is not None:
                            current.range_usage = current.range_usage + event_delta
                            current.note_range_activity(timestamp)
                    context_window = info.get("model_context_window")
                    if context_window is not None:
                        current.add_unique(
                            "context_windows", _as_nonnegative_int(context_window)
                        )
                elif event_delta.total > 0 and in_window:
                    if window is None:
                        unattributed = unattributed + event_delta
                    else:
                        range_unattributed = range_unattributed + event_delta
                    warnings.append(
                        WarningRecord(
                            "warning",
                            "unattributed_usage",
                            f"活动轮次之外增加了 {event_delta.total:,} 个 Token。",
                            line=line_number,
                        )
                    )
                if window is not None and in_window:
                    range_usage = range_usage + event_delta
                    _add_usage_bucket(
                        daily_usage,
                        window.local_date_text(timestamp),
                        event_delta,
                    )
                    note_range_activity(timestamp)
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
                if window is not None and in_window:
                    current.note_range_activity(timestamp)
                    note_range_activity(timestamp)
                turns.append(current)
                current = None
                pending_compaction = None
                continue

            if event_type == "context_compacted":
                if current is not None and in_window:
                    current.compactions += 1
                    compaction = pending_compaction
                    if compaction is None:
                        turn_offset, range_turn_offset = compaction_offsets()
                        compaction = ContextCompaction(
                            timestamp=timestamp,
                            before=previous_context_snapshot,
                            after=latest_context_snapshot,
                            turn_token_offset=turn_offset,
                            range_turn_token_offset=range_turn_offset,
                        )
                    else:
                        compaction.timestamp = timestamp
                    current.context_compactions.append(compaction)
                    pending_compaction = None
                    if window is not None:
                        current.note_range_activity(timestamp)
                        note_range_activity(timestamp)
                elif current is None and in_window:
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
                if in_window:
                    rollback_count += 1
                    note_range_activity(timestamp)
                if current is not None and in_window:
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
        if window is None or current.range_relevant:
            warnings.append(
                WarningRecord(
                    "warning" if tolerate_live else "error",
                    "unclosed_turn",
                    "rollout 结束时仍有一个活动轮次未闭合。",
                    turn_id=current.turn_id,
                )
            )
        turns.append(current)

    relevant_turns = (
        [turn for turn in turns if turn.range_relevant]
        if window is not None
        else turns
    )
    # Context events can precede finalization; make sure displayed indices are stable.
    for index, turn in enumerate(relevant_turns, 1):
        turn.index = index

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

    turn_dicts = [
        turn.to_dict(cache_write_field_present, window=window)
        for turn in relevant_turns
    ]
    turn_usage_sum = Usage()
    breakdown_mismatch_turns = 0
    for turn, turn_dict in zip(relevant_turns, turn_dicts):
        turn_usage_sum = turn_usage_sum + (
            turn.range_usage if window is not None else turn.usage_delta()
        )
        if turn_dict["breakdownMismatch"] != 0:
            breakdown_mismatch_turns += 1

    effective_unattributed = range_unattributed if window is not None else unattributed
    effective_final_usage = range_usage if window is not None else latest_usage
    accounted = turn_usage_sum + effective_unattributed
    reconciliation = effective_final_usage - accounted
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
        effective_final_usage, cache_write_field_present
    )
    status_counts: dict[str, int] = {"complete": 0, "aborted": 0, "incomplete": 0}
    for turn in relevant_turns:
        status_counts[turn.status] = status_counts.get(turn.status, 0) + 1
    tool_stats = _tool_stats(turn_dicts)

    source_stat = path.stat()
    integrity_errors = sum(1 for warning in warnings if warning.severity == "error")
    return {
        "schemaVersion": 2 if window is not None else 1,
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
                "id": selected_meta.get("id"),
                "sessionId": selected_meta.get("session_id"),
                "cwd": selected_meta.get("cwd"),
                "originator": selected_meta.get("originator"),
                "cliVersion": selected_meta.get("cli_version"),
                "forkedFromId": selected_meta.get("forked_from_id"),
                "parentThreadId": selected_meta.get("parent_thread_id"),
                "source": selected_meta.get("source"),
                "threadSource": selected_meta.get("thread_source"),
            },
            "sourceKind": (
                "subagent"
                if selected_meta.get("parent_thread_id")
                or "subagent"
                in " ".join(
                    _coerce_text(selected_meta.get(key))
                    for key in ("thread_source", "source", "originator")
                ).lower()
                else (
                    "automation"
                    if "automation"
                    in " ".join(
                        _coerce_text(selected_meta.get(key))
                        for key in ("thread_source", "source", "originator")
                    ).lower()
                    else "main"
                )
            ),
            "dateWindow": window.to_dict() if window is not None else None,
            "hasRangeActivity": range_activity if window is not None else True,
            "rangeFirstActivityAt": range_first_activity_at,
            "rangeLastActivityAt": range_last_activity_at,
            "subagentBaselineApplied": subagent_baseline_applied,
            "containsFullUserMessages": True,
            "cacheWriteFieldAvailable": cache_write_field_present,
            "reasoningFieldAvailable": reasoning_field_present,
            "hasToolEvents": tool_stats["callCount"] > 0,
        },
        "summary": {
            "turnCount": len(relevant_turns),
            "statusCounts": status_counts,
            "zeroUsageTurns": sum(
                1
                for turn in relevant_turns
                if (
                    turn.range_usage.total
                    if window is not None
                    else turn.usage_delta().total
                )
                == 0
            ),
            "tokenEvents": token_events,
            "duplicateSnapshots": duplicate_snapshots,
            "rollbacks": rollback_count,
            "contextCompactions": sum(turn.compactions for turn in relevant_turns),
            "malformedLines": malformed_lines,
            "blankLines": blank_lines,
            "orphanMessageCount": len(orphan_messages),
            "counterResets": normalizer.reset_count,
            "finalUsage": effective_final_usage.to_dict(),
            "finalBreakdown": final_breakdown,
            "finalBreakdownMismatch": final_breakdown_mismatch,
            "turnUsageSum": turn_usage_sum.to_dict(),
            "unattributedUsage": effective_unattributed.to_dict(),
            "accountedUsage": accounted.to_dict(),
            "reconciliationDifference": reconciliation.to_dict(),
            "integrityErrorCount": integrity_errors,
            "warningCount": len(warnings),
            "dailyUsage": [
                {"date": day, "usage": daily_usage[day].to_dict()}
                for day in sorted(daily_usage)
            ],
            "toolCallCount": tool_stats["callCount"],
            "toolReportedCallCount": tool_stats["reportedCallCount"],
            "toolUnknownCallCount": tool_stats["unknownCallCount"],
            "toolUsage": tool_stats["usage"],
            "toolCategories": tool_stats["categories"],
        },
        "warnings": [warning.to_dict() for warning in warnings],
        "orphanMessages": orphan_messages,
        "turns": turn_dicts,
    }


def discover_rollouts(
    roots: Iterable[Path] | None = None,
    window: DateWindow | None = None,
) -> list[Path]:
    """Find rollout JSONL files once, preferring the newest copy of each ID."""
    search_roots = list(roots) if roots is not None else default_session_roots()
    chosen: dict[str, Path] = {}
    for root in search_roots:
        if not root.exists():
            continue
        for candidate in root.rglob("*.jsonl"):
            try:
                resolved = candidate.resolve()
                modified = resolved.stat().st_mtime
            except OSError:
                continue
            if window is not None:
                if datetime.fromtimestamp(modified, tz=timezone.utc) < window.start_utc:
                    continue
                created_match = re.search(r"rollout-(\d{4}-\d{2}-\d{2})T", resolved.name)
                if created_match:
                    try:
                        created_date = date.fromisoformat(created_match.group(1))
                    except ValueError:
                        created_date = None
                    if created_date is not None and created_date > window.end_date:
                        continue
            rollout_id = _extract_thread_id(resolved.name)
            key = rollout_id or os.path.normcase(str(resolved))
            previous = chosen.get(key)
            if previous is None:
                chosen[key] = resolved
                continue
            try:
                if modified > previous.stat().st_mtime:
                    chosen[key] = resolved
            except OSError:
                chosen[key] = resolved
    return sorted(chosen.values(), key=lambda path: os.path.normcase(str(path)))


def _source_kind(report: dict[str, Any]) -> str:
    meta = report.get("metadata", {}).get("sessionMeta", {})
    rendered = " ".join(
        _coerce_text(meta.get(key)) for key in ("threadSource", "source", "originator")
    ).lower()
    if "subagent" in rendered or meta.get("parentThreadId"):
        return "subagent"
    if "automation" in rendered or "scheduled" in rendered:
        return "automation"
    return "main"


def _root_thread_id(
    report: dict[str, Any], reports_by_id: dict[str, dict[str, Any]]
) -> str:
    thread_id = _coerce_text(report.get("metadata", {}).get("threadId"))
    seen: set[str] = set()
    current = report
    while thread_id and thread_id not in seen:
        seen.add(thread_id)
        meta = current.get("metadata", {}).get("sessionMeta", {})
        parent = _coerce_text(meta.get("parentThreadId"))
        session_id = _coerce_text(meta.get("sessionId"))
        candidate = parent or (session_id if session_id != thread_id else "")
        if not candidate:
            return thread_id
        thread_id = candidate.lower()
        current = reports_by_id.get(thread_id, current)
        if current is report and thread_id not in reports_by_id:
            return thread_id
    return thread_id or _coerce_text(report.get("metadata", {}).get("threadId"))


def _sum_usage(items: Iterable[dict[str, Any]], key: str = "finalUsage") -> Usage:
    result = Usage()
    for item in items:
        result = result + Usage.from_dict(item.get("summary", {}).get(key))
    return result


def _tool_stats(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    usage = Usage()
    call_count = 0
    reported_call_count = 0
    unknown_call_count = 0
    categories: dict[str, int] = {}
    for item in items:
        summary = item.get("toolSummary", {})
        call_count += _as_nonnegative_int(summary.get("callCount"))
        reported_call_count += _as_nonnegative_int(summary.get("reportedCallCount"))
        unknown_call_count += _as_nonnegative_int(summary.get("unknownCallCount"))
        usage = usage + Usage.from_dict(summary.get("usage"))
        for category, count in (summary.get("categories") or {}).items():
            categories[category] = categories.get(category, 0) + _as_nonnegative_int(count)
    return {
        "callCount": call_count,
        "reportedCallCount": reported_call_count,
        "unknownCallCount": unknown_call_count,
        "usage": usage.to_dict(),
        "categories": categories,
    }


def _short_thread_id(thread_id: str) -> str:
    return thread_id[:8] if thread_id else "unknown"


def _fallback_session_title(cwd: Any, thread_id: str) -> str:
    rendered_cwd = _coerce_text(cwd).rstrip("\\/")
    leaf = re.split(r"[\\/]", rendered_cwd)[-1] if rendered_cwd else ""
    return f"{leaf} · {_short_thread_id(thread_id)}" if leaf else _short_thread_id(thread_id)


def _prompt_title(report: dict[str, Any]) -> str | None:
    messages: list[dict[str, Any]] = []
    for turn in report.get("turns", []):
        messages.extend(turn.get("messages", []))
    messages.extend(report.get("orphanMessages", []))
    messages.sort(key=lambda message: _coerce_text(message.get("timestamp")))
    for message in messages:
        text_value = re.sub(r"\s+", " ", _coerce_text(message.get("text"))).strip()
        if text_value:
            return text_value[:77] + "..." if len(text_value) > 80 else text_value
    return None


def _merge_daily_usage(items: Iterable[dict[str, Any]]) -> dict[str, Usage]:
    merged: dict[str, Usage] = {}
    for item in items:
        for bucket in item.get("summary", {}).get("dailyUsage", []):
            day = _coerce_text(bucket.get("date"))
            if day:
                _add_usage_bucket(merged, day, Usage.from_dict(bucket.get("usage")))
    return merged


def _date_buckets(window: DateWindow, usage: dict[str, Usage]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    current = window.start_date
    while current <= window.end_date:
        day = current.isoformat()
        result.append({"date": day, "usage": usage.get(day, Usage()).to_dict()})
        current += timedelta(days=1)
    return result


def build_range_report(
    window: DateWindow,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Build a date-scoped report grouped by user-visible top-level task."""
    parsed_reports: list[dict[str, Any]] = []
    for path in discover_rollouts(roots, window=window):
        parsed_reports.append(parse_rollout(path, window=window, tolerate_live=True))

    reports_by_id = {
        _coerce_text(report.get("metadata", {}).get("threadId")).lower(): report
        for report in parsed_reports
        if _coerce_text(report.get("metadata", {}).get("threadId"))
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for report in parsed_reports:
        if not report.get("metadata", {}).get("hasRangeActivity"):
            continue
        root_id = _root_thread_id(report, reports_by_id)
        groups.setdefault(root_id, []).append(report)

    sessions: list[dict[str, Any]] = []
    all_warnings: list[dict[str, Any]] = []
    for root_id, active_members in groups.items():
        root_report = reports_by_id.get(root_id)
        members = list(active_members)
        if root_report is not None and root_report not in members:
            members.append(root_report)
        root_meta = (
            root_report.get("metadata", {})
            if root_report is not None
            else active_members[0].get("metadata", {})
        )
        root_session_meta = root_meta.get("sessionMeta", {})
        fallback_title = _fallback_session_title(root_session_meta.get("cwd"), root_id)
        message_title = _prompt_title(root_report) if root_report is not None else None

        turns: list[dict[str, Any]] = []
        member_warnings: list[dict[str, Any]] = []
        for member in members:
            member_meta = member.get("metadata", {})
            member_id = _coerce_text(member_meta.get("threadId"))
            kind = _source_kind(member)
            for source_turn in member.get("turns", []):
                turn = dict(source_turn)
                turn["sourceTurnIndex"] = source_turn.get("index")
                turn["sourceRolloutId"] = member_id
                turn["sourceKind"] = kind
                turn["sourceLabel"] = (
                    f"子代理 {_short_thread_id(member_id)}"
                    if kind == "subagent"
                    else ("自动化" if kind == "automation" else "主会话")
                )
                turns.append(turn)
            if member_meta.get("hasRangeActivity"):
                for source_warning in member.get("warnings", []):
                    warning = dict(source_warning)
                    warning["rolloutId"] = member_id
                    warning["sourceName"] = member_meta.get("sourceName")
                    member_warnings.append(warning)
                    all_warnings.append(dict(warning, sessionId=root_id))

        turns.sort(
            key=lambda turn: (
                _coerce_text(
                    turn.get("contextSnapshot", {}).get("timestamp")
                    or turn.get("endedAt")
                    or turn.get("rangeLastActivityAt")
                    or turn.get("startedAt")
                ),
                _coerce_text(turn.get("sourceRolloutId")),
                _as_nonnegative_int(turn.get("sourceTurnIndex")),
            )
        )
        for index, turn in enumerate(turns, 1):
            turn["index"] = index

        active_usage = _sum_usage(active_members)
        tool_stats = _tool_stats(turns)
        cache_write_available = any(
            member.get("metadata", {}).get("cacheWriteFieldAvailable")
            for member in active_members
        )
        breakdown, mismatch = _usage_breakdown(active_usage, cache_write_available)
        status_counts: dict[str, int] = {"complete": 0, "aborted": 0, "incomplete": 0}
        for turn in turns:
            status = _coerce_text(turn.get("status")) or "incomplete"
            status_counts[status] = status_counts.get(status, 0) + 1
        model_usage = _model_usage_buckets(turns)
        plan_excluded_usage = _plan_excluded_usage(turns)
        daily = _merge_daily_usage(active_members)
        first_activity = min(
            (
                _coerce_text(member.get("metadata", {}).get("rangeFirstActivityAt"))
                for member in active_members
                if member.get("metadata", {}).get("rangeFirstActivityAt")
            ),
            default="",
        )
        last_activity = max(
            (
                _coerce_text(member.get("metadata", {}).get("rangeLastActivityAt"))
                for member in active_members
                if member.get("metadata", {}).get("rangeLastActivityAt")
            ),
            default="",
        )
        source_kinds = sorted({_source_kind(member) for member in active_members})
        session = {
            "metadata": {
                "threadId": root_id,
                "title": message_title or fallback_title,
                "messageTitle": message_title,
                "fallbackTitle": fallback_title,
                "cwd": root_session_meta.get("cwd"),
                "originator": root_session_meta.get("originator"),
                "source": root_session_meta.get("source"),
                "sourceKinds": source_kinds,
                "rangeFirstActivityAt": first_activity or None,
                "rangeLastActivityAt": last_activity or None,
                "rolloutCount": len(active_members),
                "sourcePaths": [
                    member.get("metadata", {}).get("sourcePath")
                    for member in active_members
                ],
                "cacheWriteFieldAvailable": cache_write_available,
                "reasoningFieldAvailable": any(
                    member.get("metadata", {}).get("reasoningFieldAvailable")
                    for member in active_members
                ),
                "hasToolEvents": tool_stats["callCount"] > 0,
                "messagesIncluded": True,
                "containsFullUserMessages": True,
            },
            "summary": {
                "turnCount": len(turns),
                "statusCounts": status_counts,
                "zeroUsageTurns": sum(
                    1 for turn in turns if _as_nonnegative_int(turn.get("usage", {}).get("total")) == 0
                ),
                "finalUsage": active_usage.to_dict(),
                "finalBreakdown": breakdown,
                "finalBreakdownMismatch": mismatch,
                "turnUsageSum": _sum_usage(
                    [{"summary": {"finalUsage": turn.get("usage", {})}} for turn in turns]
                ).to_dict(),
                "unattributedUsage": _sum_usage(
                    [
                        {"summary": {"finalUsage": member.get("summary", {}).get("unattributedUsage", {})}}
                        for member in active_members
                    ]
                ).to_dict(),
                "accountedUsage": active_usage.to_dict(),
                "reconciliationDifference": Usage().to_dict(),
                "integrityErrorCount": sum(
                    1 for warning in member_warnings if warning.get("severity") == "error"
                ),
                "warningCount": len(member_warnings),
                "dailyUsage": _date_buckets(window, daily),
                "toolCallCount": tool_stats["callCount"],
                "toolReportedCallCount": tool_stats["reportedCallCount"],
                "toolUnknownCallCount": tool_stats["unknownCallCount"],
                "toolUsage": tool_stats["usage"],
                "toolCategories": tool_stats["categories"],
                "modelUsage": model_usage,
                "planExcludedUsage": plan_excluded_usage,
            },
            "warnings": member_warnings,
            "orphanMessages": [
                message
                for member in active_members
                for message in member.get("orphanMessages", [])
            ],
            "turns": turns,
        }
        session["metadata"]["primaryModel"] = _primary_model(model_usage, plan_excluded_usage)
        session["metadata"]["efforts"] = _efforts_from_turns(turns)
        session["metadata"]["modelUsage"] = model_usage
        session["metadata"]["planExcludedUsage"] = plan_excluded_usage
        sessions.append(session)

    sessions.sort(
        key=lambda session: (
            _coerce_text(session.get("metadata", {}).get("rangeLastActivityAt")),
            _coerce_text(session.get("metadata", {}).get("threadId")),
        ),
        reverse=True,
    )
    total_usage = _sum_usage(sessions)
    total_tool_stats = _tool_stats(
        turn for session in sessions for turn in session.get("turns", [])
    )
    cache_write_available = any(
        session.get("metadata", {}).get("cacheWriteFieldAvailable")
        for session in sessions
    )
    final_breakdown, final_breakdown_mismatch = _usage_breakdown(
        total_usage, cache_write_available
    )
    daily_usage = _merge_daily_usage(sessions)
    all_turns = [turn for session in sessions for turn in session.get("turns", [])]
    model_usage = _model_usage_buckets(all_turns)
    plan_excluded_usage = _plan_excluded_usage(all_turns)
    status_counts: dict[str, int] = {"complete": 0, "aborted": 0, "incomplete": 0}
    for session in sessions:
        for status, count in session.get("summary", {}).get("statusCounts", {}).items():
            status_counts[status] = status_counts.get(status, 0) + _as_nonnegative_int(count)
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "schemaVersion": 2,
        "mode": "range",
        "generator": {"name": "codex_token_visualizer", "version": VERSION},
        "metadata": {
            "generatedAt": generated_at,
            "dateWindow": window.to_dict(),
            "containsFullUserMessages": True,
            "messagesIncluded": True,
            "cacheWriteFieldAvailable": cache_write_available,
            "reasoningFieldAvailable": any(
                session.get("metadata", {}).get("reasoningFieldAvailable")
                for session in sessions
            ),
            "hasToolEvents": total_tool_stats["callCount"] > 0,
            "rateCard": {
                "source": RATE_CARD_SOURCE,
                "effectiveDate": RATE_CARD_EFFECTIVE_DATE,
                "checkedAt": RATE_CARD_CHECKED_AT,
            },
            "sourceRoots": [
                str(path)
                for path in (list(roots) if roots is not None else default_session_roots())
            ],
            "snapshotAt": generated_at,
        },
        "summary": {
            "sessionCount": len(sessions),
            "turnCount": sum(session["summary"]["turnCount"] for session in sessions),
            "statusCounts": status_counts,
            "zeroUsageSessions": sum(
                1 for session in sessions if session["summary"]["finalUsage"]["total"] == 0
            ),
            "zeroUsageTurns": sum(session["summary"]["zeroUsageTurns"] for session in sessions),
            "finalUsage": total_usage.to_dict(),
            "finalBreakdown": final_breakdown,
            "finalBreakdownMismatch": final_breakdown_mismatch,
            "dailyUsage": _date_buckets(window, daily_usage),
            "integrityErrorCount": sum(
                1 for warning in all_warnings if warning.get("severity") == "error"
            ),
            "warningCount": len(all_warnings),
            "toolCallCount": total_tool_stats["callCount"],
            "toolReportedCallCount": total_tool_stats["reportedCallCount"],
            "toolUnknownCallCount": total_tool_stats["unknownCallCount"],
            "toolUsage": total_tool_stats["usage"],
            "toolCategories": total_tool_stats["categories"],
            "modelUsage": model_usage,
            "planExcludedUsage": plan_excluded_usage,
        },
        "warnings": all_warnings,
        "sessions": sessions,
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
.hero-copy { min-width:0; flex:1; }
.eyebrow { color: var(--accent); text-transform:uppercase; letter-spacing:.14em; font-size:12px; font-weight:800; }
h1 { font-size: clamp(28px, 4vw, 48px); line-height:1.08; margin:7px 0 10px; letter-spacing:-.035em; }
.subline { color:var(--muted); max-width:920px; overflow-wrap:anywhere; }
.sensitive { color:#984b55; background:#fae9e8; border:1px solid #e8c4c2; border-radius:999px; padding:7px 11px; font-size:12px; white-space:nowrap; }
.sensitive.safe { color:#3f765f; background:#e8f3eb; border-color:#c3ddcb; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(172px,1fr)); gap:12px; margin:20px 0; }
.summary-brief { width:min(560px,48%); padding:12px 14px; border:1px solid var(--border); border-radius:14px; background:rgba(255,254,250,.86); box-shadow:var(--shadow); }
.brief-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:9px; color:var(--muted); font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; }
.brief-head .sensitive { font-size:10px; letter-spacing:0; text-transform:none; padding:4px 8px; }
.brief-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px 16px; }
.summary-token-unit { display:flex; align-items:center; justify-content:flex-end; gap:12px; margin-top:10px; padding-top:10px; border-top:1px solid rgba(126,111,91,.18); }
.summary-token-unit-label { color:var(--muted); font-size:10px; font-weight:800; white-space:nowrap; }
.token-unit-slider { display:grid; grid-template-columns:minmax(140px,190px) auto; gap:1px 9px; align-items:center; }
.token-unit-slider input[type="range"] { width:100%; margin:0; accent-color:var(--accent); cursor:pointer; }
.token-unit-slider output { min-width:30px; color:var(--accent); font-size:11px; font-weight:850; text-align:right; }
.token-unit-scale { grid-column:1 / -1; display:flex; justify-content:space-between; color:var(--muted); font-size:9px; line-height:1; }
.brief-item { min-width:0; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:baseline; padding-bottom:6px; border-bottom:1px solid rgba(126,111,91,.16); }
.brief-item .label { color:var(--muted); font-size:10px; }
.brief-item .value { margin:0; font-size:16px; font-weight:760; font-variant-numeric:tabular-nums; text-align:right; }
.brief-item .note { grid-column:1 / -1; color:var(--muted); font-size:10px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.analysis-shell { margin-top:16px; }
.analysis-tabs { display:flex; gap:5px; padding:5px; border:1px solid var(--border); border-radius:13px 13px 0 0; background:var(--panel-2); overflow-x:auto; }
.analysis-tab { flex:1 0 auto; border:1px solid transparent; border-radius:9px; padding:9px 13px; background:transparent; color:var(--muted); font-size:12px; font-weight:750; white-space:nowrap; }
.analysis-tab:hover, .analysis-tab.active { border-color:var(--accent); background:var(--panel); color:var(--accent); }
.analysis-tab:disabled { cursor:not-allowed; opacity:.42; }
.analysis-controls { padding:14px 0 0; }
.analysis-controls .range-row, .analysis-controls .filter-row { padding-left:0; padding-right:0; }
.tab-panel { margin-top:12px; }
.tab-panel[hidden] { display:none; }
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
.tool-filter-group { display:flex; align-items:center; gap:7px; margin:0; padding:0; border:0; }
.tool-filter-group legend { color:var(--muted); font-size:11px; font-weight:700; }
.tool-filter-list { display:flex; gap:7px; flex-wrap:wrap; }
.tool-filter-list .check { padding:4px 7px; border:1px solid rgba(126,111,91,.24); border-radius:999px; background:rgba(255,254,250,.62); white-space:nowrap; }
.tool-filter-list .check:has(input:checked) { border-color:var(--accent); color:var(--accent); background:rgba(59,139,120,.08); }
.tool-filter-list input { accent-color:var(--accent); }
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
.tooltip { position:fixed; z-index:80; pointer-events:none; display:none; visibility:hidden; width:min(440px,calc(100vw - 16px)); max-height:calc(100vh - 16px); overflow:hidden; padding:12px 13px; border-radius:11px; background:#fffefa; border:1px solid var(--border); box-shadow:var(--shadow); color:var(--text); font-size:12px; transition:none; }
.tooltip strong { display:block; margin-bottom:5px; }
.tooltip .row { display:flex; justify-content:space-between; gap:20px; color:var(--muted); }
.tooltip .row b { color:var(--text); font-variant-numeric:tabular-nums; }
.tooltip-title { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:8px; }
.tooltip-title strong { margin:0; font-size:13px; }
.tooltip-badge { flex:none; border-radius:999px; padding:2px 7px; background:var(--panel-2); color:var(--accent); font-size:10px; font-weight:750; }
.tooltip-metrics { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:8px 0; }
.tooltip-metric { min-width:0; padding:9px 10px; border:1px solid color-mix(in srgb,var(--metric-color) 28%,var(--border)); border-radius:9px; background:color-mix(in srgb,var(--metric-color) 8%,#fffefa); }
.tooltip-metric.context { --metric-color:var(--accent); }
.tooltip-metric.token { --metric-color:var(--uncached); }
.tooltip-metric span { display:block; color:var(--muted); font-size:10px; font-weight:750; }
.tooltip-metric strong { display:block; margin:3px 0 1px; color:var(--metric-color); font-size:20px; line-height:1.1; font-variant-numeric:tabular-nums; }
.tooltip-metric small { display:block; min-height:30px; color:var(--text); font-size:10px; line-height:1.4; overflow-wrap:anywhere; font-variant-numeric:tabular-nums; }
.tooltip-metric strong .tooltip-change { font-size:11px; font-weight:550; color:var(--muted); white-space:nowrap; }
.tooltip-grid { display:grid; grid-template-columns:auto minmax(0,1fr); gap:3px 12px; padding-top:7px; border-top:1px solid rgba(126,111,91,.18); }
.tooltip-grid span { color:var(--muted); }
.tooltip-grid b { min-width:0; overflow-wrap:anywhere; text-align:right; font-weight:650; font-variant-numeric:tabular-nums; }
.tooltip-section { margin-top:8px; padding-top:7px; border-top:1px solid rgba(126,111,91,.18); }
.tooltip-section-label { color:var(--muted); font-size:10px; font-weight:750; letter-spacing:.06em; text-transform:uppercase; }
.tooltip-message { max-height:min(230px,30vh); margin-top:4px; overflow:hidden; white-space:pre-wrap; overflow-wrap:anywhere; color:var(--text); font:11.5px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; }
.tooltip-truncated { margin-top:4px; color:var(--warning); font-size:10px; }
.cumulative-wrap { padding:4px 18px 14px; }
.context-radial-wrap { padding:4px 18px 18px; border-top:1px solid rgba(126,111,91,.2); }
.context-radial-wrap svg { width:min(100%,900px); margin:auto; max-height:680px; }
.context-source-legend { display:flex; justify-content:center; gap:13px; flex-wrap:wrap; padding:0 18px 12px; color:var(--muted); font-size:11px; }
.context-source-legend span::before { content:""; display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; background:var(--source-color); }
.context-turn { cursor:pointer; transition:opacity .14s ease, filter .14s ease; }
.context-turn.dim { opacity:.14; }
.context-turn:hover, .context-turn:focus, .context-turn.selected { filter:brightness(1.12) saturate(1.15); outline:none; }
.context-turn .mapping-line { stroke:var(--text); stroke-width:1; opacity:.2; }
.context-turn:hover .mapping-line, .context-turn:focus .mapping-line, .context-turn.selected .mapping-line { stroke-width:2.4; opacity:.78; }
.context-turn .token-sector { stroke:#fffefa; stroke-width:1.2; }
.context-turn.source-switch .token-sector { stroke-width:4; }
.context-turn.satellite .token-sector { stroke-width:1.1; }
.context-turn.satellite .mapping-line { stroke:var(--accent); stroke-dasharray:3 3; opacity:.5; }
.context-turn.satellite .satellite-context { stroke:var(--accent); stroke-width:2; }
.context-turn.satellite .satellite-context-unknown { fill:none; stroke:var(--muted); stroke-width:1.5; stroke-dasharray:2 2; }
.context-turn.satellite .satellite-connector { stroke:var(--muted); stroke-width:1.2; stroke-dasharray:3 4; opacity:.38; }
.context-turn.satellite:hover .satellite-connector, .context-turn.satellite:focus .satellite-connector, .context-turn.satellite.selected .satellite-connector { stroke:var(--accent); stroke-width:2.4; opacity:.9; }
.context-satellite-label { fill:var(--muted); font-size:10px; font-weight:700; }
.tool-envelope { fill:none; stroke:#6f8fb7; stroke-width:5; stroke-dasharray:2 5; opacity:.72; }
.tool-satellite { cursor:pointer; }
.tool-satellite .token-sector { stroke:#fffefa; stroke-width:1.4; }
.tool-satellite-unknown { fill:none; stroke:#6f8fb7; stroke-width:2; stroke-dasharray:2 3; }
.tool-satellite-connector { stroke:#6f8fb7; stroke-width:1.1; stroke-dasharray:2 4; opacity:.55; }
.tool-satellite:hover .tool-satellite-connector, .tool-satellite:focus .tool-satellite-connector, .tool-satellite.selected .tool-satellite-connector { stroke:#3b6d9b; stroke-width:2.4; opacity:1; }
.tool-satellite-label { fill:#52749b; font-size:10px; font-weight:700; }
.context-reference { fill:none; stroke:rgba(117,110,100,.18); stroke-width:1; stroke-dasharray:3 4; }
.context-reference.context-capacity { stroke:rgba(45,41,36,.75); stroke-width:2.5; stroke-dasharray:none; }
.context-reference.context-warning { stroke:rgba(210,139,61,.72); stroke-width:1.8; }
.context-danger-zone { fill:rgba(196,86,87,.07); }
.context-contour { fill:none; stroke-width:2.8; stroke-linecap:round; stroke-linejoin:round; opacity:.92; }
.context-current-marker { stroke:#fffefa; stroke-width:2; }
.context-compaction { cursor:pointer; }
.context-compaction line { stroke:var(--warning); stroke-width:3; }
.context-compaction .compaction-position-line { stroke-dasharray:4 4; opacity:.78; }
.context-compaction .compaction-jump-line { stroke-width:3.5; }
.context-compaction circle { fill:#fffefa; stroke:var(--warning); stroke-width:2; }
.context-compaction .compaction-after { fill:var(--warning); }
.context-zero-tick { stroke-width:2; opacity:.78; }
.warning-box { margin:16px 0; border-radius:14px; border:1px solid var(--border); overflow:hidden; }
.warning-box summary { cursor:pointer; padding:13px 16px; background:rgba(247,198,107,.08); color:var(--warning); font-weight:700; }
.warning-list { margin:0; padding:8px 16px 14px 36px; max-height:300px; overflow:auto; }
.warning-list li { margin:6px 0; color:var(--muted); }
.warning-list li.error { color:#a74450; }
.warning-list li.info { color:#5b6f59; }
.table-wrap { overflow:auto; max-height:720px; border-top:1px solid var(--border); }
table { border-collapse:separate; border-spacing:0; width:100%; min-width:1510px; }
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
  .summary-brief { width:100%; }
  .brief-grid { gap:7px 12px; }
  .range-row { align-items:flex-start; flex-direction:column; gap:4px; }
  .dual-range-shell { width:100%; }
  .detail-grid { grid-template-columns:1fr; }
}
</style>
<style>
.hero-copy{min-width:0;flex:1}.summary-brief{width:min(560px,48%);padding:12px 14px;border:1px solid var(--border);border-radius:14px;background:rgba(255,254,250,.86);box-shadow:var(--shadow)}.brief-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;color:var(--muted);font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.brief-head .sensitive{font-size:10px;letter-spacing:0;text-transform:none;padding:4px 8px}.brief-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 16px}.brief-item{min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:baseline;padding-bottom:6px;border-bottom:1px solid rgba(126,111,91,.16)}.brief-item .label{color:var(--muted);font-size:10px}.brief-item .value{margin:0;font-size:16px;font-weight:760;font-variant-numeric:tabular-nums;text-align:right}.brief-item .note{grid-column:1 / -1;color:var(--muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.analysis-shell{margin-top:16px}.analysis-tabs{display:flex;gap:5px;padding:5px;border:1px solid var(--border);border-radius:13px 13px 0 0;background:var(--panel2);overflow-x:auto}.analysis-tab{flex:1 0 auto;border:1px solid transparent;border-radius:9px;padding:9px 13px;background:transparent;color:var(--muted);font-size:12px;font-weight:750;white-space:nowrap}.analysis-tab:hover,.analysis-tab.active{border-color:var(--accent);background:var(--panel);color:var(--accent)}.analysis-tab:disabled{cursor:not-allowed;opacity:.42}.analysis-controls{padding:14px 0 0}.analysis-controls .filters{padding-left:0;padding-right:0}.tab-panel{margin-top:12px}.tab-panel[hidden]{display:none}
@media(max-width:650px){.summary-brief{width:100%}.brief-grid{gap:7px 12px}}
.session-button.model-watermark{position:relative;isolation:isolate;overflow:hidden;background:var(--model-tint,transparent);border-color:color-mix(in srgb,var(--model-color,var(--border)) 24%,transparent)}.session-button.model-watermark::after{content:attr(data-model-watermark);position:absolute;z-index:0;right:-8px;bottom:-10px;color:var(--model-color,var(--muted));font-size:27px;font-weight:850;letter-spacing:-.07em;line-height:1;opacity:.14;pointer-events:none;white-space:nowrap;transform:rotate(-10deg);transform-origin:right bottom}.session-button.model-watermark>strong,.session-button.model-watermark>span{position:relative;z-index:1}.session-button.model-watermark:hover,.session-button.model-watermark.active{background:linear-gradient(135deg,var(--model-tint,transparent),var(--panel));border-color:var(--model-color,var(--accent))}.session-effort{display:inline-flex!important;width:max-content;max-width:100%;padding:2px 6px;border:1px solid color-mix(in srgb,var(--model-color,var(--muted)) 35%,var(--border));border-radius:999px;color:var(--model-color,var(--muted))!important;background:rgba(255,254,250,.62);font-size:10px!important;line-height:1.25}
.session-button.model-watermark::after{display:none}.session-button.model-watermark>.session-watermark{position:absolute;right:6px;bottom:16px;z-index:0;display:block;max-width:72%;overflow:hidden;text-overflow:ellipsis;color:var(--model-color,var(--muted));opacity:.34;font-size:38px;font-weight:950;letter-spacing:-.055em;line-height:.9;text-align:right;white-space:nowrap;transform:rotate(-18deg);transform-origin:right bottom;pointer-events:none;filter:saturate(1.45);text-shadow:0 1px 0 rgba(255,254,250,.18)}.session-button.model-watermark>.session-watermark~strong,.session-button.model-watermark>.session-watermark~span{position:relative;z-index:1}
</style>
<style>
.summary-token-unit{display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:10px;padding-top:10px;border-top:1px solid rgba(126,111,91,.18)}.summary-token-unit-label{color:var(--muted);font-size:10px;font-weight:800;white-space:nowrap}.token-unit-slider{display:grid;grid-template-columns:minmax(140px,190px) auto;gap:1px 9px;align-items:center}.token-unit-slider input[type="range"]{width:100%;margin:0;accent-color:var(--accent);cursor:pointer}.token-unit-slider output{min-width:30px;color:var(--accent);font-size:11px;font-weight:850;text-align:right}.token-unit-scale{grid-column:1 / -1;display:flex;justify-content:space-between;color:var(--muted);font-size:9px;line-height:1}
h1{font-size:clamp(14px,1.5vw,20px)}
.brief-item .label{font-size:12px}
.brief-item .value.lcd-value{min-width:0;margin:0;padding:4px 8px;border:1px solid #3e7e6c;border-radius:7px;background:linear-gradient(180deg,#17372f,#102a25);color:#a6f3c9;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:18px;font-weight:800;letter-spacing:.035em;line-height:1.2;white-space:nowrap;text-shadow:0 0 5px rgba(166,243,201,.6);box-shadow:inset 0 2px 8px rgba(0,0,0,.28),0 2px 0 rgba(255,254,250,.4);font-variant-numeric:tabular-nums}
@media(max-width:650px){.brief-item .value.lcd-value{font-size:16px;padding:3px 6px}}
.hero-title-row{display:flex;align-items:center;min-width:0}.hero-title-row h1{min-width:0}.ring-return-button{position:fixed;z-index:120;top:14px;left:50%;transform:translateX(-50%);border:1px solid var(--accent);border-radius:999px;padding:8px 15px;background:rgba(255,254,250,.94);backdrop-filter:blur(12px);color:var(--accent);font-size:11px;font-weight:800;white-space:nowrap;box-shadow:0 8px 24px rgba(45,41,36,.18),0 0 0 3px rgba(59,139,120,.08)}.ring-return-button:hover,.ring-return-button:focus-visible{background:#fffefa;box-shadow:0 10px 28px rgba(45,41,36,.22),0 0 0 4px rgba(59,139,120,.14);outline:none}
@media(max-width:650px){.ring-return-button{top:10px;padding:7px 13px}}
.filter-row,.filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted)}.filter-row input[type="search"],.filters .content-search{min-width:min(100%,320px);flex:1}.filter-group{display:flex;align-items:center;gap:7px;margin:0;padding:0;border:0}.filter-group+ .filter-group{padding-left:12px;border-left:1px solid rgba(126,111,91,.3)}.filter-group legend{padding:0;color:var(--muted);font-size:10px;font-weight:800;white-space:nowrap}.filter-options{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.filter-option{display:inline-flex;align-items:center;gap:4px;padding:4px 7px;border:1px solid rgba(126,111,91,.24);border-radius:999px;background:rgba(255,254,250,.62);font-size:11px;line-height:1.2;white-space:nowrap}.filter-option:has(input:checked){border-color:var(--accent);color:var(--accent);background:rgba(59,139,120,.08)}.filter-option input{accent-color:var(--accent);margin:0}.filter-row>.check,.filters>.check{display:inline-flex;align-items:center;gap:4px}.filter-row>#visible-count,.filters>#visible-count{font-size:11px;white-space:nowrap}.token-unit-group .filter-option{min-width:28px;justify-content:center}.token-unit-group .filter-option:first-child{padding-left:8px;padding-right:8px}@media(max-width:720px){.filter-group+ .filter-group{padding-top:9px;padding-left:0;border-top:1px solid rgba(126,111,91,.3);border-left:0}.filter-row,.filters{align-items:stretch}.filter-group{width:100%}.filter-row input[type="search"],.filters .content-search{flex-basis:100%}}
</style>
</head>
<body>
<main>
  <section class="hero">
    <div class="hero-copy">
      <div class="eyebrow">Codex Token 使用报告</div>
      <h1>线程 Token 消耗</h1>
      <div class="subline" id="thread-meta"></div>
    </div>
    <aside class="summary-brief" aria-label="概览">
      <div class="brief-head"><span>概览</span><span class="sensitive" id="privacy-indicator"></span></div>
      <div class="brief-grid" id="summary-cards"></div>
      <div class="summary-token-unit"><label class="summary-token-unit-label" for="token-unit-slider">Token 单位</label><div class="token-unit-slider"><input id="token-unit-slider" type="range" min="0" max="3" step="1" value="0" data-token-unit-slider aria-label="Token 单位"><output id="token-unit-output" for="token-unit-slider">原始</output><div class="token-unit-scale" aria-hidden="true"><span>原始</span><span>K</span><span>M</span><span>B</span></div></div></div>
    </aside>
  </section>

  <details class="warning-box" id="warning-box">
    <summary id="warning-summary"></summary>
    <ul class="warning-list" id="warning-list"></ul>
  </details>

  <section class="analysis-shell">
    <nav class="analysis-tabs" role="tablist" aria-label="查看方式">
      <button class="analysis-tab active" id="tab-context" data-tab-target="context" role="tab" aria-selected="true" type="button">Token 与 Context</button>
      <button class="analysis-tab" id="tab-composition" data-tab-target="composition" role="tab" aria-selected="false" type="button">单轮 Token 构成</button>
      <button class="analysis-tab" id="tab-cumulative" data-tab-target="cumulative" role="tab" aria-selected="false" type="button">累计 Token</button>
      <button class="analysis-tab" id="tab-table" data-tab-target="table" role="tab" aria-selected="false" type="button">逐轮明细</button>
    </nav>
    <div class="analysis-controls">
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
        <input id="search" type="search" placeholder="搜索用户消息、轮次 ID 或模型……">
        <fieldset class="filter-group tool-filter-group" id="tool-filter" data-hide-on-cumulative aria-label="按工具类型筛选"><legend>工具</legend><div class="filter-options tool-filter-list" id="tool-filter-list"></div></fieldset>
        <fieldset class="filter-group status-filter-group" data-hide-on-cumulative aria-label="按轮次状态筛选"><legend>轮次</legend><div class="filter-options status-filter-list">
          <label class="filter-option"><input type="checkbox" data-status="complete" checked> 已完成</label>
          <label class="filter-option"><input type="checkbox" data-status="aborted" checked> 已中止</label>
          <label class="filter-option"><input type="checkbox" data-status="incomplete" checked> 未闭合</label>
        </div></fieldset>
        <span id="visible-count"></span>
        <button id="reset-button" type="button">清除筛选</button>
      </div>
    </div>
  </section>

  <section class="panel tab-panel" data-tab-panel="composition" role="tabpanel" hidden>
    <div class="panel-head">
      <div><h2>单轮 Token 构成</h2></div>
      <div class="controls">
        <button id="linear-button" class="active" type="button">线性</button>
        <button id="log-button" type="button">对数</button>
      </div>
    </div>
    <div class="filter-row legend" id="legend"></div>
    <div class="chart-scroll"><div class="chart-wrap" id="turn-chart-wrap"><svg id="turn-chart" role="img" aria-label="每轮 Token 使用图表"></svg></div></div>
  </section>

  <section class="panel tab-panel" data-tab-panel="cumulative" role="tabpanel" hidden>
    <div class="panel-head"><div><h2>累计 Token</h2></div></div>
    <div class="cumulative-wrap"><svg id="cumulative-chart" role="img" aria-label="累计 Token 使用图表"></svg></div>
  </section>

  <section class="panel tab-panel" data-tab-panel="context" role="tabpanel">
    <div class="panel-head">
      <div><h2>Token 消耗与 Context 占用</h2></div>
    </div>
    <div class="context-source-legend" id="context-source-legend"></div>
    <div class="context-radial-wrap"><svg id="context-radial-chart" role="img" aria-label="累计 Token 进度与 Context 占用率双环图"></svg></div>
  </section>

  <section class="panel tab-panel" data-tab-panel="table" role="tabpanel" hidden>
    <div class="panel-head"><div><h2>逐轮明细</h2></div></div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th data-sort="index">轮次</th><th data-sort="status">状态</th><th data-sort="startedAt">开始时间</th>
          <th data-sort="modelResponses">模型响应</th><th data-sort="cachedInput">缓存输入</th>
          <th data-sort="cacheWriteInput">缓存写入</th><th data-sort="otherNonCachedInput">其他输入</th>
          <th data-sort="ordinaryOutput">普通输出</th><th data-sort="reasoningOutput">推理输出</th>
          <th data-sort="total">Token 总量</th><th data-sort="cacheRate">缓存命中率</th>
          <th data-sort="contextTokens">Context 占用</th><th data-sort="contextRate">Context 占用率</th><th data-sort="prompt">用户输入</th>
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
<div class="tooltip" id="turn-tooltip" role="tooltip"></div>
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
    cachedInput: "缓存输入", cacheWriteInput: "缓存写入",
    otherNonCachedInput: cacheWriteAvailable ? "其他非缓存输入" : "非缓存输入（日志未提供写入明细）",
    ordinaryOutput: "普通输出", reasoningOutput: "推理输出", unclassified: "未分类调整"
  };
  const statusLabels = { complete: "已完成", aborted: "已中止", incomplete: "未闭合" };
  const segmentKeys = ["cachedInput", ...(cacheWriteAvailable ? ["cacheWriteInput"] : []), "otherNonCachedInput", "ordinaryOutput", "reasoningOutput", "unclassified"];
  const DEFAULT_TOOL_CATEGORIES = ["computer-use", "chrome-use", "imagegen", "web-search"];
  const state = { tab: "context", scale: "linear", tokenUnit: "raw", start: 1, end: Math.max(1, turns.length), search: "", toolCategories: new Set(DEFAULT_TOOL_CATEGORIES), statuses: new Set(["complete", "aborted", "incomplete"]), sort: "index", direction: 1, selected: null };
  const tooltip = byId("turn-tooltip");
  const TOOLTIP_MESSAGE_LIMIT = 800;
  const toolLabels = {"computer-use":"Computer Use","chrome-use":"Chrome Use / Browser Use",imagegen:"ImageGen","exec-reasoning":"Exec Reasoning",shell:"Shell / Terminal","code-interpreter":"Code Interpreter","web-search":"Web Search","file-search":"File Search",mcp:"MCP","function-calling":"Function Calling",other:"其他工具"};
  const toolColors = {"computer-use":"#4f78a8","chrome-use":"#3b8b78",imagegen:"#bd7556","exec-reasoning":"#9a8f84",shell:"#8c78bd","code-interpreter":"#6d8c45","web-search":"#4f9d87","file-search":"#d9874c",mcp:"#b35f79","function-calling":"#a56c3f",other:"#6f8fb7"};

  function byId(id) { return document.getElementById(id); }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function esc(value) { return String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[ch]); }
  function formatGroupedNumber(value) { const n = Number(value || 0); if (!Number.isFinite(n)) return "0"; const sign = n < 0 ? "-" : "", parts = String(Math.abs(n)).split("."), integer = parts[0].replace(/\B(?=(\d{4})+(?!\d))/g, "\u2009"); return sign + integer + (parts[1] ? "." + parts[1] : ""); }
  const TOKEN_UNIT_CONFIG = { raw: { divisor: 1, suffix: "" }, K: { divisor: 1e3, suffix: "K" }, M: { divisor: 1e6, suffix: "M" }, B: { divisor: 1e9, suffix: "B" } };
  const TOKEN_UNIT_ORDER = ["raw", "K", "M", "B"];
  const TOKEN_UNIT_LABELS = { raw: "原始", K: "K", M: "M", B: "B" };
  function formatTokenDisplay(value) { const n = Number(value || 0); if (!Number.isFinite(n)) return "0"; const config = TOKEN_UNIT_CONFIG[state.tokenUnit] || TOKEN_UNIT_CONFIG.raw; if (state.tokenUnit === "raw") return formatGroupedNumber(n); return (n / config.divisor).toFixed(1).replace(/\.0$/, "") + config.suffix; }
  function formatTokens(value) { return formatTokenDisplay(value); }
  function formatCount(value) { return formatGroupedNumber(value); }
  function isLcdValue(value) { return /^[\d\u2009.\-]+[KMB]?$/.test(String(value)); }
  function compact(value) { return formatTokenDisplay(value); }
  function syncTokenUnitInputs() { const slider = byId("token-unit-slider"), output = byId("token-unit-output"), index = TOKEN_UNIT_ORDER.indexOf(state.tokenUnit); if (slider) slider.value = String(Math.max(0, index)); if (output) output.textContent = TOKEN_UNIT_LABELS[state.tokenUnit] || TOKEN_UNIT_LABELS.raw; }
  function setTokenUnit(unit) { if (!TOKEN_UNIT_CONFIG[unit]) return; state.tokenUnit = unit; syncTokenUnitInputs(); renderHeader(); renderAll(); }
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
    byId("search").placeholder = messagesIncluded ? "搜索用户消息、轮次 ID 或模型……" : "搜索轮次 ID 或模型……";
    const u = report.summary.finalUsage;
    const cards = [
      ["累计 Token", formatTokens(u.total), "全部模型响应的输入与输出总和"],
      ["输入 Token", formatTokens(u.input), `${compact(u.cached)} 来自缓存`],
      ["未命中缓存的输入 Token", formatTokens(Math.max(0, u.input - u.cached)), `缓存命中率 ${(u.input ? 100*u.cached/u.input : 0).toFixed(2)}%`],
      ["输出 Token", formatTokens(u.output), `${compact(u.reasoning)} 为推理输出`],
      ["轮次", formatCount(report.summary.turnCount), `${report.summary.statusCounts.aborted || 0} 轮中止 · ${report.summary.zeroUsageTurns} 轮零消耗`],
      ["一致性", reconciliationOk() ? "完全一致" : "存在差异", reconciliationOk() ? "逐轮总和与最终计数一致" : "请查看数据完整性提醒"]
    ];
    byId("summary-cards").innerHTML = cards.map(([label,value,note]) => `<article class="brief-item"><div class="label">${esc(label)}</div><div class="value${isLcdValue(value) ? " lcd-value" : ""}">${esc(value)}</div><div class="note">${esc(note)}</div></article>`).join("");
    byId("footer").textContent = `生成时间：${dateText(report.metadata.generatedAt)} · 工具：${report.generator.name} ${report.generator.version} · 来源：${report.metadata.sourcePath}`;
  }

  function renderWarnings() {
    const list = byId("warning-list"), box = byId("warning-box");
    byId("warning-summary").textContent = `${report.summary.integrityErrorCount} 个数据完整性问题 · 共 ${report.summary.warningCount} 条提醒`;
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
    populateToolFilter();
    byId("token-unit-slider").addEventListener("input", event => setTokenUnit(TOKEN_UNIT_ORDER[Number(event.target.value)] || "raw"));
    document.querySelectorAll("[data-status]").forEach(input => input.addEventListener("change", () => {
      if (input.checked) state.statuses.add(input.dataset.status); else state.statuses.delete(input.dataset.status); renderAll();
    }));
    byId("reset-button").addEventListener("click", () => {
      state.start = 1; state.end = Math.max(1, turns.length); state.search = ""; state.toolCategories = new Set(DEFAULT_TOOL_CATEGORIES); state.statuses = new Set(["complete","aborted","incomplete"]); state.scale = "linear";
      start.value = 1; end.value = Math.max(1, turns.length); byId("search").value = "";
      syncToolFilterInputs();
      document.querySelectorAll("[data-status]").forEach(input => input.checked = true); syncScaleButtons(); renderAll();
    });
    document.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
      const key = th.dataset.sort; if (state.sort === key) state.direction *= -1; else { state.sort = key; state.direction = key === "index" ? 1 : -1; } renderTable(filteredTurns());
    }));
    document.querySelectorAll("[data-tab-target]").forEach(button => button.addEventListener("click", () => setTab(button.dataset.tabTarget)));
    byId("drawer-close").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", event => { if (event.key === "Escape") closeDrawer(); });
    document.addEventListener("click", event => {
      const drawer = byId("drawer");
      if (event.button !== 0 || !drawer.classList.contains("open")) return;
      const target = event.target instanceof Element ? event.target : null;
      if (!target || drawer.contains(target) || target.closest("[data-turn-target=true]")) return;
      closeDrawer();
    });
    renderLegend(); syncScaleButtons(); setTab(state.tab);
  }
  function setTab(tab) {
    if (!["composition","cumulative","context","table"].includes(tab)) tab = "context";
    state.tab = tab;
    document.querySelectorAll("[data-tab-target]").forEach(button => { const active = button.dataset.tabTarget === tab; button.classList.toggle("active", active); button.setAttribute("aria-selected", String(active)); });
    document.querySelectorAll("[data-tab-panel]").forEach(panel => { panel.hidden = panel.dataset.tabPanel !== tab; });
    document.querySelectorAll("[data-hide-on-cumulative]").forEach(control => { control.hidden = tab === "cumulative"; });
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

  function turnMatchesFilter(turn) {
      if (turn.index < state.start || turn.index > state.end || !state.statuses.has(turn.status)) return false;
      if (!state.search) return true;
      const haystack = [turn.turnId, turn.status, turn.models.join(" "), turn.efforts.join(" "), firstPrompt(turn)].join(" ").toLocaleLowerCase();
      return haystack.includes(state.search);
  }
  function syncToolFilterInputs() { byId("tool-filter-list").querySelectorAll("input[data-tool-category]").forEach(input => { input.checked = state.toolCategories.has(input.value); }); }
  function populateToolFilter() { const categories=[...new Set(turns.flatMap(turn=>(turn.toolCalls||[]).map(call=>call.category)))].sort((a,b)=>(toolLabels[a]||a).localeCompare(toolLabels[b]||b)); byId("tool-filter-list").innerHTML=categories.map(category=>`<label class="check"><input type="checkbox" data-tool-category="true" value="${esc(category)}"${state.toolCategories.has(category)?" checked":""}> ${esc(toolLabels[category]||category)}</label>`).join(""); byId("tool-filter-list").querySelectorAll("input[data-tool-category]").forEach(input=>input.addEventListener("change",event=>{if(event.target.checked)state.toolCategories.add(event.target.value);else state.toolCategories.delete(event.target.value);renderAll()})); }
  function filteredTurns() { return turns.filter(turnMatchesFilter); }
  function renderAll() {
    byId("range-start-value").textContent = state.start; byId("range-end-value").textContent = state.end; updateRangeFill();
    const visible = filteredTurns(); byId("visible-count").textContent = `显示 ${visible.length} 轮`;
    renderTurnChart(visible); renderTable(visible); renderCumulative(); renderContextRadial();
  }

  function svgEl(name, attrs = {}) { const el = document.createElementNS("http://www.w3.org/2000/svg", name); Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k, v)); return el; }
  function clearSvg(svg) { while (svg.firstChild) svg.removeChild(svg.firstChild); }
  function addText(svg, x, y, text, anchor = "end") { const node = svgEl("text", {x,y,"text-anchor":anchor,fill:css("--muted"),"font-size":"11"}); node.textContent = text; svg.appendChild(node); }
  function niceTicks(max, count = 5) {
    if (max <= 0) return [0]; const rough = max / count; const power = 10 ** Math.floor(Math.log10(rough)); const fraction = rough / power;
    const nice = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power; const result = [];
    for (let n = 0; n <= max + nice * .25; n += nice) result.push(n); return result;
  }
  function positionTooltip(event, target) {
    const targetRect = target?.getBoundingClientRect?.();
    const pointerX = Number.isFinite(event?.clientX) ? event.clientX : targetRect ? targetRect.left + targetRect.width / 2 : innerWidth / 2;
    const pointerY = Number.isFinite(event?.clientY) ? event.clientY : targetRect ? targetRect.top + targetRect.height / 2 : innerHeight / 2;
    tooltip.style.display = "block"; tooltip.style.visibility = "hidden";
    const gap = 14, edge = 8, width = tooltip.offsetWidth, height = tooltip.offsetHeight;
    let x = pointerX + gap, y = pointerY + gap;
    if (x + width > innerWidth - edge) x = pointerX - width - gap;
    if (y + height > innerHeight - edge) y = pointerY - height - gap;
    tooltip.style.left = `${Math.max(edge,Math.min(x,innerWidth-width-edge))}px`;
    tooltip.style.top = `${Math.max(edge,Math.min(y,innerHeight-height-edge))}px`;
    tooltip.style.visibility = "visible";
  }
  function showTooltip(event, turn) {
    const b = turn.breakdown;
    const message = tooltipMessage(turn);
    tooltip.innerHTML = `<strong>第 ${turn.index} 轮 · ${esc(statusText(turn.status))}</strong>` + segmentKeys.filter(key => b[key]).map(key => `<div class="row"><span>${esc(segmentLabels[key])}</span><b>${formatTokens(b[key])}</b></div>`).join("") + `<div class="row"><span>总量</span><b>${formatTokens(turn.usage.total)}</b></div><div style="margin-top:6px;color:var(--muted);max-height:48px;overflow:hidden">${esc(message.text)}</div>`;
    positionTooltip(event,event.currentTarget);
  }
  function previousTurnForTooltip(turn) { const sequence=[...turns].sort((a,b)=>Number(a.index)-Number(b.index)); const index=sequence.findIndex(candidate=>candidate.turnId===turn.turnId); return index>0?sequence[index-1]:null; }
  function initialMessagePreview(turn) {
    const message = (turn.messages || [])[0];
    if (!message) return {text:messagesIncluded ? "该轮未记录用户消息。" : "生成报告时已排除用户消息。",truncated:false};
    const full = String(message.text || "");
    if (!full) { const attachments=[message.imageCount?`${message.imageCount} 张图片`:"",message.audioCount?`${message.audioCount} 条音频`:""].filter(Boolean).join(" · "); return {text:attachments?`初始消息包含 ${attachments}，无文本。`:"初始用户消息没有文本。",truncated:false}; }
    return {text:full.slice(0,TOOLTIP_MESSAGE_LIMIT),truncated:full.length>TOOLTIP_MESSAGE_LIMIT};
  }
  function outputPreview(turn) {
    const outputs = (turn.outputs || []).filter(output => String(output.text || "").trim());
    const finalOutputs = outputs.filter(output => String(output.phase || "").toLowerCase() === "final_answer");
    const selected = (finalOutputs.length ? finalOutputs : outputs).at(-1);
    if (!selected) return {text:"该轮没有可读代理输出。",truncated:false};
    const full = String(selected.text || "");
    const characters = Array.from(full);
    return characters.length > TOOLTIP_MESSAGE_LIMIT
      ? {text:characters.slice(0, TOOLTIP_MESSAGE_LIMIT - 3).join("") + "...",truncated:true}
      : {text:full,truncated:false};
  }
  function tooltipMessage(turn) { return isSatelliteTurn(turn) ? outputPreview(turn) : initialMessagePreview(turn); }
  function tooltipMessageLabel(turn) { return isSatelliteTurn(turn) ? "代理输出" : "初始用户消息"; }
  function drawerOutputSection(turn) {
    if (!isSatelliteTurn(turn)) return "";
    const outputs = (turn.outputs || []).filter(output => String(output.text || "").trim());
    const text = outputs.length ? outputs.map(output => String(output.text || "")).join("\n\n") : "该轮没有可读代理输出。";
    return `<div class="message"><div class="message-head">代理输出${outputs.length ? ` · ${outputs.length} 条` : ""}</div><pre>${esc(text)}</pre></div>`;
  }
  function showTurnTooltip(event, turn, target, conversationTotal) {
    if (event?.pointerType === "touch") { hideTooltip(); return; }
    const b = turn.breakdown || {}, snapshot = contextSnapshot(turn), previous = previousTurnForTooltip(turn), previousSnapshot = previous ? contextSnapshot(previous) : null, message = tooltipMessage(turn);
    const tokenRows = segmentKeys.map(key => `<span>${esc(segmentLabels[key])}</span><b>${formatTokens(b[key]||0)}</b>`).join("");
    const contextPortion = snapshot.occupancyRate == null ? "—" : `${Number(snapshot.occupancyRate).toFixed(2)}%`;
    const contextAbsolute = snapshot.occupancyRate == null ? "未记录 Context 快照" : `${formatTokens(snapshot.tokens)} / ${snapshot.windowTokens == null ? "—" : formatTokens(snapshot.windowTokens)} Token`;
    const tokenPortion = conversationTotal>0 ? `${(100*Math.max(0,Number(turn.usage.total)||0)/conversationTotal).toFixed(2)}%` : "—";
    const tokenAbsolute = `${formatTokens(turn.usage.total)} / ${formatTokens(conversationTotal)} Token`;
    const contextDelta = previousSnapshot?.occupancyRate == null || snapshot.occupancyRate == null ? "" : (() => { const delta=Number(snapshot.occupancyRate)-Number(previousSnapshot.occupancyRate); if (Math.abs(delta)<0.005) return "无变化"; return delta>0 ? `增加了 ${delta.toFixed(2)}%` : `减少了 ${Math.abs(delta).toFixed(2)}%`; })();
    tooltip.innerHTML = `<div class="tooltip-title"><strong>第 ${turn.index} 轮</strong><span class="tooltip-badge">${esc(statusText(turn.status))}</span></div>`
      + `<div class="tooltip-metrics"><div class="tooltip-metric context"><span>Context 占用</span><strong>${esc(contextPortion)}${contextDelta?`<span class="tooltip-change">（${esc(contextDelta)}）</span>`:""}</strong><small>${esc(contextAbsolute)}</small></div><div class="tooltip-metric token"><span>本轮 Token 占比</span><strong>${esc(tokenPortion)}</strong><small>${esc(tokenAbsolute)}</small></div></div>`
      + `<div class="tooltip-grid"><span>来源</span><b>${esc(sourceLabel(turn))}</b><span>模型</span><b>${esc((turn.models||[]).join(", ")||"—")}</b><span>开始／结束</span><b>${esc(dateText(turn.startedAt))} — ${esc(dateText(turn.endedAt))}</b><span>快照</span><b>${esc(contextTypeText(snapshot.snapshotType))} · ${esc(dateText(snapshot.timestamp))}</b><span>Compaction</span><b>${formatCount(turn.compactions)}</b></div>`
      + `<div class="tooltip-section"><div class="tooltip-section-label">Token 构成 · 总量 ${formatTokens(turn.usage.total)}</div><div class="tooltip-grid">${tokenRows}</div></div>`
      + `<div class="tooltip-section"><div class="tooltip-section-label">${tooltipMessageLabel(turn)}</div><div class="tooltip-message">${esc(message.text)}</div>${message.truncated?'<div class="tooltip-truncated">已截断；全文见轮次详情</div>':""}</div>`;
    positionTooltip(event,target);
  }
  function moveTurnTooltip(event, target) { if (event.pointerType !== "touch" && tooltip.style.display === "block") positionTooltip(event,target); }
  function focusTurnTooltip(event, turn, target, conversationTotal) { if (target.matches(":focus-visible")) showTurnTooltip(event,turn,target,conversationTotal); }
  function turnTargetKeydown(event, turn) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); hideTooltip(); openDrawer(turn); } }
  function hideTooltip() { tooltip.style.display = "none"; tooltip.style.visibility = "hidden"; }

  function contextTypeText(type) {
    return ({turn_end:"结束时",range_latest:"范围内最新",current_latest:"当前最新",unknown:"未知"})[type] || "未知";
  }
  function contextSnapshot(turn) { return turn.contextSnapshot || {snapshotType:"unknown",tokens:null,windowTokens:null,occupancyRate:null,timestamp:null}; }
  function contextTimeline(turn, tokens) {
    const points=(turn.contextTimeline||[]).filter(point=>point.occupancyRate!=null).map(point=>({...point,turnTokenOffset:Math.max(0,Math.min(tokens,Number(point.turnTokenOffset)||0))})).sort((a,b)=>a.turnTokenOffset-b.turnTokenOffset||String(a.timestamp||"").localeCompare(String(b.timestamp||"")));
    if(points.length) return points;
    const fallback=contextSnapshot(turn);
    return fallback.occupancyRate==null?[]:[{...fallback,turnTokenOffset:tokens}];
  }
  function contextBands(turn, tokens) {
    if(tokens<=0) return [];
    const points=contextTimeline(turn,tokens);
    if(!points.length) return [{start:0,end:tokens,snapshot:null}];
    const bands=[];let cursor=0,active=null;
    points.forEach(point=>{if(point.turnTokenOffset>cursor){bands.push({start:cursor,end:point.turnTokenOffset,snapshot:active||point});cursor=point.turnTokenOffset}active=point});
    if(cursor<tokens) bands.push({start:cursor,end:tokens,snapshot:active||points.at(-1)});
    return bands;
  }
  function contextBandColor(rate) {
    if(rate==null) return css("--muted");
    const value=Math.max(0,Math.min(100,Number(rate)||0));
    return value>=85?"#c45657":value>=65?"#d28b3d":"#3b8b78";
  }
  function contextRateText(snapshot) { return snapshot.occupancyRate == null ? "—" : `${Number(snapshot.occupancyRate).toFixed(2)}%`; }
  const sourcePalette=["#3b8b78","#bd7556","#8c78bd","#d9874c","#4f78a8","#b35f79","#6d8c45","#a56c3f"];
  function sourceId(turn){return turn.sourceRolloutId||report.metadata.threadId||"main"}
  function sourceLabel(turn){return turn.sourceLabel||"主会话"}
  function isSatelliteTurn(turn){const kind=String(turn.sourceKind||report.metadata.sourceKind||"").toLowerCase();return kind==="subagent"||String(turn.sourceLabel||"").includes("子代理")}
  function radialEntries(ordered,denominator){let consumed=0;const entries=ordered.map(turn=>{const tokens=Math.max(0,Number(turn.usage.total)||0),entry={turn,tokens,tokenStart:consumed,start:consumed/denominator,end:(consumed+tokens)/denominator,satellite:isSatelliteTurn(turn),parentEntry:null,satelliteLane:0};consumed+=tokens;return entry});const siblingCounts=new Map();entries.forEach((entry,index)=>{if(!entry.satellite)return;let parent=null;for(let cursor=index-1;cursor>=0;cursor-=1){if(!entries[cursor].satellite){parent=entries[cursor];break}}if(!parent)parent=entries.find(candidate=>!candidate.satellite)||null;const key=parent?.turn.turnId||"orphan";entry.parentEntry=parent;entry.satelliteLane=siblingCounts.get(key)||0;siblingCounts.set(key,entry.satelliteLane+1)});return entries}
  function satelliteGeometry(cx,cy,outerOuter,entry){const inner=outerOuter+24+(entry.satelliteLane%5)*14;return{inner,outer:inner+10,contextRadius:inner-8}}
  function satelliteFraction(entry){return(entry.start+entry.end)/2}
  function toolCallLabel(call){return toolLabels[call.category]||call.name||"未知工具"}
  function toolCallColor(call){return toolColors[call.category]||toolColors.other}
  function toolField(call,key){return Array.isArray(call.usageKnown)&&call.usageKnown.includes(key)?formatTokens(call.usage?.[key]||0):"未知"}
  function toolUsage(call){return Array.isArray(call.usageKnown)&&call.usageKnown.includes("total")?Math.max(0,Number(call.usage?.total)||0):0}
  function toolTooltip(event,turn,call,target){const usage=toolUsage(call),status=Array.isArray(call.usageKnown)&&call.usageKnown.includes("total")?`${formatTokens(usage)} Token`:"Token 未知";tooltip.innerHTML=`<div class="tooltip-title"><strong>${esc(toolCallLabel(call))}</strong><span class="tooltip-badge">第 ${turn.index} 轮</span></div><div class="tooltip-grid"><span>语义工具</span><b>${esc(call.semanticTool||toolCallLabel(call))}</b><span>Provider</span><b>${esc(call.provider||"未知")}</b><span>识别方式</span><b>${esc(call.classificationSource||"raw")}</b><span>原始工具名</span><b>${esc(call.rawName||call.name||"未知")}</b><span>调用序号</span><b>#${formatCount(call.sequence||0)}</b><span>时间</span><b>${esc(dateText(call.timestamp))}</b><span>状态</span><b>${esc(call.status||"未知")}</b><span>Token</span><b>${esc(status)}</b></div>`;positionTooltip(event,target)}
  function openToolDrawer(turn,call){if(!turn||!call)return;state.selected=turn.turnId;byId("drawer-title").textContent=`${toolCallLabel(call)} · 第 ${turn.index} 轮`;const usage=call.usage||{},details=[["工具分类",toolCallLabel(call)],["语义工具",call.semanticTool||"未知"],["Provider",call.provider||"未知"],["识别方式",call.classificationSource||"raw"],["原始工具名",call.rawName||call.name||"未知"],["传输包装",call.transportWrapper?"是":"否"],["调用序号",`#${call.sequence||"—"}`],["调用时间",dateText(call.timestamp)],["结束时间",dateText(call.endedAt)],["状态",call.status||"未知"],["Token 归因",call.usageReported?"精确":"未知"],["总 Token",toolField(call,"total")],["输入",toolField(call,"input")],["缓存输入",toolField(call,"cached")],["输出",toolField(call,"output")],["推理输出",toolField(call,"reasoning")]];byId("drawer-body").innerHTML=`<div class="detail-grid">${details.map(d=>`<div class="detail"><span>${esc(d[0])}</span><b>${esc(d[1])}</b></div>`).join("")}</div><div class="message"><div class="message-head">父 turn</div><pre>第 ${turn.index} 轮 · ${esc(turn.turnId)} · ${formatTokens(turn.usage.total)} Token</pre></div>`;byId("drawer").classList.add("open");byId("drawer").setAttribute("aria-hidden","false");renderContextRadial();}
  function toolSatelliteGeometry(cx,cy,outerOuter,index){const inner=outerOuter+76+(index%3)*14;return{inner,outer:inner+9}}
  function renderToolLayer(svg,entries,cx,cy,outerOuter,denominator){entries.forEach(entry=>{const calls=(entry.turn.toolCalls||[]).filter(call=>state.toolCategories.has(call.category));if(!calls.length)return;const parentSpan=Math.max(0,entry.end-entry.start),envelopeStart=parentSpan?entry.start:Math.max(0,entry.start-.006),envelopeEnd=parentSpan?entry.end:Math.min(1,entry.start+.012),envelopePath=arcBandPath(cx,cy,outerOuter+54,outerOuter+60,envelopeStart,envelopeEnd);if(envelopePath)svg.appendChild(svgEl("path",{d:envelopePath,class:"tool-envelope"}));const exact=calls.reduce((sum,call)=>sum+toolUsage(call),0),unknown=calls.filter(call=>!call.usageReported).length;const envelopeTitle=svgEl("title");envelopeTitle.textContent=`第 ${entry.turn.index} 轮工具包络 · ${calls.length} 次调用 · ${formatTokens(exact)} Token · ${unknown} 次 Token 未知`;const envelopeNode=svg.lastChild;if(envelopeNode)envelopeNode.appendChild(envelopeTitle);calls.forEach((call,index)=>{const fraction=parentSpan?entry.start+parentSpan*(index+.5)/calls.length:entry.start,geometry=toolSatelliteGeometry(cx,cy,outerOuter,index),usage=toolUsage(call),width=usage>0?Math.min(Math.max(parentSpan*.22,.002),usage/denominator):0,start=usage>0?Math.max(entry.start,fraction-width/2):fraction,end=usage>0?Math.min(entry.end,start+width):fraction,group=svgEl("g",{class:`tool-satellite${state.selected===entry.turn.turnId?" selected":""}`,tabindex:"0",role:"button","data-tool-target":"true","aria-label":`${toolCallLabel(call)}，${call.usageReported?formatTokens(usage)+" Token":"Token 未知"}`}),parentPoint=radialPoint(cx,cy,outerOuter+7,fraction),satellitePoint=radialPoint(cx,cy,geometry.inner-1,fraction);group.appendChild(svgEl("line",{x1:satellitePoint.x,y1:satellitePoint.y,x2:parentPoint.x,y2:parentPoint.y,class:"tool-satellite-connector"}));if(usage>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,geometry.inner,geometry.outer,start,end),fill:toolCallColor(call),class:"token-sector"}));}else{const a=radialPoint(cx,cy,geometry.inner-3,fraction),b=radialPoint(cx,cy,geometry.outer+5,fraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"tool-satellite-unknown"}))}const title=svgEl("title");title.textContent=`${toolCallLabel(call)} · ${call.usageReported?formatTokens(usage)+" Token":"Token 未知"}`;group.appendChild(title);group.addEventListener("pointerenter",event=>toolTooltip(event,entry.turn,call,group));group.addEventListener("pointermove",event=>moveTurnTooltip(event,group));group.addEventListener("pointerleave",hideTooltip);group.addEventListener("focus",event=>toolTooltip(event,entry.turn,call,group));group.addEventListener("blur",hideTooltip);group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();hideTooltip();openToolDrawer(entry.turn,call)}});group.addEventListener("click",()=>{hideTooltip();openToolDrawer(entry.turn,call)});svg.appendChild(group)})})}
  function sourceColor(source){const value=String(source||"main");let hash=0;for(let i=0;i<value.length;i++)hash=(hash*31+value.charCodeAt(i))>>>0;return sourcePalette[hash%sourcePalette.length]}
  function contextOrderTime(turn){const snapshot=contextSnapshot(turn);return snapshot.timestamp||turn.endedAt||turn.rangeLastActivityAt||turn.startedAt||""}
  const contextGap=Math.PI/180,contextStart=-Math.PI/2+contextGap/2,contextSpan=2*Math.PI-contextGap;
  function radialPoint(cx,cy,r,fraction){const angle=contextStart+Math.max(0,Math.min(1,fraction))*contextSpan;return{x:cx+r*Math.cos(angle),y:cy+r*Math.sin(angle)}}
  function arcLinePath(cx,cy,r,start,end){const a=radialPoint(cx,cy,r,start),b=radialPoint(cx,cy,r,end),large=(end-start)*contextSpan>Math.PI?1:0;return`M${a.x.toFixed(2)},${a.y.toFixed(2)} A${r},${r} 0 ${large} 1 ${b.x.toFixed(2)},${b.y.toFixed(2)}`}
  function arcBandPath(cx,cy,inner,outer,start,end){if(end-start<=1e-9)return"";const a=radialPoint(cx,cy,outer,start),b=radialPoint(cx,cy,outer,end),c=radialPoint(cx,cy,inner,end),d=radialPoint(cx,cy,inner,start),large=(end-start)*contextSpan>Math.PI?1:0;return`M${a.x.toFixed(2)},${a.y.toFixed(2)} A${outer},${outer} 0 ${large} 1 ${b.x.toFixed(2)},${b.y.toFixed(2)} L${c.x.toFixed(2)},${c.y.toFixed(2)} A${inner},${inner} 0 ${large} 0 ${d.x.toFixed(2)},${d.y.toFixed(2)} Z`}
  function renderContextRadial(){
    const svg=byId("context-radial-chart");clearSvg(svg);svg.setAttribute("viewBox","0 0 760 620");
    const ordered=[...turns].sort((a,b)=>contextOrderTime(a).localeCompare(contextOrderTime(b))||a.index-b.index),cx=380,cy=300,innerBase=105,innerMax=178,outerInner=202,outerOuter=234;
    const sources=[...new Map(ordered.map(turn=>[sourceId(turn),{id:sourceId(turn),label:sourceLabel(turn)}])).values()];
    byId("context-source-legend").innerHTML=sources.map(source=>`<span style="--source-color:${sourceColor(source.id)}">${esc(source.label)}</span>`).join("");
    if(!ordered.length){addText(svg,cx,cy,"没有可显示的 turn","middle");return}
    const defs=svgEl("defs"),pattern=svgEl("pattern",{id:"context-unknown-pattern",width:8,height:8,patternUnits:"userSpaceOnUse",patternTransform:"rotate(35)"}),arrow=svgEl("marker",{id:"context-compaction-arrow",markerWidth:8,markerHeight:8,refX:7,refY:4,orient:"auto",markerUnits:"userSpaceOnUse"});pattern.appendChild(svgEl("line",{x1:0,y1:0,x2:0,y2:8,stroke:css("--muted"),"stroke-width":2,opacity:.42}));arrow.appendChild(svgEl("path",{d:"M0,0 L8,4 L0,8 Z",fill:css("--warning")}));defs.append(pattern,arrow);svg.appendChild(defs);
    svg.appendChild(svgEl("path",{d:arcBandPath(cx,cy,outerInner,outerOuter,0,1),fill:"#e9e2d7"}));
    svg.appendChild(svgEl("path",{d:arcBandPath(cx,cy,innerBase+(innerMax-innerBase)*.75,innerMax,0,1),class:"context-danger-zone"}));
    [25,50,75,100].forEach(rate=>{const capacity=rate===100,warning=rate===75,radius=innerBase+(innerMax-innerBase)*rate/100;svg.appendChild(svgEl("path",{d:arcLinePath(cx,cy,radius,0,1),class:capacity?"context-reference context-capacity":warning?"context-reference context-warning":"context-reference"}));const point=radialPoint(cx,cy,radius,0);const label=svgEl("text",{x:point.x+5,y:point.y+3,fill:capacity?css("--text"):warning?"#b06d2b":css("--muted"),"font-size":capacity||warning?"10":"9","font-weight":capacity||warning?"700":"400"});label.textContent=capacity?"Context 100%":`${rate}%`;svg.appendChild(label)});
    const total=ordered.reduce((sum,turn)=>sum+Math.max(0,Number(turn.usage.total)||0),0),denominator=Math.max(total,1);let consumed=0;
    const entries=radialEntries(ordered,denominator);
    const observed=ordered.flatMap(turn=>contextTimeline(turn,Math.max(0,Number(turn.usage.total)||0)).map(snapshot=>({turn,snapshot}))),peak=observed.reduce((best,item)=>Number(item.snapshot.occupancyRate)>Number(best?.snapshot?.occupancyRate??-1)?item:best,null),compactionCount=ordered.reduce((sum,turn)=>sum+(turn.contextCompactions||[]).length,0);
    const centerTitle=svgEl("text",{x:cx,y:cy-38,"text-anchor":"middle",fill:css("--muted"),"font-size":"13"}),centerValue=svgEl("text",{x:cx,y:cy-4,"text-anchor":"middle",fill:css("--text"),"font-size":"27","font-weight":"760"}),centerDetail=svgEl("text",{x:cx,y:cy+24,"text-anchor":"middle",fill:css("--text"),"font-size":"12"}),centerMeta=svgEl("text",{x:cx,y:cy+48,"text-anchor":"middle",fill:css("--muted"),"font-size":"11"});svg.append(centerTitle,centerValue,centerDetail,centerMeta);
    function resetCenter(){centerTitle.textContent="完整会话";centerValue.textContent=`${compact(total)} Token`;centerDetail.textContent=peak?`Context 峰值 ${contextRateText(peak.snapshot)}`:"Context 峰值未知";centerMeta.textContent=`${compactionCount} 次 Compaction`}
    function showCenter(turn){const snapshot=contextSnapshot(turn);centerTitle.textContent=`${sourceLabel(turn)} · ${statusText(turn.status)}`;centerValue.textContent=`${compact(turn.usage.total)} Token`;centerDetail.textContent=snapshot.tokens==null?"Context 未知":`${formatTokens(snapshot.tokens)} / ${snapshot.windowTokens==null?"—":formatTokens(snapshot.windowTokens)} Token`;centerMeta.textContent=snapshot.tokens==null?contextTypeText(snapshot.snapshotType):`${contextRateText(snapshot)} · ${contextTypeText(snapshot.snapshotType)}`}
    resetCenter();
    let previousSource=null;
    entries.forEach(entry=>{const {turn,tokens,tokenStart,start,end,satellite,parentEntry}=entry,snapshot=contextSnapshot(turn),source=sourceId(turn),color=sourceColor(source),knownContext=snapshot.tokens!=null&&snapshot.occupancyRate!=null,rate=Math.max(0,Math.min(100,Number(snapshot.occupancyRate)||0)),contextOuter=innerBase+(innerMax-innerBase)*rate/100,dim=!turnMatchesFilter(turn),sourceSwitch=previousSource!==null&&previousSource!==source,satelliteBand=satellite?satelliteGeometry(cx,cy,outerOuter,entry):null,turnFraction=satellite?satelliteFraction(entry):start;previousSource=source;
      const group=svgEl("g",{class:`context-turn context-sector${satellite?" satellite":""}${dim?" dim":""}${state.selected===turn.turnId?" selected":""}${sourceSwitch?" source-switch":""}`,tabindex:"0",role:"button","data-turn-target":"true","aria-label":`${sourceLabel(turn)}，${formatTokens(tokens)} Token，Context ${knownContext?contextRateText(snapshot):"未知"}`});
      if(satellite){const parentFraction=parentEntry?satelliteFraction(parentEntry):turnFraction,connectorStart=radialPoint(cx,cy,satelliteBand.inner-1,turnFraction),connectorEnd=radialPoint(cx,cy,outerOuter+8,parentFraction);group.appendChild(svgEl("line",{x1:connectorStart.x,y1:connectorStart.y,x2:connectorEnd.x,y2:connectorEnd.y,class:"satellite-connector"}));if(tokens>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,satelliteBand.inner,satelliteBand.outer,start,end),fill:color,class:"token-sector"}));}else{const a=radialPoint(cx,cy,satelliteBand.inner-3,turnFraction),b=radialPoint(cx,cy,satelliteBand.outer+8,turnFraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:color,class:"context-zero-tick"}))}const contextPoint=radialPoint(cx,cy,knownContext?satelliteBand.contextRadius+12*rate/100:satelliteBand.contextRadius,turnFraction);group.appendChild(svgEl("circle",{cx:contextPoint.x,cy:contextPoint.y,r:knownContext?3.5:3,fill:knownContext?contextBandColor(snapshot.occupancyRate):"none",class:knownContext?"satellite-context":"satellite-context-unknown"}))}
      else if(tokens>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,outerInner,outerOuter,start,end),fill:color,class:"token-sector"}));contextBands(turn,tokens).forEach(band=>{const bandStart=(tokenStart+band.start)/denominator,bandEnd=(tokenStart+band.end)/denominator,bandRate=band.snapshot?.occupancyRate,knownBand=bandRate!=null,bandOuter=innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(bandRate)||0))/100;group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,innerBase,knownBand?Math.max(innerBase+1.5,bandOuter):innerMax,bandStart,bandEnd),fill:knownBand?color:"url(#context-unknown-pattern)",opacity:knownBand?.22:.7,class:"context-band"}));if(knownBand){group.appendChild(svgEl("path",{d:arcLinePath(cx,cy,Math.max(innerBase+1.5,bandOuter),bandStart,bandEnd),stroke:contextBandColor(bandRate),class:"context-contour"}))}});const latest=contextTimeline(turn,tokens).at(-1);if(latest?.occupancyRate!=null){const latestFraction=(tokenStart+Math.max(0,Math.min(tokens,Number(latest.turnTokenOffset)||tokens)))/denominator,latestRadius=innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(latest.occupancyRate)||0))/100,latestPoint=radialPoint(cx,cy,Math.max(innerBase+1.5,latestRadius),latestFraction);group.appendChild(svgEl("circle",{cx:latestPoint.x,cy:latestPoint.y,r:4.8,fill:contextBandColor(latest.occupancyRate),class:"context-current-marker"}))}}
      else{const a=radialPoint(cx,cy,innerBase-4,start),b=radialPoint(cx,cy,outerOuter+9,start);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:color,class:"context-zero-tick"}));if(knownContext){const p=radialPoint(cx,cy,Math.max(innerBase+1.5,contextOuter),start);group.appendChild(svgEl("circle",{cx:p.x,cy:p.y,r:4,fill:color,stroke:"#fffefa","stroke-width":1.5}))}}
      if(!satellite)[start,end].forEach(fraction=>{const a=radialPoint(cx,cy,innerBase,fraction),b=radialPoint(cx,cy,outerOuter,fraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"mapping-line"}))});
      const hitInner=satellite?satelliteBand.inner-9:innerBase-7,hitOuter=satellite?satelliteBand.outer+9:outerOuter+8,hitEnd=tokens>0?end:Math.min(1,start+.004);group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,hitInner,hitOuter,satellite?start:turnFraction,hitEnd),fill:"rgba(0,0,0,.001)"}));group.addEventListener("pointerenter",event=>{showCenter(turn);showTurnTooltip(event,turn,group,total)});group.addEventListener("pointermove",event=>moveTurnTooltip(event,group));group.addEventListener("pointerleave",()=>{resetCenter();hideTooltip()});group.addEventListener("focus",event=>{showCenter(turn);focusTurnTooltip(event,turn,group,total)});group.addEventListener("blur",()=>{resetCenter();hideTooltip()});group.addEventListener("keydown",event=>turnTargetKeydown(event,turn));group.addEventListener("click",()=>{hideTooltip();openDrawer(turn)});svg.appendChild(group);
      (turn.contextCompactions||[]).forEach(event=>{const offset=Math.max(0,Math.min(tokens,Number(event.turnTokenOffset)||0)),fraction=(entry.tokenStart+offset)/denominator,marker=svgEl("g",{class:`context-compaction${satellite?" satellite-compaction":""}`,tabindex:"0",role:"button","data-turn-target":"true","aria-label":`Compaction，累计 Token 位置 ${((fraction)*100).toFixed(2)}%`}),positionOuter=radialPoint(cx,cy,satellite?satelliteBand.outer+8:outerOuter+12,fraction),beforeRate=event.before?.occupancyRate,afterRate=event.after?.occupancyRate;if(satellite){const point=radialPoint(cx,cy,satelliteBand.contextRadius,fraction);marker.appendChild(svgEl("line",{x1:positionOuter.x,y1:positionOuter.y,x2:point.x,y2:point.y,class:"compaction-position-line"}));marker.appendChild(svgEl("circle",{cx:point.x,cy:point.y,r:4,class:"compaction-after"}))}else if(beforeRate!=null&&afterRate!=null){const before=radialPoint(cx,cy,innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(beforeRate)))/100,fraction),after=radialPoint(cx,cy,innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(afterRate)))/100,fraction);marker.appendChild(svgEl("line",{x1:positionOuter.x,y1:positionOuter.y,x2:before.x,y2:before.y,class:"compaction-position-line"}));marker.appendChild(svgEl("line",{x1:before.x,y1:before.y,x2:after.x,y2:after.y,class:"compaction-jump-line","marker-end":"url(#context-compaction-arrow)"}));marker.appendChild(svgEl("circle",{cx:before.x,cy:before.y,r:4,class:"compaction-before"}));marker.appendChild(svgEl("circle",{cx:after.x,cy:after.y,r:4,class:"compaction-after"}))}else{const positionInner=radialPoint(cx,cy,innerMax+5,fraction);marker.appendChild(svgEl("line",{x1:positionOuter.x,y1:positionOuter.y,x2:positionInner.x,y2:positionInner.y,class:"compaction-position-line"}))}const title=svgEl("title");title.textContent=`Compaction · ${dateText(event.timestamp)} · ${event.before?.tokens==null?"未知":formatTokens(event.before.tokens)} → ${event.after?.tokens==null?"未知":formatTokens(event.after.tokens)} Context Token`;marker.appendChild(title);marker.addEventListener("focus",()=>showCenter(turn));marker.addEventListener("blur",resetCenter);marker.addEventListener("keydown",keyEvent=>turnTargetKeydown(keyEvent,turn));marker.addEventListener("click",()=>{hideTooltip();openDrawer(turn)});svg.appendChild(marker)})
    });
    renderToolLayer(svg,entries,cx,cy,outerOuter,denominator);
    const compactionMarkers=[...svg.querySelectorAll(".context-compaction")];let compactionIndex=0;entries.forEach(entry=>{const count=(entry.turn.contextCompactions||[]).length;if(entry.satellite){const geometry=satelliteGeometry(cx,cy,outerOuter,entry);for(let index=0;index<count;index+=1){const event=entry.turn.contextCompactions[index],offset=Math.max(0,Math.min(entry.tokens,Number(event.turnTokenOffset)||0)),fraction=(entry.tokenStart+offset)/denominator,from=radialPoint(cx,cy,outerOuter+12,fraction),to=radialPoint(cx,cy,geometry.outer+8,fraction),marker=compactionMarkers[compactionIndex+index];if(marker){marker.classList.add("satellite-compaction");marker.setAttribute("transform",`translate(${(to.x-from.x).toFixed(2)} ${(to.y-from.y).toFixed(2)})`)}}}compactionIndex+=count});
    const startPoint=radialPoint(cx,cy,outerOuter+18,0),endPoint=radialPoint(cx,cy,outerOuter+18,1);[[startPoint,"Token 0%","start"],[endPoint,"Token 100%","end"]].forEach(([point,label,anchor])=>{const node=svgEl("text",{x:point.x,y:point.y+4,"text-anchor":anchor,fill:css("--muted"),"font-size":"11","font-weight":"700"});node.textContent=label;svg.appendChild(node)});
    const outerLabel=svgEl("text",{x:24,y:33,fill:css("--muted"),"font-size":"12","font-weight":"700"});outerLabel.textContent="主圈 · Token 消耗（累计 Token 进度）";svg.appendChild(outerLabel);const satelliteLabel=svgEl("text",{x:24,y:53,fill:css("--muted"),"font-size":"12"});satelliteLabel.textContent="卫星层 · 子 agent Token（连接至主 turn）";svg.appendChild(satelliteLabel);const innerLabel=svgEl("text",{x:24,y:73,fill:css("--muted"),"font-size":"12"});innerLabel.textContent="内环 · Context 快照（按 Token 位置阶梯变化）";svg.appendChild(innerLabel)
  }

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
      const group = svgEl("g", {class:`bar${state.selected===turn.turnId?" selected":""}`, tabindex:"0", role:"button", "data-turn-target":"true", "aria-label":`第 ${turn.index} 轮，${formatTokens(total)} Token`});
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
    if (key === "cacheRate") return cacheRate(turn); if (key === "contextTokens") return Number(contextSnapshot(turn).tokens) || 0; if (key === "contextRate") return Number(contextSnapshot(turn).occupancyRate) || 0; if (key === "prompt") return firstPrompt(turn).toLocaleLowerCase(); return turn[key] ?? "";
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
      total:numericMax(visible,t=>t.usage.total), contextTokens:numericMax(visible,t=>contextSnapshot(t).tokens)
    };
    byId("table-empty").hidden=sorted.length>0;
    byId("turn-table-body").innerHTML=sorted.map(turn=>{const b=turn.breakdown,prompt=firstPrompt(turn).replace(/\s+/g," ").trim(),rate=cacheRate(turn),snapshot=contextSnapshot(turn),contextTitle=`${contextTypeText(snapshot.snapshotType)} · ${dateText(snapshot.timestamp)}`;return `<tr data-turn-id="${esc(turn.turnId)}" data-turn-target="true" class="${state.selected===turn.turnId?"selected":""}"><td>${turn.index}</td><td><span class="status ${esc(statusText(turn.status))}">${esc(statusText(turn.status))}</span></td><td>${esc(dateText(turn.startedAt))}</td><td class="heat-cell" style="${heatStyle(turn.modelResponses,maxima.modelResponses)}">${formatCount(turn.modelResponses)}</td><td class="heat-cell" style="${heatStyle(b.cachedInput,maxima.cachedInput)}">${formatTokens(b.cachedInput)}</td><td${cacheWriteAvailable?` class="heat-cell" style="${heatStyle(b.cacheWriteInput,maxima.cacheWriteInput)}"`:""}>${cacheWriteAvailable?formatTokens(b.cacheWriteInput):"不适用"}</td><td class="heat-cell" style="${heatStyle(b.otherNonCachedInput,maxima.otherNonCachedInput)}">${formatTokens(b.otherNonCachedInput)}</td><td class="heat-cell" style="${heatStyle(b.ordinaryOutput,maxima.ordinaryOutput)}">${formatTokens(b.ordinaryOutput)}</td><td class="heat-cell" style="${heatStyle(b.reasoningOutput,maxima.reasoningOutput)}">${formatTokens(b.reasoningOutput)}</td><td class="heat-cell" style="${heatStyle(turn.usage.total,maxima.total)}"><b>${formatTokens(turn.usage.total)}</b></td><td class="heat-cell" style="${heatStyle(rate,100,true)}">${rate.toFixed(2)}%</td><td class="heat-cell" title="${esc(contextTitle)}" style="${snapshot.tokens==null?"":heatStyle(snapshot.tokens,maxima.contextTokens)}">${snapshot.tokens==null?"—":formatTokens(snapshot.tokens)}</td><td title="${esc(contextTitle)}">${esc(contextRateText(snapshot))}</td><td class="prompt-cell" title="${esc(prompt)}">${esc(prompt||"—")}</td></tr>`;}).join("");
    byId("turn-table-body").querySelectorAll("tr").forEach(row=>row.addEventListener("click",()=>openDrawer(turns.find(t=>t.turnId===row.dataset.turnId))));
  }

  function openDrawer(turn) {
    if (!turn) return; state.selected=turn.turnId; byId("drawer-title").textContent=`第 ${turn.index} 轮`; const b=turn.breakdown,snapshot=contextSnapshot(turn);
    const details=[["状态",statusText(turn.status)],["轮次 ID",turn.turnId],["开始时间",dateText(turn.startedAt)],["持续时间",durationText(turn.durationMs)],["模型",turn.models.join(", ")||"—"],["推理强度",turn.efforts.join(", ")||"—"],["模型响应",formatCount(turn.modelResponses)],["Token 快照",formatCount(turn.tokenSnapshots)],["上下文快照类型",contextTypeText(snapshot.snapshotType)],["上下文占用",snapshot.tokens==null?"—":formatTokens(snapshot.tokens)],["上下文窗口",snapshot.windowTokens==null?"—":formatTokens(snapshot.windowTokens)],["上下文占用率",contextRateText(snapshot)],["上下文快照时间",dateText(snapshot.timestamp)],["上下文压缩",formatCount(turn.compactions)],["总 Token",formatTokens(turn.usage.total)],["输入",formatTokens(turn.usage.input)],["输出",formatTokens(turn.usage.output)]];
    let body=`<div class="detail-grid">${details.map(([k,v])=>`<div class="detail-item"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
    body+=`<div class="message"><div class="message-head">Token 构成</div><pre>${segmentKeys.map(key=>`${segmentLabels[key]}：${formatTokens(b[key]||0)}`).join("\n")}</pre></div>`;
    if((turn.contextCompactions||[]).length){const compactionLines=turn.contextCompactions.map((event,index)=>{const side=value=>value?`${formatTokens(value.tokens)} / ${value.windowTokens==null?"—":formatTokens(value.windowTokens)} · ${contextRateText(value)}`:"未知";return `#${index+1} · ${dateText(event.timestamp)}\n  turn 内 Token 位置：${event.turnTokenOffset==null?"未知":formatTokens(event.turnTokenOffset)}\n  压缩前：${side(event.before)}\n  压缩后：${side(event.after)}`});body+=`<div class="message"><div class="message-head">Compaction 前后上下文</div><pre>${esc(compactionLines.join("\n\n"))}</pre></div>`;}
    if (isSatelliteTurn(turn)) body+=drawerOutputSection(turn);
    else if (turn.messages.length) body+=turn.messages.map((m,i)=>`<section class="message"><div class="message-head">${i===0?"初始用户消息":"追加用户消息"} · ${esc(dateText(m.timestamp))}${m.imageCount?` · ${m.imageCount} 张图片`:""}${m.audioCount?` · ${m.audioCount} 条音频`:""}</div><pre></pre></section>`).join("");
    else body+=`<div class="message"><div class="message-head">用户消息</div><pre>${messagesIncluded?"该轮未记录用户消息。":"生成报告时已排除用户消息。"}</pre></div>`;
    byId("drawer-body").innerHTML=body;
    byId("drawer-body").querySelectorAll("section.message pre").forEach((pre,i)=>{pre.textContent=turn.messages[i].text;});
    byId("drawer").classList.add("open"); byId("drawer").setAttribute("aria-hidden","false"); renderTurnChart(filteredTurns()); renderTable(filteredTurns()); renderContextRadial();
  }
  function closeDrawer() { state.selected=null; byId("drawer").classList.remove("open"); byId("drawer").setAttribute("aria-hidden","true"); renderTurnChart(filteredTurns()); renderTable(filteredTurns()); renderContextRadial(); }

  try { renderHeader(); renderWarnings(); configureControls(); renderAll(); document.body.dataset.reportReady="true"; }
  catch (error) { document.body.dataset.reportReady="error"; const pre=document.createElement("pre");pre.className="empty";pre.textContent=`报告渲染失败：${error.stack||error}`;document.body.prepend(pre);console.error(error); }
})();
</script>
</body>
</html>
"""


RANGE_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>__PAGE_TITLE__</title>
<style>
:root{--bg:#f5f2ec;--panel:#fffefa;--panel2:#f0e9dd;--text:#2d2924;--muted:#756e64;--border:#ddd5c9;--accent:#3b8b78;--danger:#c95561;--warning:#b77a26;--cached:#4f9d87;--cache-write:#8c78bd;--uncached:#d9874c;--output:#dca83e;--reasoning:#cf6f78;--unclassified:#928a80;--shadow:0 16px 42px rgba(92,75,54,.12)}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% -15%,rgba(87,166,141,.16),transparent 38rem),var(--bg);color:var(--text);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}button,input{font:inherit}button{cursor:pointer;color:inherit}.shell{display:grid;grid-template-columns:300px minmax(0,1fr);min-height:100vh;transition:grid-template-columns .2s ease}.shell.session-nav-closed{grid-template-columns:0 minmax(0,1fr)}.sidebar{position:sticky;z-index:60;top:0;width:300px;height:100vh;overflow:auto;padding:22px 16px;background:rgba(238,231,220,.96);border-right:1px solid var(--border);backdrop-filter:blur(18px);transition:transform .2s ease,opacity .2s ease,visibility .2s}.shell.session-nav-closed .sidebar{transform:translateX(-104%);opacity:0;visibility:hidden;pointer-events:none}.sidebar-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}.sidebar-head button,.session-drawer-toggle{border:1px solid var(--border);border-radius:9px;background:var(--panel);padding:7px 9px}.brand{padding:0 8px 16px;min-width:0;flex:1}.eyebrow{color:var(--accent);text-transform:uppercase;letter-spacing:.14em;font-size:11px;font-weight:800}.brand h2{margin:5px 0 4px;font-size:20px}.muted{color:var(--muted)}.nav-search,.content-search{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:9px;background:var(--panel);color:var(--text)}.session-list{display:grid;gap:7px;margin-top:12px}.session-button{width:100%;text-align:left;padding:11px;border:1px solid transparent;border-radius:11px;background:transparent}.session-button:hover,.session-button.active{background:var(--panel);border-color:var(--accent);box-shadow:0 6px 18px rgba(92,75,54,.08)}.session-button strong,.session-button span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.session-button span{color:var(--muted);font-size:11px;margin-top:3px}.session-drawer-backdrop{display:none;position:fixed;z-index:55;inset:0;border:0;border-radius:0;padding:0;background:rgba(45,41,36,.28)}.content{min-width:0;padding:28px clamp(16px,3vw,44px) 64px}.content-topbar{display:flex;align-items:center;min-height:36px;margin-bottom:10px}.session-drawer-toggle{display:inline-flex;align-items:center;gap:7px}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:20px}.hero h1{font-size:clamp(28px,4vw,46px);line-height:1.08;margin:7px 0 9px;letter-spacing:-.035em}.subline{color:var(--muted);overflow-wrap:anywhere}.sensitive{color:#984b55;background:#fae9e8;border:1px solid #e8c4c2;border-radius:999px;padding:7px 11px;font-size:12px;white-space:nowrap}.sensitive.safe{color:#3f765f;background:#e8f3eb;border-color:#c3ddcb}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:11px;margin:20px 0}.card,.panel{background:linear-gradient(180deg,rgba(255,254,250,.98),rgba(252,249,243,.98));border:1px solid var(--border);box-shadow:var(--shadow)}.card{padding:15px;border-radius:14px;min-height:100px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.07em}.value{font-size:clamp(21px,2.2vw,30px);font-weight:760;margin-top:7px;font-variant-numeric:tabular-nums}.note{color:var(--muted);font-size:11px}.panel{border-radius:16px;margin-top:15px;overflow:hidden}.panel-head{padding:17px 19px 12px;display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}.panel-head h2{font-size:18px;margin:0}.panel-head p{color:var(--muted);margin:3px 0 0}.controls{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.controls button{border:1px solid var(--border);border-radius:8px;padding:7px 9px;background:var(--panel)}.controls button.active{border-color:var(--accent);color:var(--accent)}.filters{padding:0 19px 14px;display:flex;gap:11px;align-items:center;flex-wrap:wrap;color:var(--muted)}.filters .content-search{min-width:260px;flex:1}.check{display:inline-flex;align-items:center;gap:4px}.legend{padding:0 19px 13px;display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:11px}.legend span:before{content:"";display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;background:var(--swatch)}.chart-scroll,.table-wrap{overflow:auto;border-top:1px solid var(--border)}.chart-wrap{min-width:900px;padding:8px 10px 2px}.trend-wrap{padding:4px 16px 12px;overflow:auto}.trend-wrap svg{min-width:760px}svg{display:block;width:100%;height:auto}.grid{stroke:rgba(117,110,100,.2)}.bar{cursor:pointer}.bar:hover{filter:brightness(1.16)}table{border-collapse:separate;border-spacing:0;width:100%;min-width:1430px}th{position:sticky;top:0;z-index:2;background:#eee7dc;color:#5f584f;text-align:right;padding:10px;border-bottom:1px solid var(--border);font-size:10px;letter-spacing:.04em;text-transform:uppercase}th:first-child,th:nth-child(2),th:nth-child(3),th:last-child,td:first-child,td:nth-child(2),td:nth-child(3),td:last-child{text-align:left}td{padding:9px 10px;border-bottom:1px solid rgba(126,111,91,.18);text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}tbody tr{cursor:pointer}tbody tr:hover{outline:1px solid rgba(59,139,120,.3);outline-offset:-1px}.title-cell{max-width:390px;overflow:hidden;text-overflow:ellipsis}.warning-box{margin:15px 0;border:1px solid var(--border);border-radius:12px;overflow:hidden}.warning-box summary{cursor:pointer;padding:12px 15px;color:var(--warning);font-weight:700}.warning-list{max-height:260px;overflow:auto;margin:0;padding:4px 18px 14px 34px}.warning-list li{margin:5px 0;color:var(--muted)}.warning-list li.error{color:#a74450}.empty{padding:36px;text-align:center;color:var(--muted)}.footer{text-align:center;color:var(--muted);font-size:11px;margin-top:25px}.drawer{position:fixed;z-index:50;top:0;right:0;width:min(620px,94vw);height:100vh;transform:translateX(104%);transition:transform .2s;background:#fbf8f2;border-left:1px solid var(--border);box-shadow:-24px 0 60px rgba(92,75,54,.22);display:flex;flex-direction:column}.drawer.open{transform:translateX(0)}.drawer-head{padding:18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:10px}.drawer-head h2{margin:2px 0}.drawer-head button{border:1px solid var(--border);border-radius:8px;background:var(--panel);padding:7px 10px}.drawer-body{overflow:auto;padding:16px 18px 50px}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.detail{padding:9px;border:1px solid var(--border);border-radius:9px;background:var(--panel)}.detail span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.detail b{display:block;margin-top:3px;overflow-wrap:anywhere}.message{margin-top:12px;border:1px solid var(--border);border-radius:10px;overflow:hidden}.message-head{padding:7px 10px;background:var(--panel2);color:var(--muted);font-size:11px}.message pre{margin:0;padding:11px;white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}.provisional{color:var(--warning)}.tool-filter-group{display:flex;align-items:center;gap:7px;margin:0;padding:0;border:0}.tool-filter-group legend{color:var(--muted);font-size:11px;font-weight:700}.tool-filter-list{display:flex;gap:7px;flex-wrap:wrap}.tool-filter-list .check{padding:4px 7px;border:1px solid rgba(126,111,91,.24);border-radius:999px;background:rgba(255,254,250,.62);white-space:nowrap}.tool-filter-list .check:has(input:checked){border-color:var(--accent);color:var(--accent);background:rgba(59,139,120,.08)}.tool-filter-list input{accent-color:var(--accent)}
.context-radial-wrap{padding:4px 18px 18px;border-top:1px solid var(--border)}.context-radial-wrap svg{width:min(100%,900px);margin:auto;max-height:680px}.source-legend{display:flex;justify-content:center;gap:11px;flex-wrap:wrap;padding:0 18px 12px;color:var(--muted);font-size:11px}.source-legend span:before{content:"";display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px;background:var(--source-color)}.context-turn{cursor:pointer;transition:opacity .14s ease,filter .14s ease}.context-turn.dim{opacity:.14}.context-turn:hover,.context-turn:focus,.context-turn.selected{filter:brightness(1.12) saturate(1.15);outline:none}.context-turn .mapping-line{stroke:var(--text);stroke-width:1;opacity:.2}.context-turn:hover .mapping-line,.context-turn:focus .mapping-line,.context-turn.selected .mapping-line{stroke-width:2.4;opacity:.78}.context-turn .token-sector{stroke:#fffefa;stroke-width:1.2}.context-turn.source-switch .token-sector{stroke-width:4}.context-reference{fill:none;stroke:rgba(117,110,100,.18);stroke-width:1;stroke-dasharray:3 4}.context-reference.context-capacity{stroke:rgba(45,41,36,.75);stroke-width:2.5;stroke-dasharray:none}.context-compaction{cursor:pointer}.context-compaction line{stroke:var(--warning);stroke-width:3}.context-compaction circle{fill:#fffefa;stroke:var(--warning);stroke-width:2}.context-zero-tick{stroke-width:2;opacity:.78}
.context-compaction .compaction-position-line{stroke-dasharray:4 4;opacity:.78}.context-compaction .compaction-jump-line{stroke-width:3.5}.context-compaction .compaction-after{fill:var(--warning)}.tool-envelope{fill:none;stroke:#6f8fb7;stroke-width:5;stroke-dasharray:2 5;opacity:.72}.tool-satellite{cursor:pointer}.tool-satellite .token-sector{stroke:#fffefa;stroke-width:1.4}.tool-satellite-unknown{fill:none;stroke:#6f8fb7;stroke-width:2;stroke-dasharray:2 3}.tool-satellite-connector{stroke:#6f8fb7;stroke-width:1.1;stroke-dasharray:2 4;opacity:.55}.tool-satellite:hover .tool-satellite-connector,.tool-satellite:focus .tool-satellite-connector,.tool-satellite.selected .tool-satellite-connector{stroke:#3b6d9b;stroke-width:2.4;opacity:1}.tool-satellite-label{fill:#52749b;font-size:10px;font-weight:700}
.tooltip{position:fixed;z-index:80;pointer-events:none;display:none;visibility:hidden;width:min(440px,calc(100vw - 16px));max-height:calc(100vh - 16px);overflow:hidden;padding:12px 13px;border:1px solid var(--border);border-radius:11px;background:#fffefa;box-shadow:var(--shadow);font-size:12px;transition:none}.tooltip-title{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:8px}.tooltip-title strong{font-size:13px}.tooltip-badge{flex:none;border-radius:999px;padding:2px 7px;background:var(--panel2);color:var(--accent);font-size:10px;font-weight:750}.tooltip-metrics{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:8px 0}.tooltip-metric{min-width:0;padding:9px 10px;border:1px solid color-mix(in srgb,var(--metric-color) 28%,var(--border));border-radius:9px;background:color-mix(in srgb,var(--metric-color) 8%,#fffefa)}.tooltip-metric.context{--metric-color:var(--accent)}.tooltip-metric.token{--metric-color:var(--uncached)}.tooltip-metric span{display:block;color:var(--muted);font-size:10px;font-weight:750}.tooltip-metric strong{display:block;margin:3px 0 1px;color:var(--metric-color);font-size:20px;line-height:1.1;font-variant-numeric:tabular-nums}.tooltip-metric small{display:block;min-height:30px;color:var(--text);font-size:10px;line-height:1.4;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}.tooltip-grid{display:grid;grid-template-columns:auto minmax(0,1fr);gap:3px 12px;padding-top:7px;border-top:1px solid rgba(126,111,91,.18)}.tooltip-grid span{color:var(--muted)}.tooltip-grid b{min-width:0;overflow-wrap:anywhere;text-align:right;font-weight:650;font-variant-numeric:tabular-nums}.tooltip-section{margin-top:8px;padding-top:7px;border-top:1px solid rgba(126,111,91,.18)}.tooltip-section-label{color:var(--muted);font-size:10px;font-weight:750;letter-spacing:.06em;text-transform:uppercase}.tooltip-message{max-height:min(230px,30vh);margin-top:4px;overflow:hidden;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--text);font:11.5px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}.tooltip-truncated{margin-top:4px;color:var(--warning);font-size:10px}
@media(max-width:900px){body.session-nav-modal-open{overflow:hidden}.shell,.shell.session-nav-closed{display:block}.sidebar{position:fixed;left:0;transform:translateX(-104%);opacity:0;visibility:hidden;width:min(320px,88vw);border-right:1px solid var(--border)}.shell.session-nav-open .sidebar{transform:translateX(0);opacity:1;visibility:visible;pointer-events:auto}.shell.session-nav-open .session-drawer-backdrop{display:block}.content{padding-top:20px}}@media(max-width:650px){.hero{flex-direction:column}.detail-grid{grid-template-columns:1fr}}
.sidebar{overflow-x:hidden}.session-list{min-width:0}.session-button{min-width:0;max-width:100%}
</style>
<style>
.hero-copy{min-width:0;flex:1}.summary-brief{width:min(560px,48%);padding:12px 14px;border:1px solid var(--border);border-radius:14px;background:rgba(255,254,250,.86);box-shadow:var(--shadow)}.brief-head{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px;color:var(--muted);font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.brief-head .sensitive{font-size:10px;letter-spacing:0;text-transform:none;padding:4px 8px}.brief-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 16px}.brief-item{min-width:0;display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:baseline;padding-bottom:6px;border-bottom:1px solid rgba(126,111,91,.16)}.brief-item .label{color:var(--muted);font-size:10px}.brief-item .value{margin:0;font-size:16px;font-weight:760;font-variant-numeric:tabular-nums;text-align:right}.brief-item .note{grid-column:1 / -1;color:var(--muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.analysis-shell{margin-top:15px}.analysis-tabs{display:flex;gap:5px;padding:5px;border:1px solid var(--border);border-radius:13px 13px 0 0;background:var(--panel2);overflow-x:auto}.analysis-tab{flex:1 0 auto;border:1px solid transparent;border-radius:9px;padding:9px 13px;background:transparent;color:var(--muted);font-size:12px;font-weight:750;white-space:nowrap}.analysis-tab:hover,.analysis-tab.active{border-color:var(--accent);background:var(--panel);color:var(--accent)}.analysis-tab:disabled{cursor:not-allowed;opacity:.42}.analysis-controls{padding:14px 0 0}.analysis-controls .filters{padding-left:0;padding-right:0}.tab-panel{margin-top:12px}.tab-panel[hidden]{display:none}.session-total-button{margin:0 0 8px;padding:9px 11px;border-color:rgba(126,111,91,.28);border-radius:9px;background:rgba(240,233,221,.62)}.session-total-button:hover{border-color:var(--border);background:var(--panel2);color:var(--text);box-shadow:none}.session-total-button.active{border-color:rgba(126,111,91,.5);background:#e7ddcf;color:var(--text);box-shadow:inset 3px 0 0 #8c6f4d,0 4px 12px rgba(92,75,54,.08)}.session-total-button strong{font-size:12px}.session-total-button span{color:var(--muted);font-size:10px}
@media(max-width:650px){.summary-brief{width:100%}.brief-grid{gap:7px 12px}}
.session-button.model-watermark{position:relative;isolation:isolate;overflow:hidden;background:var(--model-tint,transparent);border-color:color-mix(in srgb,var(--model-color,var(--border)) 24%,transparent)}.session-button.model-watermark::after{content:attr(data-model-watermark);position:absolute;z-index:0;right:-8px;bottom:-10px;color:var(--model-color,var(--muted));font-size:27px;font-weight:850;letter-spacing:-.07em;line-height:1;opacity:.14;pointer-events:none;white-space:nowrap;transform:rotate(-10deg);transform-origin:right bottom}.session-button.model-watermark>strong,.session-button.model-watermark>span{position:relative;z-index:1}.session-button.model-watermark:hover,.session-button.model-watermark.active{background:linear-gradient(135deg,var(--model-tint,transparent),var(--panel));border-color:var(--model-color,var(--accent))}.session-effort{display:inline-flex!important;width:max-content;max-width:100%;padding:2px 6px;border:1px solid color-mix(in srgb,var(--model-color,var(--muted)) 35%,var(--border));border-radius:999px;color:var(--model-color,var(--muted))!important;background:rgba(255,254,250,.62);font-size:10px!important;line-height:1.25}
.session-button.model-watermark::after{display:none}.session-button.model-watermark>.session-watermark{position:absolute;right:6px;bottom:16px;z-index:0;display:block;max-width:72%;overflow:hidden;text-overflow:ellipsis;color:var(--model-color,var(--muted));opacity:.34;font-size:38px;font-weight:950;letter-spacing:-.055em;line-height:.9;text-align:right;white-space:nowrap;transform:rotate(-18deg);transform-origin:right bottom;pointer-events:none;filter:saturate(1.45);text-shadow:0 1px 0 rgba(255,254,250,.18)}.session-button.model-watermark>.session-watermark~strong,.session-button.model-watermark>.session-watermark~span{position:relative;z-index:1}
</style>
<style>
.analysis-tabs{position:relative;padding-left:150px}.analysis-tab-model-label{position:absolute;left:8px;top:5px;z-index:3;display:block;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:7px 11px;border:1px solid color-mix(in srgb,var(--model-color,var(--accent)) 58%,#fffefa);border-radius:8px;background:var(--model-color,var(--accent));color:#fffefa;font-size:14px;font-weight:950;letter-spacing:.06em;line-height:.95;transform:rotate(-12deg);transform-origin:left top;box-shadow:0 5px 13px color-mix(in srgb,var(--model-color,var(--accent)) 30%,transparent);pointer-events:none}.shell.session-nav-closed{grid-template-columns:52px minmax(0,1fr)}.shell.session-nav-closed .sidebar{width:52px;transform:none;opacity:1;visibility:visible;pointer-events:none;overflow:hidden;padding:14px 8px}.sidebar-rail-toggle{display:none;position:fixed;z-index:70;left:8px;top:14px;width:36px;height:36px;align-items:center;justify-content:center;padding:0;border:1px solid var(--border);border-radius:10px;background:var(--panel);color:var(--accent);font-size:28px;line-height:1;box-shadow:0 6px 18px rgba(92,75,54,.12)}.shell.session-nav-closed .sidebar-rail-toggle{display:inline-flex}#session-drawer-close{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;padding:0;font-size:22px;line-height:1}.session-time{font-variant-numeric:tabular-nums}.session-token-count{font-variant-numeric:tabular-nums;font-weight:750}
.analysis-tab-model-label{display:inline-flex;align-items:center;gap:7px}.analysis-tab-model-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.analysis-tab-plan-status:not([hidden]){display:inline-flex;flex:none;padding:3px 6px;border:1px solid rgba(255,254,250,.48);border-radius:999px;background:rgba(45,41,36,.22);color:#fffefa;font-size:9px;font-weight:850;letter-spacing:0;line-height:1}
@media(max-width:900px){.shell.session-nav-closed{display:block}.shell.session-nav-closed .sidebar{width:min(320px,88vw);transform:translateX(-104%);opacity:0;visibility:hidden;pointer-events:none;padding:22px 16px}.sidebar-rail-toggle{top:12px;left:8px}}
@media(max-width:650px){.analysis-tabs{padding-left:120px}.analysis-tab-model-label{max-width:110px;font-size:12px}}
.session-donut-sector{cursor:pointer;transition:opacity .14s ease,filter .14s ease}.session-donut-sector:hover,.session-donut-sector:focus{opacity:1;filter:brightness(1.08) saturate(1.12);outline:none}.session-button.model-watermark.session-hovered{background:linear-gradient(135deg,var(--model-tint,transparent),var(--panel));border-color:var(--model-color,var(--accent));box-shadow:0 0 0 2px color-mix(in srgb,var(--model-color,var(--accent)) 24%,transparent),0 8px 20px color-mix(in srgb,var(--model-color,var(--accent)) 16%,transparent)}
.model-pie-view{padding:2px 0 20px}.model-rate-meta{margin:0 19px 8px;color:var(--muted);font-size:11px;line-height:1.45}.model-plan-note{display:flex;align-items:flex-start;gap:7px;margin:0 19px 14px;padding:9px 11px;border:1px solid rgba(183,122,38,.24);border-radius:10px;background:rgba(255,248,225,.72);color:#8a6526;font-size:11px}.model-plan-note strong{color:#78531a}.pie-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:0 18px}.pie-card{min-width:0;padding:16px;border:1px solid var(--border);border-radius:14px;background:linear-gradient(180deg,rgba(255,254,250,.95),rgba(250,246,238,.88));box-shadow:0 8px 22px rgba(92,75,54,.06)}.pie-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.pie-card h3{margin:0;font-size:15px;letter-spacing:-.01em}.pie-card p{margin:4px 0 0;color:var(--muted);font-size:11px;line-height:1.4}.pie-card-kicker{flex:none;padding:4px 7px;border:1px solid rgba(59,139,120,.2);border-radius:999px;background:rgba(59,139,120,.07);color:var(--accent);font-size:10px;font-weight:800;white-space:nowrap}.pie-chart-wrap{display:flex;align-items:center;justify-content:center;min-height:280px;padding:4px 0 0}.pie-chart-wrap svg{width:min(100%,390px);height:auto}.model-pie-legend{display:grid;gap:7px;margin-top:2px;padding-top:10px;border-top:1px solid rgba(126,111,91,.15)}.model-pie-legend .pie-legend-row{display:grid;grid-template-columns:10px minmax(0,1fr) auto;gap:8px;align-items:center;font-size:11px}.pie-legend-row .swatch{width:10px;height:10px;border-radius:50%;background:var(--swatch);box-shadow:0 0 0 2px color-mix(in srgb,var(--swatch) 16%,transparent)}.pie-legend-row .name{display:grid;min-width:0;gap:1px;overflow:hidden}.pie-legend-row .name strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}.pie-legend-row .name small,.pie-legend-row .value small{color:var(--muted);font-size:10px}.pie-legend-row .value{display:grid;justify-items:end;gap:1px;margin:0;font-size:11px;font-weight:750;white-space:nowrap}.pie-empty{fill:var(--muted);font-size:12px}.model-watermark{--model-color:var(--accent);--model-tint:rgba(59,139,120,.06)}
@media(max-width:720px){.pie-grid{grid-template-columns:1fr}}
.session-effort{position:absolute!important;right:8px;bottom:7px;width:max-content;max-width:calc(100% - 16px);margin:0!important}
.session-plan-status{position:absolute!important;right:8px;top:7px;width:max-content!important;max-width:calc(100% - 16px);margin:0!important;padding:2px 6px!important;border:1px solid color-mix(in srgb,var(--model-color,var(--muted)) 48%,var(--border));border-radius:999px;background:color-mix(in srgb,var(--model-color,var(--panel)) 16%,#fffefa);color:var(--model-color,var(--muted))!important;font-size:9px!important;font-weight:850;line-height:1.2!important}
.tooltip-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:5px}.tooltip-badge-secondary{background:rgba(183,122,38,.14);color:#8a6526}
</style>
<style>
.model-filter{margin:10px 0 2px;padding:0 2px}.model-filter-title{color:var(--muted);font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.model-filter-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}.model-filter-toggle{display:inline-flex;align-items:center;gap:5px;max-width:100%;padding:5px 8px;border:1px solid color-mix(in srgb,var(--model-color,var(--border)) 48%,var(--border));border-radius:999px;background:color-mix(in srgb,var(--model-color,var(--panel)) 13%,var(--panel));color:var(--model-color,var(--muted));font-size:10px;font-weight:800;line-height:1.2;white-space:nowrap;transition:background .14s ease,border-color .14s ease,color .14s ease,opacity .14s ease}.model-filter-toggle:hover,.model-filter-toggle:focus-visible{border-color:var(--model-color,var(--accent));box-shadow:0 0 0 2px color-mix(in srgb,var(--model-color,var(--accent)) 16%,transparent);outline:none}.model-filter-toggle[aria-pressed="false"]{border-color:var(--border);background:transparent;color:var(--muted);opacity:.68}.model-filter-toggle[aria-pressed="false"] .model-filter-swatch{background:transparent;border:2px solid var(--model-color,var(--muted));opacity:.72}.model-filter-swatch{width:8px;height:8px;flex:none;border-radius:50%;background:var(--model-color,var(--accent));box-shadow:0 0 0 2px color-mix(in srgb,var(--model-color,var(--accent)) 15%,transparent)}.model-filter-count{color:inherit;font-size:9px;font-variant-numeric:tabular-nums;opacity:.76}.session-button.session-selected{box-shadow:0 0 0 2px color-mix(in srgb,var(--model-color,var(--accent)) 28%,transparent),0 7px 18px color-mix(in srgb,var(--model-color,var(--accent)) 13%,transparent)}.session-button.session-selected:not(.active){background:color-mix(in srgb,var(--model-tint,var(--panel)) 66%,var(--panel));border-color:color-mix(in srgb,var(--model-color,var(--accent)) 66%,var(--border))}.session-donut-sector.selected{filter:brightness(1.12) saturate(1.18);stroke:#2d2924;stroke-width:3}.session-total-jump{border:1px solid var(--border);border-radius:9px;padding:7px 10px;background:var(--panel);color:var(--accent);font-size:11px;font-weight:800;white-space:nowrap}.session-total-jump:hover,.session-total-jump:focus-visible{border-color:var(--accent);box-shadow:0 0 0 2px rgba(59,139,120,.12);outline:none}
</style>
<style>
h1{font-size:clamp(14px,1.5vw,20px)}
.brief-item .label{font-size:12px}
.brief-item .value.lcd-value{min-width:0;margin:0;padding:4px 8px;border:1px solid #3e7e6c;border-radius:7px;background:linear-gradient(180deg,#17372f,#102a25);color:#a6f3c9;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:18px;font-weight:800;letter-spacing:.035em;line-height:1.2;white-space:nowrap;text-shadow:0 0 5px rgba(166,243,201,.6);box-shadow:inset 0 2px 8px rgba(0,0,0,.28),0 2px 0 rgba(255,254,250,.4);font-variant-numeric:tabular-nums}
@media(max-width:650px){.brief-item .value.lcd-value{font-size:16px;padding:3px 6px}}
.hero-title-row{display:flex;align-items:center;min-width:0}.hero-title-row h1{min-width:0}.ring-return-button{position:fixed;z-index:120;top:14px;left:50%;transform:translateX(-50%);border:1px solid var(--accent);border-radius:999px;padding:8px 15px;background:rgba(255,254,250,.94);backdrop-filter:blur(12px);color:var(--accent);font-size:11px;font-weight:800;white-space:nowrap;box-shadow:0 8px 24px rgba(45,41,36,.18),0 0 0 3px rgba(59,139,120,.08)}.ring-return-button:hover,.ring-return-button:focus-visible{background:#fffefa;box-shadow:0 10px 28px rgba(45,41,36,.22),0 0 0 4px rgba(59,139,120,.14);outline:none}
@media(max-width:650px){.ring-return-button{top:10px;padding:7px 13px}}
.filter-row,.filters{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted)}.filter-row input[type="search"],.filters .content-search{min-width:min(100%,320px);flex:1}.filter-group{display:flex;align-items:center;gap:7px;margin:0;padding:0;border:0}.filter-group+ .filter-group{padding-left:12px;border-left:1px solid rgba(126,111,91,.3)}.filter-group legend{padding:0;color:var(--muted);font-size:10px;font-weight:800;white-space:nowrap}.filter-options{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.filter-option{display:inline-flex;align-items:center;gap:4px;padding:4px 7px;border:1px solid rgba(126,111,91,.24);border-radius:999px;background:rgba(255,254,250,.62);font-size:11px;line-height:1.2;white-space:nowrap}.filter-option:has(input:checked){border-color:var(--accent);color:var(--accent);background:rgba(59,139,120,.08)}.filter-option input{accent-color:var(--accent);margin:0}.filter-row>.check,.filters>.check{display:inline-flex;align-items:center;gap:4px}.filter-row>#visible-count,.filters>#visible-count{font-size:11px;white-space:nowrap}.token-unit-group .filter-option{min-width:28px;justify-content:center}.token-unit-group .filter-option:first-child{padding-left:8px;padding-right:8px}@media(max-width:720px){.filter-group+ .filter-group{padding-top:9px;padding-left:0;border-top:1px solid rgba(126,111,91,.3);border-left:0}.filter-row,.filters{align-items:stretch}.filter-group{width:100%}.filter-row input[type="search"],.filters .content-search{flex-basis:100%}}
</style>
</head>
<body>
<div class="shell" id="report-shell">
  <button class="sidebar-rail-toggle" id="sidebar-rail-toggle" type="button" aria-controls="session-drawer" aria-label="打开会话列表"><span aria-hidden="true">›</span></button>
  <aside class="sidebar" id="session-drawer" aria-label="会话列表">
    <div class="sidebar-head"><div class="brand"><div class="eyebrow">Codex Token 使用报告</div><h2>会话列表</h2><div class="muted" id="range-label"></div></div><button id="session-drawer-close" type="button" aria-label="关闭会话列表"><span aria-hidden="true">‹</span></button></div>
    <input class="nav-search" id="nav-search" type="search" placeholder="搜索会话……">
    <div class="model-filter" id="model-filter" role="group" aria-label="按模型筛选"><div class="model-filter-title">模型筛选</div><div class="model-filter-list" id="model-filter-list"></div></div>
    <nav class="session-list" id="session-list" aria-label="报告视图"></nav>
  </aside>
  <button class="session-drawer-backdrop" id="session-drawer-backdrop" type="button" tabindex="-1" aria-label="关闭会话列表"></button>
  <main class="content">
    <section class="hero"><div class="hero-copy"><div class="eyebrow" id="view-eyebrow"></div><div class="hero-title-row"><h1 id="view-title"></h1></div><div class="subline" id="view-meta"></div></div><aside class="summary-brief" aria-label="概览"><div class="brief-head"><span>概览</span><span class="sensitive" id="privacy"></span></div><div class="brief-grid" id="cards"></div><div class="summary-token-unit"><label class="summary-token-unit-label" for="token-unit-slider">Token 单位</label><div class="token-unit-slider"><input id="token-unit-slider" type="range" min="0" max="3" step="1" value="0" data-token-unit-slider aria-label="Token 单位"><output id="token-unit-output" for="token-unit-slider">原始</output><div class="token-unit-scale" aria-hidden="true"><span>原始</span><span>K</span><span>M</span><span>B</span></div></div></div></aside></section>
    <details class="warning-box" id="warning-box"><summary id="warning-summary"></summary><ul class="warning-list" id="warning-list"></ul></details>
    <section class="analysis-shell">
      <nav class="analysis-tabs" role="tablist" aria-label="查看方式"><span class="analysis-tab-model-label" id="analysis-tab-model-label" aria-label="使用模型"><span class="analysis-tab-model-name" id="analysis-tab-model-name"></span><span class="analysis-tab-plan-status" id="analysis-tab-plan-status" hidden>计划外</span></span>
        <button class="analysis-tab active" id="tab-context" data-tab-target="context" role="tab" aria-selected="true" type="button">模型消耗概览</button>
        <button class="analysis-tab" id="tab-composition" data-tab-target="composition" role="tab" aria-selected="false" type="button">单轮 Token 构成</button>
        <button class="analysis-tab" id="tab-trend" data-tab-target="trend" role="tab" aria-selected="false" type="button">累计 Token</button>
        <button class="analysis-tab" id="tab-table" data-tab-target="table" role="tab" aria-selected="false" type="button">逐轮明细</button>
      </nav>
      <div class="analysis-controls" id="analysis-controls">
       <div class="filters"><input class="content-search" id="content-search" type="search"><fieldset class="filter-group tool-filter-group" id="tool-filter" data-hide-on-cumulative aria-label="按工具类型筛选"><legend>工具</legend><div class="filter-options tool-filter-list" id="tool-filter-list"></div></fieldset><fieldset class="filter-group status-filter-group" data-hide-on-cumulative aria-label="按轮次状态筛选"><legend>轮次</legend><div class="filter-options status-filter-list"><label class="filter-option turn-only"><input type="checkbox" data-status="complete" checked> 已完成</label><label class="filter-option turn-only"><input type="checkbox" data-status="aborted" checked> 已中止</label><label class="filter-option turn-only"><input type="checkbox" data-status="incomplete" checked> 未闭合</label></div></fieldset><span id="visible-count"></span><button id="reset-filters" type="button">清除筛选</button></div>
      </div>
    </section>
    <section class="panel tab-panel" data-tab-panel="composition" role="tabpanel" hidden>
      <div class="panel-head"><div><h2 id="composition-title"></h2><p id="composition-note"></p></div><div class="controls"><button id="linear" class="active" type="button">线性</button><button id="log" type="button">对数</button></div></div>
      <div class="legend" id="legend"></div><div class="chart-scroll"><div class="chart-wrap"><svg id="composition" viewBox="0 0 1200 410" role="img"></svg></div></div>
    </section>
    <section class="panel tab-panel" data-tab-panel="trend" role="tabpanel" hidden><div class="panel-head"><div><h2 id="trend-title"></h2><p id="trend-note"></p></div></div><div class="trend-wrap"><svg id="trend" viewBox="0 0 1200 270" role="img"></svg></div></section>
    <section class="panel tab-panel" data-tab-panel="context" role="tabpanel" id="range-context-panel">
      <div id="range-model-pie-view" class="model-pie-view">
        <div class="panel-head"><div><h2>模型消耗概览</h2></div></div>
        <div class="model-rate-meta" id="model-rate-meta"></div>
        <div class="model-plan-note" id="model-plan-note" hidden></div>
        <div class="pie-grid">
          <article class="pie-card"><div class="pie-card-head"><div><h3>按模型查看消耗</h3><p>实际 Token 总量 · 外圈细分单模型会话</p></div><span class="pie-card-kicker">原始 Token</span></div><div class="pie-chart-wrap"><svg id="model-token-pie" role="img" aria-label="按模型划分的 Token 消耗图"></svg></div><div class="model-pie-legend" id="model-token-legend"></div></article>
          <article class="pie-card"><div class="pie-card-head"><div><h3>按费率折算的模型消耗</h3><p>按官方费率折算 · 外圈细分单模型会话</p></div><span class="pie-card-kicker">Sol 等价</span></div><div class="pie-chart-wrap"><svg id="model-weighted-pie" role="img" aria-label="按费率折算的模型消耗图"></svg></div><div class="model-pie-legend" id="model-weighted-legend"></div></article>
        </div>
      </div>
      <div id="range-session-context-view" hidden>
        <div class="panel-head"><div><h2>Token 消耗与 Context 占用</h2></div><button id="go-total-session" class="session-total-jump" type="button">定位到总统计</button></div><div class="source-legend" id="context-source-legend"></div><div class="context-radial-wrap"><svg id="range-context-radial-chart" role="img" aria-label="Token 累计进度与 Context 占用率"></svg></div>
      </div>
    </section>
    <section class="panel tab-panel" data-tab-panel="table" role="tabpanel" hidden><div class="panel-head"><div><h2 id="table-title"></h2><p id="table-note"></p></div></div><div class="table-wrap"><table><thead id="table-head"></thead><tbody id="table-body"></tbody></table><div class="empty" id="table-empty" hidden></div></div></section>
    <div class="footer" id="footer"></div>
  </main>
</div>
<button id="return-total-from-ring" class="ring-return-button" type="button" hidden>返回总统计</button>
<aside class="drawer" id="drawer" aria-hidden="true"><div class="drawer-head"><div><div class="eyebrow">轮次详情</div><h2 id="drawer-title"></h2></div><button id="drawer-close" type="button">关闭</button></div><div class="drawer-body" id="drawer-body"></div></aside>
<div class="tooltip" id="turn-tooltip" role="tooltip"></div>
<script id="report-data" type="application/json">__REPORT_JSON__</script>
<script>
(() => {
  try {
    "use strict";
    const safeObject=(value)=> (value && typeof value==="object" && !Array.isArray(value)) ? value : {};
    const data = (() => {
      try {
        return safeObject(JSON.parse(document.getElementById("report-data").textContent));
      } catch (error) {
        console.error("报告 JSON 解析失败：", error);
        return {
          generator: {name:"codex_token_visualizer", version:""},
          metadata: {},
          summary: {finalUsage:{}, finalBreakdown:{}, finalBreakdownMismatch:{}, reconciliationDifference:{}, statusCounts:{}, warningCount:0, integrityErrorCount:0},
          sessions: [],
          warnings: []
        };
      }
    })();
    data.metadata = safeObject(data.metadata); data.summary = safeObject(data.summary); data.summary.finalUsage = safeObject(data.summary.finalUsage);
    data.summary.finalBreakdown = safeObject(data.summary.finalBreakdown); data.summary.reconciliationDifference = safeObject(data.summary.reconciliationDifference);
    data.summary.statusCounts = safeObject(data.summary.statusCounts); data.summary.finalUsage = safeObject(data.summary.finalUsage);
    data.summary.finalBreakdownMismatch = safeObject(data.summary.finalBreakdownMismatch);
    data.summary.dailyUsage = Array.isArray(data.summary.dailyUsage) ? data.summary.dailyUsage : [];
    data.summary.toolCategories = safeObject(data.summary.toolCategories);
    data.summary.modelUsage = Array.isArray(data.summary.modelUsage) ? data.summary.modelUsage : [];
    data.summary.planExcludedUsage = safeObject(data.summary.planExcludedUsage);
    data.warnings = Array.isArray(data.warnings) ? data.warnings : [];
    const sessions = Array.isArray(data.sessions) ? data.sessions : [];
    for (const session of sessions) {
      session.metadata = safeObject(session.metadata);
      session.summary = safeObject(session.summary);
      session.warnings = Array.isArray(session.warnings) ? session.warnings : [];
      session.orphanMessages = Array.isArray(session.orphanMessages) ? session.orphanMessages : [];
      session.turns = Array.isArray(session.turns) ? session.turns : [];
      session.summary.finalUsage = safeObject(session.summary.finalUsage);
      session.summary.finalBreakdown = safeObject(session.summary.finalBreakdown);
      session.summary.finalBreakdownMismatch = safeObject(session.summary.finalBreakdownMismatch);
      session.summary.statusCounts = safeObject(session.summary.statusCounts);
      session.summary.modelUsage = Array.isArray(session.summary.modelUsage) ? session.summary.modelUsage : [];
      session.summary.planExcludedUsage = safeObject(session.summary.planExcludedUsage);
      session.metadata.efforts = Array.isArray(session.metadata.efforts) ? session.metadata.efforts : [];
      for (const turn of session.turns) {
        turn.usage = safeObject(turn.usage); turn.breakdown = safeObject(turn.breakdown);
        turn.contextSnapshot = safeObject(turn.contextSnapshot); turn.toolCalls = Array.isArray(turn.toolCalls) ? turn.toolCalls : [];
        turn.contextCompactions = Array.isArray(turn.contextCompactions) ? turn.contextCompactions : [];
        turn.messages = Array.isArray(turn.messages) ? turn.messages : [];
      }
    }
    data.sessions = sessions;
    const messagesIncluded=data.metadata.messagesIncluded!==false;
    const colors={cachedInput:css("--cached"),cacheWriteInput:css("--cache-write"),otherNonCachedInput:css("--uncached"),ordinaryOutput:css("--output"),reasoningOutput:css("--reasoning"),unclassified:css("--unclassified")};
    const labels={cachedInput:"缓存输入",cacheWriteInput:"缓存写入",otherNonCachedInput:"其他非缓存输入",ordinaryOutput:"普通输出",reasoningOutput:"推理输出",unclassified:"未分类调整"};
    const segmentKeys=["cachedInput",...(data.metadata.cacheWriteFieldAvailable?["cacheWriteInput"]:[]),"otherNonCachedInput","ordinaryOutput","reasoningOutput","unclassified"];
    const sessionNavMedia=window.matchMedia("(max-width:900px)");
    const DEFAULT_TOOL_CATEGORIES=["computer-use","chrome-use","imagegen","web-search"];
    const state={view:"total",tab:"context",scale:"linear",tokenUnit:"raw",query:"",toolCategories:new Set(DEFAULT_TOOL_CATEGORIES),modelFilters:null,statuses:new Set(["complete","aborted","incomplete"]),selected:null,selectedSessionIds:new Set(),hoverSessionId:null,returnToTotalSessionId:null,sessionNavOpen:!sessionNavMedia.matches};
  const tooltip=document.getElementById("turn-tooltip"),TOOLTIP_MESSAGE_LIMIT=800;
  const toolLabels={"computer-use":"Computer Use","chrome-use":"Chrome Use / Browser Use",imagegen:"ImageGen","exec-reasoning":"Exec Reasoning",shell:"Shell / Terminal","code-interpreter":"Code Interpreter","web-search":"Web Search","file-search":"File Search",mcp:"MCP","function-calling":"Function Calling",other:"其他工具"};
  const toolColors={"computer-use":"#4f78a8","chrome-use":"#3b8b78",imagegen:"#bd7556","exec-reasoning":"#9a8f84",shell:"#8c78bd","code-interpreter":"#6d8c45","web-search":"#4f9d87","file-search":"#d9874c",mcp:"#b35f79","function-calling":"#a56c3f",other:"#6f8fb7"};
  function byId(id){return document.getElementById(id)} function css(name){return getComputedStyle(document.documentElement).getPropertyValue(name).trim()}
  function esc(v){return String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c])}
  function formatGroupedNumber(v){const n=Number(v||0);if(!Number.isFinite(n))return"0";const sign=n<0?"-":"",parts=String(Math.abs(n)).split("."),integer=parts[0].replace(/\B(?=(\d{4})+(?!\d))/g,"\u2009");return sign+integer+(parts[1]?"."+parts[1]:"")}
  const TOKEN_UNIT_CONFIG={raw:{divisor:1,suffix:""},K:{divisor:1e3,suffix:"K"},M:{divisor:1e6,suffix:"M"},B:{divisor:1e9,suffix:"B"}};
  const TOKEN_UNIT_ORDER=["raw","K","M","B"],TOKEN_UNIT_LABELS={raw:"原始",K:"K",M:"M",B:"B"};
  function formatTokenDisplay(v){const n=Number(v||0);if(!Number.isFinite(n))return"0";const config=TOKEN_UNIT_CONFIG[state.tokenUnit]||TOKEN_UNIT_CONFIG.raw;if(state.tokenUnit==="raw")return formatGroupedNumber(n);return(n/config.divisor).toFixed(1).replace(/\.0$/,"")+config.suffix}
  function fmt(v){return formatTokenDisplay(v)} function compact(v){return formatTokenDisplay(v)} function formatCount(v){return formatGroupedNumber(v)} function isLcdValue(v){return /^[\d\u2009.\-]+[KMB]?$/.test(String(v))}
  function syncTokenUnitInputs(){const slider=byId("token-unit-slider"),output=byId("token-unit-output"),index=TOKEN_UNIT_ORDER.indexOf(state.tokenUnit);if(slider)slider.value=String(Math.max(0,index));if(output)output.textContent=TOKEN_UNIT_LABELS[state.tokenUnit]||TOKEN_UNIT_LABELS.raw}
  function setTokenUnit(unit){if(!TOKEN_UNIT_CONFIG[unit])return;state.tokenUnit=unit;syncTokenUnitInputs();render()}
  function dateText(v){if(!v)return"—";const d=new Date(v);return Number.isNaN(d.valueOf())?v:d.toLocaleString("zh-CN")}
  function activeSession(){return sessions.find(s=>s.metadata.threadId===state.view)||null} function isTotal(){return state.view==="total"}
  function statusText(v){return({complete:"已完成",aborted:"已中止",incomplete:"未闭合"})[v]||v||"未知"}
  function cacheRate(u){return u.input?100*u.cached/u.input:0} function firstPrompt(t){return(t.messages||[]).map(m=>m.text).filter(Boolean).join("\n\n↳ 追加用户消息\n")}
  function sourceText(s){const kinds=s.metadata.sourceKinds||[];return kinds.map(k=>k==="main"?"顶层":k==="subagent"?"子代理":k==="automation"?"自动化":k).join(" + ")||"顶层"}
  const MODEL_PALETTE=[["#3b8b78","rgba(59,139,120,.08)"],["#4f78a8","rgba(79,120,168,.08)"],["#bd7556","rgba(189,117,86,.08)"],["#8c78bd","rgba(140,120,189,.08)"],["#6d8c45","rgba(109,140,69,.08)"],["#a56c3f","rgba(165,108,63,.08)"]];
  const MODEL_THEME_OVERRIDES={Spark:["#c23b75","rgba(194,59,117,.12)"]};
  function modelVisual(model){const value=String(model||"未知模型"),override=MODEL_THEME_OVERRIDES[value];if(override)return override;let hash=0;for(let i=0;i<value.length;i++)hash=(hash*31+value.charCodeAt(i))|0;return MODEL_PALETTE[Math.abs(hash)%MODEL_PALETTE.length]}
  function sessionModel(session){const usage=Array.isArray(session?.metadata?.modelUsage)?session.metadata.modelUsage:[],excluded=Number(session?.metadata?.planExcludedUsage?.rawTokens)||0;if(usage.some(entry=>entry?.model==="多模型")||usage.length>1||(usage.length&&excluded>0))return"多模型";return session?.metadata?.primaryModel||usage[0]?.model||"未知模型"}
  function modelWatermarkLabel(model){const value=String(model||"未知模型"),labels={"GPT-5.6 Sol":"SOL","GPT-5.6 Terra":"TERRA","GPT-5.6 Luna":"LUNA","GPT-5.5":"5.5","GPT-5.5 Cyber":"CYBER","GPT-5.4":"5.4","GPT-5.4 mini":"5.4 MINI","GPT-5.3 Codex":"5.3 CODEX","GPT-5.2":"5.2","GPT-Image-2.0（图像）":"IMAGE","GPT-Image-2.0（文本）":"IMAGE TEXT","Spark":"Spark","多模型":"多模型","未知模型":"未知","计划外":"计划外"};return labels[value]||value.replace(/^GPT[- ]?/i,"").replace(/\s+/g," ").trim()}
  function sessionEffort(session){const efforts=session?.metadata?.efforts||[];return efforts.length?efforts.join(" / "):"未记录 effort"}
  function modelFilterModels(){return [...new Set(sessions.map(sessionModel))].sort((a,b)=>modelWatermarkLabel(a).localeCompare(modelWatermarkLabel(b),"zh-CN")||a.localeCompare(b))}
  function ensureModelFilters(){if(state.modelFilters===null)state.modelFilters=new Set(modelFilterModels())}
  function renderModelFilter(){const list=byId("model-filter-list");if(!list)return;ensureModelFilters();const counts=new Map(modelFilterModels().map(model=>[model,sessions.filter(session=>sessionModel(session)===model).length]));list.innerHTML=modelFilterModels().map(model=>{const visual=modelVisual(model),active=state.modelFilters.has(model);return`<button class="model-filter-toggle" type="button" data-model-filter="${esc(model)}" aria-pressed="${String(active)}" aria-label="${esc(modelWatermarkLabel(model))}，${active?"已开启":"已关闭"}" style="--model-color:${visual[0]};--model-tint:${visual[1]}"><span class="model-filter-swatch" aria-hidden="true"></span><span>${esc(modelWatermarkLabel(model))}</span><small class="model-filter-count">${counts.get(model)||0}</small></button>`}).join("");list.querySelectorAll("[data-model-filter]").forEach(button=>button.addEventListener("click",()=>{const model=button.dataset.modelFilter;state.modelFilters.has(model)?state.modelFilters.delete(model):state.modelFilters.add(model);renderModelFilter();renderNav()}))}
  function setSessionSelection(ids){state.selectedSessionIds=new Set(ids.filter(id=>sessions.some(session=>session.metadata.threadId===id)))}
  function selectRingSession(event,threadId){const additive=Boolean(event?.ctrlKey||event?.metaKey||event?.shiftKey);if(additive){if(state.selectedSessionIds.has(threadId))state.selectedSessionIds.delete(threadId);else state.selectedSessionIds.add(threadId)}else setSessionSelection([threadId]);hideTooltip();renderNav();renderContext()}
  function ringSessionKeydown(event,threadId){if(event.key!=="Enter"&&event.key!==" ")return;event.preventDefault();selectRingSession(event,threadId)}
  function isSparkOnlySession(session){return Boolean(session)&&sessionModel(session)==="Spark"}
  function sessionPlanStatus(session){return isSparkOnlySession(session)?"计划外":""}
  function renderTabModelLabel(){const label=byId("analysis-tab-model-label");if(!label)return;const session=activeSession(),model=isTotal()?"多模型":sessionModel(session),modelLabel=modelWatermarkLabel(model),sparkOnly=!isTotal()&&isSparkOnlySession(session),name=byId("analysis-tab-model-name"),status=byId("analysis-tab-plan-status"),visual=modelVisual(model);if(name)name.textContent=modelLabel;else label.textContent=modelLabel;if(status)status.hidden=!sparkOnly;label.title=sparkOnly?`${model} · 计划外`:model;label.style.setProperty("--model-color",visual[0]);label.style.setProperty("--model-tint",visual[1]);label.setAttribute("aria-label",sparkOnly?`使用模型：${modelLabel}，计划外`:`使用模型：${modelLabel}`)}
  function syncAnalysisControls(){const controls=byId("analysis-controls");if(controls)controls.hidden=isTotal()}
  function syncFilterVisibility(tab){document.querySelectorAll("[data-hide-on-cumulative]").forEach(control=>{control.hidden=tab==="trend"})}
  function totalRows(){return sessions.map((s,i)=>({index:i+1,turnId:s.metadata.threadId,title:s.metadata.title,sourceLabel:sourceText(s),startedAt:s.metadata.rangeLastActivityAt,status:"complete",models:[],efforts:[],messages:[],modelResponses:s.summary.turnCount,usage:s.summary.finalUsage,breakdown:s.summary.finalBreakdown,session:s}))}
  function rowMatches(row){if(!isTotal()&&!state.statuses.has(row.status))return false;if(!state.query)return true;const rendered=[row.turnId,row.title,row.sourceLabel,row.status,(row.models||[]).join(" "),firstPrompt(row)].join(" ").toLocaleLowerCase();return rendered.includes(state.query)}
  function rows(){const base=isTotal()?totalRows():(activeSession()?.turns||[]);return base.filter(rowMatches)}
  function syncToolFilterInputs(){byId("tool-filter-list").querySelectorAll("input[data-tool-category]").forEach(input=>{input.checked=state.toolCategories.has(input.value)})}
  function populateToolFilter(){const categories=[...new Set(sessions.flatMap(session=>(session.turns||[]).flatMap(turn=>(turn.toolCalls||[]).map(call=>call.category))))].sort((a,b)=>(toolLabels[a]||a).localeCompare(toolLabels[b]||b));byId("tool-filter-list").innerHTML=categories.map(category=>`<label class="check"><input type="checkbox" data-tool-category="true" value="${esc(category)}"${state.toolCategories.has(category)?" checked":""}> ${esc(toolLabels[category]||category)}</label>`).join("");byId("tool-filter-list").querySelectorAll("input[data-tool-category]").forEach(input=>input.addEventListener("change",event=>{if(event.target.checked)state.toolCategories.add(event.target.value);else state.toolCategories.delete(event.target.value);render()}))}
  function setSessionNav(open,focusTarget=false){
    state.sessionNavOpen=Boolean(open);const shell=byId("report-shell"),drawer=byId("session-drawer");
    shell.classList.toggle("session-nav-open",state.sessionNavOpen);shell.classList.toggle("session-nav-closed",!state.sessionNavOpen);drawer.setAttribute("aria-hidden",String(!state.sessionNavOpen));drawer.inert=!state.sessionNavOpen;document.body.classList.toggle("session-nav-modal-open",state.sessionNavOpen&&sessionNavMedia.matches);
    if(focusTarget)(state.sessionNavOpen?byId("nav-search"):byId("sidebar-rail-toggle")).focus();
  }
  function setSessionHover(threadId){if(state.hoverSessionId===threadId)return;state.hoverSessionId=threadId;document.querySelectorAll(".session-button.model-watermark").forEach(button=>button.classList.toggle("session-hovered",button.dataset.view===threadId))}
  function clearSessionHover(threadId){if(threadId&&state.hoverSessionId!==threadId)return;state.hoverSessionId=null;document.querySelectorAll(".session-button.model-watermark.session-hovered").forEach(button=>button.classList.remove("session-hovered"))}
  function renderNav(){const q=byId("nav-search").value.trim().toLocaleLowerCase(),items=sessions.filter(s=>state.modelFilters.has(sessionModel(s))&&[s.metadata.title,s.metadata.threadId,s.metadata.cwd,sessionModel(s),modelWatermarkLabel(sessionModel(s)),sessionPlanStatus(s),sessionEffort(s)].join(" ").toLocaleLowerCase().includes(q));let html=`<button class="session-button session-total-button ${isTotal()?"active":""}" data-view="total"><strong>总统计</strong><span>日期范围汇总 · ${sessions.length} 个会话 · ${fmt(data.summary.finalUsage.total)} Token</span></button>`;html+=items.map(s=>{const model=sessionModel(s),modelLabel=modelWatermarkLabel(model),planStatus=sessionPlanStatus(s),planTag=planStatus?`<span class="session-plan-status" aria-label="${esc(planStatus)}">${esc(planStatus)}</span>`:"",effort=sessionEffort(s),visual=modelVisual(model),watermark=esc(modelLabel),active=state.view===s.metadata.threadId,selected=state.selectedSessionIds.has(s.metadata.threadId),hovered=state.hoverSessionId===s.metadata.threadId;return`<button class="session-button model-watermark ${active?"active ":""}${selected?"session-selected ":""}${hovered?"session-hovered":""}" aria-pressed="${String(selected)}" style="--model-color:${visual[0]};--model-tint:${visual[1]}" data-model-watermark="${esc(modelLabel)}" data-view="${esc(s.metadata.threadId)}"><span class="session-watermark" aria-hidden="true">${watermark}</span>${planTag}<strong>${esc(s.metadata.title)}</strong><span class="session-time">${esc(dateText(s.metadata.rangeLastActivityAt))}</span><span class="session-token-count">${fmt(s.summary.finalUsage.total)} Token</span><span class="session-effort">${esc(effort)}</span></button>`}).join("");byId("session-list").innerHTML=html;byId("session-list").querySelectorAll("button").forEach(button=>button.addEventListener("click",()=>selectView(button.dataset.view)))}
  function applyView(view,fromRing=false){const valid=view==="total"||sessions.some(session=>session.metadata.threadId===view),nextView=valid?view:"total";state.returnToTotalSessionId=fromRing&&nextView!=="total"?nextView:null;state.view=nextView;state.hoverSessionId=null;if(nextView!=="total")setSessionSelection([nextView]);state.tab="context";state.query="";state.toolCategories=new Set(DEFAULT_TOOL_CATEGORIES);state.statuses=new Set(["complete","aborted","incomplete"]);byId("content-search").value="";syncToolFilterInputs();document.querySelectorAll("[data-status]").forEach(x=>x.checked=true);closeDrawer();if(sessionNavMedia.matches)setSessionNav(false);renderNav();render()}
  function navigateView(view,fromRing=false){const nextReturn=fromRing&&view!=="total"?view:null;if(state.view===view&&state.returnToTotalSessionId===nextReturn){applyView(view,fromRing);return}history.pushState({...(history.state||{}),codexTokenReport:true,view,returnToTotalSessionId:nextReturn},"","");applyView(view,fromRing)}
  function selectView(view,fromRing=false){navigateView(view,fromRing)}
  function enterSessionFromRing(threadId){selectView(threadId,true)}
  function goToTotal(){const session=activeSession();if(session)setSessionSelection([session.metadata.threadId]);navigateView("total")}
  function syncRingReturnButton(){const button=byId("return-total-from-ring");if(!button)return;const visible=!isTotal()&&state.returnToTotalSessionId!=null;button.hidden=!visible;button.setAttribute("aria-hidden",String(!visible))}
  function renderHeader(){const session=activeSession(),summary=isTotal()?data.summary:session.summary,u=summary.finalUsage,window=data.metadata.dateWindow;byId("view-eyebrow").textContent=isTotal()?"日期范围总览":"会话总览";byId("view-title").textContent=isTotal()?"全部会话 · Token 消耗":session.metadata.title;byId("view-meta").textContent=isTotal()?`${window.startDate} — ${window.endDate} · ${window.timezone} · 数据截止 ${dateText(data.metadata.snapshotAt)}`:`${session.metadata.threadId} · ${summary.turnCount} 轮 · ${sourceText(session)}`;const privacy=byId("privacy");privacy.textContent=messagesIncluded?"包含范围内完整用户消息":"未包含用户消息";privacy.classList.toggle("safe",!messagesIncluded);const fifth=isTotal()?["会话数",formatCount(summary.sessionCount),`${summary.zeroUsageSessions} 个会话没有 Token 消耗`]:["轮次数",formatCount(summary.turnCount),`${summary.statusCounts.aborted||0} 轮中止 · ${summary.zeroUsageTurns} 轮无 Token 消耗`];const cards=[["区间总 Token",fmt(u.total),"按 token_count 快照时间计入"],["输入 Token",fmt(u.input),`${compact(u.cached)} 来自缓存`],["未命中缓存的输入 Token",fmt(Math.max(0,u.input-u.cached)),`缓存命中率 ${cacheRate(u).toFixed(2)}%`],["输出 Token",fmt(u.output),`${compact(u.reasoning)} 为推理输出`],fifth,["数据完整性",summary.integrityErrorCount?"发现问题":"正常",`${summary.warningCount} 条提醒`]];byId("cards").innerHTML=cards.map(c=>`<article class="brief-item"><div class="label">${esc(c[0])}</div><div class="value${isLcdValue(c[1])?" lcd-value":""}">${esc(c[1])}</div><div class="note">${esc(c[2])}</div></article>`).join("");syncRingReturnButton()}
  function renderWarnings(){const session=activeSession(),warnings=isTotal()?data.warnings:session.warnings,summary=isTotal()?data.summary:session.summary,box=byId("warning-box");byId("warning-summary").textContent=`${summary.integrityErrorCount} 个数据完整性问题 · 共 ${summary.warningCount} 条提醒`;box.hidden=!warnings.length;byId("warning-list").innerHTML=warnings.map(w=>`<li class="${esc(w.severity)}"><b>${esc(w.code)}</b>${w.rolloutId?` · ${esc(w.rolloutId.slice(0,8))}`:""}${w.line?` · 第 ${w.line} 行`:""}：${esc(w.message)}</li>`).join("");box.open=summary.integrityErrorCount>0}
  function niceTicks(max,count=5){if(max<=0)return[0];const rough=max/count,p=10**Math.floor(Math.log10(rough)),f=rough/p,n=(f<=1?1:f<=2?2:f<=5?5:10)*p,out=[];for(let x=0;x<=max+n*.25;x+=n)out.push(x);return out}
  function svgEl(name,attrs={}){const el=document.createElementNS("http://www.w3.org/2000/svg",name);Object.entries(attrs).forEach(([k,v])=>el.setAttribute(k,v));return el} function text(svg,x,y,value,anchor="end"){const t=svgEl("text",{x,y,"text-anchor":anchor,fill:css("--muted"),"font-size":"11"});t.textContent=value;svg.appendChild(t)}
  function positionTooltip(event,target){const targetRect=target?.getBoundingClientRect?.(),pointerX=Number.isFinite(event?.clientX)?event.clientX:targetRect?targetRect.left+targetRect.width/2:innerWidth/2,pointerY=Number.isFinite(event?.clientY)?event.clientY:targetRect?targetRect.top+targetRect.height/2:innerHeight/2;tooltip.style.display="block";tooltip.style.visibility="hidden";const gap=14,edge=8,width=tooltip.offsetWidth,height=tooltip.offsetHeight;let x=pointerX+gap,y=pointerY+gap;if(x+width>innerWidth-edge)x=pointerX-width-gap;if(y+height>innerHeight-edge)y=pointerY-height-gap;tooltip.style.left=`${Math.max(edge,Math.min(x,innerWidth-width-edge))}px`;tooltip.style.top=`${Math.max(edge,Math.min(y,innerHeight-height-edge))}px`;tooltip.style.visibility="visible"}
  function pieDisplayValue(value,valueKey){return valueKey==="weightedTokens"?Math.round(Number(value)||0):value}
  function showSessionTooltip(event,session,target,model,valueKey,value,modelTotal){if(event?.pointerType==="touch"){hideTooltip();return}const usage=session.summary?.finalUsage||{},metric=valueKey==="weightedTokens"?"Sol 等价 Token":"Token",share=modelTotal>0?`${(100*value/modelTotal).toFixed(1)}%` : "—",title=session.metadata?.title||"未命名会话",planStatus=sessionPlanStatus(session),modelBadges=`<span class="tooltip-badges"><span class="tooltip-badge">${esc(modelWatermarkLabel(model))}</span>${planStatus?`<span class="tooltip-badge tooltip-badge-secondary">${esc(planStatus)}</span>`:""}</span>`;tooltip.innerHTML=`<div class="tooltip-title"><strong>${esc(title)}</strong>${modelBadges}</div><div class="tooltip-metrics"><div class="tooltip-metric token"><span>当前会话消耗</span><strong>${fmt(pieDisplayValue(value,valueKey))}</strong><small>${metric} · 占该模型 ${share}</small></div><div class="tooltip-metric context"><span>会话总量</span><strong>${fmt(usage.total)}</strong><small>${fmt(session.summary?.turnCount||0)} 轮 · ${esc(sessionEffort(session))}</small></div></div><div class="tooltip-grid"><span>使用模型</span><b>${esc(model)}</b><span>最近活动</span><b>${esc(dateText(session.metadata?.rangeLastActivityAt))}</b><span>会话 ID</span><b>${esc(session.metadata?.threadId||"—")}</b></div>`;positionTooltip(event,target)}
  function moveSessionTooltip(event,target){if(event.pointerType!=="touch"&&tooltip.style.display==="block")positionTooltip(event,target)}
  function initialMessagePreview(turn){const message=(turn.messages||[])[0];if(!message)return{text:messagesIncluded?"该轮未记录范围内用户消息。":"生成报告时已排除用户消息。",truncated:false};const full=String(message.text||"");if(!full){const attachments=[message.imageCount?`${message.imageCount} 张图片`:"",message.audioCount?`${message.audioCount} 条音频`:""].filter(Boolean).join(" · ");return{text:attachments?`初始消息包含 ${attachments}，无文本。`:"初始用户消息没有文本。",truncated:false}}return{text:full.slice(0,TOOLTIP_MESSAGE_LIMIT),truncated:full.length>TOOLTIP_MESSAGE_LIMIT}}
  function outputPreview(turn){const outputs=(turn.outputs||[]).filter(output=>String(output.text||"").trim()),finalOutputs=outputs.filter(output=>String(output.phase||"").toLowerCase()==="final_answer"),selected=(finalOutputs.length?finalOutputs:outputs).at(-1);if(!selected)return{text:"该轮没有可读代理输出。",truncated:false};const full=String(selected.text||""),characters=Array.from(full);return characters.length>TOOLTIP_MESSAGE_LIMIT?{text:characters.slice(0,TOOLTIP_MESSAGE_LIMIT-3).join("")+"...",truncated:true}:{text:full,truncated:false}}
  function tooltipMessage(turn){return isSatelliteTurn(turn)?outputPreview(turn):initialMessagePreview(turn)} function tooltipMessageLabel(turn){return isSatelliteTurn(turn)?"代理输出":"初始用户消息"}
  function drawerOutputSection(turn){if(!isSatelliteTurn(turn))return"";const outputs=(turn.outputs||[]).filter(output=>String(output.text||"").trim()),text=outputs.length?outputs.map(output=>String(output.text||"")).join("\n\n"):"该轮没有可读代理输出。";return `<div class="message"><div class="message-head">代理输出${outputs.length?` · ${outputs.length} 条`:""}</div><pre>${esc(text)}</pre></div>`}
  function previousTurnForTooltip(turn){const sequence=[...(activeSession()?.turns||[])].sort((a,b)=>Number(a.index)-Number(b.index)),index=sequence.findIndex(candidate=>candidate.turnId===turn.turnId);return index>0?sequence[index-1]:null}
  function showTurnTooltip(event,turn,target,conversationTotal){if(event?.pointerType==="touch"){hideTooltip();return}const b=turn.breakdown||{},snapshot=contextSnapshot(turn),previous=previousTurnForTooltip(turn),previousSnapshot=previous?contextSnapshot(previous):null,message=tooltipMessage(turn),tokenRows=segmentKeys.map(key=>`<span>${esc(labels[key])}</span><b>${fmt(b[key]||0)}</b>`).join(""),contextPortion=snapshot.occupancyRate==null?"—":`${Number(snapshot.occupancyRate).toFixed(2)}%`,contextAbsolute=snapshot.occupancyRate==null?"未记录 Context 快照":`${fmt(snapshot.tokens)} / ${snapshot.windowTokens==null?"—":fmt(snapshot.windowTokens)} Token`,tokenPortion=conversationTotal>0?`${(100*Math.max(0,Number(turn.usage.total)||0)/conversationTotal).toFixed(2)}%`:"—",tokenAbsolute=`${fmt(turn.usage.total)} / ${fmt(conversationTotal)} Token`,contextDelta=previousSnapshot?.occupancyRate==null||snapshot.occupancyRate==null?"":(()=>{const delta=Number(snapshot.occupancyRate)-Number(previousSnapshot.occupancyRate);if(Math.abs(delta)<0.005)return"无变化";return delta>0?`增加了 ${delta.toFixed(2)}%`:`减少了 ${Math.abs(delta).toFixed(2)}%`})();tooltip.innerHTML=`<div class="tooltip-title"><strong>第 ${turn.index} 轮</strong><span class="tooltip-badge">${esc(statusText(turn.status))}</span></div><div class="tooltip-metrics"><div class="tooltip-metric context"><span>Context 占用</span><strong>${esc(contextPortion)}${contextDelta?`<span class="tooltip-change" style="font-size:11px;font-weight:550;color:var(--muted);white-space:nowrap">（${esc(contextDelta)}）</span>`:""}</strong><small>${esc(contextAbsolute)}</small></div><div class="tooltip-metric token"><span>本轮 Token 占比</span><strong>${esc(tokenPortion)}</strong><small>${esc(tokenAbsolute)}</small></div></div><div class="tooltip-grid"><span>来源</span><b>${esc(turn.sourceLabel||"主会话")}</b><span>模型</span><b>${esc((turn.models||[]).join(", ")||"—")}</b><span>开始／结束</span><b>${esc(dateText(turn.startedAt))} — ${esc(dateText(turn.endedAt))}</b><span>快照</span><b>${esc(contextTypeText(snapshot.snapshotType))} · ${esc(dateText(snapshot.timestamp))}</b><span>Compaction</span><b>${formatCount(turn.compactions)}</b></div><div class="tooltip-section"><div class="tooltip-section-label">Token 构成 · 总量 ${fmt(turn.usage.total)}</div><div class="tooltip-grid">${tokenRows}</div></div><div class="tooltip-section"><div class="tooltip-section-label">${tooltipMessageLabel(turn)}</div><div class="tooltip-message">${esc(message.text)}</div>${message.truncated?'<div class="tooltip-truncated">已截断；全文见轮次详情</div>':""}</div>`;positionTooltip(event,target)}
  function moveTurnTooltip(event,target){if(event.pointerType!=="touch"&&tooltip.style.display==="block")positionTooltip(event,target)}
  function focusTurnTooltip(event,turn,target,conversationTotal){if(target.matches(":focus-visible"))showTurnTooltip(event,turn,target,conversationTotal)}
  function turnTargetKeydown(event,turn){if(event.key==="Enter"||event.key===" "){event.preventDefault();hideTooltip();openDrawer(turn)}}
  function hideTooltip(){tooltip.style.display="none";tooltip.style.visibility="hidden"}
  function contextSnapshot(turn){return turn.contextSnapshot||{snapshotType:"unknown",tokens:null,windowTokens:null,occupancyRate:null,timestamp:null}}
  function contextTimeline(turn,tokens){const points=(turn.contextTimeline||[]).filter(point=>point.occupancyRate!=null).map(point=>({...point,turnTokenOffset:Math.max(0,Math.min(tokens,Number(point.turnTokenOffset)||0))})).sort((a,b)=>a.turnTokenOffset-b.turnTokenOffset||String(a.timestamp||"").localeCompare(String(b.timestamp||"")));if(points.length)return points;const fallback=contextSnapshot(turn);return fallback.occupancyRate==null?[]:[{...fallback,turnTokenOffset:tokens}]}
   function contextBands(turn,tokens){if(tokens<=0)return[];const points=contextTimeline(turn,tokens);if(!points.length)return[{start:0,end:tokens,snapshot:null}];const bands=[];let cursor=0,active=null;points.forEach(point=>{if(point.turnTokenOffset>cursor){bands.push({start:cursor,end:point.turnTokenOffset,snapshot:active||point});cursor=point.turnTokenOffset}active=point});if(cursor<tokens)bands.push({start:cursor,end:tokens,snapshot:active||points.at(-1)});return bands}
   function contextBandColor(rate){if(rate==null)return css("--muted");const value=Math.max(0,Math.min(100,Number(rate)||0));return value>=85?"#c45657":value>=65?"#d28b3d":"#3b8b78"}
  function contextTypeText(type){return({turn_end:"结束时",range_latest:"范围内最新",current_latest:"当前最新",unknown:"未知"})[type]||"未知"}
  function contextRateText(snapshot){return snapshot.occupancyRate==null?"—":`${Number(snapshot.occupancyRate).toFixed(2)}%`}
  const sourcePalette=["#3b8b78","#bd7556","#8c78bd","#d9874c","#4f78a8","#b35f79","#6d8c45","#a56c3f"];
  function isSatelliteTurn(turn){const kind=String(turn.sourceKind||data.metadata?.sourceKind||"").toLowerCase();return kind==="subagent"||String(turn.sourceLabel||"").includes("子代理")}
  function radialEntries(ordered,denominator){let consumed=0;const entries=ordered.map(turn=>{const tokens=Math.max(0,Number(turn.usage.total)||0),entry={turn,tokens,tokenStart:consumed,start:consumed/denominator,end:(consumed+tokens)/denominator,satellite:isSatelliteTurn(turn),parentEntry:null,satelliteLane:0};consumed+=tokens;return entry});const siblingCounts=new Map();entries.forEach((entry,index)=>{if(!entry.satellite)return;let parent=null;for(let cursor=index-1;cursor>=0;cursor-=1){if(!entries[cursor].satellite){parent=entries[cursor];break}}if(!parent)parent=entries.find(candidate=>!candidate.satellite)||null;const key=parent?.turn.turnId||"orphan";entry.parentEntry=parent;entry.satelliteLane=siblingCounts.get(key)||0;siblingCounts.set(key,entry.satelliteLane+1)});return entries}
  function satelliteGeometry(cx,cy,outerOuter,entry){const inner=outerOuter+24+(entry.satelliteLane%5)*14;return{inner,outer:inner+10,contextRadius:inner-8}}
  function satelliteFraction(entry){return(entry.start+entry.end)/2}
  function toolCallLabel(call){return toolLabels[call.category]||call.name||"未知工具"} function toolCallColor(call){return toolColors[call.category]||toolColors.other} function toolField(call,key){return Array.isArray(call.usageKnown)&&call.usageKnown.includes(key)?fmt(call.usage?.[key]||0):"未知"} function toolUsage(call){return Array.isArray(call.usageKnown)&&call.usageKnown.includes("total")?Math.max(0,Number(call.usage?.total)||0):0}
  function toolTooltip(event,turn,call,target){const usage=toolUsage(call),status=Array.isArray(call.usageKnown)&&call.usageKnown.includes("total")?`${fmt(usage)} Token`:"Token 未知";tooltip.innerHTML=`<div class="tooltip-title"><strong>${esc(toolCallLabel(call))}</strong><span class="tooltip-badge">第 ${turn.index} 轮</span></div><div class="tooltip-grid"><span>语义工具</span><b>${esc(call.semanticTool||toolCallLabel(call))}</b><span>Provider</span><b>${esc(call.provider||"未知")}</b><span>识别方式</span><b>${esc(call.classificationSource||"raw")}</b><span>原始工具名</span><b>${esc(call.rawName||call.name||"未知")}</b><span>调用序号</span><b>#${fmt(call.sequence||0)}</b><span>时间</span><b>${esc(dateText(call.timestamp))}</b><span>状态</span><b>${esc(call.status||"未知")}</b><span>Token</span><b>${esc(status)}</b></div>`;positionTooltip(event,target)}
  function openToolDrawer(turn,call){if(!turn||!call)return;state.selected=turn.turnId;byId("drawer-title").textContent=`${toolCallLabel(call)} · 第 ${turn.index} 轮`;const details=[["工具分类",toolCallLabel(call)],["语义工具",call.semanticTool||"未知"],["Provider",call.provider||"未知"],["识别方式",call.classificationSource||"raw"],["原始工具名",call.rawName||call.name||"未知"],["传输包装",call.transportWrapper?"是":"否"],["调用序号",`#${call.sequence||"—"}`],["调用时间",dateText(call.timestamp)],["结束时间",dateText(call.endedAt)],["状态",call.status||"未知"],["Token 归因",call.usageReported?"精确":"未知"],["总 Token",toolField(call,"total")],["输入",toolField(call,"input")],["缓存输入",toolField(call,"cached")],["输出",toolField(call,"output")],["推理输出",toolField(call,"reasoning")]];byId("drawer-body").innerHTML=`<div class="detail-grid">${details.map(d=>`<div class="detail"><span>${esc(d[0])}</span><b>${esc(d[1])}</b></div>`).join("")}</div><div class="message"><div class="message-head">父 turn</div><pre>第 ${turn.index} 轮 · ${esc(turn.turnId)} · ${fmt(turn.usage.total)} Token</pre></div>`;byId("drawer").classList.add("open");byId("drawer").setAttribute("aria-hidden","false");renderContext();}
  function toolSatelliteGeometry(cx,cy,outerOuter,index){const inner=outerOuter+76+(index%3)*14;return{inner,outer:inner+9}}
  function renderToolLayer(svg,entries,cx,cy,outerOuter,denominator){entries.forEach(entry=>{const calls=(entry.turn.toolCalls||[]).filter(call=>state.toolCategories.has(call.category));if(!calls.length)return;const parentSpan=Math.max(0,entry.end-entry.start),envelopeStart=parentSpan?entry.start:Math.max(0,entry.start-.006),envelopeEnd=parentSpan?entry.end:Math.min(1,entry.start+.012),envelopePath=arcBandPath(cx,cy,outerOuter+54,outerOuter+60,envelopeStart,envelopeEnd);if(envelopePath)svg.appendChild(svgEl("path",{d:envelopePath,class:"tool-envelope"}));const exact=calls.reduce((sum,call)=>sum+toolUsage(call),0),unknown=calls.filter(call=>!call.usageReported).length,envelopeNode=svg.lastChild;if(envelopeNode){const title=svgEl("title");title.textContent=`第 ${entry.turn.index} 轮工具包络 · ${calls.length} 次调用 · ${fmt(exact)} Token · ${unknown} 次 Token 未知`;envelopeNode.appendChild(title)}calls.forEach((call,index)=>{const fraction=parentSpan?entry.start+parentSpan*(index+.5)/calls.length:entry.start,geometry=toolSatelliteGeometry(cx,cy,outerOuter,index),usage=toolUsage(call),width=usage>0?Math.min(Math.max(parentSpan*.22,.002),usage/denominator):0,start=usage>0?Math.max(entry.start,fraction-width/2):fraction,end=usage>0?Math.min(entry.end,start+width):fraction,group=svgEl("g",{class:`tool-satellite${state.selected===entry.turn.turnId?" selected":""}`,tabindex:"0",role:"button","data-tool-target":"true","aria-label":`${toolCallLabel(call)}，${call.usageReported?fmt(usage)+" Token":"Token 未知"}`}),parentPoint=radialPoint(cx,cy,outerOuter+7,fraction),satellitePoint=radialPoint(cx,cy,geometry.inner-1,fraction);group.appendChild(svgEl("line",{x1:satellitePoint.x,y1:satellitePoint.y,x2:parentPoint.x,y2:parentPoint.y,class:"tool-satellite-connector"}));if(usage>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,geometry.inner,geometry.outer,start,end),fill:toolCallColor(call),class:"token-sector"}))}else{const a=radialPoint(cx,cy,geometry.inner-3,fraction),b=radialPoint(cx,cy,geometry.outer+5,fraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"tool-satellite-unknown"}))}const title=svgEl("title");title.textContent=`${toolCallLabel(call)} · ${call.usageReported?fmt(usage)+" Token":"Token 未知"}`;group.appendChild(title);group.addEventListener("pointerenter",event=>toolTooltip(event,entry.turn,call,group));group.addEventListener("pointermove",event=>moveTurnTooltip(event,group));group.addEventListener("pointerleave",hideTooltip);group.addEventListener("focus",event=>toolTooltip(event,entry.turn,call,group));group.addEventListener("blur",hideTooltip);group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();hideTooltip();openToolDrawer(entry.turn,call)}});group.addEventListener("click",()=>{hideTooltip();openToolDrawer(entry.turn,call)});svg.appendChild(group)})})}
  function sourceColor(source){const value=String(source||"main");let hash=0;for(let i=0;i<value.length;i++)hash=(hash*31+value.charCodeAt(i))>>>0;return sourcePalette[hash%sourcePalette.length]}
  function contextOrderTime(turn){const snapshot=contextSnapshot(turn);return snapshot.timestamp||turn.endedAt||turn.rangeLastActivityAt||turn.startedAt||""}
  const contextGap=Math.PI/180,contextStart=-Math.PI/2+contextGap/2,contextSpan=2*Math.PI-contextGap;
  function radialPoint(cx,cy,r,fraction){const angle=contextStart+Math.max(0,Math.min(1,fraction))*contextSpan;return{x:cx+r*Math.cos(angle),y:cy+r*Math.sin(angle)}}
  function arcLinePath(cx,cy,r,start,end){const a=radialPoint(cx,cy,r,start),b=radialPoint(cx,cy,r,end),large=(end-start)*contextSpan>Math.PI?1:0;return`M${a.x.toFixed(2)},${a.y.toFixed(2)} A${r},${r} 0 ${large} 1 ${b.x.toFixed(2)},${b.y.toFixed(2)}`}
  function arcBandPath(cx,cy,inner,outer,start,end){if(end-start<=1e-9)return"";const a=radialPoint(cx,cy,outer,start),b=radialPoint(cx,cy,outer,end),c=radialPoint(cx,cy,inner,end),d=radialPoint(cx,cy,inner,start),large=(end-start)*contextSpan>Math.PI?1:0;return`M${a.x.toFixed(2)},${a.y.toFixed(2)} A${outer},${outer} 0 ${large} 1 ${b.x.toFixed(2)},${b.y.toFixed(2)} L${c.x.toFixed(2)},${c.y.toFixed(2)} A${inner},${inner} 0 ${large} 0 ${d.x.toFixed(2)},${d.y.toFixed(2)} Z`}
  function renderContextBase(){
    if(isTotal())return;
    const svg=byId("range-context-radial-chart");svg.replaceChildren();svg.setAttribute("viewBox","0 0 760 620");const session=activeSession(),ordered=[...(session?.turns||[])].sort((a,b)=>contextOrderTime(a).localeCompare(contextOrderTime(b))||a.index-b.index),cx=380,cy=300,innerBase=105,innerMax=178,outerInner=202,outerOuter=234;
    const sources=[...new Map(ordered.map(turn=>[turn.sourceRolloutId||"main",{id:turn.sourceRolloutId||"main",label:turn.sourceLabel||"主会话"}])).values()];
    byId("context-source-legend").innerHTML=sources.map(source=>`<span style="--source-color:${sourceColor(source.id)}">${esc(source.label)}</span>`).join("");
    if(!ordered.length){text(svg,cx,cy,"所选会话没有 turn 数据","middle");return}
    const defs=svgEl("defs"),pattern=svgEl("pattern",{id:"range-context-unknown-pattern",width:8,height:8,patternUnits:"userSpaceOnUse",patternTransform:"rotate(35)"}),arrow=svgEl("marker",{id:"range-context-compaction-arrow",markerWidth:8,markerHeight:8,refX:7,refY:4,orient:"auto",markerUnits:"userSpaceOnUse"});pattern.appendChild(svgEl("line",{x1:0,y1:0,x2:0,y2:8,stroke:css("--muted"),"stroke-width":2,opacity:.42}));arrow.appendChild(svgEl("path",{d:"M0,0 L8,4 L0,8 Z",fill:css("--warning")}));defs.append(pattern,arrow);svg.appendChild(defs);svg.appendChild(svgEl("path",{d:arcBandPath(cx,cy,outerInner,outerOuter,0,1),fill:"#e9e2d7"}));
    svg.appendChild(svgEl("path",{d:arcBandPath(cx,cy,innerBase+(innerMax-innerBase)*.75,innerMax,0,1),class:"context-danger-zone",fill:"rgba(196,86,87,.07)"}));
    [25,50,75,100].forEach(rate=>{const capacity=rate===100,warning=rate===75,radius=innerBase+(innerMax-innerBase)*rate/100;svg.appendChild(svgEl("path",{d:arcLinePath(cx,cy,radius,0,1),class:capacity?"context-reference context-capacity":warning?"context-reference context-warning":"context-reference",stroke:warning?"rgba(210,139,61,.72)":"rgba(117,110,100,.18)","stroke-width":warning?1.8:1}));const point=radialPoint(cx,cy,radius,0),label=svgEl("text",{x:point.x+5,y:point.y+3,fill:capacity?css("--text"):warning?"#b06d2b":css("--muted"),"font-size":capacity||warning?"10":"9","font-weight":capacity||warning?"700":"400"});label.textContent=capacity?"Context 100%":`${rate}%`;svg.appendChild(label)});
    const total=ordered.reduce((sum,turn)=>sum+Math.max(0,Number(turn.usage.total)||0),0),denominator=Math.max(total,1),entries=radialEntries(ordered,denominator);
    const observed=ordered.flatMap(turn=>contextTimeline(turn,Math.max(0,Number(turn.usage.total)||0)).map(snapshot=>({turn,snapshot}))),peak=observed.reduce((best,item)=>Number(item.snapshot.occupancyRate)>Number(best?.snapshot?.occupancyRate??-1)?item:best,null),compactionCount=ordered.reduce((sum,turn)=>sum+(turn.contextCompactions||[]).length,0);
    const centerTitle=svgEl("text",{x:cx,y:cy-38,"text-anchor":"middle",fill:css("--muted"),"font-size":"13"}),centerValue=svgEl("text",{x:cx,y:cy-4,"text-anchor":"middle",fill:css("--text"),"font-size":"27","font-weight":"760"}),centerDetail=svgEl("text",{x:cx,y:cy+24,"text-anchor":"middle",fill:css("--text"),"font-size":"12"}),centerMeta=svgEl("text",{x:cx,y:cy+48,"text-anchor":"middle",fill:css("--muted"),"font-size":"11"});svg.append(centerTitle,centerValue,centerDetail,centerMeta);
    function resetCenter(){centerTitle.textContent="完整会话";centerValue.textContent=`${compact(total)} Token`;centerDetail.textContent=peak?`Context 峰值 ${contextRateText(peak.snapshot)}`:"Context 峰值未知";centerMeta.textContent=`${compactionCount} 次 Compaction`}
    function showCenter(turn){const snapshot=contextSnapshot(turn);centerTitle.textContent=`${turn.sourceLabel||"主会话"} · ${statusText(turn.status)}`;centerValue.textContent=`${compact(turn.usage.total)} Token`;centerDetail.textContent=snapshot.tokens==null?"Context 未知":`${fmt(snapshot.tokens)} / ${snapshot.windowTokens==null?"—":fmt(snapshot.windowTokens)} Token`;centerMeta.textContent=snapshot.tokens==null?contextTypeText(snapshot.snapshotType):`${contextRateText(snapshot)} · ${contextTypeText(snapshot.snapshotType)}`}
    resetCenter();let previousSource=null;
    entries.forEach(entry=>{const {turn,tokens,tokenStart,start,end,satellite,parentEntry}=entry,snapshot=contextSnapshot(turn),source=turn.sourceRolloutId||"main",color=sourceColor(source),knownContext=snapshot.tokens!=null&&snapshot.occupancyRate!=null,rate=Math.max(0,Math.min(100,Number(snapshot.occupancyRate)||0)),contextOuter=innerBase+(innerMax-innerBase)*rate/100,dim=!rowMatches(turn),sourceSwitch=previousSource!==null&&previousSource!==source,satelliteBand=satellite?satelliteGeometry(cx,cy,outerOuter,entry):null,turnFraction=satellite?satelliteFraction(entry):start;previousSource=source;const group=svgEl("g",{class:`context-turn context-sector${satellite?" satellite":""}${dim?" dim":""}${state.selected===turn.turnId?" selected":""}${sourceSwitch?" source-switch":""}`,tabindex:"0",role:"button","data-turn-target":"true","aria-label":`${turn.sourceLabel||"主会话"}，${fmt(tokens)} Token，Context ${knownContext?contextRateText(snapshot):"未知"}`});
      if(satellite){const parentFraction=parentEntry?satelliteFraction(parentEntry):turnFraction,connectorStart=radialPoint(cx,cy,satelliteBand.inner-1,turnFraction),connectorEnd=radialPoint(cx,cy,outerOuter+8,parentFraction);group.appendChild(svgEl("line",{x1:connectorStart.x,y1:connectorStart.y,x2:connectorEnd.x,y2:connectorEnd.y,class:"satellite-connector"}));if(tokens>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,satelliteBand.inner,satelliteBand.outer,start,end),fill:color,class:"token-sector"}));}else{const a=radialPoint(cx,cy,satelliteBand.inner-3,turnFraction),b=radialPoint(cx,cy,satelliteBand.outer+8,turnFraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:color,class:"context-zero-tick"}))}const contextPoint=radialPoint(cx,cy,knownContext?satelliteBand.contextRadius+12*rate/100:satelliteBand.contextRadius,turnFraction);group.appendChild(svgEl("circle",{cx:contextPoint.x,cy:contextPoint.y,r:knownContext?3.5:3,fill:knownContext?contextBandColor(snapshot.occupancyRate):"none",class:knownContext?"satellite-context":"satellite-context-unknown"}))}
      else if(tokens>0){group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,outerInner,outerOuter,start,end),fill:color,class:"token-sector"}));contextBands(turn,tokens).forEach(band=>{const bandStart=(tokenStart+band.start)/denominator,bandEnd=(tokenStart+band.end)/denominator,bandRate=band.snapshot?.occupancyRate,knownBand=bandRate!=null,bandOuter=innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(bandRate)||0))/100;group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,innerBase,knownBand?Math.max(innerBase+1.5,bandOuter):innerMax,bandStart,bandEnd),fill:knownBand?color:"url(#range-context-unknown-pattern)",opacity:knownBand?.22:.7,class:"context-band"}));if(knownBand){group.appendChild(svgEl("path",{d:arcLinePath(cx,cy,Math.max(innerBase+1.5,bandOuter),bandStart,bandEnd),stroke:contextBandColor(bandRate),"stroke-width":2.8,"stroke-linecap":"round","stroke-linejoin":"round",class:"context-contour"}))}});const latest=contextTimeline(turn,tokens).at(-1);if(latest?.occupancyRate!=null){const latestFraction=(tokenStart+Math.max(0,Math.min(tokens,Number(latest.turnTokenOffset)||tokens)))/denominator,latestRadius=innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(latest.occupancyRate)||0))/100,latestPoint=radialPoint(cx,cy,Math.max(innerBase+1.5,latestRadius),latestFraction);group.appendChild(svgEl("circle",{cx:latestPoint.x,cy:latestPoint.y,r:4.8,fill:contextBandColor(latest.occupancyRate),stroke:"#fffefa","stroke-width":2,class:"context-current-marker"}))}}else{const a=radialPoint(cx,cy,innerBase-4,start),b=radialPoint(cx,cy,outerOuter+9,start);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:color,class:"context-zero-tick"}));if(knownContext){const p=radialPoint(cx,cy,Math.max(innerBase+1.5,contextOuter),start);group.appendChild(svgEl("circle",{cx:p.x,cy:p.y,r:4,fill:color,stroke:"#fffefa","stroke-width":1.5}))}}
      if(!satellite)[start,end].forEach(fraction=>{const a=radialPoint(cx,cy,innerBase,fraction),b=radialPoint(cx,cy,outerOuter,fraction);group.appendChild(svgEl("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:"mapping-line"}))});const hitInner=satellite?satelliteBand.inner-9:innerBase-7,hitOuter=satellite?satelliteBand.outer+9:outerOuter+8,hitEnd=tokens>0?end:Math.min(1,start+.004);group.appendChild(svgEl("path",{d:arcBandPath(cx,cy,hitInner,hitOuter,satellite?start:turnFraction,hitEnd),fill:"rgba(0,0,0,.001)"}));group.addEventListener("pointerenter",event=>{showCenter(turn);showTurnTooltip(event,turn,group,total)});group.addEventListener("pointermove",event=>moveTurnTooltip(event,group));group.addEventListener("pointerleave",()=>{resetCenter();hideTooltip()});group.addEventListener("focus",event=>{showCenter(turn);focusTurnTooltip(event,turn,group,total)});group.addEventListener("blur",()=>{resetCenter();hideTooltip()});group.addEventListener("keydown",event=>turnTargetKeydown(event,turn));group.addEventListener("click",()=>{hideTooltip();openDrawer(turn)});svg.appendChild(group);
      (turn.contextCompactions||[]).forEach(event=>{const offset=Math.max(0,Math.min(tokens,Number(event.turnTokenOffset)||0)),fraction=(entry.tokenStart+offset)/denominator,marker=svgEl("g",{class:"context-compaction",tabindex:"0",role:"button","data-turn-target":"true","aria-label":`Compaction，累计 Token 位置 ${(fraction*100).toFixed(2)}%`}),positionOuter=radialPoint(cx,cy,outerOuter+12,fraction),beforeRate=event.before?.occupancyRate,afterRate=event.after?.occupancyRate;if(beforeRate!=null&&afterRate!=null){const before=radialPoint(cx,cy,innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(beforeRate)))/100,fraction),after=radialPoint(cx,cy,innerBase+(innerMax-innerBase)*Math.max(0,Math.min(100,Number(afterRate)))/100,fraction);marker.appendChild(svgEl("line",{x1:positionOuter.x,y1:positionOuter.y,x2:before.x,y2:before.y,class:"compaction-position-line"}));marker.appendChild(svgEl("line",{x1:before.x,y1:before.y,x2:after.x,y2:after.y,class:"compaction-jump-line","marker-end":"url(#range-context-compaction-arrow)"}));marker.appendChild(svgEl("circle",{cx:before.x,cy:before.y,r:4,class:"compaction-before"}));marker.appendChild(svgEl("circle",{cx:after.x,cy:after.y,r:4,class:"compaction-after"}))}else{const positionInner=radialPoint(cx,cy,innerMax+5,fraction);marker.appendChild(svgEl("line",{x1:positionOuter.x,y1:positionOuter.y,x2:positionInner.x,y2:positionInner.y,class:"compaction-position-line"}))}const title=svgEl("title");title.textContent=`Compaction · ${dateText(event.timestamp)} · ${event.before?.tokens==null?"未知":fmt(event.before.tokens)} → ${event.after?.tokens==null?"未知":fmt(event.after.tokens)} Context Token`;marker.appendChild(title);marker.addEventListener("focus",()=>showCenter(turn));marker.addEventListener("blur",resetCenter);marker.addEventListener("keydown",keyEvent=>turnTargetKeydown(keyEvent,turn));marker.addEventListener("click",()=>{hideTooltip();openDrawer(turn)});svg.appendChild(marker)})
    });
    const compactionMarkers=[...svg.querySelectorAll(".context-compaction")];let compactionIndex=0;entries.forEach(entry=>{const count=(entry.turn.contextCompactions||[]).length;if(entry.satellite){const geometry=satelliteGeometry(cx,cy,outerOuter,entry);for(let index=0;index<count;index+=1){const event=entry.turn.contextCompactions[index],offset=Math.max(0,Math.min(entry.tokens,Number(event.turnTokenOffset)||0)),fraction=(entry.tokenStart+offset)/denominator,from=radialPoint(cx,cy,outerOuter+12,fraction),to=radialPoint(cx,cy,geometry.outer+8,fraction),marker=compactionMarkers[compactionIndex+index];if(marker){marker.classList.add("satellite-compaction");marker.setAttribute("transform",`translate(${(to.x-from.x).toFixed(2)} ${(to.y-from.y).toFixed(2)})`)}}}compactionIndex+=count});
    const startPoint=radialPoint(cx,cy,outerOuter+18,0),endPoint=radialPoint(cx,cy,outerOuter+18,1);[[startPoint,"Token 0%","start"],[endPoint,"Token 100%","end"]].forEach(([point,label,anchor])=>{const node=svgEl("text",{x:point.x,y:point.y+4,"text-anchor":anchor,fill:css("--muted"),"font-size":"11","font-weight":"700"});node.textContent=label;svg.appendChild(node)});const outerLabel=svgEl("text",{x:24,y:33,fill:css("--muted"),"font-size":"12","font-weight":"700"});outerLabel.textContent="主圈 · Token 消耗（累计 Token 进度）";svg.appendChild(outerLabel);const satelliteLabel=svgEl("text",{x:24,y:53,fill:css("--muted"),"font-size":"12"});satelliteLabel.textContent="卫星层 · 子 agent Token（连接至主 turn）";svg.appendChild(satelliteLabel);const innerLabel=svgEl("text",{x:24,y:73,fill:css("--muted"),"font-size":"12"});innerLabel.textContent="内环 · Context 快照（按 Token 位置阶梯变化）";svg.appendChild(innerLabel)
  }
  function donutSlicePath(cx,cy,outer,inner,start,end){const x1=cx+outer*Math.cos(start),y1=cy+outer*Math.sin(start),x2=cx+outer*Math.cos(end),y2=cy+outer*Math.sin(end),ix1=cx+inner*Math.cos(start),iy1=cy+inner*Math.sin(start),ix2=cx+inner*Math.cos(end),iy2=cy+inner*Math.sin(end),large=end-start>Math.PI?1:0;return`M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${outer} ${outer} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)} L ${ix2.toFixed(2)} ${iy2.toFixed(2)} A ${inner} ${inner} 0 ${large} 0 ${ix1.toFixed(2)} ${iy1.toFixed(2)} Z`}
  function modelSliceLayout(entries,valueKey){const values=entries.map(entry=>Math.max(0,Number(entry[valueKey])||0)),total=values.reduce((sum,value)=>sum+value,0),visibleEntries=entries.filter((entry,index)=>values[index]>0),slices=[];let angle=-Math.PI/2;entries.forEach((entry,index)=>{const value=values[index];if(value<=0)return;const span=2*Math.PI*value/total,gap=visibleEntries.length>1?Math.min(.026,span/4):.004;slices.push({entry,value,span,gap,start:angle,end:angle+span});angle+=span});return{values,total,visibleEntries,slices}}
  function renderSessionRing(svg,entries,valueKey,unit){const layout=modelSliceLayout(entries,valueKey);if(!layout.total)return;layout.slices.forEach(modelSlice=>{const model=modelSlice.entry.model;if(model==="多模型")return;const sessionEntries=sessions.flatMap((session,index)=>{const usage=Array.isArray(session?.summary?.modelUsage)?session.summary.modelUsage:[];if(usage.length!==1||usage[0].model!==model)return[];const value=Math.max(0,Number(usage[0][valueKey])||0);return value?[{session,value,index}]:[]});if(!sessionEntries.length)return;let angle=modelSlice.start;sessionEntries.forEach((entry,index)=>{const span=modelSlice.span*entry.value/modelSlice.value,gap=sessionEntries.length>1?Math.min(.02,span/4):.003,start=angle+gap/2,end=angle+span-gap/2,sessionId=entry.session.metadata.threadId||String(entry.index),sessionTitle=entry.session.metadata.title||sessionId,visual=modelVisual(model),selected=state.selectedSessionIds.has(sessionId),path=svgEl("path",{d:donutSlicePath(220,145,126,111,start,end),fill:visual[0],opacity:".82",stroke:"#fffefa","stroke-width":"2","stroke-linejoin":"round",class:`session-donut-sector${selected?" selected":""}`,tabindex:"0",role:"button","aria-pressed":String(selected),"data-session-id":sessionId,"aria-label":`${model} · ${sessionTitle} · ${fmt(pieDisplayValue(entry.value,valueKey))} ${unit}`}),title=svgEl("title",{});title.textContent=`${model} · 会话 · ${sessionTitle} · ${fmt(pieDisplayValue(entry.value,valueKey))} ${unit} · ${(100*entry.value/modelSlice.value).toFixed(1)}%`;path.appendChild(title);path.addEventListener("pointerenter",event=>{setSessionHover(sessionId);showSessionTooltip(event,entry.session,path,model,valueKey,entry.value,modelSlice.value)});path.addEventListener("pointermove",event=>moveSessionTooltip(event,path));path.addEventListener("pointerleave",()=>{clearSessionHover(sessionId);hideTooltip()});path.addEventListener("focus",event=>{setSessionHover(sessionId);showSessionTooltip(event,entry.session,path,model,valueKey,entry.value,modelSlice.value)});path.addEventListener("blur",()=>{clearSessionHover(sessionId);hideTooltip()});path.addEventListener("keydown",event=>ringSessionKeydown(event,sessionId));let clickTimer;path.addEventListener("click",event=>{event.preventDefault();clearTimeout(clickTimer);clickTimer=setTimeout(()=>selectRingSession(event,sessionId),220)});path.addEventListener("dblclick",event=>{event.preventDefault();clearTimeout(clickTimer);event.stopPropagation();enterSessionFromRing(sessionId)});svg.appendChild(path);angle+=span})})}
  function renderModelPie(svgId,legendId,entries,valueKey,unit){const svg=byId(svgId),legend=byId(legendId),layout=modelSliceLayout(entries,valueKey);svg.replaceChildren();legend.replaceChildren();svg.setAttribute("viewBox","0 0 440 315");if(!layout.total){const empty=svgEl("text",{x:220,y:156,"text-anchor":"middle",class:"pie-empty"});empty.textContent="暂无计划内模型 Token 数据";svg.appendChild(empty);return}const {total,visibleEntries,slices}=layout,cx=220,cy=145,outer=104,inner=68;slices.forEach(slice=>{const {entry,value,start:angle,span,gap}=slice,start=angle+gap/2,end=angle+span-gap/2,visual=modelVisual(entry.model),path=svgEl("path",{d:donutSlicePath(cx,cy,outer,inner,start,end),fill:visual[0],stroke:"#fffefa","stroke-width":"3","stroke-linejoin":"round"});const title=svgEl("title");title.textContent=`${entry.model} · ${fmt(pieDisplayValue(value,valueKey))} ${unit} · ${(100*value/total).toFixed(2)}%`;path.appendChild(title);svg.appendChild(path)});const centerLabel=svgEl("text",{x:cx,y:cy-10,"text-anchor":"middle",fill:css("--muted"),"font-size":"12","font-weight":"750"}),centerValue=svgEl("text",{x:cx,y:cy+17,"text-anchor":"middle",fill:css("--text"),"font-size":"22","font-weight":"850"}),centerUnit=svgEl("text",{x:cx,y:cy+39,"text-anchor":"middle",fill:css("--muted"),"font-size":"11"});centerLabel.textContent="计划内模型";centerValue.textContent=fmt(pieDisplayValue(total,valueKey));centerUnit.textContent=unit;svg.append(centerLabel,centerValue,centerUnit);legend.innerHTML=visibleEntries.map(entry=>{const value=Number(entry[valueKey])||0,visual=modelVisual(entry.model),rate=entry.rateMultiplier==null?"按分类费率":`${Number(entry.rateMultiplier).toFixed(2)}×`;return`<div class="pie-legend-row"><span class="swatch" style="--swatch:${visual[0]}"></span><span class="name" title="${esc(entry.model)}"><strong>${esc(entry.model)}</strong><small>费率 ${esc(rate)}</small></span><span class="value"><b>${fmt(pieDisplayValue(value,valueKey))} ${esc(unit)}</b><small>${(100*value/total).toFixed(1)}%</small></span></div>`}).join("")}
  function renderModelPies(){const entries=Array.isArray(data.summary.modelUsage)?data.summary.modelUsage:[],rate=data.metadata.rateCard||{},excluded=data.summary.planExcludedUsage||{},excludedTokens=Math.max(0,Number(excluded.rawTokens)||0),note=byId("model-plan-note");byId("range-model-pie-view").hidden=false;byId("range-session-context-view").hidden=true;byId("model-rate-meta").textContent=`费率表生效：${rate.effectiveDate||"—"} · 最近核验：${dateText(rate.checkedAt)} · 来源：${rate.source||"—"}`;note.hidden=excludedTokens<=0;if(excludedTokens>0){note.innerHTML=`<span>ⓘ</span><span><strong>Spark 未纳入模型对比：</strong>共计 ${fmt(excludedTokens)} Token。总 Token 仍保留，但不参与模型图和费率比较。</span>`}else note.replaceChildren();renderModelPie("model-token-pie","model-token-legend",entries,"rawTokens","Token");renderSessionRing(byId("model-token-pie"),entries,"rawTokens","Token");renderModelPie("model-weighted-pie","model-weighted-legend",entries,"weightedTokens","Sol 等价 Token");renderSessionRing(byId("model-weighted-pie"),entries,"weightedTokens","Sol 等价 Token")}
  function renderToolsStandalone(){if(isTotal())return;const session=activeSession(),ordered=[...(session?.turns||[])].sort((a,b)=>contextOrderTime(a).localeCompare(contextOrderTime(b))||a.index-b.index),total=ordered.reduce((sum,turn)=>sum+Math.max(0,Number(turn.usage.total)||0),0),denominator=Math.max(total,1);renderToolLayer(byId("range-context-radial-chart"),radialEntries(ordered,denominator),380,300,234,denominator)}
  function renderContext(){if(isTotal()){renderModelPies();return}byId("range-model-pie-view").hidden=true;byId("range-session-context-view").hidden=false;renderContextBase();renderToolsStandalone()}
  function renderComposition(){const visible=rows(),svg=byId("composition");svg.replaceChildren();byId("visible-count").textContent=`显示 ${visible.length} ${isTotal()?"个会话":"轮"}`;byId("composition-title").textContent=isTotal()?"会话 Token 构成":"单轮 Token 构成";byId("composition-note").textContent="";byId("composition-note").hidden=true;byId("content-search").placeholder=isTotal()?"搜索会话标题、ID 或来源……":messagesIncluded?"搜索消息、轮次 ID、模型或来源……":"搜索轮次 ID、模型或来源……";document.querySelectorAll(".turn-only").forEach(x=>x.hidden=isTotal());byId("legend").innerHTML=segmentKeys.map(k=>`<span style="--swatch:${colors[k]}">${esc(labels[k])}</span>`).join("");const width=Math.max(1100,visible.length*30+110),height=410,m={t:16,r:20,b:48,l:76},iw=width-m.l-m.r,ih=height-m.t-m.b;svg.setAttribute("viewBox",`0 0 ${width} ${height}`);svg.style.minWidth=`${width}px`;if(!visible.length){text(svg,width/2,height/2,"没有符合条件的数据","middle");return}const max=Math.max(...visible.map(r=>r.usage.total),1),scaled=v=>state.scale==="log"?Math.log10(1+v)/Math.log10(1+max):v/max;const ticks=state.scale==="log"?[0,...Array.from({length:Math.floor(Math.log10(max))+1},(_,i)=>10**i).filter(v=>v<=max)]:niceTicks(max);ticks.forEach(tick=>{const y=m.t+ih*(1-scaled(tick));svg.appendChild(svgEl("line",{x1:m.l,x2:width-m.r,y1:y,y2:y,class:"grid"}));text(svg,m.l-9,y+4,compact(tick))});const step=iw/visible.length,bw=Math.max(4,Math.min(20,step*.72));visible.forEach((row,pos)=>{const x=m.l+pos*step+(step-bw)/2,total=Math.max(0,row.usage.total),totalH=ih*scaled(total);let y=m.t+ih;const g=svgEl("g",{class:"bar",tabindex:"0",role:"button","data-turn-target":"true"});segmentKeys.forEach(k=>{const value=Math.max(0,row.breakdown[k]||0);if(!value||!total)return;const h=totalH*value/Math.max(total,1);y-=h;g.appendChild(svgEl("rect",{x,y,width:bw,height:Math.max(.5,h),fill:colors[k],rx:"1"}))});const title=svgEl("title");title.textContent=`${isTotal()?row.title:`第 ${row.index} 轮`} · ${fmt(total)} Token`;g.appendChild(title);g.addEventListener("click",()=>isTotal()?selectView(row.turnId):openDrawer(row));svg.appendChild(g);if(visible.length<=35||pos%Math.ceil(visible.length/28)===0)text(svg,x+bw/2,height-22,String(row.index),"middle")});text(svg,m.l+iw/2,height-4,isTotal()?"会话序号":"轮次序号","middle")}
  function renderTrend(){const svg=byId("trend");svg.replaceChildren();let pointsData;if(isTotal()){pointsData=data.summary.dailyUsage||[];byId("trend-title").textContent="每日 Token 消耗"}else{let cumulative=0;pointsData=(activeSession()?.turns||[]).map(t=>({date:`第 ${t.index} 轮`,usage:{total:(cumulative+=Number(t.usage.total||0))}}));byId("trend-title").textContent="会话累计消耗"}byId("trend-note").textContent="";byId("trend-note").hidden=true;const width=1200,height=270,m={t:18,r:24,b:42,l:76},iw=width-m.l-m.r,ih=height-m.t-m.b;svg.setAttribute("viewBox",`0 0 ${width} ${height}`);if(!pointsData.length){text(svg,width/2,height/2,"没有趋势数据","middle");return}const max=Math.max(...pointsData.map(p=>Number(p.usage.total||0)),1);niceTicks(max,4).forEach(tick=>{const y=m.t+ih*(1-tick/max);svg.appendChild(svgEl("line",{x1:m.l,x2:width-m.r,y1:y,y2:y,class:"grid"}));text(svg,m.l-9,y+4,compact(tick))});const pts=pointsData.map((p,i)=>({x:m.l+(pointsData.length===1?iw/2:i*iw/(pointsData.length-1)),y:m.t+ih*(1-Number(p.usage.total||0)/max),label:p.date,value:Number(p.usage.total||0)}));if(pts.length>1)svg.appendChild(svgEl("path",{d:pts.map((p,i)=>`${i?"L":"M"}${p.x},${p.y}`).join(" "),fill:"none",stroke:css("--accent"),"stroke-width":"2.5"}));pts.forEach((p,i)=>{const c=svgEl("circle",{cx:p.x,cy:p.y,r:4,fill:css("--accent")});const title=svgEl("title");title.textContent=`${p.label} · ${fmt(p.value)} Token`;c.appendChild(title);svg.appendChild(c);if(pts.length<=12||i%Math.ceil(pts.length/10)===0)text(svg,p.x,height-16,p.label,"middle")})}
  function renderTable(){
    const visible=rows(),head=byId("table-head"),body=byId("table-body"),empty=byId("table-empty");byId("table-title").textContent=isTotal()?"会话总览":"逐轮明细";byId("table-note").textContent="";byId("table-note").hidden=true;
    if(isTotal()){
      head.innerHTML="<tr><th>#</th><th>会话</th><th>来源</th><th>最近活动</th><th>轮次</th><th>缓存输入</th><th>缓存写入</th><th>其他输入</th><th>普通输出</th><th>推理输出</th><th>Token 总量</th><th>缓存命中率</th></tr>";
      body.innerHTML=visible.map(r=>{const b=r.breakdown,u=r.usage;return`<tr data-id="${esc(r.turnId)}"><td>${r.index}</td><td class="title-cell" title="${esc(r.title)}">${esc(r.title)}</td><td>${esc(r.sourceLabel)}</td><td>${esc(dateText(r.startedAt))}</td><td>${formatCount(r.session.summary.turnCount)}</td><td>${fmt(b.cachedInput)}</td><td>${data.metadata.cacheWriteFieldAvailable?fmt(b.cacheWriteInput):"不适用"}</td><td>${fmt(b.otherNonCachedInput)}</td><td>${fmt(b.ordinaryOutput)}</td><td>${fmt(b.reasoningOutput)}</td><td><b>${fmt(u.total)}</b></td><td>${cacheRate(u).toFixed(2)}%</td></tr>`}).join("");body.querySelectorAll("tr").forEach(row=>row.addEventListener("click",()=>selectView(row.dataset.id)));
    }else{
      head.innerHTML="<tr><th>轮次</th><th>来源</th><th>状态</th><th>开始时间</th><th>模型响应</th><th>缓存输入</th><th>缓存写入</th><th>其他输入</th><th>普通输出</th><th>推理输出</th><th>Token 总量</th><th>缓存命中率</th><th>Context 占用</th><th>Context 占用率</th><th>用户输入</th></tr>";
      body.innerHTML=visible.map(r=>{const b=r.breakdown,u=r.usage,p=firstPrompt(r).replace(/\s+/g," ").trim(),snapshot=contextSnapshot(r),contextTitle=`${contextTypeText(snapshot.snapshotType)} · ${dateText(snapshot.timestamp)}`;return`<tr data-id="${esc(r.turnId)}" data-turn-target="true"><td>${r.index}${r.rangeClipped?' <span class="provisional" title="已按日期范围裁剪">◐</span>':""}</td><td>${esc(r.sourceLabel||"主会话")}</td><td>${esc(statusText(r.status))}</td><td>${esc(dateText(r.startedAt))}</td><td>${formatCount(r.modelResponses)}</td><td>${fmt(b.cachedInput)}</td><td>${activeSession().metadata.cacheWriteFieldAvailable?fmt(b.cacheWriteInput):"不适用"}</td><td>${fmt(b.otherNonCachedInput)}</td><td>${fmt(b.ordinaryOutput)}</td><td>${fmt(b.reasoningOutput)}</td><td><b>${fmt(u.total)}</b></td><td>${cacheRate(u).toFixed(2)}%</td><td title="${esc(contextTitle)}">${snapshot.tokens==null?"—":fmt(snapshot.tokens)}</td><td title="${esc(contextTitle)}">${esc(contextRateText(snapshot))}</td><td class="title-cell" title="${esc(p)}">${esc(p||"—")}</td></tr>`}).join("");body.querySelectorAll("tr").forEach(row=>row.addEventListener("click",()=>openDrawer((activeSession()?.turns||[]).find(t=>t.turnId===row.dataset.id))));
    }
    empty.hidden=visible.length>0;empty.textContent=isTotal()?"指定日期范围内没有会话活动。":"没有符合当前筛选条件的轮次。";
  }
  function openDrawer(turn){
    if(!turn)return;state.selected=turn.turnId;byId("drawer-title").textContent=`第 ${turn.index} 轮 · ${turn.sourceLabel||"主会话"}`;const snapshot=contextSnapshot(turn),details=[["状态",statusText(turn.status)],["轮次 ID",turn.turnId],["来源",turn.sourceLabel||"主会话"],["开始时间",dateText(turn.startedAt)],["结束时间",dateText(turn.endedAt)],["范围活动",`${dateText(turn.rangeFirstActivityAt)} — ${dateText(turn.rangeLastActivityAt)}`],["日期裁剪",turn.rangeClipped?"是":"否"],["模型",(turn.models||[]).join(", ")||"—"],["上下文快照类型",contextTypeText(snapshot.snapshotType)],["上下文占用",snapshot.tokens==null?"—":fmt(snapshot.tokens)],["上下文窗口",snapshot.windowTokens==null?"—":fmt(snapshot.windowTokens)],["上下文占用率",contextRateText(snapshot)],["上下文快照时间",dateText(snapshot.timestamp)],["上下文压缩",formatCount(turn.compactions)],["总 Token",fmt(turn.usage.total)],["输入",fmt(turn.usage.input)],["输出",fmt(turn.usage.output)],["推理输出",fmt(turn.usage.reasoning)]];
    let html=`<div class="detail-grid">${details.map(d=>`<div class="detail"><span>${esc(d[0])}</span><b>${esc(d[1])}</b></div>`).join("")}</div>`;
    if((turn.contextCompactions||[]).length){const lines=turn.contextCompactions.map((event,index)=>{const side=value=>value?`${fmt(value.tokens)} / ${value.windowTokens==null?"—":fmt(value.windowTokens)} · ${contextRateText(value)}`:"未知";return`#${index+1} · ${dateText(event.timestamp)}\n  turn 内 Token 位置：${event.turnTokenOffset==null?"未知":fmt(event.turnTokenOffset)}\n  压缩前：${side(event.before)}\n  压缩后：${side(event.after)}`});html+=`<div class="message"><div class="message-head">Compaction 前后上下文</div><pre>${esc(lines.join("\n\n"))}</pre></div>`}
    if(isSatelliteTurn(turn))html+=drawerOutputSection(turn);else html+=(turn.messages||[]).length?turn.messages.map((m,i)=>`<section class="message"><div class="message-head">${i?"追加用户消息":"初始用户消息"} · ${esc(dateText(m.timestamp))}</div><pre></pre></section>`).join(""):`<div class="message"><div class="message-head">用户消息</div><pre>${messagesIncluded?"该轮未记录范围内用户消息。":"生成报告时已排除用户消息。"}</pre></div>`;byId("drawer-body").innerHTML=html;byId("drawer-body").querySelectorAll("section.message pre").forEach((pre,i)=>pre.textContent=turn.messages[i].text);byId("drawer").classList.add("open");byId("drawer").setAttribute("aria-hidden","false");renderContext();renderTable();
  }
  function closeDrawer(){state.selected=null;byId("drawer").classList.remove("open");byId("drawer").setAttribute("aria-hidden","true");if(!isTotal())renderContext()}
  function setTab(tab){const contextButton=byId("tab-context");contextButton.disabled=false;contextButton.textContent=isTotal()?"模型消耗概览":"Token 与 Context";if(!["composition","trend","context","table"].includes(tab))tab="context";state.tab=tab;document.querySelectorAll("[data-tab-target]").forEach(button=>{const active=button.dataset.tabTarget===tab;button.classList.toggle("active",active);button.setAttribute("aria-selected",String(active))});document.querySelectorAll("[data-tab-panel]").forEach(panel=>{panel.hidden=panel.dataset.tabPanel!==tab});syncFilterVisibility(tab)}
  function resetFilters(){state.query="";state.toolCategories=new Set(DEFAULT_TOOL_CATEGORIES);state.modelFilters=new Set(modelFilterModels());state.statuses=new Set(["complete","aborted","incomplete"]);state.scale="linear";byId("content-search").value="";syncToolFilterInputs();renderModelFilter();document.querySelectorAll("[data-status]").forEach(input=>input.checked=true);byId("linear").classList.add("active");byId("log").classList.remove("active");renderNav();renderComposition();renderContext();renderTable()}
  function render(){renderHeader();renderTabModelLabel();syncAnalysisControls();renderWarnings();renderComposition();renderTrend();renderContext();renderTable();setTab(state.tab);byId("footer").textContent=`生成时间：${dateText(data.metadata.generatedAt)} · ${data.generator.name} ${data.generator.version} · 本地日期 ${data.metadata.dateWindow.startDate} — ${data.metadata.dateWindow.endDate}`}
  history.replaceState({...(history.state||{}),codexTokenReport:true,view:"total",returnToTotalSessionId:null},"","");
  window.addEventListener("popstate",event=>{const entry=event.state;applyView(entry?.codexTokenReport?entry.view:"total",Boolean(entry?.codexTokenReport&&entry.returnToTotalSessionId))});
  byId("range-label").textContent=`${data.metadata.dateWindow.startDate} — ${data.metadata.dateWindow.endDate} · ${data.metadata.dateWindow.timezone}`;
  byId("nav-search").addEventListener("input",renderNav);
  byId("return-total-from-ring").addEventListener("click",goToTotal);
  byId("go-total-session").addEventListener("click",goToTotal);
  byId("sidebar-rail-toggle").addEventListener("click",()=>setSessionNav(true,true));
  byId("session-drawer-close").addEventListener("click",()=>setSessionNav(false,true));
  byId("session-drawer-backdrop").addEventListener("click",()=>setSessionNav(false,true));
  document.querySelectorAll("[data-tab-target]").forEach(button=>button.addEventListener("click",()=>setTab(button.dataset.tabTarget)));
  byId("content-search").addEventListener("input",e=>{state.query=e.target.value.trim().toLocaleLowerCase();renderComposition();renderContext();renderTable()});
   byId("token-unit-slider").addEventListener("input",event=>setTokenUnit(TOKEN_UNIT_ORDER[Number(event.target.value)]||"raw"));
  document.querySelectorAll("[data-status]").forEach(input=>input.addEventListener("change",()=>{input.checked?state.statuses.add(input.dataset.status):state.statuses.delete(input.dataset.status);renderComposition();renderContext();renderTable()}));
  byId("reset-filters").addEventListener("click",resetFilters);
  byId("linear").addEventListener("click",()=>{state.scale="linear";byId("linear").classList.add("active");byId("log").classList.remove("active");renderComposition()});
  byId("log").addEventListener("click",()=>{state.scale="log";byId("log").classList.add("active");byId("linear").classList.remove("active");renderComposition()});
  byId("drawer-close").addEventListener("click",closeDrawer);
  document.addEventListener("click",event=>{const drawer=byId("drawer");if(event.button!==0||!drawer.classList.contains("open"))return;const target=event.target instanceof Element?event.target:null;if(!target||drawer.contains(target)||target.closest("[data-turn-target=true]"))return;closeDrawer()});
  document.addEventListener("keydown",event=>{if(event.key!=="Escape")return;if(byId("drawer").classList.contains("open"))closeDrawer();else if(state.sessionNavOpen)setSessionNav(false,true)});
  try{populateToolFilter();renderModelFilter();setSessionNav(state.sessionNavOpen);renderNav();render();document.body.dataset.reportReady="true"}catch(error){document.body.dataset.reportReady="error";const pre=document.createElement("pre");pre.className="empty";pre.textContent=`报告渲染失败：${error.stack||error}`;document.body.prepend(pre);console.error(error)}
  } catch (error) {
    console.error("报告初始化失败：", error);
    document.body.innerHTML = "";
    const pre = document.createElement("pre");
    pre.className = "empty";
    pre.textContent = `报告初始化失败：${error.stack || error}`;
    document.body.appendChild(pre);
  }
})();
</script>
</body>
</html>
"""


def render_html(report: dict[str, Any], title: str | None = None) -> str:
    if report.get("mode") == "range":
        date_window = report.get("metadata", {}).get("dateWindow", {})
        start_date = date_window.get("startDate", "unknown")
        end_date = date_window.get("endDate", start_date)
        date_label = start_date if start_date == end_date else f"{start_date} — {end_date}"
        page_title = title or f"Codex Token 日期报告 · {date_label}"
        template = RANGE_HTML_TEMPLATE
    else:
        thread_id = report.get("metadata", {}).get("threadId", "unknown")
        page_title = title or f"Codex Token 报告 · {thread_id}"
        template = HTML_TEMPLATE
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
    rendered = template.replace("__PAGE_TITLE__", html.escape(page_title)).replace(
        "__REPORT_JSON__", report_json
    )
    return rendered.replace("</body>", TOOL_SATELLITE_HIT_SCRIPT + "\n</body>")


def set_message_policy(report: dict[str, Any], include_messages: bool) -> None:
    """Apply the report's explicit message-embedding policy in place."""
    report.setdefault("metadata", {})["messagesIncluded"] = include_messages
    report["metadata"]["containsFullUserMessages"] = include_messages
    if report.get("mode") == "range":
        for session in report.get("sessions", []):
            metadata = session.setdefault("metadata", {})
            metadata["messagesIncluded"] = include_messages
            metadata["containsFullUserMessages"] = include_messages
            if not include_messages:
                metadata["title"] = metadata.get("fallbackTitle") or metadata.get("threadId")
                metadata["messageTitle"] = None
    if include_messages:
        return
    if report.get("mode") == "range":
        for session in report.get("sessions", []):
            for turn in session.get("turns", []):
                turn["messages"] = []
            session["orphanMessages"] = []
        return
    for turn in report.get("turns", []):
        turn["messages"] = []
    report["orphanMessages"] = []


def default_output_path(thread_id: str) -> Path:
    safe_id = re.sub(r"[^0-9A-Za-z._-]+", "-", thread_id).strip(".-") or "thread"
    return Path(tempfile.gettempdir()) / "agenttools" / f"codex-token-{safe_id}.html"


def default_range_output_path(window: DateWindow) -> Path:
    label = window.start_date.isoformat()
    if window.end_date != window.start_date:
        label += f"-to-{window.end_date.isoformat()}"
    return Path(tempfile.gettempdir()) / "agenttools" / f"codex-token-{label}.html"


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"日期必须采用 YYYY-MM-DD 格式：{value}"
        ) from exc


def _date_window_from_args(args: argparse.Namespace) -> DateWindow | None:
    selectors = bool(args.today) + bool(args.single_date) + bool(
        args.date_from or args.date_to
    )
    if args.thread and selectors:
        raise ValueError("线程输入不能与日期范围参数同时使用。")
    if selectors > 1:
        raise ValueError("--today、--date 与 --from/--to 只能选择一种。")
    if args.date_from is None and args.date_to is not None:
        raise ValueError("--to 必须与 --from 一起使用。")
    if args.date_from is not None and args.date_to is None:
        raise ValueError("--from 必须与 --to 一起使用。")
    if args.today:
        today = datetime.now().astimezone().date()
        return DateWindow.for_dates(today, today)
    if args.single_date is not None:
        return DateWindow.for_dates(args.single_date, args.single_date)
    if args.date_from is not None and args.date_to is not None:
        return DateWindow.for_dates(args.date_from, args.date_to)
    if not args.thread:
        raise ValueError(
            "请提供线程 ID/JSONL，或使用 --today、--date、--from/--to。"
        )
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为一个 Codex 线程或本地日期范围生成自包含的交互式 Token 报告。默认按尽力模式运行，发现完整性问题仅做记录；仅在加 `--strict` 时才因错误终止。"
    )
    parser.add_argument(
        "thread",
        nargs="?",
        help="Codex 线程 ID，或明确的 rollout .jsonl 文件路径。",
    )
    parser.add_argument(
        "--today",
        action="store_true",
        help="统计本机时区今天实际发生的 Token 消耗。",
    )
    parser.add_argument(
        "--date",
        dest="single_date",
        type=_iso_date,
        help="统计本机时区指定日期（YYYY-MM-DD）。",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        type=_iso_date,
        help="日期范围起点（YYYY-MM-DD，含当日；必须与 --to 同时使用）。",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        type=_iso_date,
        help="日期范围终点（YYYY-MM-DD，含当日；必须与 --from 同时使用）。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="输出 HTML 路径（默认文件名包含线程 ID 或日期范围）。",
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
        help="启用严格模式：发现完整性错误时失败，并终止写入 HTML。",
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


def configure_console_encoding() -> None:
    """Make Chinese CLI output reliable when Windows redirects to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    started_at = monotonic_time.perf_counter()
    args = build_parser().parse_args(argv)
    roots = [path.expanduser().resolve() for path in args.sessions_root] if args.sessions_root else None
    try:
        window = _date_window_from_args(args)
        if window is not None:
            report = build_range_report(window, roots=roots)
        else:
            requested_id = _extract_thread_id(args.thread)
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
    if report.get("mode") == "range":
        output = args.output or default_range_output_path(window)
    else:
        thread_id = report["metadata"]["threadId"]
        output = args.output or default_output_path(thread_id)
    output = output.expanduser().resolve()
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_html(report, args.title), encoding="utf-8")
    except OSError as exc:
        print(f"错误：无法写入 {output}：{exc}", file=sys.stderr)
        return 4

    summary = report["summary"]
    usage = summary["finalUsage"]
    if report.get("mode") == "range":
        date_window = report["metadata"]["dateWindow"]
        print(
            f"日期：{date_window['startDate']} — {date_window['endDate']}"
            f"（{date_window['timezone']}）"
        )
        session_count = summary["sessionCount"]
    else:
        print(f"线程：{thread_id}")
        session_count = 1
    print(f"会话：{session_count:,}")
    print(f"轮次：{summary['turnCount']:,}")
    print(f"总 Token：{usage['total']:,}")
    print(f"输入：{usage['input']:,}（缓存读取：{usage['cached']:,}）")
    print(f"输出：{usage['output']:,}（推理输出：{usage['reasoning']:,}）")
    print(f"完整性错误：{summary['integrityErrorCount']:,}")
    print(f"用户消息：{'已嵌入' if report['metadata']['messagesIncluded'] else '已排除'}")
    print(f"耗时：{monotonic_time.perf_counter() - started_at:.2f} 秒")
    print(f"报告：{output}")
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
