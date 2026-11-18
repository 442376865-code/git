"""用药管理结构化接口的单元测试。

覆盖：计划列表/新增/停用/删除、服药记录列表/新增/删除、
字段校验与边界值、记录不存在、数据库异常兜底、原有四个接口兼容、
以及用药接口不触发 Agent 与大模型。
全部使用内存数据库，不写真实 data/，不访问网络。
"""

import sqlite3
import unittest
from datetime import datetime, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from agent.modules.medication_db import MedicationDB
from agent.web_app import create_app


class FakeStore:
    """内存版会话存储，替换真实 SQLite，避免写入 data/。"""

    def __init__(self):
        self.data = {}

    def load(self, session_id):
        """返回已保存的消息列表。"""
        return list(self.data.get(session_id, []))

    def save(self, session_id, messages):
        """保存消息快照。"""
        self.data[session_id] = list(messages)
        return True

    def save_and_prune(self, session_id, messages, max_rows=200, protected_ids=None):
        """保存快照并忽略容量清理。"""
        self.data[session_id] = list(messages)
        return True

    def delete(self, session_id):
        """删除会话快照。"""
        self.data.pop(session_id, None)
        return True


class FakeAgent:
    """假 Agent：不调用大模型，只记录被调用的次数。"""

    calls = []

    def run(self, user_input):
        """记录调用并返回固定文本。"""
        FakeAgent.calls.append(user_input)
        return "测试回复"


class MedicationApiTestBase(unittest.TestCase):
    """为每个用例创建独立的用药数据库与测试客户端。"""

    def setUp(self):
        FakeAgent.calls = []
        # 内存库 + 关闭同线程限制：Web 请求在线程池执行，应用内部已加锁串行
        self.db = MedicationDB(":memory:", check_same_thread=False)
        self.db.initialize()
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            medication_db=self.db,
        )
        self.client = TestClient(app)

    def create_plan(self, name="氨氯地平", instruction="每天 1 次", **kwargs):
        """在数据库层直接建计划，便于测试其它接口。"""
        return self.db.create_plan(name, instruction, **kwargs)


class PlanListApiTest(MedicationApiTestBase):
    """GET /api/medication/plans"""

    def test_empty_list(self):
        """无计划时返回空数组与固定安全提示。"""
        response = self.client.get("/api/medication/plans")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["plans"], [])
        self.assertEqual(body["count"], 0)
        self.assertIn("不能替代医生诊疗", body["notice"])

    def test_list_returns_plans(self):
        """已有计划按编号升序返回。"""
        first = self.create_plan("甲药", "每天 1 次")
        second = self.create_plan("乙药", "每天 2 次")
        body = self.client.get("/api/medication/plans").json()
        self.assertEqual([plan["id"] for plan in body["plans"]], [first, second])
        self.assertEqual(body["count"], 2)

    def test_filter_active(self):
        """status=active 只返回生效中的计划。"""
        active_id = self.create_plan("甲药", "每天 1 次")
        stopped_id = self.create_plan("乙药", "每天 2 次")
        self.db.stop_plan(stopped_id)
        body = self.client.get("/api/medication/plans?status=active").json()
        self.assertEqual([plan["id"] for plan in body["plans"]], [active_id])

    def test_filter_stopped(self):
        """status=stopped 只返回已停用的计划。"""
        self.create_plan("甲药", "每天 1 次")
        stopped_id = self.create_plan("乙药", "每天 2 次")
        self.db.stop_plan(stopped_id)
        body = self.client.get("/api/medication/plans?status=stopped").json()
        self.assertEqual([plan["id"] for plan in body["plans"]], [stopped_id])

    def test_invalid_status(self):
        """status 取值非法时返回 400。"""
        response = self.client.get("/api/medication/plans?status=deleted")
        self.assertEqual(response.status_code, 400)

    def test_invalid_limit(self):
        """limit 非正整数或超上限时返回 400。"""
        for value in ("0", "-1", "abc", "101"):
            response = self.client.get(f"/api/medication/plans?limit={value}")
            self.assertEqual(response.status_code, 400, value)

    def test_limit_keeps_newest(self):
        """limit 只返回编号最大的若干条。"""
        for index in range(3):
            self.create_plan(f"药{index}", "每天 1 次")
        body = self.client.get("/api/medication/plans?limit=2").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual([plan["medication_name"] for plan in body["plans"]], ["药1", "药2"])


class PlanCreateApiTest(MedicationApiTestBase):
    """POST /api/medication/plans"""

    def test_create_minimal(self):
        """只给药名与用法即可创建，状态默认生效中。"""
        response = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "氨氯地平", "doctor_instruction": "每天 1 次"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["id"], int)
        self.assertEqual(body["plans"][0]["status"], "active")
        self.assertIn("不提供系统提醒", body["notice"])

    def test_create_full(self):
        """完整字段被正确保存。"""
        response = self.client.post(
            "/api/medication/plans",
            json={
                "medication_name": " 甲药 ",
                "doctor_instruction": " 每天 2 次 ",
                "schedule_times": ["08:00", "20:00"],
                "start_date": "2026-11-01",
                "end_date": "2026-12-01",
                "note": "早晚饭后",
            },
        )
        self.assertEqual(response.status_code, 201)
        plan = self.db.get_plan(response.json()["id"])
        self.assertEqual(plan["medication_name"], "甲药")
        self.assertEqual(plan["doctor_instruction"], "每天 2 次")
        self.assertEqual(plan["schedule_times"], "08:00,20:00")
        self.assertEqual(plan["start_date"], "2026-11-01")
        self.assertEqual(plan["end_date"], "2026-12-01")
        self.assertEqual(plan["note"], "早晚饭后")

    def test_missing_name(self):
        """缺药名时不写库。"""
        response = self.client.post(
            "/api/medication/plans", json={"doctor_instruction": "每天 1 次"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_plans(), [])

    def test_missing_instruction(self):
        """缺医嘱用法时不写库。"""
        response = self.client.post(
            "/api/medication/plans", json={"medication_name": "甲药"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_plans(), [])

    def test_blank_name(self):
        """空白药名被视为缺失。"""
        response = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "   ", "doctor_instruction": "每天 1 次"},
        )
        self.assertEqual(response.status_code, 400)

    def test_name_length_boundary(self):
        """药名 50 字通过，51 字被拒。"""
        ok = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "药" * 50, "doctor_instruction": "每天 1 次"},
        )
        self.assertEqual(ok.status_code, 201)
        too_long = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "药" * 51, "doctor_instruction": "每天 1 次"},
        )
        self.assertEqual(too_long.status_code, 400)

    def test_instruction_length_boundary(self):
        """医嘱用法 200 字通过，201 字被拒。"""
        ok = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "甲药", "doctor_instruction": "用" * 200},
        )
        self.assertEqual(ok.status_code, 201)
        too_long = self.client.post(
            "/api/medication/plans",
            json={"medication_name": "甲药", "doctor_instruction": "用" * 201},
        )
        self.assertEqual(too_long.status_code, 400)

    def test_name_not_text(self):
        """药名不是文本时返回 400。"""
        response = self.client.post(
            "/api/medication/plans",
            json={"medication_name": 123, "doctor_instruction": "每天 1 次"},
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_schedule_time(self):
        """非法用药时间不写库。"""
        for value in ("25:00", ["08:60"], ["08:00"] * 7, []):
            response = self.client.post(
                "/api/medication/plans",
                json={
                    "medication_name": "甲药",
                    "doctor_instruction": "每天 1 次",
                    "schedule_times": value,
                },
            )
            self.assertEqual(response.status_code, 400, value)
        self.assertEqual(self.db.list_plans(), [])

    def test_invalid_date(self):
        """非法日期不写库。"""
        for field in ("start_date", "end_date"):
            response = self.client.post(
                "/api/medication/plans",
                json={
                    "medication_name": "甲药",
                    "doctor_instruction": "每天 1 次",
                    field: "2026-02-30",
                },
            )
            self.assertEqual(response.status_code, 400, field)
        self.assertEqual(self.db.list_plans(), [])

    def test_end_before_start(self):
        """结束日期早于开始日期时拒绝。"""
        response = self.client.post(
            "/api/medication/plans",
            json={
                "medication_name": "甲药",
                "doctor_instruction": "每天 1 次",
                "start_date": "2026-12-01",
                "end_date": "2026-11-01",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_plans(), [])

    def test_note_too_long(self):
        """备注超过 200 字被拒。"""
        response = self.client.post(
            "/api/medication/plans",
            json={
                "medication_name": "甲药",
                "doctor_instruction": "每天 1 次",
                "note": "备" * 201,
            },
        )
        self.assertEqual(response.status_code, 400)

class PlanDeleteApiTest(MedicationApiTestBase):
    """DELETE /api/medication/plans/{plan_id}：停用与删除必须可区分。"""

    def test_default_mode_stops_and_keeps_row(self):
        """缺省 mode=stop：只改状态，计划仍然存在。"""
        plan_id = self.create_plan()
        response = self.client.delete(f"/api/medication/plans/{plan_id}")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "stop")
        self.assertEqual(self.db.get_plan(plan_id)["status"], "stopped")
        self.assertEqual(len(self.db.list_plans()), 1)

    def test_stop_keeps_logs(self):
        """停用不会删除既有服药记录。"""
        plan_id = self.create_plan()
        log_id = self.db.add_log(plan_id, "2026-11-01 08:00:00")
        self.client.delete(f"/api/medication/plans/{plan_id}?mode=stop")
        self.assertIsNotNone(self.db.get_log(log_id))

    def test_stop_twice_returns_conflict(self):
        """重复停用返回 409，不重复改状态。"""
        plan_id = self.create_plan()
        self.client.delete(f"/api/medication/plans/{plan_id}?mode=stop")
        response = self.client.delete(f"/api/medication/plans/{plan_id}?mode=stop")
        self.assertEqual(response.status_code, 409)

    def test_delete_mode_removes_plan_and_logs(self):
        """mode=delete：物理删除计划及其关联记录。"""
        plan_id = self.create_plan()
        log_id = self.db.add_log(plan_id, "2026-11-01 08:00:00")
        response = self.client.delete(f"/api/medication/plans/{plan_id}?mode=delete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "delete")
        self.assertIsNone(self.db.get_plan(plan_id))
        self.assertIsNone(self.db.get_log(log_id))
        self.assertEqual(self.db.list_logs(), [])

    def test_delete_missing_plan(self):
        """不存在的计划两种模式都返回 404。"""
        for mode in ("stop", "delete"):
            response = self.client.delete(f"/api/medication/plans/999?mode={mode}")
            self.assertEqual(response.status_code, 404, mode)

    def test_invalid_mode(self):
        """mode 只允许 stop / delete。"""
        plan_id = self.create_plan()
        response = self.client.delete(f"/api/medication/plans/{plan_id}?mode=purge")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.get_plan(plan_id)["status"], "active")

    def test_invalid_plan_id(self):
        """非正整数编号返回 400。"""
        for value in ("0", "-1", "abc"):
            response = self.client.delete(f"/api/medication/plans/{value}")
            self.assertEqual(response.status_code, 400, value)


class LogListApiTest(MedicationApiTestBase):
    """GET /api/medication/logs"""

    def test_empty_logs(self):
        """无记录时返回空数组。"""
        body = self.client.get("/api/medication/logs").json()
        self.assertEqual(body["logs"], [])
        self.assertEqual(body["count"], 0)
        self.assertIn("仅支持手动记录", body["notice"])

    def test_logs_desc_with_name(self):
        """记录按服药时间倒序，并带出药名。"""
        plan_id = self.create_plan("甲药", "每天 1 次")
        self.db.add_log(plan_id, "2026-11-01 08:00:00")
        self.db.add_log(plan_id, "2026-11-02 08:00:00")
        body = self.client.get("/api/medication/logs").json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["logs"][0]["taken_at"], "2026-11-02 08:00:00")
        self.assertEqual(body["logs"][0]["medication_name"], "甲药")

    def test_limit(self):
        """limit 生效，且非法取值返回 400。"""
        plan_id = self.create_plan()
        self.db.add_log(plan_id, "2026-11-01 08:00:00")
        self.db.add_log(plan_id, "2026-11-02 08:00:00")
        body = self.client.get("/api/medication/logs?limit=1").json()
        self.assertEqual(body["count"], 1)
        for value in ("0", "-1", "abc", "101"):
            response = self.client.get(f"/api/medication/logs?limit={value}")
            self.assertEqual(response.status_code, 400, value)


class LogCreateApiTest(MedicationApiTestBase):
    """POST /api/medication/logs"""

    def test_create_with_time_and_note(self):
        """指定时间与备注时按原样保存。"""
        plan_id = self.create_plan()
        response = self.client.post(
            "/api/medication/logs",
            json={
                "plan_id": plan_id,
                "taken_at": "2026-11-01 08:00",
                "note": " 早饭后 ",
            },
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["id"], int)
        self.assertEqual(body["logs"][0]["taken_at"], "2026-11-01 08:00")
        self.assertEqual(body["logs"][0]["note"], "早饭后")

    def test_create_defaults_to_now(self):
        """未传时间时使用当前时间，且不晚于服务器时间。"""
        plan_id = self.create_plan()
        before = datetime.now().replace(microsecond=0)
        response = self.client.post(
            "/api/medication/logs", json={"plan_id": plan_id}
        )
        self.assertEqual(response.status_code, 201)
        saved = datetime.strptime(response.json()["logs"][0]["taken_at"], "%Y-%m-%d %H:%M:%S")
        self.assertLessEqual(saved, datetime.now())
        self.assertGreaterEqual(saved, before - timedelta(seconds=2))

    def test_missing_plan_id(self):
        """缺 plan_id 时返回 400，不写记录。"""
        response = self.client.post("/api/medication/logs", json={})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_logs(), [])

    def test_invalid_plan_id(self):
        """plan_id 非正整数时返回 400。"""
        for value in (0, -1, "abc", 1.5, True):
            response = self.client.post(
                "/api/medication/logs", json={"plan_id": value}
            )
            self.assertEqual(response.status_code, 400, repr(value))

    def test_missing_plan(self):
        """计划不存在时返回 400，不写记录。"""
        response = self.client.post(
            "/api/medication/logs", json={"plan_id": 999}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不存在或已停用", response.json()["detail"])
        self.assertEqual(self.db.list_logs(), [])

    def test_stopped_plan(self):
        """计划已停用时返回 400，不写记录。"""
        plan_id = self.create_plan()
        self.db.stop_plan(plan_id)
        response = self.client.post(
            "/api/medication/logs", json={"plan_id": plan_id}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_logs(), [])

    def test_invalid_taken_at(self):
        """非法时间格式或未来时间被拒。"""
        plan_id = self.create_plan()
        future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        for value in ("2026-02-30 08:00", "2026-11-01 25:00", "昨天", future):
            response = self.client.post(
                "/api/medication/logs",
                json={"plan_id": plan_id, "taken_at": value},
            )
            self.assertEqual(response.status_code, 400, value)
        self.assertEqual(self.db.list_logs(), [])

    def test_note_too_long(self):
        """备注超过 200 字被拒。"""
        plan_id = self.create_plan()
        response = self.client.post(
            "/api/medication/logs",
            json={"plan_id": plan_id, "note": "备" * 201},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_logs(), [])


class LogDeleteApiTest(MedicationApiTestBase):
    """DELETE /api/medication/logs/{log_id}"""

    def test_delete_log(self):
        """删除成功后记录不再存在。"""
        plan_id = self.create_plan()
        log_id = self.db.add_log(plan_id, "2026-11-01 08:00:00")
        response = self.client.delete(f"/api/medication/logs/{log_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "log_id": log_id})
        self.assertIsNone(self.db.get_log(log_id))

    def test_delete_missing_log(self):
        """不存在的记录返回 404。"""
        response = self.client.delete("/api/medication/logs/999")
        self.assertEqual(response.status_code, 404)

    def test_invalid_log_id(self):
        """非正整数编号返回 400。"""
        for value in ("0", "-1", "abc"):
            response = self.client.delete(f"/api/medication/logs/{value}")
            self.assertEqual(response.status_code, 400, value)


class DatabaseFailureTest(MedicationApiTestBase):
    """数据库异常时不泄露内部信息。"""

    def assert_safe(self, response):
        """统一断言：500 + 中文提示 + 不含内部关键字。"""
        self.assertEqual(response.status_code, 500)
        text = response.text
        self.assertIn("暂时不可用", text)
        for leaked in ("Traceback", "sqlite3", "SELECT", "medication_plans"):
            self.assertNotIn(leaked, text)

    def test_list_plans_error(self):
        """读取计划异常时返回 500。"""
        with mock.patch.object(
            self.db, "list_plans", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            self.assert_safe(self.client.get("/api/medication/plans"))

    def test_create_plan_error(self):
        """写入计划异常时返回 500。"""
        with mock.patch.object(
            self.db, "create_plan", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assert_safe(
                self.client.post(
                    "/api/medication/plans",
                    json={"medication_name": "甲药", "doctor_instruction": "每天 1 次"},
                )
            )

    def test_list_logs_error(self):
        """读取记录异常时返回 500。"""
        with mock.patch.object(
            self.db, "list_logs", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            self.assert_safe(self.client.get("/api/medication/logs"))

    def test_create_log_error(self):
        """写入记录异常时返回 500。"""
        with mock.patch.object(
            self.db, "add_log", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assert_safe(
                self.client.post("/api/medication/logs", json={"plan_id": 1})
            )

    def test_delete_log_error(self):
        """删除记录异常时返回 500。"""
        with mock.patch.object(
            self.db, "delete_log", side_effect=sqlite3.OperationalError("locked")
        ):
            self.assert_safe(self.client.delete("/api/medication/logs/1"))


class LegacyAndIsolationTest(MedicationApiTestBase):
    """原有四个接口兼容，且用药接口不触发 Agent。"""

    def test_health_check(self):
        """GET /api/health 仍返回 ok。"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_index_page(self):
        """GET / 仍返回 HTML 页面。"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_chat_uses_fake_agent(self):
        """POST /api/chat 仍走注入的假 Agent，不调用大模型。"""
        response = self.client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "测试回复")
        self.assertEqual(FakeAgent.calls, ["你好"])

    def test_delete_session(self):
        """DELETE /api/session/{id} 仍可用。"""
        response = self.client.delete("/api/session/abc123")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["deleted"])

    def test_medication_api_never_calls_agent(self):
        """用药全部接口走完后，Agent 调用次数仍为 0。"""
        plan_id = self.create_plan()
        self.client.get("/api/medication/plans")
        self.client.post(
            "/api/medication/plans",
            json={"medication_name": "乙药", "doctor_instruction": "每天 2 次"},
        )
        self.client.post("/api/medication/logs", json={"plan_id": plan_id})
        self.client.get("/api/medication/logs")
        self.client.delete(f"/api/medication/plans/{plan_id}?mode=stop")
        self.assertEqual(FakeAgent.calls, [])


class LazyInitTest(unittest.TestCase):
    """未注入用药库时，启动阶段不创建数据库。"""

    def test_no_medication_db_before_first_request(self):
        """仅创建应用、不访问用药接口时不应实例化 MedicationDB。"""
        with mock.patch(
            "agent.web_app.MedicationDB", wraps=MedicationDB
        ) as patched:
            create_app(agent_factory=FakeAgent, session_store=FakeStore())
            self.assertEqual(patched.call_count, 0)


if __name__ == "__main__":
    unittest.main()