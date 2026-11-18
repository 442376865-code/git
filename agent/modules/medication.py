"""用药管理模块（MVP）。

只做三件事：
1. 记录用户主动给出的既有用药医嘱（用药计划）；
2. 手动记录服药情况；
3. 查看计划与服药记录。

不提供具体药物、剂量、频次或停药调整建议，也不提供系统提醒。
"""

import re
from datetime import datetime

from .base import MedicalModule
from .medication_db import MedicationDB

# 空输入或无法识别时的帮助提示
HELP_MESSAGE = (
    "我可以帮你记录和查看用药计划与服药记录。\n"
    "新增计划示例：新增用药计划，药名 XX，医嘱用法 XX，时间 08:00,20:00。\n"
    "查看计划：查看用药计划。\n"
    "停用计划：停用用药计划 1。\n"
    "记录服药：记录服药 计划编号 1 时间 2026-11-01 08:00 备注 早饭后。\n"
    "查看记录：查看服药记录。"
)

# 用药咨询的固定回复
CONSULT_REPLY = "不提供具体药物、剂量或停药调整建议，请咨询医生或药师。"

# 提醒请求的固定回复
REMINDER_REPLY = "当前仅支持手动记录，不提供系统通知。"

# 查询结果附带的中性提示
RECORD_NOTICE = "以上记录不能替代医生诊疗。"

# 新增计划字段识别规则
PLAN_NAME_RE = re.compile(r"药名\s*[:：]?\s*([^\s，,。;；]+)")
PLAN_INSTRUCTION_RE = re.compile(
    r"(?:医嘱用法|用法)\s*[:：]?\s*(.+?)(?=\s*(?:时间|开始日期|结束日期|备注)\s*[:：]|$)"
)
PLAN_TIMES_RE = re.compile(r"时间\s*[:：]?\s*([0-9:：,，\s]+)")
PLAN_START_RE = re.compile(r"开始日期\s*[:：]?\s*(\d{4}-\d{2}-\d{2})")
PLAN_END_RE = re.compile(r"结束日期\s*[:：]?\s*(\d{4}-\d{2}-\d{2})")
PLAN_NOTE_RE = re.compile(r"备注\s*[:：]?\s*(.+)$")

# 记录服药识别规则
LOG_RE = re.compile(
    r"(?:记录)?服药(?:记录)?\s*(?:计划编号|计划|编号|id|#)?\s*[:：]?\s*(\d+)"
)
TAKEN_AT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")

# 停用计划识别规则
STOP_RE = re.compile(
    r"(?:停用|停止)\s*(?:用药)?计划\s*(?:编号|id|#)?\s*[:：]?\s*(\d+)"
)

# 查看意图
LOG_VIEW_RE = re.compile(r"服药记录|服药历史|最近服药")
PLAN_VIEW_RE = re.compile(r"用药计划|计划列表|查看计划")

# 用药咨询关键词
CONSULT_RE = re.compile(
    r"吃什么药|吃啥药|怎么选药|选什么药|用什么药|哪些药|停药|减药|漏服|副作用|不良反应|剂量"
)


class MedicationModule(MedicalModule):
    """用药管理模块。"""

    # 模块名称，必须与 Router 返回的模块名一致
    name = "medication"
    description = "用药管理：记录用药计划与服药情况，不提供用药建议或系统提醒"

    def __init__(self, db_path=None):
        """初始化模块并打开用药数据库。

        参数：
            db_path: 可选的数据库路径，测试时可传 ":memory:" 使用内存库。
        """
        self.db = MedicationDB(db_path)
        self.db.initialize()

    def run(self, user_input, context=None):
        """识别记录/查看/停用意图并返回中文结果。"""
        # 非字符串或空内容：返回帮助提示
        if not isinstance(user_input, str) or not user_input.strip():
            return HELP_MESSAGE

        text = user_input.strip()

        # 1. 记录服药（必须带计划编号）
        log_match = LOG_RE.search(text)
        if log_match:
            return self._handle_log(int(log_match.group(1)), text)

        # 2. 停用计划
        stop_match = STOP_RE.search(text)
        if stop_match:
            return self._handle_stop(int(stop_match.group(1)))

        # 3. 新增计划（需要药名与医嘱用法）
        name_match = PLAN_NAME_RE.search(text)
        instruction_match = PLAN_INSTRUCTION_RE.search(text)
        if name_match or instruction_match or "新增用药计划" in text:
            return self._handle_create_plan(text, name_match, instruction_match)

        # 4. 查看服药记录
        if LOG_VIEW_RE.search(text):
            return self._format_logs(self.db.list_logs(10))

        # 5. 查看用药计划
        if PLAN_VIEW_RE.search(text):
            return self._format_plans(self.db.list_plans())

        # 6. 用药咨询：固定回复，不写库
        if CONSULT_RE.search(text):
            return CONSULT_REPLY

        # 7. 提醒请求：固定说明，不创建计划
        if "提醒" in text:
            return REMINDER_REPLY

        # 8. 无法识别
        return HELP_MESSAGE

    def _handle_create_plan(self, text, name_match, instruction_match):
        """校验必填字段与可选日期时间后新增计划。"""
        errors = []

        medication_name = name_match.group(1).strip() if name_match else ""
        instruction = instruction_match.group(1).strip(" ，,。;；") if instruction_match else ""
        if not medication_name:
            errors.append("请提供药名。")
        if not instruction:
            errors.append("请提供医嘱用法。")
        if errors:
            return "\n".join(errors) + "\n示例：新增用药计划，药名 XX，医嘱用法 XX。"

        schedule_times, error = self._parse_schedule_times(text)
        if error:
            errors.append(error)
        start_date, error = self._parse_optional_date(text, "开始日期", PLAN_START_RE)
        if error:
            errors.append(error)
        end_date, error = self._parse_optional_date(text, "结束日期", PLAN_END_RE)
        if error:
            errors.append(error)
        if errors:
            return "\n".join(errors)

        note_match = PLAN_NOTE_RE.search(text)
        note = note_match.group(1).strip() if note_match else None

        plan_id = self.db.create_plan(
            medication_name,
            instruction,
            schedule_times=schedule_times,
            start_date=start_date,
            end_date=end_date,
            note=note,
        )
        return f"已新增用药计划（编号 {plan_id}）。\n" + self._format_plans(
            self.db.list_plans()
        )

    def _handle_stop(self, plan_id):
        """按编号停用计划，不物理删除。"""
        plan = self.db.get_plan(plan_id)
        if not plan:
            return f"未找到编号为 {plan_id} 的用药计划。"
        if plan["status"] != "active":
            return f"编号为 {plan_id} 的用药计划已经是停用状态。"
        self.db.stop_plan(plan_id)
        return f"已停用编号 {plan_id} 的用药计划。\n" + self._format_plans(
            self.db.list_plans()
        )

    def _handle_log(self, plan_id, text):
        """校验计划有效性与服药时间后写一条服药记录。"""
        plan = self.db.get_plan(plan_id)
        if not plan:
            return f"未找到编号为 {plan_id} 的用药计划。"
        if plan["status"] != "active":
            return f"编号为 {plan_id} 的用药计划已停用，无法记录服药。"

        taken_at, error = self._parse_taken_at(text)
        if error:
            return error

        note_match = PLAN_NOTE_RE.search(text)
        note = note_match.group(1).strip() if note_match else None
        log_id = self.db.add_log(plan_id, taken_at, note)
        # 数据库层要求计划存在且生效，失败时给出安全提示而不是抛异常
        if log_id is None:
            return f"编号为 {plan_id} 的用药计划不存在或已停用，无法记录服药。"
        return f"已记录服药（编号 {log_id}）。\n" + self._format_logs(
            self.db.list_logs(10)
        )

    def _parse_schedule_times(self, text):
        """解析计划里的多个 HH:MM 时间；未提供时返回 None。"""
        match = PLAN_TIMES_RE.search(text)
        if not match:
            if "时间" in text:
                return None, "用药时间格式应为 HH:MM，可用逗号分隔多个时间。"
            return None, None

        times = re.findall(r"\d{1,2}:\d{2}", match.group(1))
        if not times:
            return None, "用药时间格式应为 HH:MM，可用逗号分隔多个时间。"
        for item in times:
            if not self._valid_time(item):
                return None, "用药时间格式应为 HH:MM，例如 08:00。"
        return ",".join(times), None

    def _parse_optional_date(self, text, label, pattern):
        """解析可选日期字段；提供了但格式不对时返回错误。"""
        match = pattern.search(text)
        if not match:
            if label in text:
                return None, f"{label}格式应为 YYYY-MM-DD，例如 2026-11-01。"
            return None, None
        value = match.group(1)
        if not self._valid_date(value):
            return None, f"{label}格式不正确或不存在，应为 YYYY-MM-DD。"
        return value, None

    def _parse_taken_at(self, text):
        """解析服药时间；未指定时使用当前时间。"""
        match = TAKEN_AT_RE.search(text)
        if match:
            date_part, time_part = match.group(1), match.group(2)
            if not self._valid_date(date_part):
                return None, "服药日期不正确或不存在，应为 YYYY-MM-DD。"
            if not self._valid_time(time_part):
                return None, "服药时间格式应为 HH:MM。"
            return f"{date_part} {time_part}:00", None
        if "时间" in text:
            return None, "服药时间格式应为 YYYY-MM-DD HH:MM，例如 2026-11-01 08:00。"
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S"), None

    @staticmethod
    def _valid_date(value):
        """校验 YYYY-MM-DD 日期是否真实存在。"""
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    @staticmethod
    def _valid_time(value):
        """校验 HH:MM 时间是否合法。"""
        try:
            datetime.strptime(value, "%H:%M")
            return True
        except ValueError:
            return False

    @staticmethod
    def _format_plans(plans):
        """把计划列表格式化为中文文本。"""
        if not plans:
            return "暂无用药计划。"
        lines = ["【用药计划】"]
        for plan in plans:
            status = "生效中" if plan["status"] == "active" else "已停用"
            line = (
                f"- 编号 {plan['id']}：{plan['medication_name']}"
                f"｜医嘱用法：{plan['doctor_instruction']}"
            )
            if plan.get("schedule_times"):
                line += f"｜时间：{plan['schedule_times']}"
            line += f"｜状态：{status}"
            lines.append(line)
        lines.append(RECORD_NOTICE)
        return "\n".join(lines)

    @staticmethod
    def _format_logs(logs):
        """把服药记录格式化为中文文本。"""
        if not logs:
            return "暂无服药记录。"
        lines = [f"【服药记录】（最近 {len(logs)} 条）"]
        for log in logs:
            line = (
                f"- 计划 {log['plan_id']}｜{log['medication_name']}"
                f"｜服药时间：{log['taken_at']}"
            )
            if log.get("note"):
                line += f"｜备注：{log['note']}"
            lines.append(line)
        lines.append(RECORD_NOTICE)
        return "\n".join(lines)