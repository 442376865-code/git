"""SQLite + FTS5 知识库的单元测试。

覆盖：种子导入、幂等初始化、别名精确匹配、FTS5 模糊召回、
未收录回退、空输入、JSON 字段往返一致。
全部使用内存库，离线执行，不访问网络。
"""

import unittest

from agent.modules.knowledge_cards import KNOWLEDGE_CARDS
from agent.modules.knowledge_db import KnowledgeDB


class KnowledgeDBTest(unittest.TestCase):
    """知识库建库与检索测试。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()

    def tearDown(self):
        self.db.close()

    def test_seed_imported(self):
        """初始化后应导入全部种子知识卡。"""
        cards = self.db.list_cards()
        self.assertEqual(len(cards), len(KNOWLEDGE_CARDS))
        self.assertEqual(
            {card["disease_name"] for card in cards},
            {card["disease_name"] for card in KNOWLEDGE_CARDS},
        )

    def test_initialize_is_idempotent(self):
        """重复初始化不得产生重复卡片。"""
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(len(self.db.list_cards()), len(KNOWLEDGE_CARDS))

    def test_card_json_roundtrip(self):
        """入库再读出的卡片字段必须与种子一致。"""
        cards = {card["disease_name"]: card for card in self.db.list_cards()}
        for seed in KNOWLEDGE_CARDS:
            with self.subTest(card=seed["disease_name"]):
                self.assertEqual(cards[seed["disease_name"]], seed)

    def test_exact_disease_name(self):
        """疾病名精确命中。"""
        cards = self.db.match_exact("高血压平时要注意什么")
        self.assertEqual([card["disease_name"] for card in cards], ["高血压"])

    def test_exact_alias(self):
        """别名子串精确命中同一张卡。"""
        cards = self.db.match_exact("血压高应该怎么调理")
        self.assertEqual([card["disease_name"] for card in cards], ["高血压"])

    def test_exact_multi_topic(self):
        """一句话命中多个主题时返回多张卡。"""
        cards = self.db.match_exact("高血压合并糖尿病")
        names = {card["disease_name"] for card in cards}
        self.assertEqual(names, {"高血压", "2型糖尿病"})

    def test_exact_unknown_returns_empty(self):
        """未收录主题精确匹配为空。"""
        self.assertEqual(self.db.match_exact("如何预防近视"), [])

    def test_fuzzy_partial_expression(self):
        """FTS5 通过 2-gram 命中「血压有点高」这类非别名单词。"""
        cards = self.db.match_fuzzy("血压有点高")
        self.assertEqual([card["disease_name"] for card in cards], ["高血压"])

    def test_fuzzy_glucose_keyword(self):
        """「血糖高」应模糊召回 2 型糖尿病卡。"""
        cards = self.db.match_fuzzy("血糖高")
        self.assertEqual([card["disease_name"] for card in cards], ["2型糖尿病"])

    def test_fuzzy_unknown_returns_empty(self):
        """与知识卡无关的输入模糊召回为空。"""
        self.assertEqual(self.db.match_fuzzy("如何预防近视"), [])

    def test_empty_and_invalid_input(self):
        """空输入与非字符串输入安全返回空列表。"""
        for value in ("", "   ", None, 123):
            with self.subTest(value=value):
                self.assertEqual(self.db.match_exact(value), [])
                self.assertEqual(self.db.match_fuzzy(value), [])


class KnowledgeFuzzyPrecisionTest(unittest.TestCase):
    """FTS5 模糊召回精度：过滤通用 2-gram，只凭强证据召回。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()

    def tearDown(self):
        self.db.close()

    def _resolve(self, text):
        """模拟医学知识模块的检索顺序：精确优先，精确未命中再模糊召回。"""
        cards = self.db.match_exact(text) or self.db.match_fuzzy(text)
        return [card["disease_name"] for card in cards]

    def test_expected_cards_still_recalled(self):
        """正常问法仍然稳定命中对应知识卡。"""
        cases = [
            ("高血压平时要注意什么", "高血压"),
            ("血压有点高怎么办", "高血压"),
            ("高血压饮食注意什么", "高血压"),
            ("2型糖尿病是什么", "2型糖尿病"),
            ("血糖高怎么办", "2型糖尿病"),
            ("感冒了怎么办", "普通感冒"),
            ("如何预防感冒", "普通感冒"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self._resolve(text), [expected])

    def test_parkinson_not_recalled_as_diabetes(self):
        """「帕金森病是什么」不得因「病是」「什么」误召回 2 型糖尿病。"""
        cards = self.db.match_fuzzy("帕金森病是什么")
        self.assertEqual(cards, [])
        self.assertNotIn(
            "2型糖尿病", [card["disease_name"] for card in cards]
        )

    def test_headache_not_recalled_as_hypertension(self):
        """「头痛怎么办」不得仅凭症状弱 token 召回高血压。"""
        self.assertEqual(self.db.match_fuzzy("头痛怎么办"), [])

    def test_symptom_only_token_is_not_enough(self):
        """只命中症状等弱证据时，同样不得召回知识卡。"""
        self.assertEqual(self.db.match_fuzzy("头痛"), [])

    def test_stop_tokens_only_returns_empty(self):
        """去掉停用词后没有有效检索词时，直接返回空结果。"""
        self.assertEqual(self.db.match_fuzzy("怎么办"), [])

    def test_unrelated_text_returns_empty(self):
        """完全无关文本必须返回空结果，不做猜测。"""
        self.assertEqual(self.db.match_fuzzy("完全无关文本"), [])


if __name__ == "__main__":
    unittest.main()