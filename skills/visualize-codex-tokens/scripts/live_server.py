"""Local live web server for Codex token reports.

The first live mode deliberately reuses the existing report parser and range
report renderer.  It polls the selected rollout roots, reparses only files
whose size or mtime changed, and publishes the resulting project snapshot over
SSE.  State is intentionally in memory; a restart rebuilds it from disk.
"""

from __future__ import annotations

import argparse
import json
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

import codex_token_visualizer as viz


DEFAULT_POLL_INTERVAL = 3.0
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))


@dataclass(frozen=True)
class LivePollResult:
    report: dict[str, Any]
    changed: bool
    candidate_count: int
    direct_match_count: int
    selected_count: int
    parse_error_count: int


class LiveProjectCollector:
    """Maintain a project-scoped collection of parsed rollout reports."""

    def __init__(
        self,
        projects: Iterable[str] = (),
        roots: Iterable[Path] | None = None,
        *,
        current_project: bool = False,
        include_messages: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        targets = [viz._resolve_project_target(value, require_git=False) for value in projects]
        if current_project:
            targets.insert(0, viz._resolve_project_target(".", require_git=True))
        if not targets:
            raise ValueError("实时服务至少需要一个项目目录或 --current-project。")

        deduped: list[viz.ProjectTarget] = []
        seen: set[str] = set()
        for target in targets:
            key = "\n".join(
                [
                    str(target.match_root).casefold(),
                    str(target.git_common_dir or "").casefold(),
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(target)

        self.targets = deduped
        self.roots = [path.expanduser().resolve() for path in roots] if roots else viz.default_session_roots()
        self.include_messages = include_messages
        self.poll_interval = poll_interval
        self._reports_by_path: dict[Path, dict[str, Any]] = {}
        self._signatures: tuple[tuple[str, tuple[int, int]], ...] | None = None
        self._parse_errors: dict[str, str] = {}
        self._report: dict[str, Any] | None = None

    def _select_reports(
        self, candidates: list[Path]
    ) -> tuple[list[dict[str, Any]], dict[str, viz.ProjectTarget], int]:
        parsed_reports = list(self._reports_by_path.values())
        git_common_cache: dict[str, Path | None] = {}
        direct_matches: dict[str, viz.ProjectTarget] = {}
        for report in parsed_reports:
            report_id = viz._coerce_text(report.get("metadata", {}).get("threadId")).lower()
            if not report_id:
                continue
            match = viz._project_match_for_report(report, self.targets, git_common_cache)
            if match is not None:
                direct_matches[report_id] = match

        included_ids = set(direct_matches)
        changed = True
        while changed:
            changed = False
            for report in parsed_reports:
                report_id = viz._coerce_text(report.get("metadata", {}).get("threadId")).lower()
                if not report_id or report_id in included_ids:
                    continue
                if viz._parent_rollout_id(report) in included_ids:
                    included_ids.add(report_id)
                    changed = True

        selected = [
            report
            for report in parsed_reports
            if viz._coerce_text(report.get("metadata", {}).get("threadId")).lower() in included_ids
        ]
        return selected, direct_matches, len(candidates)

    def _build_report(self, candidates: list[Path]) -> LivePollResult:
        selected, direct_matches, candidate_count = self._select_reports(candidates)
        direct_match_count = len(direct_matches)
        project_payload = [viz._project_target_payload(target) for target in self.targets]
        project_label = "、".join(payload["label"] for payload in project_payload)
        selected_ids = [
            viz._coerce_text(report.get("metadata", {}).get("threadId")).lower()
            for report in selected
            if viz._coerce_text(report.get("metadata", {}).get("threadId"))
        ]
        scope = {
            "type": "projects",
            "label": project_label,
            "projects": project_payload,
            "matchRule": (
                "session_meta.cwd 位于项目根目录内，或与项目 Git common directory 相同；"
                "再沿明确的 parent_thread_id/forked_from_id 纳入子 rollout。"
            ),
            "candidateRolloutCount": candidate_count,
            "directMatchRolloutCount": direct_match_count,
            "selectedRolloutCount": len(selected),
            "selectedRolloutIds": selected_ids,
        }
        report = viz._build_multi_session_report(selected, roots=self.roots, scope=scope)
        viz.set_message_policy(report, include_messages=self.include_messages)
        live_errors = dict(self._parse_errors)
        report.setdefault("metadata", {})["live"] = {
            "enabled": True,
            "pollIntervalMs": int(self.poll_interval * 1000),
            "lastPollAt": _now(),
            "status": "degraded" if live_errors else "ok",
            "parseErrors": live_errors,
            "candidateRolloutCount": candidate_count,
            "directMatchRolloutCount": direct_match_count,
            "selectedRolloutCount": len(selected),
        }
        report["metadata"]["projectDiscovery"] = {
            "candidateRolloutCount": candidate_count,
            "directMatchRolloutCount": direct_match_count,
            "selectedRolloutCount": len(selected),
            "excludedRolloutCount": max(0, candidate_count - len(selected)),
            "directMatchRolloutIds": sorted(direct_matches),
            "selectedRolloutIds": selected_ids,
        }
        return LivePollResult(
            report=report,
            changed=True,
            candidate_count=candidate_count,
            direct_match_count=direct_match_count,
            selected_count=len(selected),
            parse_error_count=len(live_errors),
        )

    def poll(self) -> LivePollResult:
        candidates = viz.discover_rollouts(self.roots)
        current_paths = set(candidates)
        signatures: list[tuple[str, tuple[int, int]]] = []
        changed_paths: list[Path] = []
        for path in candidates:
            signature = _file_signature(path)
            if signature is None:
                continue
            signatures.append((str(path), signature))
            if self._signatures is None or dict(self._signatures).get(str(path)) != signature:
                changed_paths.append(path)
        next_signature = tuple(sorted(signatures))
        if self._report is not None and next_signature == self._signatures:
            live = self._report.get("metadata", {}).get("live", {})
            live["lastPollAt"] = _now()
            self._report["metadata"]["live"] = live
            return LivePollResult(
                report=self._report,
                changed=False,
                candidate_count=len(candidates),
                direct_match_count=int(live.get("directMatchRolloutCount", 0)),
                selected_count=int(live.get("selectedRolloutCount", 0)),
                parse_error_count=len(self._parse_errors),
            )

        self._reports_by_path = {
            path: report for path, report in self._reports_by_path.items() if path in current_paths
        }
        self._parse_errors = {
            str(path): message
            for path, message in self._parse_errors.items()
            if Path(path) in current_paths
        }
        for path in changed_paths:
            try:
                self._reports_by_path[path] = viz.parse_rollout(path, tolerate_live=True)
                self._parse_errors.pop(str(path), None)
            except (OSError, ValueError, RuntimeError) as exc:
                self._parse_errors[str(path)] = str(exc)

        self._signatures = next_signature
        result = self._build_report(candidates)
        self._report = result.report
        return result


class LiveState:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._report: dict[str, Any] | None = None
        self._payload = "{}"
        self._version = 0

    def publish(self, result: LivePollResult) -> None:
        with self._condition:
            self._report = result.report
            if result.changed:
                self._version += 1
                self._payload = json.dumps(
                    {"version": self._version, "report": result.report},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            self._condition.notify_all()

    def snapshot(self) -> tuple[int, dict[str, Any]]:
        with self._condition:
            return self._version, self._report or {}

    def wait_for_update(self, after_version: int, timeout: float) -> tuple[int, str] | None:
        with self._condition:
            self._condition.wait_for(lambda: self._version > after_version, timeout=timeout)
            if self._version <= after_version:
                return None
            return self._version, self._payload


class LiveService:
    def __init__(self, collector: LiveProjectCollector, title: str | None = None) -> None:
        self.collector = collector
        self.title = title
        self.state = LiveState()
        self.stop_event = threading.Event()
        self.poll_thread = threading.Thread(target=self._poll_loop, name="codex-live-poller", daemon=True)

    def start(self) -> None:
        self.state.publish(self.collector.poll())
        self.poll_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.state._condition:
            self.state._condition.notify_all()
        if self.poll_thread.is_alive():
            self.poll_thread.join(timeout=max(1.0, self.collector.poll_interval + 1.0))

    def _poll_loop(self) -> None:
        while not self.stop_event.wait(self.collector.poll_interval):
            try:
                self.state.publish(self.collector.poll())
            except Exception as exc:  # keep the local service alive on a bad rollout
                version, report = self.state.snapshot()
                if report:
                    live = report.setdefault("metadata", {}).setdefault("live", {})
                    live["status"] = "degraded"
                    live["lastError"] = str(exc)
                    self.state.publish(
                        LivePollResult(
                            report=report,
                            changed=True,
                            candidate_count=0,
                            direct_match_count=0,
                            selected_count=0,
                            parse_error_count=1,
                        )
                    )

    def render_page(self) -> str:
        _, report = self.state.snapshot()
        return viz.render_html(report, self.title).replace("</body>", LIVE_CLIENT_SCRIPT + "\n</body>")


LIVE_CLIENT_SCRIPT = r"""
<style>
#codex-live-status{position:fixed;z-index:150;right:14px;top:14px;padding:7px 10px;border:1px solid #c7ddd3;border-radius:999px;background:rgba(232,243,235,.94);color:#3f765f;font:700 11px/1.2 system-ui,sans-serif;box-shadow:0 6px 18px rgba(45,41,36,.12);backdrop-filter:blur(10px)}
#codex-live-status.degraded{border-color:#e2c59e;background:rgba(251,241,220,.96);color:#9a6721}
#codex-live-status.offline{border-color:#e8c4c2;background:rgba(250,233,232,.96);color:#984b55}
</style>
<div id="codex-live-status" role="status" aria-live="polite">实时服务连接中…</div>
<script>
(() => {
  const badge = document.getElementById("codex-live-status");
  let version = 0;
  function setStatus(text, kind="ok") {
    badge.textContent = text;
    badge.className = kind === "ok" ? "" : kind;
  }
  function apply(message) {
    version = Number(message.version || version);
    const report = message.report || {};
    const live = report.metadata && report.metadata.live || {};
    if (typeof window.__codexApplyReport === "function") {
      window.__codexApplyReport(report);
      setStatus(live.status === "degraded" ? "实时 · 数据有提醒" : `实时 · ${new Date().toLocaleTimeString("zh-CN")}`, live.status === "degraded" ? "degraded" : "ok");
    } else {
      setStatus("实时 · 刷新页面", "ok");
      window.location.reload();
    }
  }
  function connect() {
    const source = new EventSource(`/api/stream?since=${encodeURIComponent(version)}`);
    source.onopen = () => setStatus("实时 · 已连接", "ok");
    source.onmessage = event => {
      try { apply(JSON.parse(event.data)); } catch (error) { setStatus("实时 · 数据异常", "degraded"); }
    };
    source.onerror = () => { source.close(); setStatus("实时 · 连接中断", "offline"); setTimeout(connect, 3000); };
  }
  connect();
})();
</script>
"""


class LiveRequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexLive/1.0"

    @property
    def live_server(self) -> "LiveHTTPServer":
        return self.server  # type: ignore[return-value]

    def _send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send_bytes(self.live_server.service.render_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/health":
            version, report = self.live_server.service.state.snapshot()
            live = report.get("metadata", {}).get("live", {})
            body = json.dumps(
                {"ok": bool(report), "version": version, "status": live.get("status", "starting"), "lastPollAt": live.get("lastPollAt")},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/snapshot":
            version, report = self.live_server.service.state.snapshot()
            body = json.dumps({"version": version, "report": report}, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")
            return
        if parsed.path == "/api/stream":
            self._stream(parse_qs(parsed.query))
            return
        self._send_bytes(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def _stream(self, query: dict[str, list[str]]) -> None:
        try:
            after = int((query.get("since") or ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while not self.live_server.service.stop_event.is_set():
                update = self.live_server.service.state.wait_for_update(after, timeout=15.0)
                if update is None:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                version, payload = update
                self.wfile.write(f"id: {version}\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                after = version
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def log_message(self, format: str, *args: Any) -> None:
        return


class LiveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: LiveService) -> None:
        super().__init__(address, LiveRequestHandler)
        self.service = service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动本机 Codex Token 实时报告服务。")
    parser.add_argument("--project", action="append", metavar="PATH", help="按项目目录监控会话；可重复指定。")
    parser.add_argument("--current-project", "--current-repo", action="store_true", help="监控当前 Git 项目。")
    parser.add_argument("--sessions-root", action="append", type=Path, help="会话根目录；可重复指定。")
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL, help="轮询秒数，默认 3。")
    parser.add_argument("--host", default=DEFAULT_HOST, help="绑定地址，默认仅本机访问。")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8765。")
    parser.add_argument("--include-messages", action="store_true", help="在实时报告中嵌入完整用户消息。")
    parser.add_argument("--title", help="报告标题。")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="启动后打开浏览器。")
    parser.add_argument("--no-open", dest="open_browser", action="store_false", help="不自动打开浏览器。")
    parser.set_defaults(open_browser=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_interval <= 0:
        print("错误：--poll-interval 必须大于 0。")
        return 2
    if not 1 <= args.port <= 65535:
        print("错误：--port 必须在 1 到 65535 之间。")
        return 2
    current_project = bool(args.current_project)
    if not args.project and not current_project:
        current_project = True
    try:
        collector = LiveProjectCollector(
            args.project or [],
            args.sessions_root,
            current_project=current_project,
            include_messages=args.include_messages,
            poll_interval=args.poll_interval,
        )
        service = LiveService(collector, title=args.title)
        server = LiveHTTPServer((args.host, args.port), service)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}")
        return 2

    service.start()
    url = f"http://{args.host}:{args.port}/"
    print(f"实时报告：{url}")
    print(f"项目：{', '.join(str(target.match_root) for target in collector.targets)}")
    print(f"轮询：{args.poll_interval:g} 秒")
    print("按 Ctrl+C 停止服务。")
    if args.open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在停止实时服务……")
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
