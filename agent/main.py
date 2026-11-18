import argparse
import os

from .llm import call_llm
from .memory import Memory
from .modules.health_management import HealthManagementModule
from .modules.medical_knowledge import MedicalKnowledgeModule
from .modules.medical_navigation import MedicalNavigationModule
from .modules.medication import MedicationModule
from .router import Router
from .tools import default_registry

DEFAULT_SYSTEM_PROMPT = "你是一个乐于助人的 AI 助手，可以调用工具来完成用户任务，请用简体中文回复。"


def _default_modules():
    """创建默认的四个业务模块，以模块名作为字典键，便于路由查找。"""
    modules = {}
    for module in (
        MedicalKnowledgeModule(),
        HealthManagementModule(),
        MedicationModule(),
        MedicalNavigationModule(),
    ):
        modules[module.name] = module
    return modules


class Agent:
    def __init__(
        self,
        system_prompt=None,
        registry=None,
        memory=None,
        max_rounds=8,
        router=None,
        modules=None,
    ):
        self.registry = registry or default_registry
        self.memory = memory or Memory(system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT)
        self.max_rounds = max_rounds
        # 意图路由器与业务模块，均支持外部注入，方便测试
        self.router = router or Router()
        self.modules = modules if modules is not None else _default_modules()

    def run(self, user_input):
        """主入口：先记录用户消息，再按意图分派到业务模块或通用问答。"""
        self.memory.add("user", user_input)
        module_name = self.router.route(user_input)
        # 非 general 意图：交给对应业务模块处理
        if module_name != Router.GENERAL:
            module = self.modules.get(module_name)
            if module is not None:
                result = module.run(user_input, context=self.memory)
                if not isinstance(result, str):
                    result = str(result)
            else:
                # 医学意图命中但模块缺失：返回固定安全提示，不回退大模型
                result = "该业务模块暂不可用，请稍后再试。"
            self.memory.add("assistant", result)
            return result
        # general 意图继续走原有大模型与工具调用循环
        return self._run_general()

    def _run_general(self):
        """原有通用问答逻辑：调用大模型并处理工具调用循环。"""
        tools = self.registry.schemas() or None
        for _ in range(self.max_rounds):
            message = call_llm(self.memory.get_messages(), tools=tools)
            self.memory.add_assistant(message)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                return message.get("content") or ""
            for tool_call in tool_calls:
                name = tool_call["function"]["name"]
                arguments = tool_call["function"].get("arguments", "{}")
                result = self.registry.execute(name, arguments)
                self.memory.add_tool_result(tool_call["id"], result)
        return "已达到最大工具调用轮数，未能生成最终回复。"


def main():
    parser = argparse.ArgumentParser(description="Agent 命令行入口")
    parser.add_argument("-t", "--text", help="单次提问模式：经主控 Agent 路由处理并打印回复")
    args = parser.parse_args()
    if args.text:
        try:
            # --text 与 REPL 统一走主控 Agent 路由，不再直接调用 call_llm
            print(Agent().run(args.text))
        except RuntimeError as e:
            print(f"Agent 出错: {e}")
        return
    agent_obj = Agent()
    print("Agent 已启动，输入 exit 退出")
    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break
        try:
            print(f"Agent: {agent_obj.run(user_input)}")
        except RuntimeError as e:
            print(f"Agent 出错: {e}")


if __name__ == "__main__":
    main()