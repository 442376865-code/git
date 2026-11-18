"""主控 Agent 路由接入的单元测试。

覆盖：四种医学意图分派、general 回退、memory 写入、工具调用循环。
全部使用 mock 替换大模型调用，不访问网络，不输出任何敏感配置。
"""

import inspect
import unittest
from unittest import mock

import agent.main
from agent.main import Agent
from agent.modules.base import MedicalModule
from agent.router import Router


class RecordingModule:
    """测试替身模块：记录 run() 的调用参数，返回固定格式结果。"""

    def __init__(self, name):
        self.name = name
        self.calls = []

    def run(self, user_input, context=None):
        self.calls.append((user_input, context))
        return "[" + self.name + "] " + user_input


class RouterBaselineTest(unittest.TestCase):
    """Router 六条基准用例与边界输入，确保既有规则与优先级未被改动。"""

    def test_baseline_cases(self):
        """六条基准输入必须路由到预期模块。"""
        cases = [
            ("你好", "general"),
            ("什么是高血压？", "medical_knowledge"),
            ("我想建立个人健康档案", "health_management"),
            ("帮我设置每天早上8点的服药提醒", "medication"),
            ("帮我找附近的医院", "medical_navigation"),
            ("今天天气怎么样", "general"),
        ]
        router = Router()
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(router.route(text), expected)

    def test_invalid_input_returns_general(self):
        """空串、纯空白、非字符串输入必须安全返回 general，不抛异常。"""
        router = Router()
        for bad in (None, 123, ["你好"], "", "   "):
            with self.subTest(value=bad):
                self.assertEqual(router.route(bad), "general")

    def test_medication_keywords_extended(self):
        """新增用药关键词必须路由到 medication，且不影响既有优先级。"""
        router = Router()
        cases = [
            "我想停药",
            "可以减药吗",
            "漏服了怎么办",
            "这个药有副作用吗",
            "不良反应有哪些",
            "降压药怎么选",
            "降糖药有哪些",
            "哪些药需要饭前吃",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(router.route(text), "medication")

    def test_navigation_natural_expressions(self):
        """就医地点的自然表达必须路由到 medical_navigation。"""
        router = Router()
        cases = [
            "附近医院",
            "周边医院",
            "查看就医地点",
            "添加就医地点",
            "搜索就医地点 医院",
            "删除就医地点 1",
            "附近医院怎么走",
            "附近药房在哪",
            "附近药店在哪",
            "周边药店在哪",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(router.route(text), "medical_navigation")

    def test_navigation_priority_over_medication(self):
        """“附近药店的降压药”同时含导航与用药词，必须仍进入 medical_navigation。"""
        self.assertEqual(Router().route("附近药店的降压药"), "medical_navigation")

    def test_navigation_keywords_do_not_capture_medical_questions(self):
        """新增导航关键词不得吞掉纯医学问答、用药咨询与通用闲聊。"""
        router = Router()
        cases = [
            ("什么是高血压？", "medical_knowledge"),
            ("糖尿病的症状有哪些", "medical_knowledge"),
            ("怎么预防感冒", "medical_knowledge"),
            ("医院开的降压药怎么吃", "medication"),
            ("吃药后副作用很严重", "medication"),
            ("今天天气怎么样", "general"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(router.route(text), expected)


class AgentRoutingTest(unittest.TestCase):
    """主控 Agent 的路由分派、依赖注入与记忆写入。"""

    def setUp(self):
        # 每个测试都使用全新的替身模块与 Agent，互不影响
        self.modules = {
            name: RecordingModule(name)
            for name in (
                "medical_knowledge",
                "health_management",
                "medication",
                "medical_navigation",
            )
        }
        self.agent = Agent(modules=self.modules)

    def test_run_signature_unchanged(self):
        """Agent.run 签名必须保持 (self, user_input)。"""
        params = list(inspect.signature(Agent.run).parameters)
        self.assertEqual(params, ["self", "user_input"])

    def test_init_accepts_injection(self):
        """router 与 modules 必须可注入，且不影响原有参数。"""
        router = Router()
        modules = {}
        agent = Agent(router=router, modules=modules, max_rounds=3)
        self.assertIs(agent.router, router)
        self.assertIs(agent.modules, modules)
        self.assertEqual(agent.max_rounds, 3)

    def test_default_modules_registered(self):
        """默认构造时必须注册四个业务模块，且均为 MedicalModule 子类。"""
        agent = Agent()
        self.assertEqual(
            set(agent.modules),
            {"medical_knowledge", "health_management", "medication", "medical_navigation"},
        )
        for module in agent.modules.values():
            self.assertIsInstance(module, MedicalModule)

    def test_medical_dispatch(self):
        """四种医学意图必须分派到对应模块并返回其结果。"""
        cases = [
            ("什么是高血压？", "medical_knowledge"),
            ("我想建立个人健康档案", "health_management"),
            ("帮我设置每天早上8点的服药提醒", "medication"),
            ("帮我找附近的医院", "medical_navigation"),
        ]
        for text, name in cases:
            with self.subTest(module=name):
                result = self.agent.run(text)
                self.assertEqual(len(self.modules[name].calls), 1)
                self.assertEqual(result, "[" + name + "] " + text)

    def test_module_receives_memory_context(self):
        """业务模块收到的 context 必须是主控的 memory 对象。"""
        self.agent.run("什么是高血压？")
        user_input, context = self.modules["medical_knowledge"].calls[0]
        self.assertEqual(user_input, "什么是高血压？")
        self.assertIs(context, self.agent.memory)

    def test_memory_records_module_result(self):
        """医学路径下 memory 必须记录 user 与 assistant 两条消息。"""
        self.agent.run("什么是高血压？")
        messages = self.agent.memory.messages
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "什么是高血压？")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIn("medical_knowledge", messages[1]["content"])

    def test_general_uses_llm(self):
        """general 意图必须走 call_llm，且不触碰任何业务模块。"""
        with mock.patch(
            "agent.main.call_llm",
            return_value={"role": "assistant", "content": "mock 通用回复"},
        ):
            result = self.agent.run("今天天气怎么样")
        self.assertEqual(result, "mock 通用回复")
        self.assertTrue(all(len(m.calls) == 0 for m in self.modules.values()))
        roles = [x["role"] for x in self.agent.memory.messages]
        self.assertEqual(roles, ["user", "assistant"])

    def test_general_tool_loop(self):
        """general 意图必须保留工具调用循环，memory 记录完整角色序列。"""
        replies = [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "get_current_time", "arguments": "{}"}}
            ]},
            {"role": "assistant", "content": "工具结果已处理"},
        ]
        with mock.patch("agent.main.call_llm", side_effect=replies):
            result = self.agent.run("现在几点")
        self.assertEqual(result, "工具结果已处理")
        roles = [x["role"] for x in self.agent.memory.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

    def test_text_mode_routes_through_agent(self):
        """--text 入口必须经过 Agent().run()，统一走路由。"""
        with mock.patch("sys.argv", ["main.py", "-t", "你好"]), \
                mock.patch("agent.main.Agent") as agent_cls:
            agent_cls.return_value.run.return_value = "mock 回复"
            agent.main.main()
            agent_cls.assert_called_once()
            agent_cls.return_value.run.assert_called_once_with("你好")

    def test_missing_module_returns_safe_hint(self):
        """医学意图命中但模块缺失时：返回固定提示，绝不调用大模型。"""
        cases = [
            ("什么是高血压？", "medical_knowledge"),
            ("我想建立个人健康档案", "health_management"),
            ("帮我设置每天早上8点的服药提醒", "medication"),
            ("帮我找附近的医院", "medical_navigation"),
        ]
        for text, name in cases:
            with self.subTest(module=name):
                agent = Agent(modules={})
                with mock.patch("agent.main.call_llm") as fake_llm:
                    result = agent.run(text)
                self.assertEqual(result, "该业务模块暂不可用，请稍后再试。")
                fake_llm.assert_not_called()

    def test_missing_hint_written_to_memory(self):
        """模块缺失的固定提示必须写入 memory（user 与 assistant 两条）。"""
        agent = Agent(modules={})
        with mock.patch("agent.main.call_llm") as fake_llm:
            agent.run("什么是高血压？")
        fake_llm.assert_not_called()
        messages = agent.memory.messages
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["content"], "该业务模块暂不可用，请稍后再试。")


if __name__ == "__main__":
    unittest.main()