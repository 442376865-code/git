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


if __name__ == "__main__":
    unittest.main()