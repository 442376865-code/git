"""健康管理模块（档案 + 体重记录）的单元测试。

覆盖：接口不变、档案新增与查询、出生日期校验、体重指定日期新增/列表/删除、
非法数字、非法日期、不存在编号、SQLite 重开持久化、SQL 注入文本、
旧库迁移与幂等。
全部使用内存库或临时文件，离线执行，不访问网络。
"""

import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import date
from unittest import mock

from agent.main import Agent, EMERGENCY_REPLY
from agent.modules.base import MedicalModule
from agent.modules.health_db import HealthDB
from agent.modules.health_management import (
    CANCEL_NOTHING_MESSAGE,
    CLEAR_CANCELLED_MESSAGE,
    CLEAR_CONFIRM_MESSAGE,
    CLEAR_NOTHING_MESSAGE,
    CLEARED_MESSAGE,
    HELP_MESSAGE,
    HealthManagementModule,
    RECORD_NOTICE,
)
from agent.router import Router


class HealthManagementModuleTest(unittest.TestCase):
    """健康档案与体重记录的录入、查询、删除测试。"""

    def setUp(self):
        # 使用内存库，避免测试污染项目 data 目录
        self.module = HealthManagementModule(db_path=":memory:")

    def test_interface_unchanged(self):
        """模块必须继承基类，run 签名保持 (self, user_input, context=None)。"""
        self.assertIsInstance(self.module, MedicalModule)
        self.assertEqual(self.module.name, "health_management")
        params = list(inspect.signature(HealthManagementModule.run).parameters)
        self.assertEqual(params, ["self", "user_input", "context"])

    def test_empty_returns_help(self):
        """空输入与非字符串输入返回帮助提示，不抛异常。"""
        for value in ("", "   ", None, 123):
            with self.subTest(value=value):
                self.assertEqual(self.module.run(value), HELP_MESSAGE)

    def test_profile_insert_and_query(self):
        """录入姓名、性别、出生日期、身高后应能查询到。"""
        self.module.run("姓名 张三、性别 男、出生日期 1990-05-20、身高 175cm")
        profile = self.module.db.get_profile()
        self.assertEqual(profile["name"], "张三")
        self.assertEqual(profile["gender"], "男")
        self.assertEqual(profile["birth_date"], "1990-05-20")
        self.assertEqual(profile["height_cm"], 175.0)

        result = self.module.run("查看健康档案")
        self.assertIn("【个人健康档案】", result)
        self.assertIn("姓名：张三", result)
        self.assertIn("出生日期：1990-05-20", result)
        self.assertIn("身高：175.0 cm", result)
        self.assertIn(RECORD_NOTICE, result)

    def test_profile_partial_update(self):
        """分步录入不得覆盖已有字段。"""
        self.module.run("姓名 张三、身高 170cm")
        self.module.run("身高 175cm")
        profile = self.module.db.get_profile()
        self.assertEqual(profile["name"], "张三")
        self.assertEqual(profile["height_cm"], 175.0)

    def test_invalid_birth_date_format(self):
        """出生日期格式错误时必须提示，且不写入。"""
        result = self.module.run("出生日期 1990/05/20")
        self.assertIn("YYYY-MM-DD", result)
        self.assertNotIn("birth_date", self.module.db.get_profile())

    def test_invalid_birth_date_not_exist(self):
        """不存在的日期（2 月 30 日）必须提示。"""
        result = self.module.run("出生日期 1990-02-30")
        self.assertIn("不正确或不存在", result)
        self.assertNotIn("birth_date", self.module.db.get_profile())

    def test_future_birth_date_rejected(self):
        """出生日期晚于今天必须拒绝。"""
        future = date.today().replace(year=date.today().year + 1).isoformat()
        result = self.module.run(f"出生日期 {future}")
        self.assertIn("不能晚于今天", result)
        self.assertNotIn("birth_date", self.module.db.get_profile())

    def test_invalid_height_rejected(self):
        """身高超出合理范围时必须拒绝写入。"""
        result = self.module.run("身高 300cm")
        self.assertIn("不太合理", result)
        self.assertEqual(self.module.db.get_profile(), {})

    def test_view_profile_before_record(self):
        """未建立档案时查看应提示尚未建立。"""
        self.assertIn("尚未建立健康档案", self.module.run("查看健康档案"))

    def test_weight_add_default_today(self):
        """体重缺省日期应为当天。"""
        self.module.run("体重 70kg")
        records = self.module.db.list_weights()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["weight_kg"], 70.0)
        self.assertEqual(records[0]["record_date"], date.today().isoformat())

    def test_weight_add_with_date_and_note(self):
        """体重可指定记录日期和备注。"""
        result = self.module.run("体重 72.5kg 日期 2026-11-01 备注 晨起")
        self.assertIn("已新增体重记录", result)
        record = self.module.db.list_weights()[0]
        self.assertEqual(record["weight_kg"], 72.5)
        self.assertEqual(record["record_date"], "2026-11-01")
        self.assertEqual(record["note"], "晨起")

    def test_weight_list(self):
        """查看体重记录应按日期倒序展示。"""
        self.module.run("体重 70kg 日期 2026-11-01")
        self.module.run("体重 71kg 日期 2026-11-02")
        result = self.module.run("查看体重记录")
        self.assertIn("【体重记录】", result)
        self.assertIn("日期 2026-11-02", result)
        self.assertIn("日期 2026-11-01", result)
        self.assertLess(result.index("2026-11-02"), result.index("2026-11-01"))

    def test_weight_list_empty(self):
        """无体重记录时查看应给出明确提示。"""
        self.assertEqual(self.module.run("查看体重记录"), "暂无体重记录。")

    def test_weight_delete(self):
        """按编号删除体重记录。"""
        record_id = self.module.db.add_weight(70.0, "2026-11-01", "测试")
        result = self.module.run(f"删除体重记录 {record_id}")
        self.assertIn(f"已删除编号 {record_id}", result)
        self.assertEqual(self.module.db.list_weights(), [])

    def test_weight_delete_missing_id(self):
        """删除不存在的编号应给出明确提示。"""
        result = self.module.run("删除体重记录 999")
        self.assertIn("未找到编号为 999", result)

    def test_weight_delete_without_id(self):
        """删除体重记录但缺少编号时应提示提供编号。"""
        result = self.module.run("删除体重记录")
        self.assertIn("请提供要删除的体重记录编号", result)

    def test_invalid_weight_number(self):
        """体重超出合理范围时必须拒绝写入。"""
        result = self.module.run("体重 500kg")
        self.assertIn("不太合理", result)
        self.assertEqual(self.module.db.list_weights(), [])

    def test_invalid_record_date(self):
        """体重记录日期不合法时必须提示且不写入。"""
        result = self.module.run("体重 70kg 日期 2026-13-01")
        self.assertIn("日期格式不正确或不存在", result)
        self.assertEqual(self.module.db.list_weights(), [])

    def test_record_date_wrong_format(self):
        """体重记录日期格式错误时必须提示。"""
        result = self.module.run("体重 70kg 日期 20261101")
        self.assertIn("YYYY-MM-DD", result)
        self.assertEqual(self.module.db.list_weights(), [])

    def test_no_bmi_or_health_judgement(self):
        """输出不得出现 BMI 或健康分类等判断性内容。"""
        self.module.run("姓名 张三、身高 175cm")
        self.module.run("体重 70kg")
        profile_text = self.module.run("查看健康档案")
        weight_text = self.module.run("查看体重记录")
        for text in (profile_text, weight_text):
            for keyword in ("BMI", "偏瘦", "超重", "肥胖", "正常范围"):
                self.assertNotIn(keyword, text)

    def test_clear_all_requires_confirmation(self):
        """清空必须先确认：首次只提示，确认后才删除档案与体重记录。"""
        self.module.run("姓名 张三、身高 175cm")
        self.module.run("体重 70kg")
        self.assertEqual(self.module.run("清除健康档案"), CLEAR_CONFIRM_MESSAGE)
        # 首次请求不得删除任何数据
        self.assertEqual(self.module.db.get_profile()["name"], "张三")
        self.assertEqual(len(self.module.db.list_weights()), 1)
        self.assertEqual(self.module.run("确认清空"), CLEARED_MESSAGE)
        self.assertEqual(self.module.db.get_profile(), {})
        self.assertEqual(self.module.db.list_weights(), [])

    def test_sql_injection_note_not_executed(self):
        """备注中的 SQL 片段必须按文本存储，不得执行。"""
        payload = "1'); DROP TABLE weight_records; --"
        self.module.run(f"体重 70kg 备注 {payload}")
        records = self.module.db.list_weights()
        self.assertEqual(len(records), 1)
        self.assertIn("DROP TABLE", records[0]["note"])
        # 表仍然可用
        self.assertEqual(len(self.module.db.list_weights()), 1)

    def test_sql_injection_name_not_executed(self):
        """姓名只接受中文/字母，注入片段被截断且表仍可用。"""
        self.module.run("姓名 张三'; DROP TABLE health_profile; --")
        profile = self.module.db.get_profile()
        self.assertEqual(profile["name"], "张三")
        self.assertIn("name", profile)

    def test_unrecognized_input_returns_help(self):
        """无法识别的输入返回帮助提示。"""
        self.assertEqual(self.module.run("今天心情不错"), HELP_MESSAGE)


class HealthDBPersistenceAndMigrationTest(unittest.TestCase):
    """SQLite 重开持久化与旧库迁移测试（临时目录，不污染项目）。"""

    def test_persistence_across_instances(self):
        """关闭连接后重新打开，档案与体重记录仍然存在。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.db")

            db = HealthDB(path)
            db.initialize()
            db.update_profile(name="张三", gender="男", birth_date="1990-05-20", height_cm=175.0)
            record_id = db.add_weight(70.0, "2026-11-01", "晨起")
            db.close()

            db2 = HealthDB(path)
            db2.initialize()
            profile = db2.get_profile()
            self.assertEqual(profile["name"], "张三")
            self.assertEqual(profile["birth_date"], "1990-05-20")
            weights = db2.list_weights()
            self.assertEqual(len(weights), 1)
            self.assertEqual(weights[0]["id"], record_id)
            self.assertEqual(weights[0]["weight_kg"], 70.0)
            db2.close()

    def test_legacy_database_migration(self):
        """旧版库应补齐新列、保留旧数据并迁移旧体重。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.db")

            # 构造旧版表结构与数据
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE health_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    gender TEXT,
                    age INTEGER,
                    height_cm REAL,
                    weight_kg REAL,
                    medical_history TEXT,
                    updated_at TEXT
                );
                CREATE TABLE health_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    value REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                INSERT INTO health_profile
                    (id, gender, age, height_cm, weight_kg, medical_history, updated_at)
                VALUES (1, '男', 30, 175.0, 70.0, '无', '2026-01-01 08:00:00');
                INSERT INTO health_metrics (metric, value, recorded_at)
                VALUES ('weight_kg', 70.0, '2026-01-01 08:00:00');
                """
            )
            conn.commit()
            conn.close()

            db = HealthDB(path)
            try:
                db.initialize()

                # 新列可写入，旧字段仍保留
                profile = db.update_profile(name="张三", birth_date="1990-05-20")
                self.assertEqual(profile["name"], "张三")
                self.assertEqual(profile["birth_date"], "1990-05-20")
                self.assertEqual(profile["gender"], "男")
                self.assertEqual(profile["height_cm"], 175.0)

                # 旧体重已迁移为一条体重记录
                weights = db.list_weights()
                self.assertEqual(len(weights), 1)
                self.assertEqual(weights[0]["weight_kg"], 70.0)
                self.assertEqual(weights[0]["record_date"], "2026-01-01")

                # 重复初始化不得重复迁移
                db.initialize()
                self.assertEqual(len(db.list_weights()), 1)

                # 旧表仍然存在
                tables = {
                    row[0]
                    for row in db._conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertIn("health_metrics", tables)
            finally:
                db.close()

    @staticmethod
    def _create_legacy_db(path):
        """构造一个旧版健康库：只有旧字段，没有 schema_migrations 表。"""
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE health_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                gender TEXT,
                age INTEGER,
                height_cm REAL,
                weight_kg REAL,
                medical_history TEXT,
                updated_at TEXT
            );
            CREATE TABLE health_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );
            INSERT INTO health_profile
                (id, gender, age, height_cm, weight_kg, medical_history, updated_at)
            VALUES (1, '男', 30, 175.0, 70.0, '无', '2026-01-01 08:00:00');
            INSERT INTO health_metrics (metric, value, recorded_at)
            VALUES ('weight_kg', 70.0, '2026-01-01 08:00:00');
            """
        )
        conn.commit()
        conn.close()

    def test_repeated_initialize_migrates_once(self):
        """连续多次初始化只迁移一次，迁移标记只有一行。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.db")
            self._create_legacy_db(path)

            db = HealthDB(path)
            try:
                for _ in range(3):
                    db.initialize()
                self.assertEqual(len(db.list_weights()), 1)
                count = db._conn.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE key = ?",
                    ("legacy_weight_v1",),
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                db.close()

    def test_deleted_migration_record_not_recreated(self):
        """删除迁移记录后重开数据库，记录保持为 0，不会重新出现。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.db")
            self._create_legacy_db(path)

            db = HealthDB(path)
            try:
                db.initialize()
                records = db.list_weights()
                self.assertEqual(len(records), 1)
                self.assertTrue(db.delete_weight(records[0]["id"]))
                self.assertEqual(db.list_weights(), [])
            finally:
                db.close()

            # 关闭后重新打开并初始化，不得重新生成迁移记录
            db2 = HealthDB(path)
            try:
                db2.initialize()
                self.assertEqual(db2.list_weights(), [])
            finally:
                db2.close()

    def test_already_migrated_without_marker_only_adds_marker(self):
        """已迁移但缺标记的库只补标记，不新增体重记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "health.db")

            # 模拟当前版本：体重记录已迁移，但没有 schema_migrations 标记
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE health_profile (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    gender TEXT,
                    age INTEGER,
                    height_cm REAL,
                    weight_kg REAL,
                    medical_history TEXT,
                    updated_at TEXT
                );
                INSERT INTO health_profile
                    (id, gender, age, height_cm, weight_kg, medical_history, updated_at)
                VALUES (1, '男', 30, 175.0, 70.0, '无', '2026-01-01 08:00:00');
                CREATE TABLE weight_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    weight_kg REAL NOT NULL,
                    record_date TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO weight_records (weight_kg, record_date, note, created_at)
                VALUES (70.0, '2026-01-01', '由旧版本迁移', '2026-01-01 08:00:00');
                """
            )
            conn.commit()
            conn.close()

            db = HealthDB(path)
            try:
                db.initialize()
                # 不新增记录
                self.assertEqual(len(db.list_weights()), 1)
                # 只补写迁移标记
                marker = db._conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE key = ?",
                    ("legacy_weight_v1",),
                ).fetchone()
                self.assertIsNotNone(marker)
            finally:
                db.close()

    def test_update_unknown_field_ignored(self):
        """更新时传入未知字段应被忽略，不得报错。"""
        db = HealthDB(":memory:")
        db.initialize()
        profile = db.update_profile(name="张三", unknown_field="x")
        self.assertNotIn("unknown_field", profile)
        self.assertEqual(profile["name"], "张三")
        db.close()


class RecordingModule:
    """测试替身：只记录 run 调用次数，用于确认其他模块未被触发。"""

    def __init__(self, name):
        self.name = name
        self.calls = 0

    def run(self, user_input, context=None):
        self.calls += 1
        return "[" + self.name + "]"


class HealthClearConfirmationTest(unittest.TestCase):
    """健康档案清空的二次确认机制。"""

    def setUp(self):
        self.module = HealthManagementModule(db_path=":memory:")
        self.module.run("姓名 张三、身高 175cm")
        self.module.run("体重 70kg")

    def _assert_data_kept(self):
        """断言档案与体重记录都还在。"""
        self.assertEqual(self.module.db.get_profile()["name"], "张三")
        self.assertEqual(len(self.module.db.list_weights()), 1)

    def test_first_request_only_prompts(self):
        """五种清空表达在首次输入时都只返回确认提示，不删除数据。"""
        for text in (
            "清空健康档案",
            "清除健康档案",
            "清空档案",
            "清空记录",
            "删除健康档案",
        ):
            with self.subTest(text=text):
                self.module._pending_clear = False
                self.assertEqual(self.module.run(text), CLEAR_CONFIRM_MESSAGE)
                self._assert_data_kept()

    def test_confirm_with_pending_deletes(self):
        """有待确认状态时，确认后才删除档案和体重记录。"""
        self.module.run("清空健康档案")
        self.assertEqual(self.module.run("确认清空"), CLEARED_MESSAGE)
        self.assertEqual(self.module.db.get_profile(), {})
        self.assertEqual(self.module.db.list_weights(), [])

    def test_confirm_without_pending_does_not_delete(self):
        """没有待确认状态时，单独确认不得删除数据。"""
        self.assertEqual(self.module.run("确认清空"), CLEAR_NOTHING_MESSAGE)
        self._assert_data_kept()

    def test_cancel_with_pending_does_not_delete(self):
        """有待确认状态时取消，只清除状态，不删除数据。"""
        self.module.run("清空健康档案")
        self.assertEqual(self.module.run("取消"), CLEAR_CANCELLED_MESSAGE)
        self._assert_data_kept()

    def test_cancel_without_pending_does_not_delete(self):
        """没有待确认状态时取消，返回固定提示且不删除数据。"""
        self.assertEqual(self.module.run("取消"), CANCEL_NOTHING_MESSAGE)
        self.assertEqual(self.module.run("取消清空"), CANCEL_NOTHING_MESSAGE)
        self._assert_data_kept()

    def test_cancel_then_confirm_does_not_delete(self):
        """取消后再次确认，不得删除数据。"""
        self.module.run("清空健康档案")
        self.module.run("取消")
        self.assertEqual(self.module.run("确认清空"), CLEAR_NOTHING_MESSAGE)
        self._assert_data_kept()

    def test_two_instances_isolated(self):
        """两个模块实例的待确认状态互不影响。"""
        other = HealthManagementModule(db_path=":memory:")
        other.run("姓名 李四")
        self.module.run("清空健康档案")
        self.assertTrue(self.module._pending_clear)
        self.assertFalse(other._pending_clear)
        self.assertEqual(other.run("确认清空"), CLEAR_NOTHING_MESSAGE)
        self.assertEqual(other.db.get_profile()["name"], "李四")

    def test_other_session_confirm_does_not_delete(self):
        """一个会话发起清空，另一个会话确认不得删除发起会话的数据。"""
        session_a = HealthManagementModule(db_path=":memory:")
        session_b = HealthManagementModule(db_path=":memory:")
        session_a.run("姓名 张三")
        session_b.run("姓名 李四")
        session_a.run("清空健康档案")
        self.assertEqual(session_b.run("确认清空"), CLEAR_NOTHING_MESSAGE)
        self.assertEqual(session_a.db.get_profile()["name"], "张三")
        self.assertEqual(session_b.db.get_profile()["name"], "李四")
        # 只有发起会话自己确认才生效
        self.assertEqual(session_a.run("确认清空"), CLEARED_MESSAGE)
        self.assertEqual(session_a.db.get_profile(), {})
        self.assertEqual(session_b.db.get_profile()["name"], "李四")


class HealthClearAgentIntegrationTest(unittest.TestCase):
    """主控 Agent 层：确认流程不调用大模型，也不影响其他模块。"""

    def _agent(self, extra_modules=None):
        modules = {"health_management": HealthManagementModule(db_path=":memory:")}
        if extra_modules:
            modules.update(extra_modules)
        return Agent(modules=modules), modules

    def test_confirm_flow_does_not_call_llm(self):
        """清空与确认流程全程不调用大模型。"""
        agent, _ = self._agent()
        with mock.patch(
            "agent.main.call_llm",
            return_value={"role": "assistant", "content": "mock"},
        ) as fake:
            self.assertEqual(agent.run("清空健康档案"), CLEAR_CONFIRM_MESSAGE)
            self.assertEqual(agent.run("确认清空"), CLEARED_MESSAGE)
            fake.assert_not_called()

    def test_confirm_or_cancel_without_pending_no_llm(self):
        """没有待确认状态时，确认和取消也不得掉入大模型。"""
        agent, _ = self._agent()
        with mock.patch(
            "agent.main.call_llm",
            return_value={"role": "assistant", "content": "mock"},
        ) as fake:
            self.assertEqual(agent.run("确认清空"), CLEAR_NOTHING_MESSAGE)
            self.assertEqual(agent.run("取消"), CANCEL_NOTHING_MESSAGE)
            fake.assert_not_called()

    def test_clear_does_not_touch_other_modules(self):
        """清空确认流程只调用健康管理模块。"""
        others = {
            name: RecordingModule(name)
            for name in ("medical_knowledge", "medication", "medical_navigation")
        }
        agent, _ = self._agent(others)
        agent.run("清空健康档案")
        agent.run("确认清空")
        self.assertTrue(all(module.calls == 0 for module in others.values()))

    def test_qingkong_jilu_routes_to_health(self):
        """「清空记录」必须进入健康管理模块，而不是通用问答。"""
        self.assertEqual(Router().route("清空记录"), "health_management")
        agent, _ = self._agent()
        with mock.patch(
            "agent.main.call_llm",
            return_value={"role": "assistant", "content": "mock"},
        ) as fake:
            self.assertEqual(agent.run("清空记录"), CLEAR_CONFIRM_MESSAGE)
            fake.assert_not_called()

    def test_normal_health_still_works(self):
        """普通健康管理功能不受二次确认影响。"""
        agent, _ = self._agent()
        self.assertIn("已更新健康档案", agent.run("姓名 张三、身高 175cm"))
        self.assertIn("张三", agent.run("查看健康档案"))
        self.assertIn("已新增体重记录", agent.run("体重 70kg"))
        self.assertIn("已删除", agent.run("删除体重记录 1"))

    def test_emergency_interception_unchanged(self):
        """危急症状拦截仍是第一优先级，且不调用大模型。"""
        agent, _ = self._agent()
        with mock.patch(
            "agent.main.call_llm",
            return_value={"role": "assistant", "content": "mock"},
        ) as fake:
            self.assertEqual(agent.run("我胸痛怎么办"), EMERGENCY_REPLY)
            fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()