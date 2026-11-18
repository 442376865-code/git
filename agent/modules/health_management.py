"""健康管理模块（档案 + 体重记录）。

只做数据录入、查看与删除：
- 健康档案：姓名、性别、出生日期、身高；
- 体重记录：体重、记录日期、备注，支持新增、查看最近记录、按编号删除。

本模块不做任何健康判断，不计算 BMI，不提供诊断或用药建议。
"""

import re
from datetime import date, datetime

from .base import MedicalModule
from .health_db import HealthDB

# 空输入或无法识别时的帮助提示
HELP_MESSAGE = (
    "我可以帮你记录健康档案和体重。\n"
    "档案示例：更新健康档案，姓名 张三、性别 男、出生日期 1990-05-20、身高 175cm。\n"
    "体重示例：体重 70kg；也可以指定日期和备注：体重 70kg 日期 2026-11-01 备注 晨起。\n"
    "查看：查看健康档案 / 查看体重记录。\n"
    "删除：删除体重记录 3。"
)

# 清空后的提示
CLEARED_MESSAGE = "健康档案和体重记录已清空。"

# 首次提出清空时的确认提示：不立即删除，等待用户再次确认
CLEAR_CONFIRM_MESSAGE = (
    "将清空健康档案和全部体重记录，删除后无法恢复。\n"
    "如果确认，请回复「确认清空」；回复「取消」可放弃本次操作。"
)

# 有待确认状态时选择取消
CLEAR_CANCELLED_MESSAGE = "已取消清空操作，未删除任何数据。"

# 没有待确认状态时输入「确认清空」
CLEAR_NOTHING_MESSAGE = "当前没有待确认的清空操作，未删除任何数据。"

# 没有待确认状态时输入「取消」或「取消清空」
CANCEL_NOTHING_MESSAGE = "当前没有待确认的操作，未删除任何数据。"

# 所有记录类回答附带的中性提示
RECORD_NOTICE = "以上为你录入的日常记录，本模块不做健康评估，也不能替代医生诊疗。"

# 合理范围，超出视为录入错误
HEIGHT_RANGE = (100.0, 250.0)
WEIGHT_RANGE = (20.0, 300.0)

# 档案字段识别规则
NAME_RE = re.compile(r"姓名\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z·]{1,20})")
GENDER_RE = re.compile(r"性别\s*[:：]?\s*(男|女)|我是\s*(男|女)生?|(男|女)性")
BIRTH_RE = re.compile(r"(?:出生日期|生日)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})")
HEIGHT_RE = re.compile(r"身高\s*[:：]?\s*(\d{2,3}(?:\.\d+)?)\s*(?:cm|厘米|公分|CM|Cm)?")

# 体重记录识别规则
WEIGHT_RE = re.compile(r"体重\s*[:：]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:kg|公斤|千克|KG|Kg)?")
DATE_RE = re.compile(r"(?:日期|记录日期)\s*[:：]?\s*(\d{4}-\d{2}-\d{2})")
NOTE_RE = re.compile(r"备注\s*[:：]?\s*(.+)$")
DELETE_WEIGHT_RE = re.compile(
    r"(?:删除|移除)\s*(?:体重记录|体重)\s*(?:id|ID|编号|#)?\s*[:：]?\s*(\d+)"
)
VIEW_WEIGHT_RE = re.compile(r"查看\s*体重|体重记录|体重历史")

# 清空档案的表达（注意不能误命中"删除体重记录"）
CLEAR_RE = re.compile(r"(清除|清空)\s*(健康)?(档案|记录)|删除\s*健康档案")

# 二次确认与取消：必须整句匹配，避免误触发
CONFIRM_CLEAR_RE = re.compile(r"^确认清空$")
CANCEL_CLEAR_RE = re.compile(r"^(取消|取消清空)$")


class HealthManagementModule(MedicalModule):
    """健康管理模块。"""

    # 模块名称，必须与 Router 返回的模块名一致
    name = "health_management"
    description = "健康管理：记录并查看姓名、性别、出生日期、身高与体重记录"

    def __init__(self, db_path=None):
        """初始化模块并打开健康档案库。

        参数：
            db_path: 可选的数据库路径，测试时可传 ":memory:" 使用内存库。
        """
        self.db = HealthDB(db_path)
        self.db.initialize()
        # 实例级待确认状态：只在当前模块实例（当前会话）内有效，不使用类属性
        self._pending_clear = False

    def run(self, user_input, context=None):
        """识别录入/查看/删除意图并返回中文结果。"""
        # 非字符串或空内容：返回帮助提示
        if not isinstance(user_input, str) or not user_input.strip():
            return HELP_MESSAGE

        text = user_input.strip()

        # 0. 待确认流程：确认与取消必须先于普通识别处理
        if CONFIRM_CLEAR_RE.match(text):
            if not self._pending_clear:
                return CLEAR_NOTHING_MESSAGE
            # 先复位状态再删除，避免重复确认导致重复执行
            self._pending_clear = False
            self.db.clear_all()
            return CLEARED_MESSAGE

        if CANCEL_CLEAR_RE.match(text):
            if not self._pending_clear:
                return CANCEL_NOTHING_MESSAGE
            self._pending_clear = False
            return CLEAR_CANCELLED_MESSAGE

        # 1. 清空档案与体重记录：首次只登记待确认，不立即删除
        if CLEAR_RE.search(text):
            self._pending_clear = True
            return CLEAR_CONFIRM_MESSAGE

        # 2. 按编号删除体重记录
        match = DELETE_WEIGHT_RE.search(text)
        if match:
            record_id = int(match.group(1))
            if self.db.delete_weight(record_id):
                return f"已删除编号 {record_id} 的体重记录。"
            return f"未找到编号为 {record_id} 的体重记录。"
        if "删除" in text and "体重" in text:
            return "请提供要删除的体重记录编号，例如：删除体重记录 3。"

        # 3. 解析档案字段与体重录入
        profile_fields, errors = self._parse_profile(text)
        weight_info, weight_error = self._parse_weight(text)
        if weight_error:
            errors.append(weight_error)
        if errors:
            return "\n".join(errors)

        results = []
        if profile_fields:
            profile = self.db.update_profile(**profile_fields)
            results.append("已更新健康档案。\n" + self._format_profile(profile))
        if weight_info:
            record_id = self.db.add_weight(**weight_info)
            results.append(
                f"已新增体重记录（编号 {record_id}）。\n"
                + self._format_weights(self.db.list_weights(10))
            )
        if results:
            return "\n\n".join(results)

        # 4. 查看体重记录
        if VIEW_WEIGHT_RE.search(text):
            return self._format_weights(self.db.list_weights(10))

        # 5. 查看健康档案
        if "档案" in text or "健康记录" in text or "健康数据" in text:
            profile = self.db.get_profile()
            if not profile:
                return "尚未建立健康档案。\n" + HELP_MESSAGE
            return self._format_profile(profile)

        # 6. 无法识别：返回帮助
        return HELP_MESSAGE

    def handle_pending(self, user_input):
        """供主控在路由前调用的待确认钩子。

        「确认清空」「取消」「取消清空」即使没有待确认状态也由本模块接管，
        返回固定中文提示，确保不会掉入通用问答而调用大模型；
        其他输入返回 None，继续走原有路由流程。
        """
        if not isinstance(user_input, str):
            return None
        text = user_input.strip()
        if CONFIRM_CLEAR_RE.match(text) or CANCEL_CLEAR_RE.match(text):
            return self.run(text)
        return None

    def _parse_profile(self, text):
        """解析姓名、性别、出生日期、身高，并校验合理范围与日期。"""
        fields = {}
        errors = []

        match = NAME_RE.search(text)
        if match:
            fields["name"] = match.group(1)

        match = GENDER_RE.search(text)
        if match:
            gender = next((group for group in match.groups() if group), None)
            if gender:
                fields["gender"] = gender

        match = BIRTH_RE.search(text)
        if match:
            birth_date = match.group(1)
            parsed = self._parse_date_str(birth_date)
            if parsed is None:
                errors.append(
                    "出生日期格式不正确或不存在，应为 YYYY-MM-DD，例如 1990-05-20。"
                )
            elif parsed > date.today():
                errors.append("出生日期不能晚于今天。")
            else:
                fields["birth_date"] = birth_date
        elif "出生日期" in text or "生日" in text:
            errors.append("出生日期格式应为 YYYY-MM-DD，例如 1990-05-20。")

        match = HEIGHT_RE.search(text)
        if match:
            value = float(match.group(1))
            if HEIGHT_RANGE[0] <= value <= HEIGHT_RANGE[1]:
                fields["height_cm"] = value
            else:
                errors.append("身高数值看起来不太合理（应为 100-250 cm）。")

        return fields, errors

    def _parse_weight(self, text):
        """解析体重录入，返回 (字段字典或 None, 错误提示或 None)。"""
        match = WEIGHT_RE.search(text)
        if not match:
            # 只有"体重"没有数字，且不是查看/删除意图时给出提示
            if "体重" in text and not VIEW_WEIGHT_RE.search(text):
                return None, "请提供体重数值，例如：体重 70kg。"
            return None, None

        value = float(match.group(1))
        if not WEIGHT_RANGE[0] <= value <= WEIGHT_RANGE[1]:
            return None, "体重数值看起来不太合理（应为 20-300 kg）。"

        record_date = None
        date_match = DATE_RE.search(text)
        if date_match:
            candidate = date_match.group(1)
            if self._parse_date_str(candidate) is None:
                return None, "记录日期格式不正确或不存在，应为 YYYY-MM-DD。"
            record_date = candidate
        elif "日期" in text:
            return None, "记录日期格式应为 YYYY-MM-DD，例如 2026-11-17。"

        note_match = NOTE_RE.search(text)
        note = note_match.group(1).strip() if note_match else None

        return {"weight_kg": value, "record_date": record_date, "note": note}, None

    @staticmethod
    def _parse_date_str(value):
        """把 YYYY-MM-DD 字符串解析为日期，非法时返回 None。"""
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _format_profile(profile):
        """把档案格式化为中文分段文本，只展示录入数据。"""
        lines = ["【个人健康档案】"]
        if profile.get("name"):
            lines.append(f"姓名：{profile['name']}")
        if profile.get("gender"):
            lines.append(f"性别：{profile['gender']}")
        if profile.get("birth_date"):
            lines.append(f"出生日期：{profile['birth_date']}")
        if profile.get("height_cm"):
            lines.append(f"身高：{profile['height_cm']:.1f} cm")
        if profile.get("updated_at"):
            lines.append(f"更新时间：{profile['updated_at']}")
        lines.append(RECORD_NOTICE)
        return "\n".join(lines)

    @staticmethod
    def _format_weights(records):
        """把体重记录列表格式化为中文文本。"""
        if not records:
            return "暂无体重记录。"
        lines = [f"【体重记录】（最近 {len(records)} 条）"]
        for record in records:
            line = (
                f"- 编号 {record['id']}：{record['weight_kg']:.1f} kg，"
                f"日期 {record['record_date']}"
            )
            if record.get("note"):
                line += f"，备注：{record['note']}"
            lines.append(line)
        lines.append(RECORD_NOTICE)
        return "\n".join(lines)