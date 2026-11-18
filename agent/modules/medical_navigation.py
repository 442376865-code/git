"""医疗导航模块（离线 MVP）。

只做一件事：保存和查看用户自己录入的常用就医地点。

明确不做：
- 不联网、不接地图 API、不获取定位；
- 不声称能查询真实的"附近"地点；
- 不提供距离、路线或地图链接。

输入包含"急诊"时，结果前会附加紧急就医提示。
"""

import re

from .base import MedicalModule
from .medical_navigation_db import CATEGORIES, MedicalNavigationDB

# 空输入或无法识别时的帮助提示
HELP_MESSAGE = (
    "我可以帮你保存和查看常用就医地点（离线记录，不联网）。\n"
    "新增地点示例：添加就医地点，类型 医院，名称 XX医院，地址 XX路，电话 XXX。\n"
    "查看全部：查看就医地点。\n"
    "搜索地点：搜索就医地点 医院。\n"
    "删除地点：删除就医地点 1。"
)

# 实时导航请求的安全回退说明
NO_MAP_REPLY = "当前未接入实时地图和定位服务。可先查看已保存的常用就医地点。"

# 紧急情况提示
EMERGENCY_REPLY = "如属紧急情况，请立即拨打120或前往最近医院急诊。"

# 查询结果附带的中性提示
RECORD_NOTICE = "以上信息由你自行录入，仅供参考，不能替代医生诊疗。"

# 类型同义词：用户写法 → 数据库规范类型（规范类型仍只有 CATEGORIES 中的五种）
CATEGORY_SYNONYMS = {"药店": "药房"}

# 新增地点的字段识别规则
NAME_RE = re.compile(r"名称\s*[:：]?\s*([^\s，,。;；]+)")
CATEGORY_RE = re.compile(r"类型\s*[:：]?\s*([^\s，,。;；]+)")
ADDRESS_RE = re.compile(
    r"地址\s*[:：]?\s*(.+?)(?=\s*(?:电话|备注|类型|名称)\s*[:：]?|$)"
)
PHONE_RE = re.compile(r"电话\s*[:：]?\s*([^\s，,。;；]+)")
NOTE_RE = re.compile(r"备注\s*[:：]?\s*(.+)$")

# 删除 / 搜索 / 查看 / 新增意图
DELETE_RE = re.compile(r"(?:删除|移除)\s*就医地点\s*(?:编号|id|#)?\s*[:：]?\s*(\d+)")
DELETE_INTENT_RE = re.compile(r"(?:删除|移除)\s*就医地点")
ADD_INTENT_RE = re.compile(r"(?:添加|新增)\s*就医地点")
SEARCH_RE = re.compile(r"(?:搜索|查找|查询)\s*就医地点\s*[:：]?\s*(.*)$")
VIEW_RE = re.compile(r"查看就医地点|就医地点列表|全部就医地点|常用就医地点|常用地点")

# 实时导航类请求（不提供真实路线）
LIVE_NAV_RE = re.compile(r"附近|周边|怎么去|怎么走|导航到|导航|路线|离我")


def _normalize_category(value):
    """把类型同义词归一化为数据库规范类型。"""
    return CATEGORY_SYNONYMS.get(value, value)


class MedicalNavigationModule(MedicalModule):
    """医疗导航模块：离线保存常用就医地点。"""

    # 模块名称，必须与 Router 返回的模块名一致
    name = "medical_navigation"
    description = "医疗导航：离线保存和查看常用就医地点，不提供实时地图或定位"

    def __init__(self, db_path=None):
        """初始化模块并打开就医地点数据库。

        参数：
            db_path: 可选的数据库路径，测试时可传 ":memory:" 使用内存库。
        """
        self.db = MedicalNavigationDB(db_path)
        self.db.initialize()

    def run(self, user_input, context=None):
        """识别新增/查看/搜索/删除/实时导航意图并返回中文结果。"""
        # 非字符串或空内容：返回帮助提示
        if not isinstance(user_input, str) or not user_input.strip():
            return HELP_MESSAGE

        text = user_input.strip()
        # 只要提到"急诊"，都在结果前附加紧急提示
        emergency = "急诊" in text

        # 1. 删除地点
        delete_match = DELETE_RE.search(text)
        if delete_match:
            reply = self._handle_delete(int(delete_match.group(1)))
        elif DELETE_INTENT_RE.search(text):
            reply = "请提供正确的就医地点编号。\n示例：删除就医地点 1"
        # 2. 新增地点
        elif ADD_INTENT_RE.search(text):
            reply = self._handle_add(text)
        # 3. 按名称或类型搜索
        elif SEARCH_RE.search(text):
            reply = self._handle_search(SEARCH_RE.search(text).group(1))
        # 4. 查看全部
        elif VIEW_RE.search(text):
            reply = self._format_places(self.db.list_places())
        # 5. 实时导航请求：明确说明未接入，并列出已保存的匹配地点
        elif LIVE_NAV_RE.search(text):
            reply = self._handle_live_nav(text)
        # 6. 无法识别
        else:
            reply = HELP_MESSAGE

        if emergency:
            return EMERGENCY_REPLY + "\n\n" + reply
        return reply

    def _handle_add(self, text):
        """校验必填名称与类型后新增地点。"""
        name_match = NAME_RE.search(text)
        name = name_match.group(1).strip() if name_match else ""
        if not name:
            return (
                "请提供就医地点名称。\n"
                "示例：添加就医地点，类型 医院，名称 XX医院，地址 XX路，电话 XXX"
            )

        category_match = CATEGORY_RE.search(text)
        category = category_match.group(1).strip() if category_match else "其他"
        # "药店"等写法统一存为规范类型"药房"
        category = _normalize_category(category)
        if category not in CATEGORIES:
            return f"类型仅支持：{'、'.join(CATEGORIES)}。"

        address = self._optional_field(text, ADDRESS_RE)
        phone = self._optional_field(text, PHONE_RE)
        note = self._optional_field(text, NOTE_RE)

        place_id = self.db.create_place(
            name,
            category=category,
            address=address,
            phone=phone,
            note=note,
        )
        return f"已添加就医地点（编号 {place_id}）。\n" + self._format_places(
            self.db.list_places()
        )

    def _handle_delete(self, place_id):
        """按编号删除地点，找不到时给出提示。"""
        if not self.db.delete_place(place_id):
            return f"未找到编号为 {place_id} 的就医地点。"
        return f"已删除编号 {place_id} 的就医地点。\n" + self._format_places(
            self.db.list_places()
        )

    def _handle_search(self, keyword):
        """按名称或类型搜索地点，类型同义词先归一化。"""
        keyword = (keyword or "").strip().strip("，,。;；")
        if not keyword:
            return "请输入要搜索的名称或类型关键词。\n示例：搜索就医地点 医院"
        return self._format_places(
            self.db.search_places(_normalize_category(keyword))
        )

    def _handle_live_nav(self, text):
        """实时导航请求：说明未接入服务，并列出已保存的匹配地点。"""
        lines = [NO_MAP_REPLY]
        # 从输入里识别就医类型（含同义词），用来筛选已保存的地点
        for category in CATEGORIES:
            words = [category]
            words.extend(
                synonym
                for synonym, standard in CATEGORY_SYNONYMS.items()
                if standard == category
            )
            if any(word in text for word in words):
                places = self.db.search_places(category)
                if places:
                    lines.append(self._format_places(places))
                break
        return "\n".join(lines)

    @staticmethod
    def _optional_field(text, pattern):
        """读取可选字段；未提供时返回 None。"""
        match = pattern.search(text)
        if not match:
            return None
        value = match.group(1).strip().strip("，,。;；")
        return value or None

    @staticmethod
    def _format_places(places):
        """把地点列表格式化为中文文本。"""
        if not places:
            return "暂无就医地点。"
        lines = ["【就医地点】"]
        for place in places:
            line = f"- 编号 {place['id']}：{place['name']}（{place['category']}）"
            if place.get("address"):
                line += f"｜地址：{place['address']}"
            if place.get("phone"):
                line += f"｜电话：{place['phone']}"
            if place.get("note"):
                line += f"｜备注：{place['note']}"
            lines.append(line)
        lines.append(RECORD_NOTICE)
        return "\n".join(lines)