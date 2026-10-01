"""HTTP API 集成测试：路由、角色鉴权、隐私裁剪、幂等头、并发写。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rotation_queue.api import build_server
from rotation_queue.service import QueueService
from rotation_queue.store import EventStore


class ApiHarness:
    def __init__(self) -> None:
        self.store = EventStore(":memory:")
        self.service = QueueService(self.store)
        self.httpd = build_server(self.service, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.close()

    def call(self, method: str, path: str, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    # 便捷引导
    def seed(self) -> None:
        T = {"Content-Type": "application/json", "X-Role": "teacher"}
        self.call("POST", "/api/classes", {"class_id": "c1", "name": "三年级1班"}, T)
        self.call("POST", "/api/sessions", {
            "session_id": "s1", "class_id": "c1",
            "starts_at": "2026-10-05T10:00:00+00:00", "capacity": 2,
        }, T)
        for sid in "abc":
            self.call("POST", "/api/students", {"student_id": sid, "class_id": "c1", "name": f"n{sid}"}, T)
            self.call("POST", "/api/consents/grant", {"student_id": sid, "guardian": "家长电话13800"}, T)
        for i, sid in enumerate("abc"):
            self.call("POST", "/api/sessions/s1/signups",
                      {"enqueued_at": f"2026-09-20T09:0{i}:00+00:00"},
                      {"X-Role": "student", "X-Student-Id": sid})


class HttpApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ApiHarness()
        self.h.seed()

    def tearDown(self) -> None:
        self.h.stop()

    def test_auth_required(self) -> None:
        status, body = self.h.call("GET", "/api/sessions/s1")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")

    def test_student_self_service_only(self) -> None:
        # a 试图替 b 报名 -> 403
        status, body = self.h.call("POST", "/api/sessions/s1/signups",
                                   {"student_id": "b", "enqueued_at": "2026-09-20T09:00:00+00:00"},
                                   {"X-Role": "student", "X-Student-Id": "a"})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

    def test_volunteer_cannot_see_reasons_or_accommodation(self) -> None:
        self.h.call("POST", "/api/accommodations",
                    {"student_id": "a", "case_id": "x1", "reason_type": "medical",
                     "evidence_ref": "secret"},
                    {"X-Role": "teacher"})
        self.h.call("POST", "/api/sessions/s1/roster/publish", {}, {"X-Role": "teacher"})

        status, vview = self.h.call("GET", "/api/sessions/s1", None, {"X-Role": "volunteer"})
        self.assertEqual(status, 200)
        blob = json.dumps(vview, ensure_ascii=False)
        self.assertNotIn("reason_text", blob)
        self.assertNotIn("factors", blob)
        self.assertNotIn("secret", blob)
        self.assertNotIn("家长电话", blob)
        self.assertIn("attendees", vview)

        # 志愿者不能发布名单
        status, body = self.h.call("POST", "/api/sessions/s1/roster/publish", {},
                                   {"X-Role": "volunteer"})
        self.assertEqual(status, 403)

    def test_student_view_is_scoped_to_self(self) -> None:
        self.h.call("POST", "/api/sessions/s1/roster/publish", {}, {"X-Role": "teacher"})
        status, view = self.h.call("GET", "/api/sessions/s1", None,
                                   {"X-Role": "student", "X-Student-Id": "a"})
        self.assertEqual(status, 200)
        self.assertEqual(view["entry"]["student_id"], "a")
        blob = json.dumps(view, ensure_ascii=False)
        self.assertNotIn('"student_id": "b"', blob)
        self.assertNotIn('"student_id": "c"', blob)

    def test_idempotency_header_replays_without_new_place(self) -> None:
        # 新场次，a 首次报名携带固定幂等键
        self.h.call("POST", "/api/sessions", {
            "session_id": "s9", "class_id": "c1",
            "starts_at": "2026-10-12T10:00:00+00:00", "capacity": 2,
        }, {"X-Role": "teacher"})
        hdr = {"X-Role": "student", "X-Student-Id": "a", "Idempotency-Key": "fixed-key-9"}
        body = {"enqueued_at": "2026-09-20T09:00:00+00:00"}
        s1, r1 = self.h.call("POST", "/api/sessions/s9/signups", body, hdr)
        s2, r2 = self.h.call("POST", "/api/sessions/s9/signups", body, hdr)
        self.assertEqual((s1, s2), (200, 200))
        self.assertFalse(r1["replayed"])
        self.assertTrue(r2["replayed"])
        events = self.h.store.list_events("s9")
        count = sum(1 for e in events if e.event_type == "signed_up")
        self.assertEqual(count, 1)

    def test_concurrent_duplicate_signup_only_one_wins(self) -> None:
        # 新场次，b/c 两线程用不同幂等键抢同一唯一名额场景：
        # 这里验证同一学生并发重复报名不会产生两条
        self.h.call("POST", "/api/sessions", {
            "session_id": "s2", "class_id": "c1",
            "starts_at": "2026-10-09T10:00:00+00:00", "capacity": 1,
        }, {"X-Role": "teacher"})
        results: list[tuple[int, dict]] = []

        def fire(i: int) -> None:
            results.append(self.h.call(
                "POST", "/api/sessions/s2/signups",
                {"enqueued_at": f"2026-09-21T09:0{i}:00+00:00"},
                {"X-Role": "student", "X-Student-Id": "a", "Idempotency-Key": f"k{i}"}))

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 只有一次成功（200 且非重放），其余 409
        codes = sorted(code for code, _ in results)
        self.assertEqual(codes.count(200), 1)
        self.assertEqual(codes.count(409), 4)

    def test_full_flow_publish_cancel_promote_attendance(self) -> None:
        T = {"X-Role": "teacher"}
        self.h.call("POST", "/api/sessions/s1/roster/publish", {}, T)
        # a 临时退出 -> c 原子递补
        status, body = self.h.call("DELETE", "/api/sessions/s1/signups/a", {}, T)
        self.assertEqual(status, 200)
        _, view = self.h.call("GET", "/api/sessions/s1", None, T)
        self.assertEqual([r["student_id"] for r in view["roster"] if r["selected"]], ["b", "c"])
        # c 同时签到两次：只有一次成功
        s1, _ = self.h.call("POST", "/api/sessions/s1/attendance", {"student_id": "c"},
                            {"X-Role": "volunteer"})
        s2, b2 = self.h.call("POST", "/api/sessions/s1/attendance", {"student_id": "c"},
                             {"X-Role": "volunteer"})
        self.assertEqual((s1, s2), (200, 409))
        self.assertEqual(b2["error"]["code"], "already_checked_in")


if __name__ == "__main__":
    unittest.main()
