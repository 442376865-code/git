"""业务模块统一基类。

所有医学业务模块都继承 MedicalModule，由主控 Agent 统一调度。
"""


class MedicalModule:
    """业务模块基类。

    子类需要：
    1. 设置 name（模块名称）和 description（功能描述）；
    2. 重写 run() 方法实现具体功能。
    """

    # 模块名称，子类必须覆盖
    name = ""
    # 模块功能描述，子类必须覆盖
    description = ""

    def run(self, user_input, context=None):
        """执行模块逻辑，返回字符串结果。

        参数：
            user_input: 用户输入的文本。
            context: 可选上下文信息（如会话、记忆等），默认为 None。

        返回：
            字符串类型的处理结果。

        说明：
            基类没有具体业务能力，子类必须重写本方法。
        """
        raise NotImplementedError(
            f"模块 {self.__class__.__name__} 尚未实现 run() 方法，请子类重写 run()。"
        )