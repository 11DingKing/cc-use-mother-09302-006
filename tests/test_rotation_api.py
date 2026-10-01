"""HTTP 接口的冒烟测试：命令入口、角色权限与视图裁剪。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation.api import make_handler
from rotation.service import RotationService


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = RotationService(str(Path(cls.tmp.name) / "api.db"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def call(self, kind, payload, *, role="teacher", actor="t1", event_id=None):
        body = {"type": kind, "payload": payload}
        if event_id:
            body["event_id"] = event_id
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/commands",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Actor-Role": role,
                "X-Actor-Id": actor,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def queue(self, session_id, *, role, actor=""):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/sessions/{session_id}/queue",
            headers={"X-Actor-Role": role, "X-Actor-Id": actor},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_and_role_views(self) -> None:
        status, _ = self.call(
            "create_student",
            {"student_id": "st1", "class_id": "C1", "name": "小明",
             "guardian_contact": "13900000000"},
            event_id="api-st1",
        )
        self.assertEqual(status, 200)
        self.call("grant_consent", {"student_id": "st1"}, event_id="api-c1")
        status, session = self.call(
            "create_session",
            {"session_id": "sess1", "class_id": "C1", "title": "木偶互动课",
             "capacity": 1, "starts_at": "2026-10-01T08:00:00+00:00"},
            event_id="api-sess1",
        )
        self.assertEqual(status, 200)
        status, registered = self.call(
            "register", {"session_id": "sess1", "student_id": "st1"}, event_id="api-reg1"
        )
        self.assertEqual((status, registered["status"]), (200, "offered"))
        # 重放同一 event_id：返回首次结果，不重复占位
        status, replay = self.call(
            "register", {"session_id": "sess1", "student_id": "st1"}, event_id="api-reg1"
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entry_id"], registered["entry_id"])
        # 班主任视图：有排序理由，无敏感信息
        status, teacher = self.queue("sess1", role="teacher")
        self.assertEqual(status, 200)
        self.assertEqual(teacher["offered"][0]["student_id"], "st1")
        self.assertNotIn("13900000000", json.dumps(teacher, ensure_ascii=False))
        # 志愿者视图：只有签到名单
        status, volunteer = self.queue("sess1", role="volunteer")
        self.assertEqual(status, 200)
        self.assertEqual(volunteer["roster"][0]["name"], "小明")
        self.assertNotIn("offered", volunteer)
        # 学生视图：只看自己
        status, own = self.queue("sess1", role="student", actor="st1")
        self.assertEqual(own["section"], "offered")
        # 匿名无权查看
        status, _ = self.queue("sess1", role="")
        self.assertEqual(status, 403)

    def test_permission_and_validation_errors(self) -> None:
        # 志愿者不能扩容
        status, denied = self.call(
            "set_capacity", {"session_id": "sess1", "capacity": 5},
            role="volunteer", actor="v1", event_id="api-denied",
        )
        self.assertEqual(status, 403)
        # 学生不能替他人报名
        status, denied = self.call(
            "register", {"session_id": "sess1", "student_id": "someone-else"},
            role="student", actor="st1", event_id="api-denied-2",
        )
        self.assertEqual(status, 403)
        # 未知命令
        status, unknown = self.call("fly_to_moon", {}, event_id="api-unknown")
        self.assertEqual(status, 400)
        self.assertEqual(unknown["error"]["code"], "unknown_command")
        # 不存在的场次 → 404
        status, missing = self.queue("no-such-session", role="teacher")
        self.assertEqual(status, 404)

    def test_health(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/health", timeout=5
        ) as response:
            self.assertEqual(json.loads(response.read()), {"ok": True})


if __name__ == "__main__":
    unittest.main()
