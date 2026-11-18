"""健康管理结构化接口的单元测试。

覆盖：健康档案读写与校验、体重记录增删查与校验、健康指标读写与校验、
数据库异常兜底、懒加载建库、原有四个接口兼容性。
全部使用内存数据库或临时目录，不访问网络、不写真实 data/。
"""

import sqlite3
import unittest
from datetime import date, timedelta
from unittest import mock

from fastapi.testclient import TestClient

from agent import web_app as web_app_module
from agent.modules.health_db import HealthDB
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
        """保存快照并忽略容量清理，测试无需模拟淘汰。"""
        self.data[session_id] = list(messages)
        return True

    def delete(self, session_id):
        """删除会话快照。"""
        self.data.pop(session_id, None)
        return True


class FakeAgent:
    """假 Agent：不调用大模型，直接返回固定回复。"""

    def run(self, user_input):
        """返回固定文本，便于验证接口兼容性。"""
        return "测试回复"


class HealthApiTestBase(unittest.TestCase):
    """为每个用例创建独立的健康数据库与测试客户端。"""

    def setUp(self):
        # 使用内存库，绝不触碰项目 data/health.db；
        # Web 请求在线程池中执行，因此关闭同线程限制（应用内部已加锁串行）
        self.db = HealthDB(":memory:", check_same_thread=False)
        self.db.initialize()
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            health_db=self.db,
        )
        self.client = TestClient(app)


class ProfileApiTest(HealthApiTestBase):
    """健康档案接口：GET / PUT。"""

    def test_get_empty_profile(self):
        """未建立档案时返回空对象且 exists 为 False。"""
        response = self.client.get("/api/health/profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"profile": {}, "exists": False})

    def test_put_create_profile(self):
        """完整字段写入后能读回相同内容。"""
        response = self.client.put(
            "/api/health/profile",
            json={
                "name": "张三",
                "gender": "男",
                "birth_date": "1990-05-20",
                "height_cm": 175,
            },
        )
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["name"], "张三")
        self.assertEqual(profile["gender"], "男")
        self.assertEqual(profile["birth_date"], "1990-05-20")
        self.assertEqual(profile["height_cm"], 175.0)
        self.assertTrue(profile["updated_at"])

        again = self.client.get("/api/health/profile").json()
        self.assertTrue(again["exists"])
        self.assertEqual(again["profile"]["name"], "张三")

    def test_put_partial_update_keeps_other_fields(self):
        """只传身高时，姓名等已有字段保持不变。"""
        self.client.put("/api/health/profile", json={"name": "李四", "gender": "女"})
        response = self.client.put("/api/health/profile", json={"height_cm": 160.5})
        self.assertEqual(response.status_code, 200)
        profile = response.json()["profile"]
        self.assertEqual(profile["name"], "李四")
        self.assertEqual(profile["gender"], "女")
        self.assertEqual(profile["height_cm"], 160.5)

    def test_put_empty_body_rejected(self):
        """空对象没有任何可更新字段，返回 400。"""
        response = self.client.put("/api/health/profile", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("至少提供一个", response.json()["detail"])

    def test_put_invalid_name(self):
        """姓名为空白或超长时返回 400。"""
        for value in ("", "   ", "张" * 21, 123):
            with self.subTest(value=value):
                response = self.client.put("/api/health/profile", json={"name": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("姓名", response.json()["detail"])

    def test_put_invalid_gender(self):
        """性别只允许男或女。"""
        response = self.client.put("/api/health/profile", json={"gender": "未知"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("性别", response.json()["detail"])

    def test_put_invalid_birth_date(self):
        """出生日期格式错误或晚于今天时返回 400。"""
        future = (date.today() + timedelta(days=1)).isoformat()
        for value in ("1990/05/20", "1990-13-01", "abc", future, 19900520):
            with self.subTest(value=value):
                response = self.client.put(
                    "/api/health/profile", json={"birth_date": value}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("出生日期", response.json()["detail"])

    def test_put_invalid_height(self):
        """身高超出 100～250 cm 或非数字时返回 400。"""
        for value in (99, 251, "abc", True, None):
            with self.subTest(value=value):
                response = self.client.put(
                    "/api/health/profile", json={"height_cm": value}
                )
                # None 表示未传字段，空请求体应返回"至少提供一个"
                self.assertEqual(response.status_code, 400)
                self.assertTrue(response.json()["detail"])


class WeightApiTest(HealthApiTestBase):
    """体重记录接口：GET / POST / DELETE。"""

    def test_list_empty(self):
        """没有记录时返回空列表。"""
        response = self.client.get("/api/health/weights")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"records": [], "count": 0})

    def test_add_and_list(self):
        """新增记录后能在列表中查到，并返回 201。"""
        response = self.client.post(
            "/api/health/weights",
            json={"weight_kg": 70.5, "record_date": "2026-11-01", "note": "晨起"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["id"], int)
        self.assertEqual(body["records"][0]["weight_kg"], 70.5)
        self.assertEqual(body["records"][0]["record_date"], "2026-11-01")
        self.assertEqual(body["records"][0]["note"], "晨起")

        listed = self.client.get("/api/health/weights").json()
        self.assertEqual(listed["count"], 1)

    def test_add_default_date_is_today(self):
        """未传日期时使用当天日期。"""
        response = self.client.post("/api/health/weights", json={"weight_kg": 65})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json()["records"][0]["record_date"], date.today().isoformat()
        )

    def test_add_invalid_weight(self):
        """缺失、非数字或超范围的体重返回 400。"""
        for value in (None, "", "abc", 19.9, 300.1, True):
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/health/weights", json={"weight_kg": value}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("体重", response.json()["detail"])

    def test_add_future_date_rejected(self):
        """记录日期不能晚于今天。"""
        future = (date.today() + timedelta(days=1)).isoformat()
        response = self.client.post(
            "/api/health/weights", json={"weight_kg": 70, "record_date": future}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("记录日期", response.json()["detail"])

    def test_add_invalid_date_format(self):
        """非法日期格式返回 400。"""
        response = self.client.post(
            "/api/health/weights", json={"weight_kg": 70, "record_date": "2026/11/01"}
        )
        self.assertEqual(response.status_code, 400)

    def test_add_note_too_long(self):
        """备注超过 200 字符返回 400。"""
        response = self.client.post(
            "/api/health/weights", json={"weight_kg": 70, "note": "备" * 201}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("备注", response.json()["detail"])

    def test_add_note_not_text(self):
        """备注必须是文本。"""
        response = self.client.post(
            "/api/health/weights", json={"weight_kg": 70, "note": 123}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("备注", response.json()["detail"])

    def test_delete_existing(self):
        """删除已存在的记录返回成功。"""
        record_id = self.client.post(
            "/api/health/weights", json={"weight_kg": 70}
        ).json()["id"]
        response = self.client.delete(f"/api/health/weights/{record_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "record_id": record_id})
        self.assertEqual(self.client.get("/api/health/weights").json()["count"], 0)

    def test_delete_missing_returns_404(self):
        """删除不存在的记录返回 404。"""
        response = self.client.delete("/api/health/weights/9999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("未找到", response.json()["detail"])

    def test_delete_invalid_id(self):
        """非法编号返回 400。"""
        for value in ("abc", "0", "-1", "1.5"):
            with self.subTest(value=value):
                response = self.client.delete(f"/api/health/weights/{value}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("编号", response.json()["detail"])

    def test_list_invalid_limit(self):
        """limit 非 1～100 的整数时返回 400。"""
        for value in ("0", "101", "abc", "-3"):
            with self.subTest(value=value):
                response = self.client.get("/api/health/weights", params={"limit": value})
                self.assertEqual(response.status_code, 400)
                self.assertIn("limit", response.json()["detail"])

class MetricApiTest(HealthApiTestBase):
    """健康指标接口：GET / POST。"""

    def test_list_empty(self):
        """没有指标时返回空列表。"""
        response = self.client.get("/api/health/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"records": [], "count": 0})

    def test_add_and_filter(self):
        """新增指标后可读取，并支持按类型过滤。"""
        response = self.client.post(
            "/api/health/metrics",
            json={"metric": "systolic_bp", "value": 120, "recorded_at": "2026-11-10 08:30"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIsInstance(body["id"], int)
        self.assertEqual(body["records"][0]["metric"], "systolic_bp")
        self.assertEqual(body["records"][0]["value"], 120.0)

        self.client.post(
            "/api/health/metrics", json={"metric": "heart_rate", "value": 72}
        )

        filtered = self.client.get(
            "/api/health/metrics", params={"metric": "heart_rate"}
        ).json()
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["records"][0]["metric"], "heart_rate")

        all_records = self.client.get("/api/health/metrics").json()
        self.assertEqual(all_records["count"], 2)

    def test_add_invalid_metric_name(self):
        """不支持的指标类型返回 400。"""
        for value in ("unknown", "", None, ["systolic_bp"], 123):
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/health/metrics", json={"metric": value, "value": 100}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("metric", response.json()["detail"])

    def test_add_out_of_range_value(self):
        """数值超出该指标合理范围时返回 400。"""
        cases = [
            ("systolic_bp", 59),
            ("systolic_bp", 261),
            ("heart_rate", 29),
            ("spo2", 101),
            ("blood_glucose", 0.5),
            ("temperature", 46),
            ("systolic_bp", "abc"),
            ("systolic_bp", True),
        ]
        for metric, value in cases:
            with self.subTest(metric=metric, value=value):
                response = self.client.post(
                    "/api/health/metrics", json={"metric": metric, "value": value}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("数值", response.json()["detail"])

    def test_add_invalid_recorded_at(self):
        """记录时间格式非法时返回 400。"""
        for value in ("2026/11/10 08:30", "2026-11-10", "2026-11-10 25:00", 20261110):
            with self.subTest(value=value):
                response = self.client.post(
                    "/api/health/metrics",
                    json={"metric": "heart_rate", "value": 70, "recorded_at": value},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("记录时间", response.json()["detail"])

    def test_list_invalid_metric_filter(self):
        """过滤参数不在支持类型内时返回 400。"""
        response = self.client.get("/api/health/metrics", params={"metric": "unknown"})
        self.assertEqual(response.status_code, 400)

    def test_list_invalid_limit(self):
        """limit 非法时返回 400。"""
        response = self.client.get("/api/health/metrics", params={"limit": "1000"})
        self.assertEqual(response.status_code, 400)


class DbErrorTest(HealthApiTestBase):
    """数据库异常时返回中文 500，不泄漏堆栈。"""

    def assert_safe_500(self, response):
        """统一断言：状态码 500、中文提示、无内部细节。"""
        self.assertEqual(response.status_code, 500)
        self.assertIn("健康数据暂时不可用", response.json()["detail"])
        self.assertNotIn("boom", response.text)
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("sqlite3", response.text)

    def test_get_profile_db_error(self):
        """读取档案失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "get_profile", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.get("/api/health/profile")
        self.assert_safe_500(response)

    def test_put_profile_db_error(self):
        """保存档案失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "update_profile", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.put(
                "/api/health/profile", json={"name": "张三"}
            )
        self.assert_safe_500(response)

    def test_list_weights_db_error(self):
        """读取体重失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "list_weights", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.get("/api/health/weights")
        self.assert_safe_500(response)

    def test_add_weight_db_error(self):
        """写入体重失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "add_weight", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.post(
                "/api/health/weights", json={"weight_kg": 70}
            )
        self.assert_safe_500(response)

    def test_delete_weight_db_error(self):
        """删除体重失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "delete_weight", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.delete("/api/health/weights/1")
        self.assert_safe_500(response)

    def test_add_metric_db_error(self):
        """写入指标失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "add_metric", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.post(
                "/api/health/metrics", json={"metric": "heart_rate", "value": 70}
            )
        self.assert_safe_500(response)

    def test_list_metrics_db_error(self):
        """读取指标失败时返回安全的中文错误。"""
        with mock.patch.object(
            HealthDB, "list_metrics", side_effect=sqlite3.Error("boom")
        ):
            response = self.client.get("/api/health/metrics")
        self.assert_safe_500(response)


class LazyInitTest(unittest.TestCase):
    """健康数据库懒加载：应用创建时不建库，首次请求才建库。"""

    def test_lazy_init_creates_db_on_first_request(self):
        """创建应用后不实例化 HealthDB，首次访问健康接口才初始化。"""
        with mock.patch.object(web_app_module, "HealthDB") as fake_cls:
            fake_db = fake_cls.return_value
            fake_db.get_profile.return_value = {}
            app = create_app(agent_factory=FakeAgent, session_store=FakeStore())
            client = TestClient(app)

            # 应用创建完成时还没有创建数据库连接
            fake_cls.assert_not_called()

            response = client.get("/api/health/profile")
            self.assertEqual(response.status_code, 200)
            # 首次健康请求才建库并执行初始化
            fake_cls.assert_called_once_with(check_same_thread=False)
            fake_db.initialize.assert_called_once()

            # 再次请求复用同一个实例，不会重复建库
            client.get("/api/health/profile")
            self.assertEqual(fake_cls.call_count, 1)


class CompatibilityTest(HealthApiTestBase):
    """原有四个接口保持兼容。"""

    def test_index_page(self):
        """GET / 仍返回聊天页面。"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_health_check(self):
        """GET /api/health 仍返回 ok。"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_chat(self):
        """POST /api/chat 仍返回 reply / session_id / module。"""
        response = self.client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(set(body), {"reply", "session_id", "module"})
        self.assertEqual(body["reply"], "测试回复")

    def test_chat_empty_message(self):
        """空消息仍返回 400。"""
        response = self.client.post("/api/chat", json={"message": "  "})
        self.assertEqual(response.status_code, 400)

    def test_delete_session(self):
        """DELETE /api/session/{id} 仍返回删除成功。"""
        response = self.client.delete("/api/session/abc123")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "session_id": "abc123"})

    def test_health_api_does_not_require_agent(self):
        """健康接口不经过 Agent，不触发大模型调用。"""
        with mock.patch.object(FakeAgent, "run", side_effect=AssertionError("不应调用")):
            response = self.client.get("/api/health/profile")
        self.assertEqual(response.status_code, 200)