"""用药管理模块与用药数据库的单元测试。

覆盖：接口不变、计划新增/查询/列表/停用/重复停用、有效计划写服药记录、
不存在计划与已停用计划的拒绝（含数据库层直接调用）、非法日期时间不写库、
咨询与提醒固定回复且不写库、关闭重开后的持久化、SQL 注入文本按普通文本保存。
全部使用内存库或临时文件，离线执行，不访问网络。
"""

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.modules.base import MedicalModule
from agent.modules.medication import (
    CONSULT_REPLY,
    HELP_MESSAGE,
    RECORD_NOTICE,
    REMINDER_REPLY,
    MedicationModule,
)
from agent.modules.medication_db import MedicationDB


class MedicationDBPlanTest(unittest.TestCase):
    """用药计划的新增、查询、列表与停用。"""

    def setUp(self):
        self.db = MedicationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_create_plan_returns_id_and_defaults_active(self):
        """新增计划返回编号，默认状态为 active。"""
        plan_id = self.db.create_plan("氨氯地平", "每天 1 次", schedule_times="08:00")
        self.assertIsInstance(plan_id, int)
        plan = self.db.get_plan(plan_id)
        self.assertEqual(plan["medication_name"], "氨氯地平")
        self.assertEqual(plan["doctor_instruction"], "每天 1 次")
        self.assertEqual(plan["schedule_times"], "08:00")
        self.assertEqual(plan["status"], "active")

    def test_get_missing_plan_returns_none(self):
        """不存在的编号返回 None。"""
        self.assertIsNone(self.db.get_plan(999))

    def test_list_plans_in_id_order(self):
        """列表按编号升序返回全部计划。"""
        first = self.db.create_plan("甲药", "用法一")
        second = self.db.create_plan("乙药", "用法二")
        ids = [plan["id"] for plan in self.db.list_plans()]
        self.assertEqual(ids, [first, second])

    def test_stop_plan_changes_status(self):
        """停用把状态改为 stopped。"""
        plan_id = self.db.create_plan("甲药", "用法一")
        self.assertTrue(self.db.stop_plan(plan_id))
        self.assertEqual(self.db.get_plan(plan_id)["status"], "stopped")

    def test_stop_plan_twice_returns_false(self):
        """重复停用返回 False，状态保持 stopped。"""
        plan_id = self.db.create_plan("甲药", "用法一")
        self.assertTrue(self.db.stop_plan(plan_id))
        self.assertFalse(self.db.stop_plan(plan_id))
        self.assertEqual(self.db.get_plan(plan_id)["status"], "stopped")

    def test_stop_missing_plan_returns_false(self):
        """停用不存在的计划返回 False。"""
        self.assertFalse(self.db.stop_plan(999))


class MedicationDBLogTest(unittest.TestCase):
    """服药记录的写入与拒绝规则（数据库层）。"""

    def setUp(self):
        self.db = MedicationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_add_log_for_active_plan(self):
        """生效计划可以写入记录，并带出药名。"""
        plan_id = self.db.create_plan("甲药", "每天 1 次")
        log_id = self.db.add_log(plan_id, "2026-11-01 08:00:00", "早饭后")
        self.assertIsInstance(log_id, int)
        logs = self.db.list_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["medication_name"], "甲药")
        self.assertEqual(logs[0]["taken_at"], "2026-11-01 08:00:00")
        self.assertEqual(logs[0]["note"], "早饭后")

    def test_add_log_missing_plan_returns_none_and_no_row(self):
        """计划不存在时直接调用也拒绝，数据库零新增。"""
        self.assertIsNone(self.db.add_log(999, "2026-11-01 08:00:00"))
        self.assertEqual(self.db.list_logs(), [])

    def test_add_log_stopped_plan_returns_none_and_no_row(self):
        """计划已停用时直接调用也拒绝，数据库零新增。"""
        plan_id = self.db.create_plan("甲药", "每天 1 次")
        self.db.stop_plan(plan_id)
        self.assertIsNone(self.db.add_log(plan_id, "2026-11-01 08:00:00"))
        self.assertEqual(self.db.list_logs(), [])

    def test_list_logs_order_desc_and_limit(self):
        """记录按服药时间倒序，limit 生效。"""
        plan_id = self.db.create_plan("甲药", "每天 1 次")
        self.db.add_log(plan_id, "2026-11-01 08:00:00")
        self.db.add_log(plan_id, "2026-11-02 08:00:00")
        logs = self.db.list_logs(1)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["taken_at"], "2026-11-02 08:00:00")

class MedicationModuleTest(unittest.TestCase):
    """模块的意图识别、字段校验与安全边界。"""

    def setUp(self):
        self.module = MedicationModule(db_path=":memory:")
        self.addCleanup(self.module.db.close)

    def test_interface_unchanged(self):
        """继承基类，run 签名保持 (self, user_input, context=None)。"""
        self.assertIsInstance(self.module, MedicalModule)
        self.assertEqual(self.module.name, "medication")
        params = list(inspect.signature(MedicationModule.run).parameters)
        self.assertEqual(params, ["self", "user_input", "context"])

    def test_empty_returns_help(self):
        """空输入与非字符串返回帮助提示。"""
        self.assertEqual(self.module.run(""), HELP_MESSAGE)
        self.assertEqual(self.module.run("   "), HELP_MESSAGE)
        self.assertEqual(self.module.run(None), HELP_MESSAGE)

    def test_create_plan_via_run(self):
        """按示例句式可以新增计划。"""
        reply = self.module.run("新增用药计划，药名 氨氯地平，医嘱用法 每天 1 次，时间 08:00")
        self.assertIn("已新增用药计划", reply)
        plans = self.module.db.list_plans()
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["medication_name"], "氨氯地平")
        self.assertEqual(plans[0]["schedule_times"], "08:00")

    def test_create_plan_missing_name(self):
        """缺药名时不写库。"""
        reply = self.module.run("新增用药计划，医嘱用法 每天 1 次")
        self.assertIn("请提供药名", reply)
        self.assertEqual(self.module.db.list_plans(), [])

    def test_create_plan_missing_instruction(self):
        """缺医嘱用法时不写库。"""
        reply = self.module.run("新增用药计划，药名 氨氯地平")
        self.assertIn("请提供医嘱用法", reply)
        self.assertEqual(self.module.db.list_plans(), [])

    def test_create_plan_invalid_time_not_saved(self):
        """非法时间不写库。"""
        reply = self.module.run("新增用药计划，药名 甲药，医嘱用法 每天 1 次，时间 25:99")
        self.assertIn("时间格式", reply)
        self.assertEqual(self.module.db.list_plans(), [])

    def test_create_plan_invalid_start_date_not_saved(self):
        """非法开始日期不写库。"""
        reply = self.module.run(
            "新增用药计划，药名 甲药，医嘱用法 每天 1 次，开始日期 2026-02-30"
        )
        self.assertIn("开始日期", reply)
        self.assertEqual(self.module.db.list_plans(), [])

    def test_create_plan_invalid_end_date_not_saved(self):
        """非法结束日期不写库。"""
        reply = self.module.run(
            "新增用药计划，药名 甲药，医嘱用法 每天 1 次，结束日期 2026-13-01"
        )
        self.assertIn("结束日期", reply)
        self.assertEqual(self.module.db.list_plans(), [])

    def test_view_plans(self):
        """查看计划返回中文列表并附中性提示。"""
        self.module.db.create_plan("甲药", "每天 1 次")
        reply = self.module.run("查看用药计划")
        self.assertIn("【用药计划】", reply)
        self.assertIn("甲药", reply)
        self.assertIn(RECORD_NOTICE, reply)

    def test_view_plans_empty(self):
        """无计划时返回空态文案。"""
        self.assertEqual(self.module.run("查看用药计划"), "暂无用药计划。")

    def test_view_logs_empty(self):
        """无记录时返回空态文案。"""
        self.assertEqual(self.module.run("查看服药记录"), "暂无服药记录。")
    def test_log_valid_plan(self):
        """生效计划可以记录服药，时间与备注被保存。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        reply = self.module.run(
            f"记录服药 计划编号 {plan_id} 时间 2026-11-01 08:00 备注 早饭后"
        )
        self.assertIn("已记录服药", reply)
        logs = self.module.db.list_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["taken_at"], "2026-11-01 08:00:00")
        self.assertEqual(logs[0]["note"], "早饭后")

    def test_log_missing_plan(self):
        """计划不存在时提示未找到，且不写记录。"""
        reply = self.module.run("记录服药 计划编号 999")
        self.assertIn("未找到编号为 999", reply)
        self.assertEqual(self.module.db.list_logs(), [])

    def test_log_stopped_plan(self):
        """计划已停用时提示无法记录，且不写记录。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        self.module.db.stop_plan(plan_id)
        reply = self.module.run(f"记录服药 计划编号 {plan_id}")
        self.assertIn("已停用", reply)
        self.assertEqual(self.module.db.list_logs(), [])

    def test_log_handles_none_from_db(self):
        """数据库返回 None 时给出安全提示，不抛异常。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        with mock.patch.object(self.module.db, "add_log", return_value=None):
            reply = self.module.run(f"记录服药 计划编号 {plan_id}")
        self.assertIn("无法记录服药", reply)

    def test_log_invalid_date_not_saved(self):
        """非法服药日期不写库。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        reply = self.module.run(f"记录服药 计划编号 {plan_id} 时间 2026-02-30 08:00")
        self.assertIn("服药日期", reply)
        self.assertEqual(self.module.db.list_logs(), [])

    def test_log_invalid_time_not_saved(self):
        """非法服药时间不写库。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        reply = self.module.run(f"记录服药 计划编号 {plan_id} 时间 2026-11-01 25:00")
        self.assertIn("服药时间", reply)
        self.assertEqual(self.module.db.list_logs(), [])

    def test_stop_plan_via_run(self):
        """按编号停用计划。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        reply = self.module.run(f"停用用药计划 {plan_id}")
        self.assertIn("已停用编号", reply)
        self.assertEqual(self.module.db.get_plan(plan_id)["status"], "stopped")

    def test_stop_missing_plan(self):
        """停用不存在的计划给出提示。"""
        reply = self.module.run("停用用药计划 999")
        self.assertIn("未找到编号为 999", reply)

    def test_stop_already_stopped(self):
        """重复停用给出已停用提示。"""
        plan_id = self.module.db.create_plan("甲药", "每天 1 次")
        self.module.db.stop_plan(plan_id)
        reply = self.module.run(f"停用用药计划 {plan_id}")
        self.assertIn("已经是停用状态", reply)

    def test_consult_returns_fixed_reply_and_no_write(self):
        """用药咨询返回固定回复，不产生计划或记录。"""
        for text in ["这药有什么副作用", "能不能停药", "吃什么药好", "剂量多少"]:
            self.assertEqual(self.module.run(text), CONSULT_REPLY)
        self.assertEqual(self.module.db.list_plans(), [])
        self.assertEqual(self.module.db.list_logs(), [])

    def test_reminder_returns_fixed_reply_and_no_plan(self):
        """提醒请求返回固定说明，不创建计划。"""
        reply = self.module.run("明天提醒我吃药")
        self.assertEqual(reply, REMINDER_REPLY)
        self.assertEqual(self.module.db.list_plans(), [])
        self.assertEqual(self.module.db.list_logs(), [])


class MedicationPersistenceTest(unittest.TestCase):
    """关闭连接后重新打开，数据仍然存在。"""

    def test_reopen_keeps_plans_and_logs(self):
        """同一文件重开后计划与服药记录都可读。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "medication.db")

            db = MedicationDB(db_path=path)
            db.initialize()
            plan_id = db.create_plan("甲药", "每天 1 次", schedule_times="08:00")
            db.add_log(plan_id, "2026-11-01 08:00:00", "早饭后")
            db.close()

            reopened = MedicationDB(db_path=path)
            try:
                reopened.initialize()
                plan = reopened.get_plan(plan_id)
                self.assertEqual(plan["medication_name"], "甲药")
                logs = reopened.list_logs()
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0]["note"], "早饭后")
            finally:
                reopened.close()


class MedicationInjectionTest(unittest.TestCase):
    """注入文本按普通文本保存，相关表仍可正常使用。"""

    def setUp(self):
        self.db = MedicationDB(db_path=":memory:")
        self.db.initialize()
        self.addCleanup(self.db.close)

    def test_injection_text_stored_as_plain_text(self):
        """药名、医嘱、备注里的注入文本原样保存。"""
        payload = "'; DROP TABLE medication_plans; --"
        plan_id = self.db.create_plan(
            payload,
            "用法 ' OR 1=1 --",
            note="'; DELETE FROM medication_logs; --",
        )
        log_id = self.db.add_log(plan_id, "2026-11-01 08:00:00", payload)
        self.assertIsNotNone(log_id)

        plan = self.db.get_plan(plan_id)
        self.assertEqual(plan["medication_name"], payload)
        self.assertEqual(plan["doctor_instruction"], "用法 ' OR 1=1 --")

        logs = self.db.list_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["note"], payload)

        # 表仍存在且可继续写入
        second = self.db.create_plan("乙药", "每天 2 次")
        self.assertIsNotNone(second)
        self.assertEqual(len(self.db.list_plans()), 2)

    def test_injection_in_note_via_module_run(self):
        """通过模块新增计划时，备注中的注入文本按普通文本保存。"""
        module = MedicationModule(db_path=":memory:")
        try:
            reply = module.run(
                "新增用药计划，药名 甲药，医嘱用法 每天 1 次，"
                "备注 '; DROP TABLE medication_plans; --"
            )
            self.assertIn("已新增用药计划", reply)
            plans = module.db.list_plans()
            self.assertEqual(len(plans), 1)
            self.assertIn("DROP TABLE", plans[0]["note"])
        finally:
            module.db.close()