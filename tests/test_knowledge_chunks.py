"""知识片段（knowledge_chunks + chunks_fts）的单元测试。

覆盖：表与索引创建、来源字段完整、来源追溯、page 空值处理、
幂等导入、scope 人群隔离、首批 7 个主题可检索、暂缓主题不导入，
以及原有卡片回答行为保持不变。
全部使用内存库离线执行，不联网，不调用大模型。
"""

import unittest

from agent.modules.knowledge_chunks import KNOWLEDGE_CHUNKS
from agent.modules.knowledge_db import KnowledgeDB
from agent.modules.medical_knowledge import (
    MULTI_TOPIC_MESSAGE,
    MedicalKnowledgeModule,
    NOT_FOUND_MESSAGE,
    SAFETY_NOTICE,
    SCOPE_NOTICE,
)

# 首批正式主题
EXPECTED_TOPICS = {
    "高血压",
    "2型糖尿病",
    "冠心病",
    "脑卒中",
    "哮喘",
    "流感",
    "儿童肺炎",
}

# 本阶段禁止导入的主题
EXCLUDED_TOPICS = {"普通感冒", "胃炎", "过敏性疾病", "成人肺炎"}


class KnowledgeChunksSchemaTest(unittest.TestCase):
    """知识片段表与 FTS5 表的结构测试。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()

    def tearDown(self):
        self.db.close()

    def test_chunks_table_created(self):
        """knowledge_chunks 表必须可以创建。"""
        row = self.db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'knowledge_chunks'"
        ).fetchone()
        self.assertIsNotNone(row)

    def test_chunks_fts_created(self):
        """chunks_fts 必须创建，且原有 knowledge_fts 仍然存在。"""
        names = {
            row["name"]
            for row in self.db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE name IN ('chunks_fts', 'knowledge_fts')"
            ).fetchall()
        }
        self.assertEqual(names, {"chunks_fts", "knowledge_fts"})

    def test_seed_chunks_imported(self):
        """初始化后应导入全部种子片段。"""
        self.assertEqual(len(self.db.list_chunks()), len(KNOWLEDGE_CHUNKS))

    def test_fts_rows_match_chunks(self):
        """全文索引行数必须与片段表行数一致。"""
        total = self.db._conn.execute(
            "SELECT COUNT(*) AS total FROM chunks_fts"
        ).fetchone()["total"]
        self.assertEqual(total, len(KNOWLEDGE_CHUNKS))

    def test_source_fields_complete(self):
        """每条片段都要有完整来源字段，网页来源不填页码。"""
        required = (
            "source_title",
            "source_publisher",
            "source_edition",
            "source_url",
            "source_section",
            "source_page",
            "source_level",
            "accessed_at",
            "scope",
        )
        for chunk in self.db.list_chunks():
            with self.subTest(chunk=chunk["disease_name"]):
                for field in required:
                    self.assertIn(field, chunk)
                self.assertTrue(chunk["source_title"].strip())
                self.assertEqual(chunk["source_publisher"], "世界卫生组织")
                self.assertTrue(
                    chunk["source_url"].startswith("https://www.who.int/")
                )
                self.assertEqual(chunk["source_level"], 1)
                self.assertEqual(chunk["accessed_at"], "2026-11-18")
                # 网页来源统一空页码，不得猜页码
                self.assertEqual(chunk["source_page"], "")

    def test_source_section_readable(self):
        """已核验章节的主题必须能读出非空章节名。"""
        sections = {
            chunk["source_section"]
            for chunk in self.db.list_chunks()
            if chunk["disease_name"] == "脑卒中"
        }
        self.assertIn("症状", sections)
        self.assertIn("危险因素", sections)

    def test_scope_recorded(self):
        """每个主题的 scope 必须按方案记录，儿童肺炎单独标注。"""
        scopes = {
            chunk["disease_name"]: chunk["scope"]
            for chunk in self.db.list_chunks()
        }
        self.assertEqual(scopes["高血压"], "成人高血压")
        self.assertEqual(scopes["2型糖尿病"], "2型糖尿病")
        self.assertEqual(scopes["冠心病"], "冠心病")
        self.assertEqual(scopes["脑卒中"], "脑卒中")
        self.assertEqual(scopes["哮喘"], "哮喘")
        self.assertEqual(scopes["流感"], "季节性流感")
        self.assertEqual(scopes["儿童肺炎"], "儿童肺炎")


class KnowledgeChunksImportTest(unittest.TestCase):
    """导入幂等与暂缓主题测试。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()

    def tearDown(self):
        self.db.close()

    def test_import_is_idempotent(self):
        """重复导入不得产生重复片段。"""
        before = len(self.db.list_chunks())
        self.db.import_chunks()
        self.db.import_chunks()
        self.assertEqual(len(self.db.list_chunks()), before)

    def test_initialize_twice_is_idempotent(self):
        """重复 initialize 同样不得产生重复片段。"""
        before = len(self.db.list_chunks())
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(len(self.db.list_chunks()), before)

    def test_chunk_id_stable(self):
        """同一业务键必须始终得到同一个 chunk_id。"""
        first = {chunk["chunk_id"] for chunk in self.db.list_chunks()}
        self.db.import_chunks()
        second = {chunk["chunk_id"] for chunk in self.db.list_chunks()}
        self.assertEqual(first, second)

    def test_fts_rows_after_reimport(self):
        """重复导入后全文索引行数仍与片段数一致。"""
        self.db.import_chunks()
        total = self.db._conn.execute(
            "SELECT COUNT(*) AS total FROM chunks_fts"
        ).fetchone()["total"]
        self.assertEqual(total, len(self.db.list_chunks()))

    def test_excluded_topics_absent(self):
        """暂缓主题不得产生任何正式片段。"""
        names = {chunk["disease_name"] for chunk in self.db.list_chunks()}
        self.assertFalse(names & EXCLUDED_TOPICS)

class KnowledgeChunksTopicTest(unittest.TestCase):
    """首批 7 个主题的检索测试。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()

    def tearDown(self):
        self.db.close()

    def test_seven_topics_present(self):
        """首批 7 个主题必须全部存在，且没有多余主题。"""
        names = {chunk["disease_name"] for chunk in self.db.list_chunks()}
        self.assertEqual(names, EXPECTED_TOPICS)

    def test_each_topic_recallable(self):
        """每个主题都能被对应问题召回。"""
        cases = [
            ("高血压要注意什么", "高血压"),
            ("2型糖尿病饮食怎么控制", "2型糖尿病"),
            ("冠心病有什么症状", "冠心病"),
            ("脑卒中怎么预防", "脑卒中"),
            ("哮喘怎么管理", "哮喘"),
            ("流感怎么预防", "流感"),
            ("儿童肺炎有哪些表现", "儿童肺炎"),
        ]
        for text, topic in cases:
            with self.subTest(text=text):
                names = {
                    chunk["disease_name"] for chunk in self.db.match_chunks(text)
                }
                self.assertIn(topic, names)


class KnowledgeChunksScopeTest(unittest.TestCase):
    """scope 人群隔离测试。"""

    def setUp(self):
        self.db = KnowledgeDB(":memory:")
        self.db.initialize()
        self.module = MedicalKnowledgeModule(db_path=":memory:")

    def tearDown(self):
        self.db.close()
        self.module.db.close()

    def test_child_chunk_hit_when_asked_for_child(self):
        """明确问儿童时可以命中儿童肺炎片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.db.match_chunks("儿童肺炎有哪些表现")
        }
        self.assertIn("儿童肺炎", names)

    def test_adult_question_not_hit_child_chunk(self):
        """成人、老人、社区或医院获得性肺炎都不得命中儿童肺炎片段。"""
        cases = (
            "成人肺炎怎么办",
            "老年人肺炎怎么办",
            "成人社区获得性肺炎怎么办",
            "医院获得性肺炎怎么办",
        )
        for text in cases:
            with self.subTest(text=text):
                names = {
                    chunk["disease_name"] for chunk in self.db.match_chunks(text)
                }
                self.assertNotIn("儿童肺炎", names)

    def test_plain_pneumonia_not_hit_child_chunk(self):
        """未说明人群的「肺炎」不得被儿童限定资料冒充。"""
        names = {
            chunk["disease_name"] for chunk in self.db.match_chunks("肺炎严重吗")
        }
        self.assertNotIn("儿童肺炎", names)

    def test_adult_pneumonia_returns_not_found(self):
        """成人肺炎问题必须返回未收录提示，而不是儿童肺炎内容。"""
        result = self.module.run("成人肺炎怎么办")
        self.assertEqual(result, NOT_FOUND_MESSAGE)
        self.assertNotIn("儿童肺炎", result)

    def test_plain_pneumonia_returns_not_found(self):
        """「肺炎严重吗」保持未收录，符合既有安全基线。"""
        self.assertEqual(self.module.run("肺炎严重吗"), NOT_FOUND_MESSAGE)

    def test_child_answer_shows_scope_notice(self):
        """命中儿童肺炎时必须提示适用范围。"""
        result = self.module.run("儿童肺炎有哪些表现")
        self.assertIn("儿童肺炎", result)
        self.assertIn(SCOPE_NOTICE.format(scope="儿童肺炎"), result)

    def test_asthma_not_used_for_allergy(self):
        """「过敏性疾病」不得因为哮喘片段含过敏相关词而命中哮喘。"""
        names = {
            chunk["disease_name"]
            for chunk in self.db.match_chunks("过敏性疾病怎么办")
        }
        self.assertNotIn("哮喘", names)
        # 片段层没有过敏主题，回答中同样不得出现哮喘内容
        self.assertNotIn("哮喘", self.module.run("过敏性疾病怎么办"))

    def test_chd_not_used_for_myocardial_infarction(self):
        """心肌梗死问题不得被当成冠心病来源。"""
        names = {
            chunk["disease_name"]
            for chunk in self.db.match_chunks("心肌梗死怎么办")
        }
        self.assertNotIn("冠心病", names)

    def test_stroke_not_used_for_headache(self):
        """头痛问题不得命中脑卒中片段。"""
        names = {
            chunk["disease_name"] for chunk in self.db.match_chunks("头痛怎么办")
        }
        self.assertNotIn("脑卒中", names)


class KnowledgeChunksOutputTest(unittest.TestCase):
    """回答中的来源展示测试。"""

    def setUp(self):
        self.module = MedicalKnowledgeModule(db_path=":memory:")

    def tearDown(self):
        self.module.db.close()

    def test_chunk_answer_shows_source(self):
        """仅命中片段时，回答必须展示来源标题、章节、链接和核验日期。"""
        result = self.module.run("脑卒中怎么预防")
        self.assertIn("【脑卒中】来源摘要", result)
        self.assertIn("《卒中（实况报道）》", result)
        self.assertIn("世界卫生组织", result)
        self.assertIn("章节：", result)
        self.assertIn(
            "https://www.who.int/zh/news-room/fact-sheets/detail/stroke", result
        )
        self.assertIn("核验日期：2026-11-18", result)
        self.assertIn(SAFETY_NOTICE, result)

    def test_empty_page_not_rendered(self):
        """网页来源没有页码时，不得输出页码行或占位垃圾文本。"""
        result = self.module.run("脑卒中怎么预防")
        self.assertNotIn("页码：", result)
        self.assertNotIn("第0页", result)
        self.assertNotIn("第 0 页", result)
        self.assertNotIn("None", result)

    def test_card_answer_appends_chunk_summary(self):
        """卡片命中时应在原有内容后追加来源摘要。"""
        result = self.module.run("高血压平时要注意什么")
        self.assertIn("【高血压】科普知识卡", result)
        self.assertIn("来源摘要：", result)
        self.assertIn("《高血压（实况报道）》", result)
        self.assertIn("补充来源：", result)

    def test_card_answer_keeps_original_sections(self):
        """追加片段后，卡片原有分段必须完整保留。"""
        result = self.module.run("高血压")
        for text in (
            "概述：",
            "常见表现：",
            "危险因素：",
            "日常管理建议：",
            "信息来源：",
            SAFETY_NOTICE,
        ):
            self.assertIn(text, result)

    def test_chd_and_stroke_hit_via_module(self):
        """冠心病与脑卒中在没有卡片时也能通过片段命中。"""
        self.assertIn("冠心病", self.module.run("冠心病要注意什么"))
        self.assertIn("脑卒中", self.module.run("脑卒中怎么预防"))


class MedicalKnowledgeRegressionTest(unittest.TestCase):
    """原有卡片行为不得因为新增片段而改变。"""

    def setUp(self):
        self.module = MedicalKnowledgeModule(db_path=":memory:")

    def tearDown(self):
        self.module.db.close()

    def test_same_card_for_name_and_alias(self):
        """名称与别名仍然返回完全相同的内容。"""
        self.assertEqual(self.module.run("高血压"), self.module.run("血压高"))

    def test_unknown_topic_returns_not_found(self):
        """未收录主题仍然返回固定提示。"""
        for text in (
            "如何预防近视",
            "我没听说过这种病",
            "帕金森病是什么",
            "咳嗽怎么办",
            "头痛怎么办",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.module.run(text), NOT_FOUND_MESSAGE)

    def test_empty_input_returns_not_found(self):
        """空输入与非字符串输入安全回退，不抛异常。"""
        for value in ("", "   ", None, 123):
            with self.subTest(value=value):
                self.assertEqual(self.module.run(value), NOT_FOUND_MESSAGE)

    def test_multi_topic_returns_hint(self):
        """一次命中多个主题仍然返回分别查询提示。"""
        self.assertEqual(self.module.run("高血压合并糖尿病"), MULTI_TOPIC_MESSAGE)

    def test_no_drug_or_dosage_in_chunks(self):
        """片段内容不得出现药物名称或剂量信息。"""
        for chunk in KNOWLEDGE_CHUNKS:
            with self.subTest(chunk=chunk["disease_name"]):
                text = chunk["content"]
                for keyword in ("mg", "毫克", "剂量", "处方", "mmol/L"):
                    self.assertNotIn(keyword, text)

    def test_knowledge_cards_unchanged(self):
        """新增片段不得改变原有知识卡片的内容。"""
        from agent.modules.knowledge_cards import KNOWLEDGE_CARDS

        cards = {card["disease_name"]: card for card in self.module.db.list_cards()}
        for seed in KNOWLEDGE_CARDS:
            with self.subTest(card=seed["disease_name"]):
                self.assertEqual(cards[seed["disease_name"]], seed)


if __name__ == "__main__":
    unittest.main()