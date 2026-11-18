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

# 危急症状关键词：命中后直接返回固定急救提示，不进入路由、不调用大模型和业务模块。
# 采用保守的短语包含匹配，宁可多提醒一次，也不让高危情况进入普通问答。
EMERGENCY_KEYWORDS = (
    # 胸痛与胸部压迫
    "胸痛", "胸口痛", "胸部剧痛", "胸口压迫", "胸部压迫",
    # 呼吸困难
    "呼吸困难", "喘不过气", "无法呼吸", "不能呼吸", "没有呼吸",
    "窒息", "呼吸骤停",
    # 意识异常
    "意识不清", "失去意识", "意识丧失", "叫不醒", "昏迷", "晕倒", "晕厥",
    # 抽搐
    "抽搐", "惊厥", "抽风",
    # 出血
    "大量出血", "大出血", "止不住血", "血止不住", "出血不止", "血流不止",
    # 疑似卒中
    "口角歪斜", "嘴歪", "说不出话", "言语不清", "说话不清", "口齿不清",
    # 单侧肢体无力
    "单侧肢体无力", "一侧肢体无力", "一侧手脚无力", "半身无力", "半边身子",
    # 心跳呼吸骤停
    "心脏骤停", "猝死",
)

# 命中危急症状时返回的固定提示：只做急救引导，不含诊断、药名或剂量。
EMERGENCY_REPLY = (
    "⚠️ 可能存在紧急健康风险。\n\n"
    "请立即拨打当地急救电话；如果你在中国大陆，请拨打 120。\n\n"
    "请不要等待 AI 判断或继续在线咨询。尽量让患者保持安全体位，"
    "不要自行驾车前往医院。请立即寻求现场人员帮助。\n\n"
    "本系统不能代替急救人员，也不会自动通知医院或紧急联系人。"
)


def _contains_emergency(text):
    """判断用户输入是否包含危急症状关键词。

    非字符串输入一律返回 False，沿用原有的安全处理方式。
    """
    if not isinstance(text, str):
        return False
    return any(keyword in text for keyword in EMERGENCY_KEYWORDS)


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
        # 危急症状优先拦截：必须在路由和大模型之前，避免高危情况进入普通问答
        if _contains_emergency(user_input):
            self.memory.add("assistant", EMERGENCY_REPLY)
            return EMERGENCY_REPLY
        # 待确认流程优先于路由：确保「确认清空」「取消」不会落入大模型
        pending_reply = self._run_pending(user_input)
        if pending_reply is not None:
            self.memory.add("assistant", pending_reply)
            return pending_reply
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

    def _run_pending(self, user_input):
        """依次询问各模块是否有待确认操作；没有模块接管时返回 None。"""
        for module in self.modules.values():
            handler = getattr(module, "handle_pending", None)
            if not callable(handler):
                continue
            reply = handler(user_input)
            if reply is not None:
                return reply
        return None

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