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

# 使用范围受限的来源时，必须明确提示适用范围
SCOPE_NOTICE = "本条信息仅适用于{scope}，不适用于其他人群。"

# 在卡片回答中追加来源摘要时使用的小标题
CHUNK_SECTION_TITLE = "来源摘要："

# 追加来源摘要时使用的小标题
CHUNK_SOURCE_TITLE = "补充来源："


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
            card = exact_cards[0]
            return self._format_card(card, self._match_card_chunks(text, card))

        # 第二步：FTS5 模糊召回，仅当唯一最高分卡片时返回
        fuzzy_cards = self.db.match_fuzzy(text)
        if len(fuzzy_cards) == 1:
            card = fuzzy_cards[0]
            return self._format_card(card, self._match_card_chunks(text, card))

        # 第三步：卡片未命中时，用来源级知识片段补充
        chunks = self.db.match_chunks(text)
        if chunks:
            names = {chunk["disease_name"] for chunk in chunks}
            if len(names) > 1:
                return MULTI_TOPIC_MESSAGE
            return self._format_chunks(chunks)

        return NOT_FOUND_MESSAGE

    def _match_card_chunks(self, text, card):
        """只保留与当前卡片同主题的片段，避免其他主题内容混进卡片回答。"""
        name = card["disease_name"]
        return [
            chunk
            for chunk in self.db.match_chunks(text)
            if chunk["disease_name"] == name
        ]

    @staticmethod
    def _format_card(card, chunks=None):
        """把知识卡格式化成清晰的中文分段文本。

        参数：
            chunks: 可选的知识片段；有值时在安全提示前追加来源摘要小节。
        """
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
            # 只展示有值的字段，避免出现空行
            if source.get("section"):
                lines.append(f"  章节：{source['section']}")
            if source.get("page"):
                lines.append(f"  页码：{source['page']}")
            if source.get("url"):
                lines.append(f"  链接：{source['url']}")
            if source.get("accessed_at"):
                lines.append(f"  核验日期：{source['accessed_at']}")
        if chunks:
            lines += ["", CHUNK_SECTION_TITLE]
            for index, chunk in enumerate(chunks, start=1):
                lines.append(f"{index}. {chunk['content']}")
            lines += ["", CHUNK_SOURCE_TITLE]
            lines += MedicalKnowledgeModule._format_chunk_sources(chunks)
        lines += ["", SAFETY_NOTICE, f"知识卡更新时间：{card['updated_at']}"]
        return "\n".join(lines)

    @classmethod
    def _format_chunks(cls, chunks):
        """只命中知识片段时，输出来源摘要模式。"""
        names = {chunk["disease_name"] for chunk in chunks}
        disease_name = names.pop() if len(names) == 1 else "医学知识"

        lines = [f"【{disease_name}】来源摘要", ""]

        # 范围受限的来源（例如儿童肺炎）必须明确提示适用范围
        restricted = [c for c in chunks if "儿童" in (c.get("scope") or "")]
        if restricted:
            lines.append(SCOPE_NOTICE.format(scope=restricted[0]["scope"]))
            lines.append("")

        for index, chunk in enumerate(chunks, start=1):
            lines.append(f"{index}. {chunk['content']}")

        lines += ["", "信息来源："]
        lines += cls._format_chunk_sources(chunks)
        lines += ["", SAFETY_NOTICE]
        return "\n".join(lines)

    @staticmethod
    def _format_chunk_source(chunk):
        """把一条片段的来源渲染成若干行；空字段直接跳过，避免出现空标签。"""
        line = f"- 《{chunk['source_title']}》"
        detail = "，".join(
            item
            for item in (chunk.get("source_publisher"), chunk.get("source_edition"))
            if item
        )
        if detail:
            line += f"（{detail}）"
        lines = [line]
        if chunk.get("source_section"):
            lines.append(f"  章节：{chunk['source_section']}")
        if chunk.get("source_page"):
            lines.append(f"  页码：{chunk['source_page']}")
        if chunk.get("source_url"):
            lines.append(f"  链接：{chunk['source_url']}")
        if chunk.get("accessed_at"):
            lines.append(f"  核验日期：{chunk['accessed_at']}")
        return lines
    @classmethod
    def _format_chunk_sources(cls, chunks):
        """把多条片段的来源去重后渲染，同一来源与章节只展示一次。"""
        seen = set()
        lines = []
        for chunk in chunks:
            key = (
                chunk.get("source_title"),
                chunk.get("source_section"),
                chunk.get("source_page"),
                chunk.get("source_url"),
                chunk.get("accessed_at"),
            )
            if key in seen:
                continue
            seen.add(key)
            lines += cls._format_chunk_source(chunk)
        return lines