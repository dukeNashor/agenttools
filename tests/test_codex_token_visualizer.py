from __future__ import annotations

import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
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


class VisualizerTests(unittest.TestCase):
    def write_rollout(self, lines: list[str], trailing_newline: bool = True) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "rollout-00000000-0000-0000-0000-000000000002.jsonl"
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
        start = viz.HTML_TEMPLATE.index("<script>\n") + len("<script>\n")
        end = viz.HTML_TEMPLATE.rindex("\n</script>")
        checked = subprocess.run(
            [node, "--check", "-"],
            input=viz.HTML_TEMPLATE[start:end],
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
