"""医学知识问答模块（SQLite + FTS5 知识库版）。

知识卡存放在本地 SQLite 数据库，通过 FTS5 全文索引支持关键词与模糊检索；
不联网，不提供诊断、处方或用药建议。
"""

from .base import MedicalModule
from .knowledge_db import KnowledgeDB

# 未收录主题时的固定提示
NOT_FOUND_MESSAGE = "暂未收录该主题，建议咨询专业医生"

# 一次命中多个主题时的固定提示
MULTI_TOPIC_MESSAGE = "当前一次仅支持一个主题，请分别查询"

# 所有回答都附带的安全提示
SAFETY_NOTICE = "科普信息不能替代医生诊疗，如有不适请及时就医。"


class MedicalKnowledgeModule(MedicalModule):
    """医学知识问答模块。"""

    # 模块名称，必须与 Router 返回的模块名一致
    name = "medical_knowledge"
    description = "医学知识问答：解释疾病、症状、指标等医学常识问题"

    def __init__(self, db_path=None):
        """初始化模块并打开知识库。

        参数：
            db_path: 可选的数据库路径，测试时可传 ":memory:" 使用内存库。
        """
        self.db = KnowledgeDB(db_path)
        self.db.initialize()

    def run(self, user_input, context=None):
        """在知识库中查找疾病，命中则返回分段科普内容。"""
        # 非字符串或空内容：安全回退到未收录提示
        if not isinstance(user_input, str) or not user_input.strip():
            return NOT_FOUND_MESSAGE

        text = user_input.strip()

        # 第一步：疾病名/别名精确命中，优先级最高
        exact_cards = self.db.match_exact(text)
        if exact_cards:
            # 一次命中多个主题时，提示用户分别查询，避免混答
            if len(exact_cards) > 1:
                return MULTI_TOPIC_MESSAGE
            return self._format_card(exact_cards[0])

        # 第二步：FTS5 模糊召回，仅当唯一最高分卡片时返回
        fuzzy_cards = self.db.match_fuzzy(text)
        if len(fuzzy_cards) == 1:
            return self._format_card(fuzzy_cards[0])

        return NOT_FOUND_MESSAGE

    @staticmethod
    def _format_card(card):
        """把知识卡格式化成清晰的中文分段文本。"""
        lines = [
            f"【{card['disease_name']}】科普知识卡",
            "",
            "概述：",
            card["overview"],
            "",
            "常见表现：",
        ]
        lines += [f"- {item}" for item in card["common_signs"]]
        lines += ["", "危险因素："]
        lines += [f"- {item}" for item in card["risk_factors"]]
        lines += ["", "日常管理建议："]
        lines += [f"- {item}" for item in card["daily_management"]]
        lines += ["", "出现以下情况请尽快就医："]
        lines += [f"- {item}" for item in card["urgent_signs"]]
        lines += ["", "出现以下紧急情况请立即拨打120或前往急诊："]
        lines += [f"- {item}" for item in card["emergency_signs"]]
        lines += ["", "信息来源："]
        for source in card["sources"]:
            line = f"- 《{source['title']}》"
            detail = "，".join(
                item for item in (source.get("publisher"), source.get("edition")) if item
            )
            if detail:
                line += f"（{detail}）"
            lines.append(line)
            if source.get("url"):
                lines.append(f"  链接：{source['url']}")
        lines += ["", SAFETY_NOTICE, f"知识卡更新时间：{card['updated_at']}"]
        return "\n".join(lines)