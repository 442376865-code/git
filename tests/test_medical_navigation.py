"""医疗导航模块（离线 MVP）的单元测试。

覆盖：接口不变、空输入、地点新增/查看/名称搜索/类型搜索/删除/重复删除、
缺名称、非法类型、非法编号、文件库重开持久化、SQL 注入文本按普通文本保存、
实时导航请求的安全回退、已保存地点同时列出、急诊强提示、
以及不写入其他业务数据库。
全部使用内存库或临时文件，离线执行，不访问网络。
"""

import inspect

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from agent.modules.base import MedicalModule
from agent.modules.medical_navigation import (
    EMERGENCY_REPLY,
    HELP_MESSAGE,
    NO_MAP_REPLY,
    RECORD_NOTICE,
    MedicalNavigationModule,
)
from agent.modules.medical_navigation_db import MedicalNavigationDB


class MedicalNavigationDBTest(unittest.TestCase):
    """就医地点的增删查与搜索（数据库层）。"""

    def setUp(self):
        self.db = MedicalNavigationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_create_place_returns_id_and_defaults(self):
        """新增地点返回编号，类型默认"其他"。"""
        place_id = self.db.create_place("某社区卫生站")
        self.assertIsInstance(place_id, int)
        place = self.db.get_place(place_id)
        self.assertEqual(place["name"], "某社区卫生站")
        self.assertEqual(place["category"], "其他")
        self.assertIsNone(place["address"])

    def test_get_missing_place_returns_none(self):
        """不存在的编号返回 None。"""
        self.assertIsNone(self.db.get_place(999))

    def test_list_places_in_id_order(self):
        """列表按编号升序返回。"""
        first = self.db.create_place("甲医院", category="医院")
        second = self.db.create_place("乙药房", category="药房")
        ids = [place["id"] for place in self.db.list_places()]
        self.assertEqual(ids, [first, second])

    def test_search_by_category(self):
        """按类型搜索只返回该类型。"""
        self.db.create_place("甲医院", category="医院")
        self.db.create_place("乙药房", category="药房")
        results = self.db.search_places("医院")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "甲医院")

    def test_search_by_name(self):
        """按名称片段搜索。"""
        self.db.create_place("中心医院", category="医院")
        self.db.create_place("社区诊所", category="诊所")
        results = self.db.search_places("中心")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["name"], "中心医院")

    def test_search_empty_keyword_returns_empty(self):
        """空关键词不返回全部数据。"""
        self.db.create_place("甲医院", category="医院")
        self.assertEqual(self.db.search_places(""), [])

    def test_delete_place(self):
        """删除成功返回 True，之后查不到。"""
        place_id = self.db.create_place("甲医院", category="医院")
        self.assertTrue(self.db.delete_place(place_id))
        self.assertIsNone(self.db.get_place(place_id))

    def test_delete_missing_returns_false(self):
        """删除不存在的编号返回 False。"""
        self.assertFalse(self.db.delete_place(999))

class MedicalNavigationModuleTest(unittest.TestCase):
    """模块的意图识别、字段校验与安全边界。"""

    def setUp(self):
        self.module = MedicalNavigationModule(db_path=":memory:")
        self.addCleanup(self.module.db.close)

    def test_interface_unchanged(self):
        """继承基类，run 签名保持 (self, user_input, context=None)。"""
        self.assertIsInstance(self.module, MedicalModule)
        self.assertEqual(self.module.name, "medical_navigation")
        params = list(inspect.signature(MedicalNavigationModule.run).parameters)
        self.assertEqual(params, ["self", "user_input", "context"])

    def test_empty_returns_help(self):
        """空输入与非字符串返回帮助提示。"""
        self.assertEqual(self.module.run(""), HELP_MESSAGE)
        self.assertEqual(self.module.run("   "), HELP_MESSAGE)
        self.assertEqual(self.module.run(None), HELP_MESSAGE)

    def test_add_place_via_run(self):
        """按示例句式可新增地点，字段完整保存。"""
        reply = self.module.run(
            "添加就医地点，类型 医院，名称 市中心医院，地址 人民路 8 号，电话 0571-12345678"
        )
        self.assertIn("已添加就医地点", reply)
        places = self.module.db.list_places()
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["name"], "市中心医院")
        self.assertEqual(places[0]["category"], "医院")
        self.assertEqual(places[0]["address"], "人民路 8 号")
        self.assertEqual(places[0]["phone"], "0571-12345678")

    def test_add_place_missing_name(self):
        """缺名称时不写库。"""
        reply = self.module.run("添加就医地点，类型 医院")
        self.assertIn("请提供就医地点名称", reply)
        self.assertEqual(self.module.db.list_places(), [])

    def test_add_place_default_category(self):
        """未给类型时默认"其他"。"""
        self.module.run("添加就医地点，名称 某卫生站")
        places = self.module.db.list_places()
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["category"], "其他")

    def test_add_place_invalid_category(self):
        """非法类型不写库并说明支持范围。"""
        reply = self.module.run("添加就医地点，类型 超市，名称 某商店")
        self.assertIn("类型仅支持", reply)
        self.assertEqual(self.module.db.list_places(), [])

    def test_view_places(self):
        """查看全部返回列表与中性提示。"""
        self.module.db.create_place("市中心医院", category="医院")
        reply = self.module.run("查看就医地点")
        self.assertIn("【就医地点】", reply)
        self.assertIn("市中心医院", reply)
        self.assertIn(RECORD_NOTICE, reply)

    def test_view_places_empty(self):
        """无地点时返回空态文案。"""
        self.assertEqual(self.module.run("查看全部就医地点"), "暂无就医地点。")

    def test_search_by_category_via_run(self):
        """按类型搜索。"""
        self.module.db.create_place("市中心医院", category="医院")
        self.module.db.create_place("社区药房", category="药房")
        reply = self.module.run("搜索就医地点 医院")
        self.assertIn("市中心医院", reply)
        self.assertNotIn("社区药房", reply)

    def test_search_by_name_via_run(self):
        """按名称搜索。"""
        self.module.db.create_place("市中心医院", category="医院")
        self.module.db.create_place("社区药房", category="药房")
        reply = self.module.run("搜索就医地点 社区")
        self.assertIn("社区药房", reply)
        self.assertNotIn("市中心医院", reply)

    def test_search_without_keyword(self):
        """搜索缺关键词时给出提示。"""
        reply = self.module.run("搜索就医地点")
        self.assertIn("请输入要搜索的名称或类型关键词", reply)

    def test_delete_via_run(self):
        """按编号删除地点。"""
        place_id = self.module.db.create_place("市中心医院", category="医院")
        reply = self.module.run(f"删除就医地点 {place_id}")
        self.assertIn("已删除编号", reply)
        self.assertEqual(self.module.db.list_places(), [])

    def test_delete_missing_place(self):
        """删除不存在的编号给出提示。"""
        reply = self.module.run("删除就医地点 999")
        self.assertIn("未找到编号为 999", reply)

    def test_delete_repeat(self):
        """重复删除同一编号时第二次提示未找到。"""
        place_id = self.module.db.create_place("市中心医院", category="医院")
        self.module.run(f"删除就医地点 {place_id}")
        reply = self.module.run(f"删除就医地点 {place_id}")
        self.assertIn("未找到编号为", reply)

    def test_delete_invalid_number(self):
        """删除时编号非法给出明确提示，不抛异常。"""
        reply = self.module.run("删除就医地点 abc")
        self.assertIn("请提供正确的就医地点编号", reply)
    def test_live_nav_returns_fallback(self):
        """实时导航请求明确说明未接入，不虚构地点。"""
        reply = self.module.run("附近的医院怎么去")
        self.assertIn(NO_MAP_REPLY, reply)
        self.assertNotIn("公里", reply)
        self.assertNotIn("http", reply)

    def test_live_nav_lists_saved_matching_places(self):
        """有已保存的匹配地点时同时列出。"""
        self.module.db.create_place("市中心医院", category="医院", address="人民路 8 号")
        self.module.db.create_place("社区药房", category="药房")
        reply = self.module.run("附近医院怎么走")
        self.assertIn(NO_MAP_REPLY, reply)
        self.assertIn("市中心医院", reply)
        self.assertNotIn("社区药房", reply)

    def test_live_nav_without_saved_places(self):
        """无匹配地点时只返回回退说明。"""
        reply = self.module.run("附近药房在哪")
        self.assertIn(NO_MAP_REPLY, reply)
        self.assertNotIn("【就医地点】", reply)

    def test_emergency_appends_strong_notice(self):
        """包含"急诊"时附加强提示。"""
        reply = self.module.run("我要去急诊")
        self.assertTrue(reply.startswith(EMERGENCY_REPLY))
        self.assertIn("拨打120或前往最近医院急诊", reply)

    def test_emergency_with_other_intent(self):
        """急诊与其他操作同时出现时，强提示仍在最前。"""
        self.module.db.create_place("市中心医院", category="医院")
        reply = self.module.run("急诊，查看就医地点")
        self.assertTrue(reply.startswith(EMERGENCY_REPLY))
        self.assertIn("【就医地点】", reply)

    def test_emergency_not_saved_as_place(self):
        """急诊请求不会创建地点。"""
        self.module.run("附近急诊医院")
        self.assertEqual(self.module.db.list_places(), [])

    def test_add_place_with_pharmacy_alias(self):
        """类型写"药店"也能新增，且规范化为"药房"。"""
        reply = self.module.run("添加就医地点，类型 药店，名称 济民药店")
        self.assertIn("已添加就医地点", reply)
        places = self.module.db.list_places()
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0]["category"], "药房")

    def test_search_pharmacy_alias_and_standard_same_record(self):
        """搜索"药房"和"药店"都能找到同一条记录。"""
        self.module.db.create_place("济民药店", category="药房")
        by_standard = self.module.run("搜索就医地点 药房")
        by_alias = self.module.run("搜索就医地点 药店")
        self.assertIn("济民药店", by_standard)
        self.assertIn("济民药店", by_alias)

    def test_live_nav_pharmacy_alias_lists_saved(self):
        """输入"附近药店在哪"时列出已保存的药房。"""
        self.module.db.create_place("济民药店", category="药房")
        reply = self.module.run("附近药店在哪")
        self.assertIn(NO_MAP_REPLY, reply)
        self.assertIn("济民药店", reply)

    def test_live_nav_pharmacy_standard_lists_saved(self):
        """输入"附近药房在哪"时仍正常列出。"""
        self.module.db.create_place("济民药店", category="药房")
        reply = self.module.run("附近药房在哪")
        self.assertIn(NO_MAP_REPLY, reply)
        self.assertIn("济民药店", reply)

    def test_live_nav_pharmacy_without_saved_returns_fallback(self):
        """没有已保存药房时，两种表达都只返回地图未接入提示。"""
        for text in ("附近药房在哪", "附近药店在哪", "附近的药店", "附近的药房"):
            reply = self.module.run(text)
            self.assertIn(NO_MAP_REPLY, reply)
            self.assertNotIn("【就医地点】", reply)


class MedicalNavigationPersistenceTest(unittest.TestCase):
    """关闭连接后重新打开，数据仍然存在。"""

    def test_reopen_keeps_places(self):
        """同一文件重开后地点可读。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "navigation.db")

            db = MedicalNavigationDB(db_path=path)
            db.initialize()
            place_id = db.create_place("市中心医院", category="医院", phone="120")
            db.close()

            reopened = MedicalNavigationDB(db_path=path)
            try:
                reopened.initialize()
                place = reopened.get_place(place_id)
                self.assertEqual(place["name"], "市中心医院")
                self.assertEqual(place["phone"], "120")
                self.assertEqual(len(reopened.list_places()), 1)
            finally:
                reopened.close()


class MedicalNavigationIsolationTest(unittest.TestCase):
    """注入文本按普通文本保存，且不写入其他业务数据库。"""

    def setUp(self):
        self.db = MedicalNavigationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_injection_text_stored_as_plain_text(self):
        """名称、地址、备注里的注入文本原样保存，表仍可用。"""
        payload = "'; DROP TABLE navigation_places; --"
        place_id = self.db.create_place(
            payload,
            category="医院",
            address="' OR 1=1 --",
            note="'; DELETE FROM navigation_places; --",
        )
        place = self.db.get_place(place_id)
        self.assertEqual(place["name"], payload)
        self.assertEqual(place["address"], "' OR 1=1 --")
        self.assertEqual(place["note"], "'; DELETE FROM navigation_places; --")

        second = self.db.create_place("乙药房", category="药房")
        self.assertIsNotNone(second)
        self.assertEqual(len(self.db.list_places()), 2)

    def test_injection_in_note_via_module_run(self):
        """通过模块新增时，备注中的注入文本按普通文本保存。"""
        module = MedicalNavigationModule(db_path=":memory:")
        try:
            reply = module.run(
                "添加就医地点，类型 医院，名称 甲医院，"
                "备注 '; DROP TABLE navigation_places; --"
            )
            self.assertIn("已添加就医地点", reply)
            places = module.db.list_places()
            self.assertEqual(len(places), 1)
            self.assertIn("DROP TABLE", places[0]["note"])
        finally:
            module.db.close()

    def test_does_not_write_other_business_db(self):
        """只生成 navigation.db，不创建用药或健康数据库。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "navigation.db")
            module = MedicalNavigationModule(db_path=path)
            try:
                module.run("添加就医地点，类型 医院，名称 市中心医院")
                module.run("附近医院怎么走")
                module.run("我要去急诊")
            finally:
                module.db.close()
            files = sorted(item.name for item in Path(tmp).iterdir())
            self.assertEqual(files, ["navigation.db"])

class MedicalNavigationDBThreadTest(unittest.TestCase):
    """连接线程参数兼容性：默认行为不变，可按需放开跨线程访问。"""

    def test_check_same_thread_defaults_to_true(self):
        """构造参数存在且默认 True，原有调用语义不变。"""
        params = inspect.signature(MedicalNavigationDB.__init__).parameters
        self.assertIn("check_same_thread", params)
        self.assertIs(params["check_same_thread"].default, True)

    def test_positional_db_path_still_supported(self):
        """只传路径的位置参数写法仍然可用。"""
        db = MedicalNavigationDB(":memory:")
        try:
            db.initialize()
            self.assertEqual(db.list_places(), [])
        finally:
            db.close()

    def test_child_thread_allowed_when_check_same_thread_false(self):
        """传 False 后子线程可以读写同一连接。"""
        db = MedicalNavigationDB(db_path=":memory:", check_same_thread=False)
        db.initialize()
        db.create_place("市中心医院", category="医院")
        errors = []
        names = []

        def worker():
            try:
                names.extend(place["name"] for place in db.list_places())
            except Exception as exc:  # 测试里显式收集异常，便于断言
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        try:
            self.assertEqual(errors, [])
            self.assertEqual(names, ["市中心医院"])
        finally:
            db.close()

    def test_child_thread_blocked_by_default(self):
        """默认 True 时子线程访问抛 ProgrammingError，与 sqlite3 原生行为一致。"""
        db = MedicalNavigationDB(db_path=":memory:")
        db.initialize()
        errors = []

        def worker():
            try:
                db.list_places()
            except sqlite3.ProgrammingError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        try:
            self.assertEqual(len(errors), 1)
        finally:
            db.close()


class MedicalNavigationDBUpdateTest(unittest.TestCase):
    """手动地点编辑：成功、缺失、非法输入与数据保全。"""

    def setUp(self):
        self.db = MedicalNavigationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.place_id = self.db.create_place(
            "市中心医院",
            category="医院",
            address="人民路 8 号",
            phone="0571-12345678",
            note="门诊楼三楼",
        )

    def test_update_returns_true_and_writes_new_values(self):
        """同时修改名称、类型、地址、电话、备注。"""
        ok = self.db.update_place(
            self.place_id,
            name="第一人民医院",
            category="医院",
            address="解放路 1 号",
            phone="0571-87654321",
            note="急诊 24 小时",
        )
        self.assertTrue(ok)
        place = self.db.get_place(self.place_id)
        self.assertEqual(place["name"], "第一人民医院")
        self.assertEqual(place["address"], "解放路 1 号")
        self.assertEqual(place["phone"], "0571-87654321")
        self.assertEqual(place["note"], "急诊 24 小时")

    def test_update_only_given_field_keeps_others(self):
        """只改电话时，其他字段保持原值。"""
        self.assertTrue(self.db.update_place(self.place_id, phone="120"))
        place = self.db.get_place(self.place_id)
        self.assertEqual(place["phone"], "120")
        self.assertEqual(place["name"], "市中心医院")
        self.assertEqual(place["category"], "医院")
        self.assertEqual(place["address"], "人民路 8 号")
        self.assertEqual(place["note"], "门诊楼三楼")

    def test_update_empty_string_clears_optional_field(self):
        """可选字段传空字符串表示清空。"""
        self.assertTrue(self.db.update_place(self.place_id, address="", note="   "))
        place = self.db.get_place(self.place_id)
        self.assertIsNone(place["address"])
        self.assertIsNone(place["note"])

    def test_update_missing_place_returns_false(self):
        """不存在的编号返回 False。"""
        self.assertFalse(self.db.update_place(999, name="不存在的医院"))

    def test_update_without_fields_returns_false(self):
        """不传任何字段时返回 False，且不改变原有记录。"""
        before = self.db.get_place(self.place_id)
        self.assertFalse(self.db.update_place(self.place_id))
        after = self.db.get_place(self.place_id)
        self.assertEqual(before, after)

    def test_update_invalid_category_keeps_original_data(self):
        """非法类型抛 ValueError，原数据保持不变。"""
        before = self.db.get_place(self.place_id)
        with self.assertRaises(ValueError):
            self.db.update_place(self.place_id, name="新名字", category="超市")
        self.assertEqual(self.db.get_place(self.place_id), before)

    def test_update_blank_name_keeps_original_data(self):
        """名称为空白抛 ValueError，原数据保持不变。"""
        before = self.db.get_place(self.place_id)
        with self.assertRaises(ValueError):
            self.db.update_place(self.place_id, name="   ")
        self.assertEqual(self.db.get_place(self.place_id), before)

    def test_update_name_is_stripped(self):
        """名称前后空格会被去掉。"""
        self.assertTrue(self.db.update_place(self.place_id, name="  中心医院  "))
        self.assertEqual(self.db.get_place(self.place_id)["name"], "中心医院")

    def test_update_refreshes_updated_at_format(self):
        """更新后 updated_at 仍是标准时间格式。"""
        self.assertTrue(self.db.update_place(self.place_id, phone="120"))
        updated_at = self.db.get_place(self.place_id)["updated_at"]
        self.assertRegex(updated_at, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

class MedicalNavigationDBSearchAndFilterTest(unittest.TestCase):
    """地址搜索、类别过滤与 limit。"""

    def setUp(self):
        self.db = MedicalNavigationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.db.create_place("市中心医院", category="医院", address="人民路 8 号")
        self.db.create_place("社区药房", category="药房", address="解放路 20 号")
        self.db.create_place("街角诊所", category="诊所", address="人民路 66 号")

    def test_search_matches_address(self):
        """关键词命中地址时返回对应地点。"""
        results = self.db.search_places("解放路")
        self.assertEqual([place["name"] for place in results], ["社区药房"])

    def test_search_matches_name_category_and_address(self):
        """名称、类型、地址三种匹配都能命中。"""
        self.assertEqual(
            [p["name"] for p in self.db.search_places("市中心")],
            ["市中心医院"],
        )
        self.assertEqual([p["name"] for p in self.db.search_places("诊所")], ["街角诊所"])
        self.assertEqual(
            sorted(p["name"] for p in self.db.search_places("人民路")),
            ["市中心医院", "街角诊所"],
        )

    def test_search_keeps_empty_keyword_behaviour(self):
        """空关键词仍返回空列表，不泄露全部数据。"""
        self.assertEqual(self.db.search_places(""), [])
        self.assertEqual(self.db.search_places(None), [])

    def test_list_filter_by_category(self):
        """按类型过滤只返回该类型。"""
        names = [p["name"] for p in self.db.list_places(category="医院")]
        self.assertEqual(names, ["市中心医院"])

    def test_list_filter_by_category_without_data_returns_empty(self):
        """支持范围内但无数据的类型返回空列表。"""
        self.assertEqual(self.db.list_places(category="卫生院"), [])

    def test_list_invalid_category_raises(self):
        """不在 CATEGORIES 中的类型抛 ValueError。"""
        with self.assertRaises(ValueError):
            self.db.list_places(category="超市")

    def test_list_limit(self):
        """limit 只返回前 N 条。"""
        names = [p["name"] for p in self.db.list_places(limit=2)]
        self.assertEqual(names, ["市中心医院", "社区药房"])

    def test_list_limit_zero_returns_empty(self):
        """limit=0 返回空列表。"""
        self.assertEqual(self.db.list_places(limit=0), [])

    def test_list_limit_and_category_combined(self):
        """类型过滤与 limit 可以同时使用。"""
        self.db.create_place("第二医院", category="医院")
        names = [p["name"] for p in self.db.list_places(category="医院", limit=1)]
        self.assertEqual(names, ["市中心医院"])

    def test_list_negative_limit_raises(self):
        """负数 limit 抛 ValueError。"""
        with self.assertRaises(ValueError):
            self.db.list_places(limit=-1)

    def test_list_non_integer_limit_raises(self):
        """字符串与布尔值都不能当 limit。"""
        for value in ("3", True, 2.5):
            with self.subTest(limit=value):
                with self.assertRaises(ValueError):
                    self.db.list_places(limit=value)

    def test_list_without_arguments_returns_all(self):
        """无参数调用与旧行为一致。"""
        self.assertEqual(len(self.db.list_places()), 3)


class MedicalNavigationDBSqlSafetyTest(unittest.TestCase):
    """新增的搜索与编辑同样使用参数化 SQL。"""

    def setUp(self):
        self.db = MedicalNavigationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_search_payload_is_treated_as_plain_text(self):
        """搜索框注入不会删表。"""
        self.db.create_place("市中心医院", category="医院")
        payload = "'; DROP TABLE navigation_places; --"
        self.assertEqual(self.db.search_places(payload), [])
        self.assertEqual(len(self.db.list_places()), 1)

    def test_update_payload_is_stored_as_plain_text(self):
        """编辑时注入的文本原样保存，表仍可用。"""
        place_id = self.db.create_place("甲医院", category="医院")
        payload = "'; DROP TABLE navigation_places; --"
        self.assertTrue(self.db.update_place(place_id, name=payload, note=payload))
        place = self.db.get_place(place_id)
        self.assertEqual(place["name"], payload)
        self.assertEqual(place["note"], payload)
        self.assertIsNotNone(self.db.create_place("乙药房", category="药房"))
        self.assertEqual(len(self.db.list_places()), 2)

    def test_limit_is_rejected_not_interpolated(self):
        """limit 不是合法整数时直接拒绝，不会拼进 SQL。"""
        self.db.create_place("甲医院", category="医院")
        with self.assertRaises(ValueError):
            self.db.list_places(limit="1; DROP TABLE navigation_places")
        self.assertEqual(len(self.db.list_places()), 1)


class MedicalNavigationDBCloseTest(unittest.TestCase):
    """连接关闭后的行为。"""

    def test_close_is_idempotent(self):
        """重复关闭不抛异常。"""
        db = MedicalNavigationDB(db_path=":memory:")
        db.initialize()
        db.close()
        db.close()

    def test_query_after_close_raises_programming_error(self):
        """关闭后再查询抛 ProgrammingError，不会静默返回脏数据。"""
        db = MedicalNavigationDB(db_path=":memory:")
        db.initialize()
        db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.list_places()