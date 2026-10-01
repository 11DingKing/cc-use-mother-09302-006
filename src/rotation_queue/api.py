"""HTTP API（标准库实现，无第三方依赖）。

鉴权约定（生产环境应替换为真实身份提供方，此处用请求头表达角色声明）：
- ``X-Role: teacher``   班主任：全部管理操作 + 排序理由视图
- ``X-Role: volunteer`` 活动志愿者：签到名单、签到
- ``X-Role: student``   学生：本人报名/取消/查看；配合 ``X-Student-Id``
幂等：写请求可携带 ``Idempotency-Key``，同键重放返回首次结果且不产生新事件。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .errors import DomainError
from .service import QueueService

ROLE_TEACHER = "teacher"
ROLE_VOLUNTEER = "volunteer"
ROLE_STUDENT = "student"


class ApiContext:
    def __init__(self, role: str, student_id: str | None) -> None:
        self.role = role
        self.student_id = student_id

    def require(self, *roles: str) -> None:
        if self.role not in roles:
            raise DomainError("forbidden", f"角色 {self.role} 无权执行该操作", 403)


# 命令所需角色
WRITE_ROLES: dict[str, str | tuple[str, ...]] = {
    "register_class": ROLE_TEACHER,
    "register_student": ROLE_TEACHER,
    "schedule_session": ROLE_TEACHER,
    "approve_accommodation": ROLE_TEACHER,
    "grant_consent": ROLE_TEACHER,
    "withdraw_consent": ROLE_TEACHER,
    "publish_roster": ROLE_TEACHER,
    "expand_capacity": ROLE_TEACHER,
    "close_session": ROLE_TEACHER,
    "offer_transfer": (ROLE_TEACHER, ROLE_STUDENT),
    "accept_transfer": ROLE_STUDENT,
    "decline_transfer": ROLE_STUDENT,
    "record_notifications": ROLE_TEACHER,
    "sign_up": (ROLE_TEACHER, ROLE_STUDENT),
    "cancel_signup": (ROLE_TEACHER, ROLE_STUDENT),
    "mark_attendance": (ROLE_TEACHER, ROLE_VOLUNTEER),
}


class QueueHttpHandler(BaseHTTPRequestHandler):
    service: QueueService  # 由工厂函数注入到类

    server_version = "RotationQueue/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默，测试干净
        return

    # ------------------------------------------------------------ 工具

    def _ctx(self) -> ApiContext:
        role = self.headers.get("X-Role", "")
        if role not in (ROLE_TEACHER, ROLE_VOLUNTEER, ROLE_STUDENT):
            raise DomainError("unauthorized", "缺少或非法的 X-Role 声明", 401)
        student_id = self.headers.get("X-Student-Id")
        return ApiContext(role, student_id)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("bad_json", f"请求体不是合法 JSON：{exc}", 400)
        if not isinstance(data, dict):
            raise DomainError("bad_json", "请求体必须是 JSON 对象", 400)
        return data

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard_student_self(self, ctx: ApiContext, payload: dict) -> None:
        if ctx.role == ROLE_STUDENT:
            if not ctx.student_id:
                raise DomainError("unauthorized", "学生角色需提供 X-Student-Id", 401)
            if payload.get("student_id") not in (None, ctx.student_id):
                raise DomainError("forbidden", "学生只能操作本人数据", 403)
            payload["student_id"] = ctx.student_id

    # ------------------------------------------------------------ 路由

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            ctx = self._ctx()
            matched = self._match(method, self.path)
            if matched is None:
                raise DomainError("not_found", f"无此路由：{method} {self.path}", 404)
            name, kwargs = matched
            payload = self._read_json() if method in ("POST", "DELETE") else {}
            getattr(self, name)(ctx, payload, kwargs)
        except DomainError as exc:
            self._send(exc.http_status, {"error": {"code": exc.code, "message": exc.message}})
        except (KeyError, ValueError) as exc:
            self._send(400, {"error": {"code": "bad_request",
                                       "message": f"请求缺少或含非法字段：{exc}"}})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def _match(self, method: str, path: str) -> tuple[str, dict] | None:
        rules = self._ROUTES.get(method, [])
        for pattern, name in rules:
            m = pattern.fullmatch(path)
            if m:
                return name, m.groupdict()
        return None

    # ------------------------------------------------------------ 处理

    def _require_role(self, ctx: ApiContext, command: str) -> None:
        allowed = WRITE_ROLES[command]
        ctx.require(*((allowed,) if isinstance(allowed, str) else allowed))

    def _run_command(self, ctx: ApiContext, command: str, payload: dict, svc: str) -> None:
        self._require_role(ctx, command)
        idem = self.headers.get("Idempotency-Key")
        result = getattr(self.service, svc)(payload, idem)
        self._send(200, result)

    # ---- 基础数据 ----

    def h_register_class(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(201, self.service.register_class(payload, self.headers.get("Idempotency-Key")))

    def h_register_student(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(201, self.service.register_student(payload, self.headers.get("Idempotency-Key")))

    def h_schedule_session(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(201, self.service.schedule_session(payload, self.headers.get("Idempotency-Key")))

    # ---- 合规 ----

    def h_approve_accommodation(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(201, self.service.approve_accommodation(payload, self.headers.get("Idempotency-Key")))

    def h_grant_consent(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(200, self.service.grant_consent(payload, self.headers.get("Idempotency-Key")))

    def h_withdraw_consent(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(200, self.service.withdraw_consent(payload, self.headers.get("Idempotency-Key")))

    # ---- 场次动作 ----

    def h_preview(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        self._send(200, self.service.preview_roster(kwargs["session_id"]))

    def h_publish(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        body = {**payload, "session_id": kwargs["session_id"]}
        self._send(200, self.service.publish_roster(body, self.headers.get("Idempotency-Key")))

    def h_expand(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        body = {**payload, "session_id": kwargs["session_id"]}
        self._send(200, self.service.expand_capacity(body, self.headers.get("Idempotency-Key")))

    def h_close(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        body = {**payload, "session_id": kwargs["session_id"]}
        self._send(200, self.service.close_session(body, self.headers.get("Idempotency-Key")))

    def h_signup(self, ctx, payload, kwargs):
        body = {**payload, "session_id": kwargs["session_id"]}
        self._guard_student_self(ctx, body)
        self._run_command(ctx, "sign_up", body, "sign_up")

    def h_cancel(self, ctx, payload, kwargs):
        body = {**payload, "session_id": kwargs["session_id"], "student_id": kwargs["student_id"]}
        self._guard_student_self(ctx, body)
        self._run_command(ctx, "cancel_signup", body, "cancel_signup")

    def h_attendance(self, ctx, payload, kwargs):
        body = {**payload, "session_id": kwargs["session_id"]}
        if ctx.role == ROLE_STUDENT:
            raise DomainError("forbidden", "学生不能签到", 403)
        self._run_command(ctx, "mark_attendance", body, "mark_attendance")

    def h_notifications(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER)
        body = {**payload, "session_id": kwargs["session_id"]}
        self._send(200, self.service.record_notifications(body, self.headers.get("Idempotency-Key")))

    # ---- 转移 ----

    def h_offer_transfer(self, ctx, payload, kwargs):
        self._guard_student_self(ctx, payload)
        self._run_command(ctx, "offer_transfer", payload, "offer_transfer")

    def h_accept_transfer(self, ctx, payload, kwargs):
        ctx.require(ROLE_STUDENT)
        if not ctx.student_id:
            raise DomainError("unauthorized", "学生角色需提供 X-Student-Id", 401)
        payload["student_id"] = ctx.student_id
        self._send(200, self.service.accept_transfer(payload, self.headers.get("Idempotency-Key")))

    def h_decline_transfer(self, ctx, payload, kwargs):
        ctx.require(ROLE_STUDENT)
        if not ctx.student_id:
            raise DomainError("unauthorized", "学生角色需提供 X-Student-Id", 401)
        payload["student_id"] = ctx.student_id
        self._send(200, self.service.decline_transfer(payload, self.headers.get("Idempotency-Key")))

    # ---- 读取（按角色返回不同裁剪视图） ----

    def h_session_view(self, ctx, payload, kwargs):
        session_id = kwargs["session_id"]
        if ctx.role == ROLE_TEACHER:
            self._send(200, self.service.teacher_view(session_id))
        elif ctx.role == ROLE_VOLUNTEER:
            self._send(200, self.service.volunteer_view(session_id))
        else:
            if not ctx.student_id:
                raise DomainError("unauthorized", "学生角色需提供 X-Student-Id", 401)
            self._send(200, self.service.student_view(session_id, ctx.student_id))

    def h_overview(self, ctx, payload, kwargs):
        ctx.require(ROLE_TEACHER, ROLE_VOLUNTEER)
        self._send(200, self.service.overview(kwargs["session_id"]))

    # 路由表（方法 -> [(正则, 处理方法名)]）
    def _routes_build(self) -> dict:
        sid = r"/api/sessions/(?P<session_id>[A-Za-z0-9_\-:.]+)"
        return {
            "GET": [
                (re.compile(sid + r"/?$"), "h_session_view"),
                (re.compile(sid + r"/preview$"), "h_preview"),
                (re.compile(sid + r"/overview$"), "h_overview"),
            ],
            "POST": [
                (re.compile(r"/api/classes/?$"), "h_register_class"),
                (re.compile(r"/api/students/?$"), "h_register_student"),
                (re.compile(r"/api/sessions/?$"), "h_schedule_session"),
                (re.compile(r"/api/accommodations/?$"), "h_approve_accommodation"),
                (re.compile(r"/api/consents/grant/?$"), "h_grant_consent"),
                (re.compile(r"/api/consents/withdraw/?$"), "h_withdraw_consent"),
                (re.compile(sid + r"/roster/publish$"), "h_publish"),
                (re.compile(sid + r"/capacity$"), "h_expand"),
                (re.compile(sid + r"/close$"), "h_close"),
                (re.compile(sid + r"/signups/?$"), "h_signup"),
                (re.compile(sid + r"/attendance$"), "h_attendance"),
                (re.compile(sid + r"/notifications$"), "h_notifications"),
                (re.compile(r"/api/transfers/offers/?$"), "h_offer_transfer"),
                (re.compile(r"/api/transfers/accept/?$"), "h_accept_transfer"),
                (re.compile(r"/api/transfers/decline/?$"), "h_decline_transfer"),
            ],
            "DELETE": [
                (re.compile(sid + r"/signups/(?P<student_id>[A-Za-z0-9_\-:.]+)$"), "h_cancel"),
            ],
        }


def build_server(service: QueueService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """构造绑定了具体服务实例的 HTTP 服务器。"""

    class _BoundHandler(QueueHttpHandler):
        pass

    _BoundHandler.service = service
    _BoundHandler._ROUTES = _BoundHandler.__new__(_BoundHandler)._routes_build()
    return ThreadingHTTPServer((host, port), _BoundHandler)
