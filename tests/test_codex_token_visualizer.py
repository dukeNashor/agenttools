from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO = Path(__file__).parents[1]
SCRIPT = (
    REPO
    / "skills"
    / "visualize-codex-tokens"
    / "scripts"
    / "codex_token_visualizer.py"
)
FIXTURE = REPO / "tests" / "fixtures" / "synthetic-rollout.jsonl"
SPEC = importlib.util.spec_from_file_location("codex_token_visualizer", SCRIPT)
assert SPEC and SPEC.loader
viz = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = viz
SPEC.loader.exec_module(viz)


def rec(timestamp: str, record_type: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": record_type, "payload": payload},
        ensure_ascii=False,
    )


def usage(
    input_tokens: int,
    cached: int,
    output: int,
    reasoning: int = 0,
    cache_write: int | None = None,
) -> dict:
    value = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }
    if cache_write is not None:
        value["cache_write_input_tokens"] = cache_write
    return value


def token_info(
    total_usage: dict,
    context_tokens: int,
    context_window: int = 1_000,
) -> dict:
    return {
        "total_token_usage": total_usage,
        # Compaction snapshots can carry only the context total. The parser
        # must not derive context occupancy by summing these component fields.
        "last_token_usage": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": context_tokens,
        },
        "model_context_window": context_window,
    }


class VisualizerTests(unittest.TestCase):
    def write_rollout(self, lines: list[str], trailing_newline: bool = True) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "rollout-00000000-0000-0000-0000-000000000002.jsonl"
        text = "\n".join(lines) + ("\n" if trailing_newline else "")
        path.write_text(text, encoding="utf-8")
        return path

    def write_named_rollout(
        self,
        root: Path,
        thread_id: str,
        lines: list[str],
        trailing_newline: bool = True,
    ) -> Path:
        path = root / f"rollout-2026-01-02T00-00-00-{thread_id}.jsonl"
        text = "\n".join(lines) + ("\n" if trailing_newline else "")
        path.write_text(text, encoding="utf-8")
        return path

    def test_synthetic_fixture_closes_and_reconciles(self) -> None:
        report = viz.parse_rollout(FIXTURE)
        self.assertEqual(report["metadata"]["threadId"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(report["summary"]["turnCount"], 2)
        self.assertEqual(report["summary"]["statusCounts"]["aborted"], 1)
        self.assertEqual(report["summary"]["finalUsage"]["total"], 205)
        self.assertEqual(report["turns"][0]["usage"]["total"], 120)
        self.assertEqual(report["turns"][1]["usage"]["total"], 85)
        self.assertEqual(report["turns"][1]["breakdown"]["cacheWriteInput"], 5)
        self.assertEqual(report["summary"]["reconciliationDifference"]["total"], 0)
        self.assertEqual(report["summary"]["integrityErrorCount"], 0)

    def test_steering_messages_stay_in_the_active_turn(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec("2026-01-01T00:00:01Z", "event_msg", {"type": "user_message", "message": "first"}),
            rec("2026-01-01T00:00:02Z", "event_msg", {"type": "user_message", "message": "steer"}),
            rec(
                "2026-01-01T00:00:03Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(100, 60, 20, 5, 10)}},
            ),
            rec("2026-01-01T00:00:04Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        report = viz.parse_rollout(self.write_rollout(lines))
        self.assertEqual(len(report["turns"]), 1)
        self.assertTrue(report["turns"][0]["messages"][1]["steering"])

    def test_outside_usage_is_reconciled_as_unattributed(self) -> None:
        lines = [
            rec(
                "2026-01-01T00:00:00Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(10, 0, 2)}},
            ),
            rec("2026-01-01T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:02Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(30, 10, 5)}},
            ),
            rec("2026-01-01T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        report = viz.parse_rollout(self.write_rollout(lines))
        self.assertEqual(report["summary"]["unattributedUsage"]["total"], 12)
        self.assertEqual(report["turns"][0]["usage"]["total"], 23)
        self.assertEqual(report["summary"]["reconciliationDifference"]["total"], 0)

    def test_counter_reset_creates_an_integrity_error(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(100, 50, 10)}},
            ),
            rec("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
            rec("2026-01-01T00:00:03Z", "event_msg", {"type": "task_started", "turn_id": "t2"}),
            rec(
                "2026-01-01T00:00:04Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(20, 5, 3)}},
            ),
            rec("2026-01-01T00:00:05Z", "event_msg", {"type": "task_complete", "turn_id": "t2"}),
        ]
        report = viz.parse_rollout(self.write_rollout(lines))
        self.assertEqual(report["summary"]["counterResets"], 1)
        self.assertGreater(report["summary"]["integrityErrorCount"], 0)
        self.assertEqual(report["summary"]["reconciliationDifference"]["total"], 0)

    def test_date_window_attributes_snapshot_delta_and_marks_clipped_turn(self) -> None:
        window = viz.DateWindow.for_dates(
            date(2026, 1, 2),
            date(2026, 1, 2),
            local_tz=timezone(timedelta(hours=8)),
        )
        lines = [
            rec("2026-01-01T15:59:50Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T15:59:55Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(10, 0, 2)}},
            ),
            rec(
                "2026-01-01T16:00:01Z",
                "event_msg",
                {"type": "user_message", "message": "inside range"},
            ),
            rec(
                "2026-01-01T16:00:02Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(30, 10, 5)}},
            ),
            rec("2026-01-01T16:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        report = viz.parse_rollout(self.write_rollout(lines), window=window)
        self.assertEqual(report["summary"]["finalUsage"]["total"], 23)
        self.assertEqual(report["summary"]["dailyUsage"][0]["usage"]["total"], 23)
        self.assertEqual(len(report["turns"]), 1)
        self.assertTrue(report["turns"][0]["rangeClipped"])
        self.assertEqual(report["turns"][0]["messages"][0]["text"], "inside range")

    def test_completed_turn_uses_last_context_snapshot_before_terminal(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(10, 5, 2), 320, 1_000)},
            ),
            rec(
                "2026-01-01T00:00:02Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(30, 15, 5), 391, 1_000)},
            ),
            rec("2026-01-01T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        turn = viz.parse_rollout(self.write_rollout(lines))["turns"][0]
        self.assertEqual(
            turn["contextSnapshot"],
            {
                "snapshotType": "turn_end",
                "tokens": 391,
                "windowTokens": 1_000,
                "occupancyRate": 39.1,
                "timestamp": "2026-01-01T00:00:02Z",
            },
        )

    def test_incomplete_turn_marks_context_snapshot_as_current_latest(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(10, 5, 2), 250, 1_000)},
            ),
        ]
        turn = viz.parse_rollout(self.write_rollout(lines), tolerate_live=True)["turns"][0]
        self.assertEqual(turn["contextSnapshot"]["snapshotType"], "current_latest")
        self.assertEqual(turn["contextSnapshot"]["tokens"], 250)

    def test_turn_without_context_snapshot_is_explicitly_unknown(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(10, 5, 2)}},
            ),
            rec("2026-01-01T00:00:02Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        snapshot = viz.parse_rollout(self.write_rollout(lines))["turns"][0]["contextSnapshot"]
        self.assertEqual(snapshot["snapshotType"], "unknown")
        self.assertIsNone(snapshot["tokens"])

    def test_date_range_uses_latest_in_range_context_for_clipped_turn(self) -> None:
        window = viz.DateWindow.for_dates(
            date(2026, 1, 2),
            date(2026, 1, 2),
            local_tz=timezone.utc,
        )
        lines = [
            rec("2026-01-01T23:59:55Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-02T00:00:02Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(10, 5, 2), 400, 1_000)},
            ),
            rec(
                "2026-01-03T00:00:00Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(30, 15, 5), 600, 1_000)},
            ),
            rec("2026-01-03T00:00:01Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        turn = viz.parse_rollout(self.write_rollout(lines), window=window)["turns"][0]
        self.assertTrue(turn["rangeClipped"])
        self.assertEqual(turn["contextSnapshot"]["snapshotType"], "range_latest")
        self.assertEqual(turn["contextSnapshot"]["tokens"], 400)
        self.assertEqual(turn["contextSnapshot"]["timestamp"], "2026-01-02T00:00:02Z")
        self.assertEqual(
            turn["contextTimeline"],
            [
                {
                    "tokens": 400,
                    "windowTokens": 1_000,
                    "occupancyRate": 40.0,
                    "timestamp": "2026-01-02T00:00:02Z",
                    "turnTokenOffset": 12,
                }
            ],
        )

    def test_compaction_records_context_before_and_after(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(10, 5, 2), 800, 1_000)},
            ),
            rec("2026-01-01T00:00:02Z", "compacted", {}),
            rec(
                "2026-01-01T00:00:03Z",
                "event_msg",
                {"type": "token_count", "info": token_info(usage(10, 5, 2), 100, 1_000)},
            ),
            rec("2026-01-01T00:00:04Z", "event_msg", {"type": "context_compacted"}),
            rec("2026-01-01T00:00:05Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        turn = viz.parse_rollout(self.write_rollout(lines))["turns"][0]
        self.assertEqual(turn["compactions"], 1)
        self.assertEqual(turn["contextCompactions"][0]["before"]["tokens"], 800)
        self.assertEqual(turn["contextCompactions"][0]["after"]["tokens"], 100)
        self.assertEqual(turn["contextCompactions"][0]["turnTokenOffset"], 12)
        self.assertEqual(turn["contextSnapshot"]["tokens"], 100)
        self.assertEqual(
            [
                (point["turnTokenOffset"], point["tokens"])
                for point in turn["contextTimeline"]
            ],
            [(12, 800), (12, 100)],
        )

    def test_range_report_groups_subagent_usage_under_top_level_session(self) -> None:
        root_id = "00000000-0000-0000-0000-000000000010"
        child_id = "00000000-0000-0000-0000-000000000011"
        window = viz.DateWindow.for_dates(
            date(2026, 1, 2),
            date(2026, 1, 2),
            local_tz=timezone.utc,
        )
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            self.write_named_rollout(
                sessions,
                root_id,
                [
                    rec("2026-01-02T00:00:00Z", "session_meta", {"id": root_id, "session_id": root_id, "cwd": "D:\\dev\\root", "source": "vscode"}),
                    rec("2026-01-02T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "root-turn"}),
                    rec("2026-01-02T00:00:02Z", "event_msg", {"type": "user_message", "message": "Root prompt"}),
                    rec("2026-01-02T00:00:09Z", "event_msg", {"type": "token_count", "info": token_info(usage(10, 5, 2), 120, 1_000)}),
                    rec("2026-01-02T00:00:10Z", "event_msg", {"type": "task_complete", "turn_id": "root-turn"}),
                ],
            )
            self.write_named_rollout(
                sessions,
                child_id,
                [
                    rec("2026-01-02T00:00:01Z", "session_meta", {"id": child_id, "session_id": root_id, "parent_thread_id": root_id, "thread_source": "subagent", "cwd": "D:\\dev\\root"}),
                    rec("2026-01-02T00:00:01Z", "session_meta", {"id": root_id, "session_id": root_id, "cwd": "D:\\dev\\root"}),
                    rec("2026-01-02T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "inherited-parent-turn"}),
                    rec("2026-01-02T00:00:01Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": usage(10, 5, 2)}}),
                    rec("2026-01-02T00:00:02Z", "event_msg", {"type": "thread_settings_applied"}),
                    rec("2026-01-02T00:00:03Z", "event_msg", {"type": "task_started", "turn_id": "child-turn"}),
                    rec("2026-01-02T00:00:05Z", "event_msg", {"type": "token_count", "info": token_info(usage(30, 10, 5), 240, 2_000)}),
                    rec("2026-01-02T00:00:06Z", "event_msg", {"type": "task_complete", "turn_id": "child-turn"}),
                ],
            )
            report = viz.build_range_report(window, roots=[sessions])

        self.assertEqual(report["mode"], "range")
        self.assertEqual(report["summary"]["sessionCount"], 1)
        self.assertEqual(report["summary"]["finalUsage"]["total"], 35)
        session = report["sessions"][0]
        self.assertEqual(session["metadata"]["threadId"], root_id)
        self.assertEqual(session["metadata"]["title"], "Root prompt")
        self.assertEqual(session["summary"]["finalUsage"]["total"], 35)
        self.assertEqual([turn["sourceKind"] for turn in session["turns"]], ["subagent", "main"])
        self.assertEqual(
            [turn["contextSnapshot"]["tokens"] for turn in session["turns"]],
            [240, 120],
        )
        self.assertEqual(
            [turn["contextSnapshot"]["windowTokens"] for turn in session["turns"]],
            [2_000, 1_000],
        )
        self.assertFalse(any(w["code"] == "nested_task_start" for w in report["warnings"]))

    def test_live_tail_is_nonfatal_for_range_report(self) -> None:
        window = viz.DateWindow.for_dates(date(2026, 1, 2), date(2026, 1, 2), local_tz=timezone.utc)
        path = self.write_rollout(
            [
                rec("2026-01-02T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
                rec("2026-01-02T00:00:01Z", "event_msg", {"type": "user_message", "message": "still running"}),
            ]
        )
        report = viz.parse_rollout(path, window=window, tolerate_live=True)
        warning = next(w for w in report["warnings"] if w["code"] == "unclosed_turn")
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(report["summary"]["integrityErrorCount"], 0)

    def test_subagent_settings_between_completed_turns_do_not_drop_history(self) -> None:
        child_id = "00000000-0000-0000-0000-000000000014"
        root_id = "00000000-0000-0000-0000-000000000015"
        window = viz.DateWindow.for_dates(date(2026, 1, 2), date(2026, 1, 2), local_tz=timezone.utc)
        lines = [
            rec("2026-01-02T00:00:00Z", "session_meta", {"id": child_id, "session_id": root_id, "parent_thread_id": root_id, "thread_source": "subagent"}),
            rec("2026-01-02T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec("2026-01-02T00:00:02Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": usage(10, 5, 2)}}),
            rec("2026-01-02T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
            rec("2026-01-02T00:00:04Z", "event_msg", {"type": "thread_settings_applied"}),
            rec("2026-01-02T00:00:05Z", "event_msg", {"type": "task_started", "turn_id": "t2"}),
            rec("2026-01-02T00:00:06Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": usage(20, 10, 3)}}),
            rec("2026-01-02T00:00:07Z", "event_msg", {"type": "task_complete", "turn_id": "t2"}),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_named_rollout(Path(temp), child_id, lines)
            report = viz.parse_rollout(path, window=window, tolerate_live=True)
        self.assertEqual(report["summary"]["turnCount"], 2)
        self.assertEqual(report["summary"]["finalUsage"]["total"], 23)
        self.assertFalse(report["metadata"]["subagentBaselineApplied"])

    def test_empty_range_report_is_valid(self) -> None:
        window = viz.DateWindow.for_dates(date(2026, 1, 2), date(2026, 1, 3), local_tz=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            report = viz.build_range_report(window, roots=[Path(temp)])
        self.assertEqual(report["summary"]["sessionCount"], 0)
        self.assertEqual(report["summary"]["finalUsage"]["total"], 0)
        self.assertEqual(report["sessions"], [])

    def test_date_range_cli_generates_range_report(self) -> None:
        root_id = "00000000-0000-0000-0000-000000000013"
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions"
            sessions.mkdir()
            self.write_named_rollout(
                sessions,
                root_id,
                [
                    rec("2026-01-02T00:00:00Z", "session_meta", {"id": root_id, "session_id": root_id}),
                    rec("2026-01-02T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
                    rec("2026-01-02T00:00:02Z", "event_msg", {"type": "token_count", "info": {"total_token_usage": usage(10, 5, 2)}}),
                    rec("2026-01-02T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
                ],
            )
            output = Path(temp) / "range.html"
            rc = viz.main(
                [
                    "--from",
                    "2026-01-02",
                    "--to",
                    "2026-01-02",
                    "--sessions-root",
                    str(sessions),
                    "--strict",
                    "--output",
                    str(output),
                ]
            )
            rendered = output.read_text(encoding="utf-8")
        self.assertEqual(rc, 0)
        self.assertIn('"mode":"range"', rendered)
        self.assertIn("会话列表", rendered)
        self.assertIn("总统计", rendered)

    def test_incomplete_date_range_is_rejected(self) -> None:
        self.assertEqual(viz.main(["--from", "2026-01-02"]), 2)

    def test_trailing_partial_line_warns_and_strict_cli_rejects(self) -> None:
        path = self.write_rollout(
            [
                rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
                '{"timestamp":"broken"',
            ],
            trailing_newline=False,
        )
        report = viz.parse_rollout(path)
        self.assertTrue(any(w["code"] == "trailing_partial_line" for w in report["warnings"]))
        output = path.with_suffix(".html")
        self.assertEqual(viz.main([str(path), "--strict", "--output", str(output)]), 3)
        self.assertFalse(output.exists())

    def test_exclude_messages_and_default_temp_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(viz.tempfile, "gettempdir", return_value=temp):
                rc = viz.main([str(FIXTURE), "--strict", "--exclude-messages"])
            self.assertEqual(rc, 0)
            output = (
                Path(temp)
                / "agenttools"
                / "codex-token-00000000-0000-0000-0000-000000000001.html"
            )
            html_text = output.read_text(encoding="utf-8")
            self.assertNotIn("Create a synthetic report.", html_text)
            self.assertIn('"messagesIncluded":false', html_text)
            self.assertIn('"containsFullUserMessages":false', html_text)

    def test_default_report_includes_messages(self) -> None:
        report = viz.parse_rollout(FIXTURE)
        viz.set_message_policy(report, include_messages=True)
        html_text = viz.render_html(report)
        self.assertIn("Create a synthetic report.", html_text)
        self.assertIn('"messagesIncluded":true', html_text)

    def test_single_token_progress_dual_ring_exists_in_both_report_modes(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn("Token 消耗与 Context 占用", template)
            self.assertIn("累计 Token 进度", template)
            self.assertIn("Context 占用率", template)
            self.assertIn("context-sector", template)
            self.assertNotIn('data-context-scale="rate"', template)
            self.assertNotIn("context-ring-card", template)
        self.assertIn('id="context-radial-chart"', viz.HTML_TEMPLATE)
        self.assertNotIn('id="context-chart"', viz.HTML_TEMPLATE)
        self.assertNotIn('id="context-ring-strip"', viz.HTML_TEMPLATE)
        self.assertIn('id="range-context-radial-chart"', viz.RANGE_HTML_TEMPLATE)
        self.assertNotIn('id="range-context-chart"', viz.RANGE_HTML_TEMPLATE)
        self.assertNotIn('id="range-context-ring-strip"', viz.RANGE_HTML_TEMPLATE)

    def test_report_views_use_tabs_and_compact_summary_brief(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn("summary-brief", template)
            self.assertIn("brief-grid", template)
            self.assertIn("analysis-tabs", template)
            self.assertIn('data-tab-target="context"', template)
            self.assertIn('data-tab-target="composition"', template)
            self.assertIn('data-tab-target="table"', template)
            self.assertIn("setTab", template)
            self.assertIn("analysis-controls", template)
        self.assertIn('data-tab-target="trend"', viz.RANGE_HTML_TEMPLATE)
        self.assertIn('data-tab-target="cumulative"', viz.HTML_TEMPLATE)
        self.assertIn("contextDisabled=isTotal()", viz.RANGE_HTML_TEMPLATE)
        self.assertIn("reset-filters", viz.RANGE_HTML_TEMPLATE)

    def test_range_report_distinguishes_total_summary_and_defaults_to_first_session(self) -> None:
        self.assertIn("session-total-button", viz.RANGE_HTML_TEMPLATE)
        self.assertIn("session-total-button.active", viz.RANGE_HTML_TEMPLATE)
        self.assertIn("inset 3px 0 0 #8c6f4d", viz.RANGE_HTML_TEMPLATE)
        self.assertIn('范围汇总 · ${sessions.length} 个会话', viz.RANGE_HTML_TEMPLATE)
        self.assertIn('view:sessions[0]?.metadata.threadId||"total"', viz.RANGE_HTML_TEMPLATE)
        self.assertIn('tab:sessions.length?"context":"composition"', viz.RANGE_HTML_TEMPLATE)

    def test_subagents_render_as_outer_satellites_in_both_ring_templates(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn("function radialEntries", template)
            self.assertIn("satellite-connector", template)
            self.assertIn("satellite-context", template)
            self.assertIn("satellite-compaction", template)
            self.assertIn("卫星层 · 子 agent Token", template)
            self.assertIn("if(satellite)", template)

    def test_subagent_rollout_metadata_exposes_source_kind(self) -> None:
        thread_id = "00000000-0000-0000-0000-000000000020"
        path = self.write_rollout(
            [
                rec(
                    "2026-01-01T00:00:00Z",
                    "session_meta",
                    {
                        "id": thread_id,
                        "session_id": thread_id,
                        "parent_thread_id": "00000000-0000-0000-0000-000000000019",
                        "thread_source": "subagent",
                    },
                ),
                rec(
                    "2026-01-01T00:00:01Z",
                    "event_msg",
                    {"type": "task_started", "turn_id": "child-turn"},
                ),
                rec(
                    "2026-01-01T00:00:02Z",
                    "event_msg",
                    {"type": "token_count", "info": token_info(usage(10, 5, 2), 120)},
                ),
                rec(
                    "2026-01-01T00:00:03Z",
                    "event_msg",
                    {"type": "task_complete", "turn_id": "child-turn"},
                ),
            ]
        )
        report = viz.parse_rollout(path)
        self.assertEqual(report["metadata"]["sourceKind"], "subagent")

    def test_dual_ring_turns_have_instant_detailed_tooltips(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn('class="tooltip" id="turn-tooltip"', template)
            self.assertIn("TOOLTIP_MESSAGE_LIMIT=800", template.replace(" ", ""))
            self.assertIn("初始用户消息", template)
            self.assertIn("showTurnTooltip", template)
            self.assertIn('group.addEventListener("pointerenter"', template)
            self.assertIn('group.addEventListener("pointermove"', template)
            self.assertIn('group.addEventListener("keydown"', template)
            self.assertIn("pointer-events:none", template.replace(" ", ""))

    def test_turn_tooltip_shows_context_delta_and_current_token_only(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn("previousTurnForTooltip", template)
            self.assertIn("previousSnapshot", template)
            self.assertIn("contextDelta", template)
            self.assertIn("tooltip-change", template)
            self.assertIn("增加了", template)
            self.assertIn("减少了", template)
            self.assertIn("无变化", template)
            self.assertNotIn("previousTokenPortion", template)
            self.assertNotIn("tooltip-transition", template)
            self.assertNotIn("上个 turn 结束：", template)
            self.assertIn("本轮 Token 占比", template)
            self.assertTrue(
                "contextPortion = snapshot.occupancyRate == null ? \"—\"" in template
                or "contextPortion=snapshot.occupancyRate==null?\"—\"" in template
            )

    def test_dual_ring_uses_a_one_degree_token_seam_and_context_capacity_line(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            compact_template = template.replace(" ", "")
            self.assertIn("contextGap=Math.PI/180", compact_template)
            self.assertIn("contextSpan=2*Math.PI-contextGap", compact_template)
            self.assertIn("context-capacity", template)
            self.assertIn("stroke-width:2.5", compact_template)
            self.assertIn("Context 100%", template)
            self.assertIn("Token 100%", template)
            self.assertIn("Token 0%", template)

    def test_context_occupancy_has_high_contrast_step_contours_and_current_marker(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            compact_template = template.replace(" ", "")
            self.assertIn("contextBandColor", template)
            self.assertIn("context-contour", template)
            self.assertIn("context-current-marker", template)
            self.assertIn("context-danger-zone", template)
            self.assertIn("context-warning", template)
            self.assertIn('class:"context-contour"', compact_template)
            self.assertIn('class:"context-current-marker"', compact_template)
            self.assertIn("opacity:knownBand?.22:.7", compact_template)

    def test_compaction_splits_context_timeline_and_uses_a_connected_marker(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            compact_template = template.replace(" ", "")
            self.assertIn("contextTimeline", template)
            self.assertIn("contextBands", template)
            self.assertIn('class:"compaction-position-line"', compact_template)
            self.assertIn('class:"compaction-jump-line"', compact_template)
            self.assertIn("x2:before.x", compact_template)
            self.assertNotIn("outerA=radialPoint(cx,cy,innerMax+5", compact_template)

    def test_turn_tooltip_has_independent_context_and_token_portion_cards(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn("tooltip-metrics", template)
            self.assertIn('tooltip-metric context', template)
            self.assertIn('tooltip-metric token', template)
            self.assertIn("Context 占用", template)
            self.assertIn("本轮 Token 占比", template)
            self.assertIn("conversationTotal>0", template.replace(" ", ""))
            self.assertIn("未记录 Context 快照", template)

    def test_turn_drawer_closes_on_non_turn_primary_clicks(self) -> None:
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            self.assertIn('data-turn-target="true"', template)
            self.assertIn('document.addEventListener("click"', template)
            self.assertIn('closest("[data-turn-target=true]")', template)
            self.assertIn("event.button!==0", template.replace(" ", ""))

    def test_range_session_list_is_a_responsive_nonpersistent_drawer(self) -> None:
        template = viz.RANGE_HTML_TEMPLATE
        for element_id in (
            "report-shell",
            "session-drawer",
            "session-drawer-toggle",
            "session-drawer-close",
            "session-drawer-backdrop",
        ):
            self.assertIn(f'id="{element_id}"', template)
        self.assertIn("setSessionNav", template)
        self.assertIn("session-nav-closed", template)
        self.assertIn("session-nav-modal-open", template)
        self.assertNotIn("localStorage", template)

    def test_range_message_exclusion_also_removes_prompt_derived_titles(self) -> None:
        root_id = "00000000-0000-0000-0000-000000000012"
        window = viz.DateWindow.for_dates(date(2026, 1, 2), date(2026, 1, 2), local_tz=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp)
            self.write_named_rollout(
                sessions,
                root_id,
                [
                    rec("2026-01-02T00:00:00Z", "session_meta", {"id": root_id, "session_id": root_id, "cwd": "D:\\dev\\private"}),
                    rec("2026-01-02T00:00:01Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
                    rec("2026-01-02T00:00:02Z", "event_msg", {"type": "user_message", "message": "secret title"}),
                    rec("2026-01-02T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
                ],
            )
            report = viz.build_range_report(window, roots=[sessions])
        viz.set_message_policy(report, include_messages=False)
        rendered = viz.render_html(report)
        self.assertNotIn("secret title", rendered)
        self.assertIn('"messagesIncluded":false', rendered)

    def test_console_encoding_is_reconfigured_for_chinese_output(self) -> None:
        stdout_bytes = io.BytesIO()
        stderr_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
        stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
        with patch.object(viz.sys, "stdout", stdout), patch.object(viz.sys, "stderr", stderr):
            viz.configure_console_encoding()
            self.assertEqual(viz.sys.stdout.encoding.lower(), "utf-8")
            self.assertEqual(viz.sys.stderr.encoding.lower(), "utf-8")
            viz.sys.stdout.write("线程")
            viz.sys.stdout.flush()
        self.assertEqual(stdout_bytes.getvalue().decode("utf-8"), "线程")
        stdout.detach()
        stderr.detach()

    def test_report_json_cannot_break_out_of_script_tag(self) -> None:
        lines = [
            rec("2026-01-01T00:00:00Z", "event_msg", {"type": "task_started", "turn_id": "t1"}),
            rec(
                "2026-01-01T00:00:01Z",
                "event_msg",
                {"type": "user_message", "message": '</script><script>alert("x")</script>'},
            ),
            rec(
                "2026-01-01T00:00:02Z",
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": usage(10, 5, 2)}},
            ),
            rec("2026-01-01T00:00:03Z", "event_msg", {"type": "task_complete", "turn_id": "t1"}),
        ]
        html_text = viz.render_html(viz.parse_rollout(self.write_rollout(lines)))
        self.assertNotIn('</script><script>alert("x")</script>', html_text)
        self.assertIn("\\u003c/script\\u003e", html_text)

    def test_embedded_javascript_parses_with_node_when_available(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is unavailable")
        for template in (viz.HTML_TEMPLATE, viz.RANGE_HTML_TEMPLATE):
            start = template.index("<script>\n") + len("<script>\n")
            end = template.rindex("\n</script>")
            checked = subprocess.run(
                [node, "--check", "-"],
                input=template[start:end],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_repository_skill_and_plugin_structure(self) -> None:
        plugin = json.loads((REPO / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "agenttools")
        self.assertEqual(plugin["skills"], "./skills/")
        self.assertEqual(plugin["license"], "MIT")

        skill = (REPO / "skills" / "visualize-codex-tokens" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: visualize-codex-tokens", skill)
        self.assertNotIn("TODO", skill)

        metadata = (
            REPO / "skills" / "visualize-codex-tokens" / "agents" / "openai.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", metadata)


if __name__ == "__main__":
    unittest.main(verbosity=2)
