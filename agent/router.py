"""主控 Agent 的规则路由器。

通过关键词规则识别用户意图，返回对应业务模块名称，不调用大模型。
"""


class Router:
    """意图路由器：把用户输入分派到对应的业务模块名称。

    五种可能结果：
        medical_knowledge  —— 医学知识问答
        health_management  —— 健康管理（健康档案等）
        medication         —— 用药管理（服药、提醒等）
        medical_navigation —— 医疗导航（医院、药房等地点）
        general            —— 普通聊天与兜底
    """

    # 五种模块名称常量，避免手写字符串出错
    MEDICAL_KNOWLEDGE = "medical_knowledge"
    HEALTH_MANAGEMENT = "health_management"
    MEDICATION = "medication"
    MEDICAL_NAVIGATION = "medical_navigation"
    GENERAL = "general"

    # 规则表：按顺序匹配，先匹配到的优先，保证优先级清晰稳定。
    # 优先级说明：医疗导航 > 用药 > 健康管理 > 医学知识。
    # 例如"附近药店的降压药"应优先进入导航；"高血压吃什么药"优先进入用药。
    _RULES = [
        (
            MEDICAL_NAVIGATION,
            [
                "附近的医院", "附近医院", "周边医院",
                "最近的医院", "找医院", "医院在哪", "医院怎么走",
                "有哪些医院", "有什么医院", "哪些医院",
                "挂号", "药房", "药店", "诊所", "卫生院", "急诊",
                "就医地点", "医疗导航", "导航",
            ],
        ),
        (
            MEDICATION,
            [
                "服药", "吃药", "用药", "药物", "药品", "处方", "剂量", "药片",
                "服药提醒", "吃药提醒", "吃什么药", "吃啥药", "用什么药",
                "停药", "减药", "漏服", "副作用", "不良反应", "降压药", "降糖药", "哪些药",
            ],
        ),
        (
            HEALTH_MANAGEMENT,
            [
                "健康档案", "个人健康", "健康管理", "建立档案", "健康数据",
                "体重记录", "运动记录", "体检报告", "健康记录",
                "清空记录", "清除记录",
                "身高", "体重", "bmi", "BMI", "年龄", "性别", "病史", "档案",
            ],
        ),
        (
            MEDICAL_KNOWLEDGE,
            [
                "什么是", "病因", "症状", "怎么预防", "如何预防", "怎么治疗",
                "如何治疗", "是什么病", "疾病", "高血压", "糖尿病", "感冒",
                "发烧", "血压", "血糖", "医学知识",
            ],
        ),
    ]

    # 医学疑似词根：所有明确业务规则都没命中后，只要出现这些词，
    # 就交给医学知识模块，由知识库决定能否回答。
    # 这里只做"疑似医学"判断，不猜测具体疾病；真正的检索仍由知识库完成。
    _MEDICAL_HINTS = [
        "病", "症", "痛", "疼", "炎", "癌", "肿",
        "感染", "发烧", "发热", "咳嗽", "腹泻", "呕吐",
        "头晕", "头痛", "胸闷", "血压", "血糖", "心率", "体温",
        "检查", "化验", "治疗", "预防", "症状", "疾病", "药物",
        "用药", "康复", "诊断", "就医", "疫苗", "过敏",
        # 已收录主题的疾病名与俗称：路由只负责把问题交给知识库，
        # 能否回答由强证据闸门决定，未收录时返回"暂未收录"提示。
        "哮喘", "卒中", "中风", "心肌梗死", "心梗", "梗死",
    ]

    def route(self, user_input):
        """识别用户输入，返回模块名称。

        参数：
            user_input: 用户输入的文本，理论上应为字符串。

        返回：
            五种模块名称之一；无法识别或输入无效时返回 general。
        """
        # 非字符串或空内容：直接兜底，避免程序崩溃
        if not isinstance(user_input, str) or not user_input.strip():
            return self.GENERAL

        text = user_input.strip()
        for module_name, keywords in self._RULES:
            # 命中任意关键词即认为属于该模块
            if any(keyword in text for keyword in keywords):
                return module_name

        # 最后一级兜底：出现医学相关词根即交给医学知识模块，
        # 避免未收录的医学问题落到通用大模型；普通闲聊不含这些词根，仍回退 general。
        if any(hint in text for hint in self._MEDICAL_HINTS):
            return self.MEDICAL_KNOWLEDGE

        # 没有匹配到任何医学业务关键词，回退到通用问答
        return self.GENERAL