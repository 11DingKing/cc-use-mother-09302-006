"""HTTP JSON 接口（仅标准库）。

- POST /commands：所有变更统一入口，body 为
  {"event_id": "...", "type": "register", "payload": {...}}，
  由 X-Actor-Role / X-Actor-Id 头标识操作者，按角色做权限校验；
- GET /sessions/{id}/queue：按角色返回裁剪后的队列视图；
- GET /health：存活检查。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import views
from .errors import NotFound
from .service import COMMANDS

ROLE_PERMISSIONS = {
    "admin": {"*"},
    "teacher": {
        "create_student",
        "create_session",
        "grant_consent",
        "withdraw_consent",
        "submit_care_grant",
        "approve_care_grant",
        "revoke_care_grant",
        "register",
        "withdraw_entry",
        "transfer",
        "set_capacity",
        "check_in",
        "complete_session",
        "record_notification_result",
    },
    "volunteer": {"check_in"},
    "student": {"register", "withdraw_entry"},
    "guardian": {"grant_consent", "withdraw_consent", "submit_care_grant"},
}

ERROR_HTTP = {"not_found": 404, "validation_error": 400, "unknown_command": 400}


def _permitted(role: str, kind: str) -> bool:
    allowed = ROLE_PERMISSIONS.get(role, set())
    return "*" in allowed or kind in allowed


def make_handler(service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RotationQueue/1.0"

        # -- 工具 ------------------------------------------------------
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _actor(self) -> tuple[str, str]:
            return (
                self.headers.get("X-Actor-Role", ""),
                self.headers.get("X-Actor-Id", ""),
            )

        @staticmethod
        def _error(code: str, message: str) -> dict:
            return {"ok": False, "error": {"code": code, "message": message}}

        def log_message(self, *args) -> None:  # 静默访问日志
            pass

        # -- 变更入口 --------------------------------------------------
        def do_POST(self) -> None:
            if urlparse(self.path).path != "/commands":
                self._send(404, self._error("not_found", "未知路径"))
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(400, self._error("bad_json", "请求体不是合法 JSON"))
                return
            role, actor = self._actor()
            kind = body.get("type", "")
            payload = body.get("payload") or {}
            if kind not in COMMANDS:
                self._send(400, self._error("unknown_command", f"未知命令：{kind}"))
                return
            if not _permitted(role, kind):
                self._send(403, self._error("forbidden", f"角色 {role or '匿名'} 无权执行 {kind}"))
                return
            if role == "student" and payload.get("student_id") not in (None, actor):
                self._send(403, self._error("forbidden", "学生只能操作本人记录"))
                return
            try:
                result = service.execute(
                    kind, payload, event_id=body.get("event_id"), actor=actor or role
                )
            except Exception as exc:  # 未预期异常：不落事件日志，可安全重试
                self._send(500, self._error("internal", str(exc)))
                return
            if result.get("ok"):
                self._send(200, result)
                return
            code = result.get("error", {}).get("code", "")
            self._send(ERROR_HTTP.get(code, 409), result)

        # -- 查询入口 --------------------------------------------------
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"ok": True})
                return
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "queue":
                self._queue_view(parts[1], parse_qs(parsed.query))
                return
            self._send(404, self._error("not_found", "未知路径"))

        def _queue_view(self, session_id: str, query: dict) -> None:
            role, actor = self._actor()
            try:
                snapshot = service.queue_snapshot(session_id)
            except NotFound as exc:
                self._send(404, self._error(exc.code, exc.message))
                return
            if role in ("teacher", "admin"):
                self._send(200, views.teacher_view(snapshot))
                return
            if role == "volunteer":
                self._send(200, views.volunteer_view(snapshot))
                return
            if role == "student":
                student_id = actor or (query.get("student_id") or [None])[0]
                if not student_id:
                    self._send(400, self._error("validation_error", "缺少学生标识"))
                    return
                self._send(200, views.student_view(snapshot, student_id))
                return
            self._send(403, self._error("forbidden", "缺少有效角色"))

    return Handler


def serve(service, host: str = "127.0.0.1", port: int = 8080) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(service))
    try:
        server.serve_forever()
    finally:
        server.server_close()
