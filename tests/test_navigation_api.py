"""医疗导航结构化接口的单元测试。

覆盖：地点列表/新增/查询/编辑/删除五个接口、字段校验与边界值、
关键词与类别组合查询、非法 limit、数据库注入与懒加载、
同一连接的并发访问加锁、以及健康与用药接口兼容。
全部使用内存数据库，不写真实 data/，不访问网络，不调用大模型。
"""

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from agent.modules.amap_client import AmapClient
from agent.modules.health_db import HealthDB
from agent.modules.medical_navigation_db import MedicalNavigationDB
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


class NavigationApiTestBase(unittest.TestCase):
    """为每个用例创建独立的导航数据库与测试客户端。"""

    def setUp(self):
        FakeAgent.calls = []
        # 内存库 + 关闭同线程限制：Web 请求在线程池执行，应用内部已加锁串行
        self.db = MedicalNavigationDB(":memory:", check_same_thread=False)
        self.db.initialize()
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            navigation_db=self.db,
        )
        self.client = TestClient(app)

    def create_place(self, name="市中心医院", category="医院", **kwargs):
        """在数据库层直接建地点，便于测试其它接口。"""
        return self.db.create_place(name, category=category, **kwargs)


class PlaceListApiTest(NavigationApiTestBase):
    """GET /api/navigation/places"""

    def test_empty_list(self):
        """无数据时返回空列表与 count 0。"""
        response = self.client.get("/api/navigation/places")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"places": [], "count": 0})

    def test_list_returns_id_order_and_count(self):
        """列表按编号升序，count 与实际条数一致。"""
        self.create_place("甲医院")
        self.create_place("乙药房", category="药房")
        data = self.client.get("/api/navigation/places").json()
        self.assertEqual([place["name"] for place in data["places"]], ["甲医院", "乙药房"])
        self.assertEqual(data["count"], 2)

    def test_keyword_matches_name(self):
        """关键词命中名称。"""
        self.create_place("市中心医院")
        self.create_place("社区药房", category="药房")
        data = self.client.get("/api/navigation/places?keyword=中心").json()
        self.assertEqual([p["name"] for p in data["places"]], ["市中心医院"])

    def test_keyword_matches_address(self):
        """关键词命中地址。"""
        self.create_place("市中心医院", address="人民路 8 号")
        self.create_place("社区药房", category="药房", address="解放路 20 号")
        data = self.client.get("/api/navigation/places?keyword=解放路").json()
        self.assertEqual([p["name"] for p in data["places"]], ["社区药房"])

    def test_keyword_matches_category(self):
        """关键词命中类别。"""
        self.create_place("甲医院")
        self.create_place("乙药房", category="药房")
        data = self.client.get("/api/navigation/places?keyword=药房").json()
        self.assertEqual([p["name"] for p in data["places"]], ["乙药房"])

    def test_category_filter(self):
        """类别过滤只返回该类别。"""
        self.create_place("甲医院", category="医院")
        self.create_place("乙药房", category="药房")
        data = self.client.get("/api/navigation/places?category=药房").json()
        self.assertEqual([p["name"] for p in data["places"]], ["乙药房"])

    def test_keyword_and_category_combined(self):
        """关键词与类别可以同时使用。"""
        self.create_place("人民路医院", category="医院", address="人民路 1 号")
        self.create_place("人民路药房", category="药房", address="人民路 2 号")
        data = self.client.get(
            "/api/navigation/places?keyword=人民路&category=医院"
        ).json()
        self.assertEqual([p["name"] for p in data["places"]], ["人民路医院"])

    def test_limit_caps_result_size(self):
        """limit 只返回前 N 条。"""
        for index in range(3):
            self.create_place(f"医院{index}")
        data = self.client.get("/api/navigation/places?limit=2").json()
        self.assertEqual(data["count"], 2)
        self.assertEqual([p["name"] for p in data["places"]], ["医院0", "医院1"])

    def test_limit_combined_with_keyword(self):
        """关键词搜索结果同样受 limit 限制。"""
        for index in range(3):
            self.create_place(f"人民路医院{index}", address="人民路")
        data = self.client.get("/api/navigation/places?keyword=人民路&limit=2").json()
        self.assertEqual(data["count"], 2)

    def test_invalid_limit_returns_400(self):
        """limit 非法值统一返回 400，不会变成 500。"""
        for value in ("0", "-1", "abc", "101", "1.5", ""):
            with self.subTest(limit=value):
                response = self.client.get(f"/api/navigation/places?limit={value}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("limit", response.json()["detail"])

    def test_invalid_category_returns_400(self):
        """类别不在支持范围时返回 400。"""
        response = self.client.get("/api/navigation/places?category=超市")
        self.assertEqual(response.status_code, 400)
        self.assertIn("医院", response.json()["detail"])

    def test_blank_keyword_behaves_like_list(self):
        """空白关键词等价于不传关键词。"""
        self.create_place("甲医院")
        data = self.client.get("/api/navigation/places?keyword=%20%20").json()
        self.assertEqual(data["count"], 1)


class PlaceCreateApiTest(NavigationApiTestBase):
    """POST /api/navigation/places"""

    def test_create_returns_201_and_detail(self):
        """新增成功返回 201 与完整地点详情。"""
        response = self.client.post(
            "/api/navigation/places",
            json={
                "name": "市中心医院",
                "category": "医院",
                "address": "人民路 8 号",
                "phone": "0571-12345678",
                "note": "门诊楼三楼",
            },
        )
        self.assertEqual(response.status_code, 201)
        place = response.json()["place"]
        self.assertEqual(place["name"], "市中心医院")
        self.assertEqual(place["category"], "医院")
        self.assertEqual(place["address"], "人民路 8 号")
        self.assertEqual(place["phone"], "0571-12345678")
        self.assertEqual(place["note"], "门诊楼三楼")
        self.assertIsInstance(place["id"], int)

    def test_optional_fields_default_to_none(self):
        """只传必填字段时，可选字段为 None。"""
        response = self.client.post(
            "/api/navigation/places",
            json={"name": "社区诊所", "category": "诊所"},
        )
        self.assertEqual(response.status_code, 201)
        place = response.json()["place"]
        self.assertIsNone(place["address"])
        self.assertIsNone(place["phone"])
        self.assertIsNone(place["note"])

    def test_blank_optional_strings_become_none(self):
        """可选字段传空白字符串时规范化为 None。"""
        response = self.client.post(
            "/api/navigation/places",
            json={
                "name": "社区诊所",
                "category": "诊所",
                "address": "   ",
                "phone": "",
                "note": " ",
            },
        )
        place = response.json()["place"]
        self.assertIsNone(place["address"])
        self.assertIsNone(place["phone"])
        self.assertIsNone(place["note"])

    def test_name_and_category_are_stripped(self):
        """名称与类别传入时去掉首尾空格。"""
        response = self.client.post(
            "/api/navigation/places",
            json={"name": "  社区诊所  ", "category": "诊所"},
        )
        self.assertEqual(response.json()["place"]["name"], "社区诊所")

    def test_missing_name_returns_400(self):
        """缺少名称返回 400。"""
        response = self.client.post(
            "/api/navigation/places", json={"category": "医院"}
        )
        self.assertEqual(response.status_code, 400)

    def test_blank_name_returns_400(self):
        """名称为空白返回 400。"""
        response = self.client.post(
            "/api/navigation/places", json={"name": "   ", "category": "医院"}
        )
        self.assertEqual(response.status_code, 400)

    def test_name_too_long_returns_400(self):
        """名称超过 100 字返回 400。"""
        response = self.client.post(
            "/api/navigation/places", json={"name": "医" * 101, "category": "医院"}
        )
        self.assertEqual(response.status_code, 400)

    def test_name_at_max_length_accepted(self):
        """名称恰好 100 字可以保存。"""
        response = self.client.post(
            "/api/navigation/places", json={"name": "医" * 100, "category": "医院"}
        )
        self.assertEqual(response.status_code, 201)

    def test_missing_category_returns_400(self):
        """缺少类别返回 400。"""
        response = self.client.post(
            "/api/navigation/places", json={"name": "某医院"}
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_category_returns_400(self):
        """类别不在支持范围时返回 400，且不写库。"""
        response = self.client.post(
            "/api/navigation/places", json={"name": "某超市", "category": "超市"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_places(), [])

    def test_non_text_address_returns_400(self):
        """地址传非文本返回 400。"""
        response = self.client.post(
            "/api/navigation/places",
            json={"name": "某医院", "category": "医院", "address": 123},
        )
        self.assertEqual(response.status_code, 400)

    def test_address_too_long_returns_400(self):
        """地址超过 200 字返回 400。"""
        response = self.client.post(
            "/api/navigation/places",
            json={"name": "某医院", "category": "医院", "address": "路" * 201},
        )
        self.assertEqual(response.status_code, 400)

    def test_created_place_is_visible_in_list(self):
        """新增后立即可在列表中看到。"""
        self.client.post(
            "/api/navigation/places", json={"name": "中心医院", "category": "医院"}
        )
        data = self.client.get("/api/navigation/places").json()
        self.assertEqual(data["count"], 1)

class PlaceGetApiTest(NavigationApiTestBase):
    """GET /api/navigation/places/{place_id}"""

    def test_get_existing_place(self):
        """按编号返回地点详情。"""
        place_id = self.create_place("市中心医院", address="人民路 8 号")
        response = self.client.get(f"/api/navigation/places/{place_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["place"]["name"], "市中心医院")
        self.assertEqual(response.json()["place"]["address"], "人民路 8 号")

    def test_get_missing_place_returns_404(self):
        """不存在的编号返回 404。"""
        response = self.client.get("/api/navigation/places/999")
        self.assertEqual(response.status_code, 404)
        self.assertIn("未找到", response.json()["detail"])

    def test_invalid_id_returns_400(self):
        """非法编号统一返回 400。"""
        for value in ("abc", "0", "-1", "1.5"):
            with self.subTest(place_id=value):
                response = self.client.get(f"/api/navigation/places/{value}")
                self.assertEqual(response.status_code, 400)
                self.assertIn("正整数", response.json()["detail"])


class PlaceUpdateApiTest(NavigationApiTestBase):
    """PUT /api/navigation/places/{place_id}"""

    def setUp(self):
        super().setUp()
        self.place_id = self.create_place(
            "市中心医院",
            category="医院",
            address="人民路 8 号",
            phone="0571-12345678",
            note="门诊楼三楼",
        )

    def test_update_single_field_keeps_others(self):
        """只改电话时其他字段保持不变。"""
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}", json={"phone": "120"}
        )
        self.assertEqual(response.status_code, 200)
        place = response.json()["place"]
        self.assertEqual(place["phone"], "120")
        self.assertEqual(place["name"], "市中心医院")
        self.assertEqual(place["category"], "医院")
        self.assertEqual(place["address"], "人民路 8 号")
        self.assertEqual(place["note"], "门诊楼三楼")

    def test_update_multiple_fields(self):
        """同时修改名称、类别与地址。"""
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}",
            json={"name": "第一人民医院", "category": "医院", "address": "解放路 1 号"},
        )
        place = response.json()["place"]
        self.assertEqual(place["name"], "第一人民医院")
        self.assertEqual(place["address"], "解放路 1 号")

    def test_empty_string_clears_optional_field(self):
        """可选字段传空字符串表示清空。"""
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}",
            json={"address": "", "note": "   "},
        )
        place = response.json()["place"]
        self.assertIsNone(place["address"])
        self.assertIsNone(place["note"])
        self.assertEqual(place["phone"], "0571-12345678")

    def test_blank_name_returns_400_and_keeps_data(self):
        """空名称返回 400，原数据不变。"""
        before = self.db.get_place(self.place_id)
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}", json={"name": "   "}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.get_place(self.place_id), before)

    def test_invalid_category_returns_400_and_keeps_data(self):
        """非法类别返回 400，原数据不变。"""
        before = self.db.get_place(self.place_id)
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}",
            json={"name": "新名字", "category": "超市"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.get_place(self.place_id), before)

    def test_update_missing_place_returns_404(self):
        """不存在的编号返回 404。"""
        response = self.client.put(
            "/api/navigation/places/999", json={"phone": "120"}
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_id_returns_400(self):
        """非法编号返回 400。"""
        response = self.client.put("/api/navigation/places/abc", json={"phone": "120"})
        self.assertEqual(response.status_code, 400)

    def test_empty_payload_returns_400(self):
        """没有任何字段时返回 400，避免误以为保存成功。"""
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}", json={}
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_fields_do_not_change_id_or_created_at(self):
        """请求体里传 id/created_at 也不会被采用。"""
        before = self.db.get_place(self.place_id)
        response = self.client.put(
            f"/api/navigation/places/{self.place_id}",
            json={"id": 999, "created_at": "2020-01-01 00:00:00", "phone": "120"},
        )
        self.assertEqual(response.status_code, 200)
        after = self.db.get_place(self.place_id)
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertEqual(after["phone"], "120")


class PlaceDeleteApiTest(NavigationApiTestBase):
    """DELETE /api/navigation/places/{place_id}"""

    def test_delete_existing_place(self):
        """删除成功返回 deleted 与 place_id。"""
        place_id = self.create_place("市中心医院")
        response = self.client.delete(f"/api/navigation/places/{place_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "place_id": place_id})
        self.assertIsNone(self.db.get_place(place_id))

    def test_delete_missing_place_returns_404(self):
        """不存在的编号返回 404。"""
        response = self.client.delete("/api/navigation/places/999")
        self.assertEqual(response.status_code, 404)

    def test_invalid_id_returns_400(self):
        """非法编号返回 400。"""
        for value in ("abc", "0", "-1"):
            with self.subTest(place_id=value):
                response = self.client.delete(f"/api/navigation/places/{value}")
                self.assertEqual(response.status_code, 400)

    def test_deleted_place_disappears_from_list(self):
        """删除后列表不再包含该地点。"""
        first = self.create_place("甲医院")
        self.create_place("乙药房", category="药房")
        self.client.delete(f"/api/navigation/places/{first}")
        data = self.client.get("/api/navigation/places").json()
        self.assertEqual([p["name"] for p in data["places"]], ["乙药房"])

class NavigationDatabaseInjectionTest(unittest.TestCase):
    """数据库注入与懒加载。"""

    def test_injected_database_is_used(self):
        """注入的内存库直接被接口使用。"""
        db = MedicalNavigationDB(":memory:", check_same_thread=False)
        db.initialize()
        db.create_place("注入医院", category="医院")
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            navigation_db=db,
        )
        client = TestClient(app)
        data = client.get("/api/navigation/places").json()
        self.assertEqual([p["name"] for p in data["places"]], ["注入医院"])

    def test_app_creation_does_not_build_navigation_db(self):
        """只创建应用、不访问接口时不会构造导航数据库。"""
        with mock.patch("agent.web_app.MedicalNavigationDB") as db_class:
            fake_db = db_class.return_value
            fake_db.list_places.return_value = []
            app = create_app(agent_factory=FakeAgent, session_store=FakeStore())
            self.assertEqual(db_class.call_count, 0)

            client = TestClient(app)
            response = client.get("/api/navigation/places")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(db_class.call_count, 1)
        # Web 场景必须关闭同线程限制，由应用内部加锁串行
        db_class.assert_called_once_with(check_same_thread=False)
        fake_db.initialize.assert_called_once_with()

    def test_first_access_creates_database_file(self):
        """首次访问导航接口时才生成 navigation.db。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "navigation.db"
            created = []
            real_class = MedicalNavigationDB

            def factory(*args, **kwargs):
                db = real_class(*args, **kwargs)
                created.append(db)
                return db

            try:
                with mock.patch(
                    "agent.modules.medical_navigation_db.DEFAULT_DB_PATH", target
                ):
                    with mock.patch("agent.web_app.MedicalNavigationDB", factory):
                        app = create_app(
                            agent_factory=FakeAgent, session_store=FakeStore()
                        )
                        self.assertFalse(target.exists())
                        client = TestClient(app)
                        self.assertFalse(target.exists())
                        response = client.get("/api/navigation/places")
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(target.exists())
            finally:
                # Windows 下必须显式关闭连接，否则临时目录无法清理
                for db in created:
                    db.close()


class NavigationConcurrencyTest(NavigationApiTestBase):
    """同一连接上的并发访问受锁保护。"""

    def _list_endpoint(self):
        """取出导航列表接口的处理函数，便于直接多线程调用。"""
        for route in self.client.app.routes:
            methods = getattr(route, "methods", None) or set()
            if (
                getattr(route, "path", "") == "/api/navigation/places"
                and "GET" in methods
            ):
                return route.endpoint
        self.fail("未找到导航列表接口")

    def test_parallel_calls_do_not_overlap_database_access(self):
        """并发调用列表接口时，数据库访问被锁串行化。"""
        endpoint = self._list_endpoint()
        active = []
        overlaps = []
        original = self.db.list_places

        def slow_list_places(*args, **kwargs):
            active.append(1)
            if len(active) > 1:
                overlaps.append(True)
            time.sleep(0.02)
            try:
                return original(*args, **kwargs)
            finally:
                active.pop()

        self.db.list_places = slow_list_places
        errors = []

        def worker():
            try:
                result = endpoint()
                if not isinstance(result, dict):
                    errors.append(result)
            except Exception as exc:  # 显式收集异常，便于断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(overlaps, [])

    def test_concurrent_http_gets_all_succeed(self):
        """多个并发 HTTP 查询都返回 200。"""
        self.create_place("市中心医院")
        statuses = []

        def worker():
            response = self.client.get("/api/navigation/places")
            statuses.append(response.status_code)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(statuses, [200] * 5)


class NavigationCoexistenceTest(unittest.TestCase):
    """导航接口不得影响健康、用药与聊天接口。"""

    def setUp(self):
        FakeAgent.calls = []
        self.navigation_db = MedicalNavigationDB(":memory:", check_same_thread=False)
        self.navigation_db.initialize()
        self.health_db = HealthDB(":memory:", check_same_thread=False)
        self.health_db.initialize()
        self.medication_db = MedicationDB(":memory:", check_same_thread=False)
        self.medication_db.initialize()
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            health_db=self.health_db,
            medication_db=self.medication_db,
            navigation_db=self.navigation_db,
        )
        self.client = TestClient(app)

    def test_health_api_still_works(self):
        """健康档案接口行为不变。"""
        response = self.client.get("/api/health/profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"profile": {}, "exists": False})

    def test_medication_api_still_works(self):
        """用药计划列表接口行为不变。"""
        response = self.client.get("/api/medication/plans")
        self.assertEqual(response.status_code, 200)
        self.assertIn("plans", response.json())

    def test_chat_api_still_works(self):
        """聊天接口仍可正常返回。"""
        response = self.client.post(
            "/api/chat", json={"message": "你好", "session_id": "nav-test"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reply", response.json())

    def test_navigation_api_does_not_call_agent(self):
        """导航接口不触发 Agent 与大模型。"""
        self.client.post(
            "/api/navigation/places", json={"name": "中心医院", "category": "医院"}
        )
        self.client.get("/api/navigation/places")
        self.client.delete("/api/navigation/places/1")
        self.assertEqual(FakeAgent.calls, [])

    def test_navigation_and_health_data_are_isolated(self):
        """导航数据不会写进健康数据库。"""
        self.client.post(
            "/api/navigation/places", json={"name": "中心医院", "category": "医院"}
        )
        self.assertEqual(self.health_db.get_profile(), {})
        self.assertEqual(
            self.navigation_db.list_places()[0]["name"], "中心医院"
        )

# ---------------------------------------------------------------------------
# 高德实时搜索接口（GET /api/navigation/search）
# 全部使用 httpx.MockTransport 离线模拟，不访问高德、不使用真实 Key。
# ---------------------------------------------------------------------------

TEST_AMAP_KEY = "test-amap-key-for-navigation-search"


def json_response(payload, status_code=200):
    """构造 JSON 响应。"""
    return httpx.Response(status_code, json=payload)


def amap_success_payload(pois=None, count=None):
    """构造高德成功响应。"""
    if pois is None:
        pois = [
            {
                "id": "B0FFNAV001",
                "name": "杭州市第一人民医院",
                "type": "医疗保健服务;综合医院;综合医院",
                "address": "浣纱路 261 号",
                "location": "120.164902,30.253000",
                "cityname": "杭州市",
                "adname": "上城区",
                "business": {"tel": "0571-56005600"},
            },
            {
                "id": "B0FFNAV002",
                "name": "上城区社区卫生服务中心",
                "type": "医疗保健服务;卫生院;卫生院",
                "address": "解放路 100 号",
                "location": "120.170000,30.250000",
                "cityname": "杭州市",
                "adname": "上城区",
                "business": [],
            },
        ]
    if count is None:
        count = str(len(pois))
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": count,
        "pois": pois,
    }


def amap_error_payload(infocode, info="错误"):
    """构造高德业务错误响应。"""
    return {
        "status": "0",
        "info": info,
        "infocode": infocode,
        "count": "0",
        "pois": [],
    }


class AmapHandler:
    """记录请求并按预设返回响应或抛出异常。"""

    def __init__(self, response=None, exception=None):
        self.requests = []
        self._response = response
        self._exception = exception

    def __call__(self, request):
        self.requests.append(request)
        if self._exception is not None:
            raise self._exception
        return self._response


class NavigationSearchApiTestBase(unittest.TestCase):
    """为实时搜索接口创建带离线高德客户端的测试应用。"""

    def setUp(self):
        FakeAgent.calls = []
        self.db = MedicalNavigationDB(":memory:", check_same_thread=False)
        self.db.initialize()

    def make_client(self, response=None, exception=None, api_key=TEST_AMAP_KEY):
        """注入 MockTransport 的高德客户端，返回测试客户端。"""
        self.handler = AmapHandler(response=response, exception=exception)
        self.amap = AmapClient(
            api_key=api_key, transport=httpx.MockTransport(self.handler)
        )
        self.addCleanup(self.amap.close)
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            navigation_db=self.db,
            amap_client=self.amap,
        )
        return TestClient(app)

    def params(self, index=0):
        """返回第 index 次高德请求的查询参数。"""
        return self.handler.requests[index].url.params


class NavigationSearchSuccessTest(NavigationSearchApiTestBase):
    """GET /api/navigation/search 的成功路径。"""

    def test_search_returns_multiple_pois(self):
        """正常返回多个 POI，字段与来源标注正确。"""
        client = self.make_client(json_response(amap_success_payload()))
        response = client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 2)
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["data_source"], "高德地图")
        self.assertIn("不构成医疗建议", data["notice"])
        self.assertEqual(
            [item["name"] for item in data["results"]],
            ["杭州市第一人民医院", "上城区社区卫生服务中心"],
        )
        first = data["results"][0]
        self.assertEqual(first["amap_poi_id"], "B0FFNAV001")
        self.assertEqual(first["address"], "浣纱路 261 号")
        self.assertEqual(first["city"], "杭州市")
        self.assertEqual(first["district"], "上城区")
        self.assertEqual(first["phone"], "0571-56005600")
        self.assertEqual(first["category"], "医院")

    def test_search_passes_official_parameters(self):
        """keywords/city/citylimit/types 按官方参数名透传。"""
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search",
            params={
                "keywords": "药店",
                "city": "杭州市",
                "citylimit": "true",
                "types": "090601",
            },
        )
        request = self.handler.requests[0]
        self.assertEqual(request.url.host, "restapi.amap.com")
        self.assertEqual(request.url.path, "/v5/place/text")
        params = self.params()
        self.assertEqual(params["keywords"], "药店")
        self.assertEqual(params["region"], "杭州市")
        self.assertEqual(params["city_limit"], "true")
        self.assertEqual(params["types"], "090601")

    def test_citylimit_defaults_to_true(self):
        """未传 citylimit 时默认严格限制在指定城市内。"""
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(self.params()["city_limit"], "true")

    def test_citylimit_false_is_forwarded(self):
        """citylimit=false 时传 city_limit=false。"""
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search",
            params={"keywords": "医院", "city": "杭州市", "citylimit": "false"},
        )
        self.assertEqual(self.params()["city_limit"], "false")

    def test_types_omitted_is_not_sent(self):
        """未传 types 时不发送该参数。"""
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertNotIn("types", self.params())

    def test_multiple_types_separated_by_pipe(self):
        """多个 typecode 用 | 分隔可以正常透传。"""
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search",
            params={
                "keywords": "医疗",
                "city": "杭州市",
                "types": "090100|090200",
            },
        )
        self.assertEqual(self.params()["types"], "090100|090200")

    def test_empty_result_is_success(self):
        """高德返回 0 条属于正常结果，不是错误。"""
        client = self.make_client(
            json_response(amap_success_payload(pois=[], count="0"))
        )
        response = client.get(
            "/api/navigation/search",
            params={"keywords": "不存在的医院", "city": "杭州市"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["results"], [])
        self.assertEqual(data["count"], 0)

    def test_search_does_not_write_database(self):
        """实时搜索不写入、不修改 SQLite 中的用户地点。"""
        self.db.create_place("已有医院", category="医院")
        before = self.db.list_places()
        client = self.make_client(json_response(amap_success_payload()))
        client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(self.db.list_places(), before)
        self.assertEqual(len(self.db.list_places()), 1)

class NavigationSearchValidationTest(NavigationSearchApiTestBase):
    """实时搜索的参数校验：非法参数不发起高德请求。"""

    def setUp(self):
        super().setUp()
        self.client = self.make_client(json_response(amap_success_payload()))

    def test_missing_keywords_returns_400(self):
        """缺少 keywords 返回 400。"""
        response = self.client.get(
            "/api/navigation/search", params={"city": "杭州市"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.handler.requests, [])

    def test_blank_keywords_returns_400(self):
        """keywords 为空白返回 400。"""
        response = self.client.get(
            "/api/navigation/search", params={"keywords": "   ", "city": "杭州市"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_keywords_too_long_returns_400(self):
        """keywords 超过 80 字返回 400。"""
        response = self.client.get(
            "/api/navigation/search",
            params={"keywords": "医" * 81, "city": "杭州市"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_keywords_at_max_length_accepted(self):
        """keywords 恰好 80 字可以请求。"""
        response = self.client.get(
            "/api/navigation/search",
            params={"keywords": "医" * 80, "city": "杭州市"},
        )
        self.assertEqual(response.status_code, 200)

    def test_missing_city_returns_400(self):
        """缺少 city 返回 400。"""
        response = self.client.get(
            "/api/navigation/search", params={"keywords": "医院"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_blank_city_returns_400(self):
        """city 为空白返回 400。"""
        response = self.client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "  "}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_city_too_long_returns_400(self):
        """city 超过 20 字返回 400。"""
        response = self.client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "城" * 21}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_invalid_citylimit_returns_400(self):
        """citylimit 只接受 true/false。"""
        for value in ("yes", "1", "on", ""):
            with self.subTest(citylimit=value):
                response = self.client.get(
                    "/api/navigation/search",
                    params={
                        "keywords": "医院",
                        "city": "杭州市",
                        "citylimit": value,
                    },
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("citylimit", response.json()["detail"])
        self.assertEqual(self.handler.requests, [])

    def test_invalid_types_format_returns_400(self):
        """types 不是 6 位数字编码时返回 400。"""
        for value in ("abc", "09060", "0906011", "090601|abc", "090601|"):
            with self.subTest(types=value):
                response = self.client.get(
                    "/api/navigation/search",
                    params={
                        "keywords": "医院",
                        "city": "杭州市",
                        "types": value,
                    },
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

    def test_types_too_long_returns_400(self):
        """types 总长度超限时返回 400。"""
        value = "|".join(["090601"] * 21)
        response = self.client.get(
            "/api/navigation/search",
            params={"keywords": "医院", "city": "杭州市", "types": value},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.handler.requests, [])

class NavigationSearchErrorTest(NavigationSearchApiTestBase):
    """高德失败与网络异常的错误分类映射。"""

    def search(self, response=None, exception=None):
        client = self.make_client(response=response, exception=exception)
        return client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )

    def test_auth_failed_returns_502(self):
        """10001 Key 无效映射为 502 + AUTH_FAILED。"""
        response = self.search(
            response=json_response(amap_error_payload("10001", "INVALID_USER_KEY"))
        )
        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["error_code"], "AUTH_FAILED")
        self.assertIn("Key", body["detail"])
        self.assertNotIn("results", body)
        self.assertNotIn("count", body)

    def test_quota_exceeded_returns_429(self):
        """日配额用尽映射为 429 + QUOTA_EXCEEDED。"""
        response = self.search(
            response=json_response(
                amap_error_payload("10003", "DAILY_QUERY_OVER_LIMIT")
            )
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error_code"], "QUOTA_EXCEEDED")

    def test_unknown_infocode_returns_502(self):
        """未知 infocode 走安全兜底，不猜测含义。"""
        response = self.search(
            response=json_response(amap_error_payload("99999", "UNKNOWN"))
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "INVALID_RESPONSE")

    def test_route_planning_code_is_not_reported_as_no_results(self):
        """20800 属于路径规划错误，不得表现为“没有找到”。"""
        response = self.search(
            response=json_response(amap_error_payload("20800", "OUT_OF_SERVICE"))
        )
        self.assertEqual(response.status_code, 502)
        body = response.json()
        self.assertEqual(body["error_code"], "INVALID_RESPONSE")
        self.assertNotIn("没有找到", body["detail"])
        self.assertNotIn("count", body)

    def test_network_timeout_returns_504(self):
        """请求超时映射为 504 + TIMEOUT。"""
        response = self.search(exception=httpx.ReadTimeout("超时"))
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error_code"], "TIMEOUT")

    def test_network_error_returns_502(self):
        """连接失败映射为 502 + NETWORK_ERROR。"""
        response = self.search(exception=httpx.ConnectError("连接失败"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "NETWORK_ERROR")

    def test_http_error_returns_502(self):
        """HTTP 非 200 映射为 502 + HTTP_ERROR。"""
        response = self.search(response=httpx.Response(500, text="busy"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "HTTP_ERROR")

    def test_invalid_json_returns_502(self):
        """返回非法 JSON 映射为 502 + INVALID_RESPONSE。"""
        response = self.search(response=httpx.Response(200, text="not-json"))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error_code"], "INVALID_RESPONSE")

    def test_missing_key_returns_503(self):
        """未配置 Key 时返回 503，且不发起任何请求。"""
        client = self.make_client(
            response=json_response(amap_success_payload()), api_key=""
        )
        response = client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "CONFIG_MISSING")
        self.assertEqual(self.handler.requests, [])

class NavigationSearchConcurrencyTest(NavigationSearchApiTestBase):
    """并发实时搜索：共享客户端下所有请求都能正常返回。"""

    def test_concurrent_searches_all_succeed(self):
        """多个并发搜索都返回 200，且每次请求都真正发出。"""
        client = self.make_client(json_response(amap_success_payload()))
        statuses = []
        errors = []

        def worker():
            try:
                response = client.get(
                    "/api/navigation/search",
                    params={"keywords": "医院", "city": "杭州市"},
                )
                statuses.append(response.status_code)
            except Exception as exc:  # 显式收集异常，便于断言
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(statuses, [200] * 5)
        self.assertEqual(len(self.handler.requests), 5)


class NavigationSearchCoexistenceTest(unittest.TestCase):
    """实时搜索不得影响手动地点 CRUD、健康、用药与聊天接口。"""

    def setUp(self):
        FakeAgent.calls = []
        self.navigation_db = MedicalNavigationDB(":memory:", check_same_thread=False)
        self.navigation_db.initialize()
        self.health_db = HealthDB(":memory:", check_same_thread=False)
        self.health_db.initialize()
        self.medication_db = MedicationDB(":memory:", check_same_thread=False)
        self.medication_db.initialize()
        self.handler = AmapHandler(response=json_response(amap_success_payload()))
        self.amap = AmapClient(
            api_key=TEST_AMAP_KEY, transport=httpx.MockTransport(self.handler)
        )
        app = create_app(
            agent_factory=FakeAgent,
            session_store=FakeStore(),
            health_db=self.health_db,
            medication_db=self.medication_db,
            navigation_db=self.navigation_db,
            amap_client=self.amap,
        )
        self.client = TestClient(app)

    def tearDown(self):
        self.amap.close()

    def test_health_api_still_works(self):
        """健康档案接口行为不变。"""
        response = self.client.get("/api/health/profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"profile": {}, "exists": False})

    def test_medication_api_still_works(self):
        """用药计划列表接口行为不变。"""
        response = self.client.get("/api/medication/plans")
        self.assertEqual(response.status_code, 200)
        self.assertIn("plans", response.json())

    def test_chat_api_still_works(self):
        """聊天接口仍可正常返回。"""
        response = self.client.post(
            "/api/chat", json={"message": "你好", "session_id": "nav-search-test"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reply", response.json())

    def test_manual_place_crud_still_works(self):
        """实时搜索前后，手动地点的增删改查都正常。"""
        created = self.client.post(
            "/api/navigation/places", json={"name": "中心医院", "category": "医院"}
        )
        self.assertEqual(created.status_code, 201)
        place_id = created.json()["place"]["id"]

        self.client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(
            self.client.get("/api/navigation/places").json()["count"], 1
        )
        updated = self.client.put(
            f"/api/navigation/places/{place_id}", json={"phone": "120"}
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            self.client.delete(f"/api/navigation/places/{place_id}").status_code, 200
        )

    def test_search_does_not_create_user_place(self):
        """实时搜索结果不会变成用户收藏地点。"""
        self.client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(self.navigation_db.list_places(), [])

    def test_search_does_not_call_agent(self):
        """实时搜索不触发 Agent 与大模型。"""
        self.client.get(
            "/api/navigation/search", params={"keywords": "医院", "city": "杭州市"}
        )
        self.assertEqual(FakeAgent.calls, [])