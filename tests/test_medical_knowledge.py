"""医学知识模块（SQLite + FTS5 知识库）的单元测试。

覆盖：名称/别名命中、字段完整性、紧急情况分级、来源结构、未收录回退、
安全提示、否定表达、多主题提示、模糊检索、接口不变。
全部使用内存库离线执行，不调用大模型，不访问网络。
"""

import inspect
import unittest

from agent.modules.base import MedicalModule
from agent.modules.knowledge_cards import KNOWLEDGE_CARDS
from agent.modules.medical_knowledge import (
    MULTI_TOPIC_MESSAGE,
    MedicalKnowledgeModule,
    NOT_FOUND_MESSAGE,
    SAFETY_NOTICE,
)

# 知识卡必须具备的字段清单
REQUIRED_FIELDS = [
    "disease_name",
    "aliases",
    "overview",
    "common_signs",
    "risk_factors",
    "daily_management",
    "urgent_signs",
    "emergency_signs",
    "sources",
    "updated_at",
]

# 高血压卡紧急情况必须覆盖的关键词
EMERGENCY_KEYWORDS = ["胸痛", "呼吸困难", "偏瘫", "言语不清", "意识"]


class MedicalKnowledgeModuleTest(unittest.TestCase):
    """知识卡查询与模块接口测试。"""

    def setUp(self):
        # 使用内存库，避免测试污染项目 data 目录
        self.module = MedicalKnowledgeModule(db_path=":memory:")

    def test_interface_unchanged(self):
        """模块必须继承基类，run 签名保持 (self, user_input, context=None)。"""
        self.assertIsInstance(self.module, MedicalModule)
        self.assertEqual(self.module.name, "medical_knowledge")
        params = list(inspect.signature(MedicalKnowledgeModule.run).parameters)
        self.assertEqual(params, ["self", "user_input", "context"])

    def test_name_hit(self):
        """问题包含"高血压"时必须命中高血压知识卡。"""
        result = self.module.run("高血压平时要注意什么")
        self.assertIn("【高血压】", result)
        self.assertIn("日常管理建议", result)

    def test_alias_hit(self):
        """问题包含别名"血压高"时必须命中同一张卡。"""
        result = self.module.run("血压高应该怎么调理")
        self.assertIn("【高血压】", result)

    def test_same_card_for_name_and_alias(self):
        """名称与别名返回同一张卡的内容。"""
        self.assertEqual(self.module.run("高血压"), self.module.run("血压高"))

    def test_type2_diabetes_hit(self):
        """2型糖尿病的名称与别名都能命中同一张卡。"""
        for text in ("2型糖尿病要注意什么", "糖尿病怎么预防", "二型糖尿病是什么"):
            with self.subTest(text=text):
                self.assertIn("【2型糖尿病】", self.module.run(text))

    def test_common_cold_hit(self):
        """普通感冒的名称与别名都能命中同一张卡。"""
        for text in ("普通感冒怎么办", "感冒了吃什么", "伤风了"):
            with self.subTest(text=text):
                self.assertIn("【普通感冒】", self.module.run(text))

    def test_fuzzy_partial_expression_hit(self):
        """FTS5 模糊检索应命中「血压有点高」这类非别名单词。"""
        result = self.module.run("血压有点高怎么办")
        self.assertIn("【高血压】科普知识卡", result)
        self.assertIn(SAFETY_NOTICE, result)

    def test_fuzzy_keyword_hit(self):
        """「血糖高」应模糊召回 2 型糖尿病知识卡。"""
        self.assertIn("【2型糖尿病】科普知识卡", self.module.run("血糖高"))

    def test_fuzzy_unknown_returns_not_found(self):
        """与知识卡无关的输入返回未收录提示。"""
        self.assertEqual(self.module.run("我没听说过这种病"), NOT_FOUND_MESSAGE)

    def test_common_cold_is_self_limiting(self):
        """普通感冒卡必须说明多为自限性病毒感染。"""
        card = next(c for c in KNOWLEDGE_CARDS if c["disease_name"] == "普通感冒")
        self.assertIn("自限性", card["overview"])
        self.assertIn("病毒", card["overview"])

    def test_diabetes_has_no_glucose_target(self):
        """糖尿病卡不得写具体血糖数值目标。"""
        card = next(c for c in KNOWLEDGE_CARDS if c["disease_name"] == "2型糖尿病")
        text = str(card["overview"])
        for field in ("common_signs", "risk_factors", "daily_management"):
            text += "".join(card[field])
        self.assertNotIn("mmol/L", text)
        self.assertNotIn("空腹血糖", text)

    def test_single_topic_returns_card(self):
        """单一主题仍返回完整知识卡。"""
        self.assertIn("【高血压】科普知识卡", self.module.run("高血压"))

    def test_multi_topic_returns_hint(self):
        """一次命中多个主题时返回分别查询提示。"""
        result = self.module.run("高血压合并糖尿病")
        self.assertEqual(result, MULTI_TOPIC_MESSAGE)
        self.assertIn("分别查询", result)

    def test_negative_expression_returns_card(self):
        """否定表达按包含关系命中知识卡，返回科普内容而非个人诊断。"""
        result = self.module.run("我没有高血压")
        self.assertIn("【高血压】科普知识卡", result)
        self.assertNotIn("您患有", result)
        self.assertNotIn("诊断为", result)

    def test_card_fields_complete(self):
        """每张知识卡必须包含 10 个字段，且核心字段非空。"""
        for card in KNOWLEDGE_CARDS:
            with self.subTest(card=card.get("disease_name")):
                for field in REQUIRED_FIELDS:
                    self.assertIn(field, card)
                    self.assertIsNotNone(card[field])
                # 文本字段必须非空
                for field in ("disease_name", "overview", "updated_at"):
                    self.assertTrue(str(card[field]).strip())
                # 列表字段必须是列表且至少一条
                for field in (
                    "aliases",
                    "common_signs",
                    "risk_factors",
                    "daily_management",
                    "urgent_signs",
                    "emergency_signs",
                    "sources",
                ):
                    self.assertIsInstance(card[field], list)
                    self.assertGreater(len(card[field]), 0)

    def test_emergency_signs_covered(self):
        """高血压卡紧急情况必须覆盖胸痛、呼吸困难、偏瘫、言语不清、意识改变。"""
        card = next(c for c in KNOWLEDGE_CARDS if c["disease_name"] == "高血压")
        text = "".join(card["emergency_signs"])
        for keyword in EMERGENCY_KEYWORDS:
            self.assertIn(keyword, text)

    def test_all_cards_emergency_mentions_120(self):
        """所有知识卡的紧急情况都必须提示拨打120或前往急诊。"""
        for card in KNOWLEDGE_CARDS:
            with self.subTest(card=card["disease_name"]):
                text = "".join(card["emergency_signs"])
                self.assertTrue("拨打120" in text or "急诊" in text)

    def test_emergency_output_mentions_120_and_er(self):
        """返回内容必须提示紧急情况立即拨打120或前往急诊。"""
        result = self.module.run("高血压")
        self.assertIn("拨打120", result)
        self.assertIn("急诊", result)

    def test_unknown_topic_returns_hint(self):
        """未收录主题必须返回固定提示。"""
        self.assertEqual(self.module.run("如何预防近视"), NOT_FOUND_MESSAGE)

    def test_empty_and_invalid_input(self):
        """空输入与非字符串输入安全回退，不抛异常。"""
        for value in ("", "   ", None, 123):
            with self.subTest(value=value):
                self.assertEqual(self.module.run(value), NOT_FOUND_MESSAGE)

    def test_answer_contains_safety_notice(self):
        """命中知识卡时返回内容必须包含安全提示。"""
        result = self.module.run("高血压")
        self.assertIn(SAFETY_NOTICE, result)
        self.assertIn("科普信息不能替代医生诊疗", result)

    def test_no_drug_names_or_dosage(self):
        """全部知识卡都不得出现药物名称或具体剂量信息。"""
        for card in KNOWLEDGE_CARDS:
            with self.subTest(card=card["disease_name"]):
                text = str(card["overview"])
                for field in (
                    "common_signs",
                    "risk_factors",
                    "daily_management",
                    "urgent_signs",
                    "emergency_signs",
                ):
                    text += "".join(card[field])
                for keyword in ("mg", "毫克", "剂量", "处方", "片/次", "氨氯地平"):
                    self.assertNotIn(keyword, text)

    def test_sources_structured(self):
        """来源必须为结构化字段；url 可为空，非空时必须是合法链接。"""
        for card in KNOWLEDGE_CARDS:
            self.assertTrue(card["sources"])
            for source in card["sources"]:
                self.assertIsInstance(source, dict)
                with self.subTest(source=source.get("title")):
                    for field in ("title", "publisher", "edition", "url"):
                        self.assertIn(field, source)
                    self.assertTrue(str(source["title"]).strip())
                    self.assertTrue(str(source["publisher"]).strip())
                    self.assertTrue(str(source["edition"]).strip())
                    url = source["url"]
                    self.assertIsInstance(url, str)
                    if url:
                        self.assertTrue(
                            url.startswith("http://") or url.startswith("https://")
                        )


if __name__ == "__main__":
    unittest.main()