from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).parents[1]
SCRIPT_DIR = REPO / "skills" / "visualize-codex-tokens" / "scripts"
if "codex_token_visualizer" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "codex_token_visualizer", SCRIPT_DIR / "codex_token_visualizer.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
sys.path.insert(0, str(SCRIPT_DIR))
import live_server  # noqa: E402


THREAD_ID = "00000000-0000-0000-0000-000000000101"


def record(timestamp: str, payload_type: str, payload: dict) -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": "event_msg", "payload": {"type": payload_type, **payload}},
        ensure_ascii=False,
    )


def task_lines(project: Path, total: int, turn_id: str = "turn-1") -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "timestamp": "2026-08-24T00:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": THREAD_ID, "session_id": THREAD_ID, "cwd": str(project)},
                },
                ensure_ascii=False,
            ),
            record("2026-08-24T00:00:01Z", "task_started", {"turn_id": turn_id}),
            record("2026-08-24T00:00:02Z", "user_message", {"message": "private prompt"}),
            record(
                "2026-08-24T00:00:03Z",
                "token_count",
                {"info": {"total_token_usage": {"input_tokens": total - 2, "output_tokens": 2, "total_tokens": total}}},
            ),
            record("2026-08-24T00:00:04Z", "task_complete", {"turn_id": turn_id}),
            "",
        ]
    )


class LiveServerTests(unittest.TestCase):
    def test_collector_reparses_only_changed_rollout_and_keeps_project_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            sessions = root / "sessions"
            project.mkdir()
            sessions.mkdir()
            rollout = sessions / f"rollout-2026-08-24T00-00-00-{THREAD_ID}.jsonl"
            rollout.write_text(task_lines(project, 12), encoding="utf-8")

            collector = live_server.LiveProjectCollector(
                projects=[str(project)],
                roots=[sessions],
                include_messages=False,
            )
            first = collector.poll()
            self.assertTrue(first.changed)
            self.assertEqual(first.selected_count, 1)
            self.assertEqual(first.report["summary"]["finalUsage"]["total"], 12)
            self.assertEqual(first.report["sessions"][0]["turns"][0]["messages"], [])

            rollout.write_text(task_lines(project, 24, turn_id="turn-2"), encoding="utf-8")
            second = collector.poll()
            self.assertTrue(second.changed)
            self.assertEqual(second.report["summary"]["finalUsage"]["total"], 24)
            self.assertEqual(second.report["metadata"]["scope"]["type"], "projects")

            third = collector.poll()
            self.assertFalse(third.changed)
            self.assertEqual(third.report["summary"]["finalUsage"]["total"], 24)

    def test_live_page_exposes_project_scope_and_refresh_hook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            sessions = root / "sessions"
            project.mkdir()
            sessions.mkdir()
            rollout = sessions / f"rollout-2026-08-24T00-00-00-{THREAD_ID}.jsonl"
            rollout.write_text(task_lines(project, 12), encoding="utf-8")
            service = live_server.LiveService(
                live_server.LiveProjectCollector(projects=[str(project)], roots=[sessions])
            )
            service.state.publish(service.collector.poll())
            page = service.render_page()
            self.assertIn("项目总览", page)
            self.assertIn("/api/stream", page)
            self.assertIn("window.__codexApplyReport", page)
            self.assertLess(page.index("<script>", page.index("实时服务连接中")), page.index("</body>"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
