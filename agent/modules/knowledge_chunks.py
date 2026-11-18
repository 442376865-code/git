"""知识片段（knowledge_chunks）的静态种子数据。

本文件只存放人工整理过的医学知识要点与来源元数据，不联网、不接数据库。
首批片段全部来自已人工核验的 WHO 实况报道（source_level = 1）。

内容约定：
1. content 是项目自己整理的短摘要，不是网页原文，也不做批量复制；
2. source_page 对网页来源统一为空字符串，绝不猜页码；
3. source_section 只有实际核验过的章节才填写，未核验的留空；
4. 普通感冒、胃炎、过敏性疾病、成人肺炎不在首批导入范围内；
5. 儿童肺炎使用 scope = "儿童肺炎"，检索时必须在明确提到儿童时才返回。
"""

# 本批来源的核验日期（人工打开页面确认内容的日期）
ACCESSED_AT = "2026-11-18"

# 首批 7 个已核验来源的元数据（edition 填 WHO 页面标注的更新日期）
WHO_HYPERTENSION = {
    "title": "高血压（实况报道）",
    "edition": "2025-09-25",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/hypertension",
}

WHO_DIABETES = {
    "title": "糖尿病（实况报道）",
    "edition": "2024-11-14",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/diabetes",
}

WHO_HEART_ATTACK = {
    "title": "心脏病发作（实况报道）",
    "edition": "2026-09-10",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/heart-attack",
}

WHO_STROKE = {
    "title": "卒中（实况报道）",
    "edition": "2025-12-19",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/stroke",
}

WHO_ASTHMA = {
    "title": "哮喘（实况报道）",
    "edition": "2026-04-28",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/asthma",
}

WHO_INFLUENZA = {
    "title": "季节性流感（实况报道）",
    "edition": "2025-02-28",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/influenza-(seasonal)",
}

WHO_PNEUMONIA = {
    "title": "儿童肺炎（实况报道）",
    "edition": "2022-11-11",
    "url": "https://www.who.int/zh/news-room/fact-sheets/detail/pneumonia",
}


def _chunk(disease_name, scope, chunk_index, content, source, section=""):
    """按统一结构组装一条知识片段。

    参数：
        disease_name: 所属主题，必须与知识卡片主题同名（没有卡片时也可独立存在）
        scope: 适用范围，参与检索时的人群过滤
        chunk_index: 同一来源同一章节内的稳定序号
        content: 人工整理的中文摘要
        source: 来源元数据（title / edition / url）
        section: 实际核验过的章节名，未核验留空
    """
    return {
        "disease_name": disease_name,
        "scope": scope,
        "chunk_index": chunk_index,
        "content": content,
        "source_title": source["title"],
        "source_publisher": "世界卫生组织",
        "source_edition": source["edition"],
        "source_url": source["url"],
        "source_section": section,
        "source_page": "",
        "source_level": 1,
        "accessed_at": ACCESSED_AT,
        "updated_at": ACCESSED_AT,
    }


# 首批知识片段总表，后续新增主题时在此追加
KNOWLEDGE_CHUNKS = [
    _chunk(
        "高血压", "成人高血压", 0,
        "高血压指血液对血管壁的压力长期高于正常范围。多数患者早期没有明显不适，"
        "常在体检时被发现；长期控制不佳会逐步损伤心脏、大脑、肾脏和眼底血管。",
        WHO_HYPERTENSION,
    ),
    _chunk(
        "高血压", "成人高血压", 1,
        "常见危险因素包括高盐饮食、超重或肥胖、缺乏运动、长期精神紧张与睡眠不足、"
        "吸烟、过量饮酒、高血压家族史以及年龄增长。减少食盐摄入、控制体重、"
        "规律有氧运动、戒烟限酒有助于降低风险。",
        WHO_HYPERTENSION,
    ),
    _chunk(
        "高血压", "成人高血压", 2,
        "日常管理重点是规律测量并记录血压、坚持低盐饮食、保持运动和控制体重、"
        "保证睡眠与情绪平稳，并按医生要求定期复查。出现剧烈头痛、视物模糊、胸痛、"
        "呼吸困难、一侧肢体无力或言语不清时，应尽快就医或拨打120。",
        WHO_HYPERTENSION,
    ),
    _chunk(
        "2型糖尿病", "2型糖尿病", 0,
        "2型糖尿病是一种以血糖长期偏高为特征的慢性代谢性疾病，"
        "与胰岛素抵抗和胰岛素分泌不足有关。早期可能没有明显症状，常在体检时被发现。",
        WHO_DIABETES,
    ),
    _chunk(
        "2型糖尿病", "2型糖尿病", 1,
        "常见表现包括多饮、多尿、多食、体重下降、乏力、视物模糊、伤口不易愈合和"
        "皮肤容易感染。超重肥胖（尤其是腹型肥胖）、缺乏运动、糖尿病家族史、"
        "长期高糖高脂饮食和年龄增长是常见危险因素。",
        WHO_DIABETES,
    ),
    _chunk(
        "2型糖尿病", "2型糖尿病", 2,
        "日常管理包括在医生指导下控制饮食总热量、主食粗细搭配、规律运动并控制体重、"
        "按医嘱监测血糖并记录、定期复查眼底、肾脏和足部。出现意识模糊、嗜睡、"
        "呼吸深快或剧烈腹痛伴呕吐时，应立即就医。",
        WHO_DIABETES,
    ),
    _chunk(
        "冠心病", "冠心病", 0,
        "缺血性心脏病又称冠状动脉心脏病，是全球主要死亡原因之一。"
        "冠状动脉发生粥样硬化、管腔变窄后导致心肌供血不足，是这类疾病的核心机制。",
        WHO_HEART_ATTACK,
        section="重要事实",
    ),
    _chunk(
        "冠心病", "冠心病", 1,
        "心肌供血不足时可出现胸闷、胸痛（多在活动或情绪激动时加重）、气短和乏力，"
        "部分人症状不典型。胸痛持续不缓解并伴有出汗、恶心或呼吸困难时，需要立即就医。",
        WHO_HEART_ATTACK,
        section="症状",
    ),
    _chunk(
        "冠心病", "冠心病", 2,
        "主要危险因素包括高血压、血脂异常、糖尿病、吸烟、超重肥胖、缺乏运动、"
        "不健康饮食、长期精神压力和心血管疾病家族史。",
        WHO_HEART_ATTACK,
        section="病因和危险因素",
    ),
    _chunk(
        "冠心病", "冠心病", 3,
        "医生通常结合症状特点、心电图、心肌损伤标志物和影像学检查综合判断。"
        "出现疑似急性心肌梗死的情况时，应尽快到急诊评估。",
        WHO_HEART_ATTACK,
        section="诊断",
    ),
    _chunk(
        "冠心病", "冠心病", 4,
        "控制血压、血脂和血糖，戒烟，保持健康饮食与规律运动，控制体重，"
        "并按医生要求管理慢性疾病，有助于降低缺血性心脏病的发生风险。",
        WHO_HEART_ATTACK,
        section="预防",
    ),
    _chunk(
        "脑卒中", "脑卒中", 0,
        "卒中（俗称中风）是脑部血液供应突然中断或脑血管破裂导致的急性脑损伤，"
        "起病急、进展快，需要尽快就医。",
        WHO_STROKE,
        section="重要事实",
    ),
    _chunk(
        "脑卒中", "脑卒中", 1,
        "常见表现为突发一侧面部或肢体无力、麻木，言语不清或理解困难，视物障碍，"
        "行走不稳，以及剧烈头痛。出现上述任何一种表现都应立即拨打120。",
        WHO_STROKE,
        section="症状",
    ),
    _chunk(
        "脑卒中", "脑卒中", 2,
        "高血压是最重要的可控危险因素，此外还包括糖尿病、血脂异常、吸烟、心房颤动、"
        "缺乏运动、肥胖、不健康饮食和过量饮酒。",
        WHO_STROKE,
        section="危险因素",
    ),
    _chunk(
        "脑卒中", "脑卒中", 3,
        "控制血压、血糖和血脂，戒烟限酒，规律运动，保持健康饮食和控制体重，"
        "并按医生要求管理心脏疾病，可以降低卒中发生风险。",
        WHO_STROKE,
        section="预防",
    ),
    _chunk(
        "脑卒中", "脑卒中", 4,
        "卒中后可能出现肢体活动障碍、言语和吞咽困难、认知与情绪改变等。"
        "康复训练应尽早开始并长期坚持，具体方案由医生和康复团队制定。",
        WHO_STROKE,
        section="并发症",
    ),
    _chunk(
        "哮喘", "哮喘", 0,
        "哮喘是一种以气道慢性炎症和可变性气流受限为特征的慢性呼吸系统疾病，"
        "常表现为反复发作的喘息、气短、胸闷和咳嗽，症状常在夜间或清晨加重。",
        WHO_ASTHMA,
    ),
    _chunk(
        "哮喘", "哮喘", 1,
        "常见诱发因素包括尘螨、花粉、动物皮屑等过敏原，呼吸道感染，冷空气，烟雾以及"
        "剧烈运动。识别并尽量避免接触自身已知的诱发因素，有助于减少发作。",
        WHO_ASTHMA,
    ),
    _chunk(
        "哮喘", "哮喘", 2,
        "哮喘需要长期规范管理，按医生要求规律使用控制药物并定期复诊。"
        "出现说话困难、口唇发紫、缓解药物无效等严重发作表现时，应立即就医。",
        WHO_ASTHMA,
    ),
    _chunk(
        "流感", "季节性流感", 0,
        "季节性流感是由流感病毒引起的急性呼吸道传染病，常在冬春季流行。"
        "典型表现为突然发热、畏寒、头痛、肌肉酸痛和乏力，可伴有咳嗽和咽痛。",
        WHO_INFLUENZA,
    ),
    _chunk(
        "流感", "季节性流感", 1,
        "流感主要通过飞沫和密切接触传播，人群密集场所传播风险较高。"
        "勤洗手、保持室内通风、咳嗽或打喷嚏时遮挡口鼻、流行季节少去人群密集场所，"
        "可以降低感染风险。",
        WHO_INFLUENZA,
    ),
    _chunk(
        "流感", "季节性流感", 2,
        "多数人可在一到两周内逐渐恢复，但老年人、儿童、孕妇和慢性病患者更容易出现"
        "较重并发症。出现持续高热、呼吸困难、胸痛或意识改变时应及时就医。",
        WHO_INFLUENZA,
    ),
    _chunk(
        "儿童肺炎", "儿童肺炎", 0,
        "本段信息仅适用于儿童肺炎。儿童肺炎是儿童常见的下呼吸道感染，"
        "可由病毒、细菌等病原体引起，常表现为发热、咳嗽、呼吸增快和精神不佳。",
        WHO_PNEUMONIA,
    ),
    _chunk(
        "儿童肺炎", "儿童肺炎", 1,
        "呼吸急促、鼻翼扇动、吸气时胸骨上窝或肋间隙凹陷、口唇发紫、拒食或精神萎靡，"
        "是病情较重的提示，应尽快就医。",
        WHO_PNEUMONIA,
    ),
    _chunk(
        "儿童肺炎", "儿童肺炎", 2,
        "预防措施包括按国家免疫规划接种疫苗、保持室内通风、注意手卫生、"
        "避免接触呼吸道感染患者，以及保证营养和充足休息。"
        "儿童出现呼吸困难或高热不退时应及时就医。",
        WHO_PNEUMONIA,
    ),
]