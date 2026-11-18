# 医知有据 V2.0

本地运行的医学信息助手演示项目：1 个主控 Agent + 4 个业务模块，另有一个完全独立的 SOS 急救页面。

> 本助手仅提供健康信息参考，不能替代医生诊疗，也不提供个人化诊断、处方或剂量建议。
> 紧急情况请立即拨打 **120** 或前往最近医院急诊。

## 功能模块

| 模块 | 用途 |
| --- | --- |
| 医学知识 `medical_knowledge` | 常见疾病科普知识卡 |
| 健康管理 `health_management` | 个人健康档案、体重记录 |
| 用药管理 `medication` | 用药计划与服药记录 |
| 医疗导航 `medical_navigation` | 已保存的医院、药房等就医地点 |
| 通用问答 `general` | 未命中上述意图时调用大模型 |
| SOS 急救 `sos` | 独立启动的急救指引页，不依赖主站 |

## 环境要求

- Python 3.11
- 依赖见 `requirements.txt`：`fastapi`、`uvicorn`、`httpx`

```bash
pip install -r requirements.txt
```

## 配置

在项目根目录准备 `.env`，配置模型服务：

- `OPENAI_API_KEY`：模型服务密钥
- `OPENAI_BASE_URL`：模型服务地址
- `LLM_MODEL`：模型名称

`.env` 已被 `.gitignore` 忽略，请勿提交。不配置模型时，四个本地业务模块仍可离线使用，只有通用问答会失败。

## 启动

主站（默认 8000 端口）：

```bash
python -m uvicorn agent.web_app:app --host 127.0.0.1 --port 8000
```

浏览器访问 `http://127.0.0.1:8000`。

SOS 急救页（独立程序，默认 8001 端口，可在主站不可用时单独启动）：

```bash
python -m uvicorn sos.web_app:app --host 127.0.0.1 --port 8001
```

浏览器访问 `http://127.0.0.1:8001`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 聊天页面 |
| GET | `/api/health` | 健康检查，返回 `{"status":"ok"}` |
| POST | `/api/chat` | 请求体 `{"message": "...", "session_id": "..."}`，返回 `{"reply": "...", "session_id": "...", "module": "..."}` |
| DELETE | `/api/session/{session_id}` | 清除指定会话快照 |

`session_id` 可省略；省略时服务端自动生成。`module` 为本次请求命中的业务模块名。

## 测试

```bash
python -B -m pytest -q
```

全部用例使用临时数据库和假 Agent，不调用真实大模型、不访问网络。

## 已知限制

1. **仅支持单进程部署**。会话缓存、会话锁和 LRU 淘汰都在进程内存中，不支持多 worker；多 worker 会导致同一会话状态不一致。
2. **仅适用于本地或内网试用**。没有鉴权、HTTPS、限流等生产级能力，不要直接暴露到公网。
3. **Router 是关键词规则**，优先级为：医疗导航 > 用药 > 健康管理 > 医学知识。未命中任何关键词时回退到通用问答，离线环境会返回 500。
4. **LLM 超时与重试**：单次请求超时 45 秒，最多尝试 2 次（含首次），失败后按 1 秒起指数退避；只重试临时性故障。
5. **会话生命周期**：超过 7 天未更新的会话视为过期，再次访问不会恢复旧历史；最多保留 200 个会话；单条消息最多 4000 个字符，超长返回中文 400。
6. **医学边界**：不做个人化诊断，不推荐具体药名、剂量，不指导停药或减药；涉及紧急症状一律提示拨打 120。

## 目录结构

```text
agent/                 主控 Agent、路由、LLM、会话存储、Web 应用与页面
agent/modules/         四个业务模块及各自的 SQLite 封装
sos/                   独立 SOS 急救应用（不导入 agent 包）
tests/                 单元测试与接口测试
data/                  本地 SQLite 数据（不提交）
requirements.txt       Python 依赖
```

`data/`、`agent_memory.json`、`__pycache__/` 和测试数据库均不纳入版本控制。