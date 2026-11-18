"""医学检索词与别名安全扩展的单元测试。

覆盖 STEP 03.7-C-E 的两个修复点：
1. Router 医学词根补齐：哮喘 / 心肌梗死 / 心梗 / 卒中 / 中风 / 梗死；
2. 「中风 → 脑卒中」别名映射：WHO《卒中（实况报道）》正文明确写
   "卒中（俗称中风）"，属于有来源依据的安全映射。

同时回归验证：
- 心肌梗死 / 心梗不得误召回冠心病片段（强证据闸门仍然生效）；
- 过敏性疾病不得召回哮喘片段；
- 儿童肺炎的 scope 硬过滤完全保持；
- 危急症状前置拦截仍然优先于一切检索。
全部使用内存库离线执行，不联网、不调用真实大模型、不写 data/ 目录。
"""

import unittest
from unittest import mock

from agent.main import Agent, EMERGENCY_REPLY
from agent.modules.medical_knowledge import (
    MedicalKnowledgeModule,
    NOT_FOUND_MESSAGE,
)
from agent.router import Router


def _new_agent():
    """构造带内存医学知识库的 Agent，避免测试触碰正式数据库。"""
    module = MedicalKnowledgeModule(db_path=":memory:")
    return Agent(modules={"medical_knowledge": module}), module


class RouterMedicalTermsTest(unittest.TestCase):
    """Router 医学词根补齐测试。"""

    def setUp(self):
        self.router = Router()

    def test_registered_medical_terms_route_to_knowledge(self):
        """新增医学词根的问题必须进入医学知识模块。"""
        cases = [
            "哮喘是什么",
            "心肌梗死是什么",
            "心梗是什么",
            "卒中怎么办",
            "中风怎么预防",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(
                    self.router.route(text), Router.MEDICAL_KNOWLEDGE
                )

    def test_medical_terms_keep_business_priority(self):
        """新词根处于兜底层，不得抢走用药等更高优先级的业务意图。"""
        self.assertEqual(
            self.router.route("中风吃什么药"), Router.MEDICATION
        )
        self.assertEqual(
            self.router.route("心梗用什么药"), Router.MEDICATION
        )

    def test_plain_chat_still_routes_to_general(self):
        """普通闲聊不含新增词根，必须保持 general 不变。"""
        for text in ("你好", "今天天气怎么样", "帮我写一首诗"):
            with self.subTest(text=text):
                self.assertEqual(self.router.route(text), Router.GENERAL)


class ChunkAliasRecallTest(unittest.TestCase):
    """片段检索与别名映射测试。"""

    def setUp(self):
        self.agent, self.module = _new_agent()

    def tearDown(self):
        self.module.db.close()

    def test_asthma_question_recalls_asthma_chunk(self):
        """「哮喘是什么」必须命中哮喘片段并返回来源摘要。"""
        result = self.module.run("哮喘是什么")
        self.assertIn("哮喘", result)
        self.assertIn("来源摘要", result)
        self.assertNotEqual(result, NOT_FOUND_MESSAGE)

    def test_stroke_term_recalls_stroke_chunk(self):
        """「卒中怎么办」必须命中脑卒中片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("卒中怎么办")
        }
        self.assertIn("脑卒中", names)
        self.assertIn("脑卒中", self.module.run("卒中怎么办"))

    def test_zhongfeng_recalls_stroke_chunk(self):
        """「中风怎么预防」必须通过别名映射命中脑卒中片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("中风怎么预防")
        }
        self.assertIn("脑卒中", names)
        result = self.module.run("中风怎么预防")
        self.assertIn("【脑卒中】来源摘要", result)
        self.assertIn("《卒中（实况报道）》", result)
        self.assertNotEqual(result, NOT_FOUND_MESSAGE)

    def test_mi_questions_not_recall_chd(self):
        """心肌梗死 / 心梗不得误召回冠心病片段。"""
        for text in ("心肌梗死是什么", "心梗是什么"):
            with self.subTest(text=text):
                names = {
                    chunk["disease_name"]
                    for chunk in self.module.db.match_chunks(text)
                }
                self.assertNotIn("冠心病", names)
                self.assertEqual(self.module.run(text), NOT_FOUND_MESSAGE)

    def test_allergy_not_recall_asthma(self):
        """「过敏性疾病」不得因为包含过敏而召回哮喘片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("过敏性疾病怎么办")
        }
        self.assertNotIn("哮喘", names)
        self.assertNotIn("哮喘", self.module.run("过敏性疾病怎么办"))

    def test_unmapped_alias_not_recall_stroke(self):
        """「脑梗」没有登记映射依据，不得凭猜测命中脑卒中片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("脑梗怎么预防")
        }
        self.assertNotIn("脑卒中", names)


class ScopeSafetyRegressionTest(unittest.TestCase):
    """儿童肺炎 scope 硬过滤回归测试。"""

    def setUp(self):
        self.agent, self.module = _new_agent()

    def tearDown(self):
        self.module.db.close()

    def test_plain_pneumonia_not_child(self):
        """未说明人群的「肺炎」不得召回儿童肺炎片段。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("肺炎怎么办")
        }
        self.assertNotIn("儿童肺炎", names)
        self.assertEqual(self.module.run("肺炎怎么办"), NOT_FOUND_MESSAGE)

    def test_adult_pneumonia_not_found(self):
        """成人肺炎必须返回未收录，不冒充儿童肺炎内容。"""
        self.assertEqual(self.module.run("成人肺炎怎么办"), NOT_FOUND_MESSAGE)

    def test_child_question_still_child_only(self):
        """儿童问题仍只召回儿童肺炎，并带适用范围提示。"""
        names = {
            chunk["disease_name"]
            for chunk in self.module.db.match_chunks("儿童肺炎有哪些表现")
        }
        self.assertEqual(names, {"儿童肺炎"})
        result = self.module.run("儿童肺炎有哪些表现")
        self.assertIn("儿童肺炎", result)
        self.assertIn("不适用于其他人群", result)


class EmergencyInterceptRegressionTest(unittest.TestCase):
    """危急症状前置拦截回归测试。"""

    def test_emergency_inputs_intercepted_before_knowledge(self):
        """四条危急输入必须返回急救提示且零 LLM 调用。"""
        cases = [
            "胸痛怎么办",
            "呼吸困难怎么办",
            "突然口角歪斜怎么办",
            "突然说话不清怎么办",
        ]
        for text in cases:
            with self.subTest(text=text):
                agent, module = _new_agent()
                with mock.patch("agent.main.call_llm") as fake_llm:
                    result = agent.run(text)
                self.assertEqual(result, EMERGENCY_REPLY)
                self.assertEqual(fake_llm.call_count, 0)
                module.db.close()


if __name__ == "__main__":
    unittest.main()