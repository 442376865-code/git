"""FastAPI Web 服务：托管聊天页面并提供聊天接口。

设计要点：
- 复用主控 Agent：每个 session_id 对应一个 Agent 实例，从而实现会话内记忆；
- 会话快照保存到 SQLite，同一 session_id 在服务重启后能恢复历史；
- 不改动 Router、业务模块与路由优先级，Web 层只做输入校验与异常兜底；
- agent_factory 可注入，方便测试时替换成不调用大模型的假 Agent。

部署限制：SQLite 会话持久化只适用于单进程运行。多个 worker 会各自持有
内存中的会话缓存，无法保证同一会话的一致性，因此不要用多 worker 启动。

启动方式（项目根目录下）：
    python -m uvicorn agent.web_app:app --host 127.0.0.1 --port 8000
"""

import logging
import re
import uuid
from pathlib import Path
from threading import Lock

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .main import Agent
from .router import Router
from .session_store import SessionStore

logger = logging.getLogger(__name__)

# session_id 只允许 1～64 位字母、数字、下划线或连字符
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 前端页面位置
STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_FILE = STATIC_DIR / "index.html"

# 会话数量上限，避免长期运行时内存无限增长
MAX_SESSIONS = 200

# 单条消息最大字符数，避免一次请求占用过多内存和数据库空间
MAX_MESSAGE_CHARS = 4000


class ChatRequest(BaseModel):
    """聊天请求体；字段都有默认值，避免缺字段时报 422。"""

    message: str = ""
    session_id: str = ""


def _restore_history(store, session_id, agent):
    """把数据库中的历史消息恢复到 Agent 的 Memory 上。

    假 Agent 没有 memory 时直接跳过；恢复失败按新会话处理，
    绝不因为历史数据异常导致本次请求失败。
    """
    memory = getattr(agent, "memory", None)
    restore = getattr(memory, "restore", None)
    if not callable(restore):
        return
    try:
        history = store.load(session_id)
        if history:
            restore(history)
    except Exception:
        # 日志只写固定文案，不打印聊天正文或会话凭据
        logger.warning("会话历史恢复失败，按新会话处理")


def _save_snapshot(store, session_id, agent, max_rows, protected_ids):
    """Agent 成功后保存会话快照，并在同一事务里清理超额旧会话。

    保存失败时保留数据库中的旧快照，不影响本次回复。
    """
    memory = getattr(agent, "memory", None)
    snapshot = getattr(memory, "snapshot", None)
    if not callable(snapshot):
        return
    try:
        messages = snapshot()
    except Exception:
        logger.warning("会话快照读取失败，本次不保存")
        return

    # 优先使用带容量清理的原子接口；旧存储实现回退到普通保存
    saver = getattr(store, "save_and_prune", None)
    if callable(saver):
        saved = saver(session_id, messages, max_rows, protected_ids)
    else:
        saved = store.save(session_id, messages)
    if not saved:
        logger.warning("会话快照保存失败，本次不写入数据库")


def create_app(agent_factory=None, session_store=None):
    """创建 FastAPI 应用。

    参数：
        agent_factory: 可选的 Agent 工厂函数，默认使用 Agent；测试可注入假实现。
        session_store: 可选的会话存储，默认使用 data/sessions.db；测试应传入临时目录。
    """
    app = FastAPI(title="医知有据", description="医学知识、健康管理、用药与就医地点助手")
    factory = agent_factory or Agent
    store = session_store if session_store is not None else SessionStore()

    # session_id -> Agent 实例，实现同一会话内的上下文记忆
    agents = {}
    # session_id -> 会话专属锁，保证同一会话的请求串行处理
    session_locks = {}
    # session_id -> 活动请求数（正在执行或正在等待会话锁）
    active_counts = {}
    # session_id -> 最近访问序号，用于淘汰最久未使用的空闲会话
    last_used = {}
    access_counter = 0
    # 全局锁只保护上面这些字典的读写，不参与 agent.run 的执行
    lock = Lock()

    def drop_session_locked(session_id):
        """在全局锁内同步删除会话的 Agent、锁、活动计数和访问记录。"""
        agents.pop(session_id, None)
        session_locks.pop(session_id, None)
        active_counts.pop(session_id, None)
        last_used.pop(session_id, None)

    def evict_idle_locked():
        """超额时淘汰最久未使用的空闲会话。

        先删除数据库快照，成功后再删除内存中的 Agent、会话锁和计数。
        数据库删除失败时保留内存会话，避免出现“内存已删但快照还在、
        下次访问又恢复旧历史”的复活问题；活跃会话（活动计数大于 0）
        永远不会被淘汰，如果全部活跃则允许暂时超过上限。
        """
        remove = getattr(store, "delete", None)
        while len(agents) > MAX_SESSIONS:
            idle = [sid for sid in agents if active_counts.get(sid, 0) == 0]
            if not idle:
                break
            victim = min(idle, key=lambda sid: last_used.get(sid, 0))
            if callable(remove) and not remove(victim):
                # 删除失败时保留内存会话，等下一次请求结束时再尝试
                logger.warning("会话快照删除失败，保留内存会话")
                break
            drop_session_locked(victim)

    def active_session_ids():
        """返回当前所有活动会话，数据库容量清理时必须保护它们。"""
        with lock:
            return set(active_counts)

    def get_agent_and_lock(session_id):
        """取出会话的 Agent 与专属锁，并登记一次活动请求。

        先创建再写入缓存：初始化失败时不会影响已有会话，也不会留下半个实例。
        活动计数包含正在执行和正在等待会话锁的请求，避免被误判为空闲。
        """
        nonlocal access_counter
        with lock:
            agent = agents.get(session_id)
            if agent is None:
                agent = factory()
                # 新会话先从数据库恢复历史，再放进缓存
                _restore_history(store, session_id, agent)
                agents[session_id] = agent
                session_locks[session_id] = Lock()
            access_counter += 1
            last_used[session_id] = access_counter
            active_counts[session_id] = active_counts.get(session_id, 0) + 1
            evict_idle_locked()
            return agent, session_locks[session_id]

    def finish_request(session_id):
        """请求结束时减少活动计数（成功或异常都要调用）。"""
        with lock:
            remaining = active_counts.get(session_id, 1) - 1
            if remaining <= 0:
                active_counts.pop(session_id, None)
            else:
                active_counts[session_id] = remaining
            evict_idle_locked()

    @app.get("/")
    def index():
        """返回聊天页面。"""
        return FileResponse(INDEX_FILE, media_type="text/html")

    @app.get("/api/health")
    def health():
        """健康检查，便于确认服务已启动。"""
        return {"status": "ok"}

    @app.post("/api/chat")
    def chat(payload: ChatRequest):
        """接收一条消息，交给对应会话的 Agent 处理并返回回复。"""
        message = (payload.message or "").strip()
        if not message:
            return JSONResponse(
                status_code=400,
                content={"detail": "消息不能为空，请输入内容后再发送。"},
            )
        # 服务端必须独立校验长度，前端的 maxlength 只用于改善体验
        if len(message) > MAX_MESSAGE_CHARS:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"消息过长：最多允许 {MAX_MESSAGE_CHARS} 个字符，请精简后再发送。"
                },
            )

        # 客户端没带会话编号时新建一个，前端会保存并复用
        raw_session_id = (payload.session_id or "").strip()
        # 非法会话编号直接拒绝，不读取也不写入数据库
        if raw_session_id and not SESSION_ID_PATTERN.fullmatch(raw_session_id):
            return JSONResponse(
                status_code=400,
                content={"detail": "会话编号不合法：只允许 1～64 位字母、数字、下划线或连字符。"},
            )
        session_id = raw_session_id or uuid.uuid4().hex
        try:
            # 只把创建过程纳入兜底：初始化失败也返回中文 500
            agent, session_lock = get_agent_and_lock(session_id)
        except Exception:
            # 日志只写固定文案，不打印内部异常、聊天正文或会话编号
            logger.warning("会话初始化失败")
            return JSONResponse(
                status_code=500,
                content={"detail": "服务暂时不可用，请稍后再试。"},
            )

        # 同一会话必须串行：避免并发请求同时修改同一份 memory 造成串话；
        # 不同会话用的是各自的锁，互不阻塞。
        session_lock.acquire()
        try:
            reply = agent.run(message)
        except Exception:
            # 只写固定日志，不打印异常详情、聊天正文或会话编号
            logger.warning("聊天接口处理失败")
            return JSONResponse(
                status_code=500,
                content={"detail": "服务暂时不可用，请稍后再试。"},
            )
        else:
            # 只有 Agent 成功返回后才保存；失败时数据库里的旧快照保持不变。
            # 传入内存上限与当前所有活动会话，“全部活跃”时允许数据库暂时超限。
            _save_snapshot(store, session_id, agent, MAX_SESSIONS, active_session_ids())
        finally:
            # 成功或异常都要释放，避免后续请求被永久阻塞
            session_lock.release()
            # 活动计数必须同步减少，否则会话会被误判为一直在执行
            finish_request(session_id)

        if not isinstance(reply, str):
            reply = str(reply)
        # 返回本次请求命中的业务模块，便于前端与验收确认路由结果。
        # Router.route 是纯函数，重复调用不会改变会话状态。
        module_name = Router.GENERAL
        router = getattr(agent, "router", None)
        route = getattr(router, "route", None)
        if callable(route):
            try:
                module_name = route(message)
            except Exception:
                module_name = Router.GENERAL
        return {"reply": reply, "session_id": session_id, "module": module_name}

    @app.delete("/api/session/{session_id}")
    def delete_session(session_id: str):
        """清除指定会话：先删数据库快照，成功后再删内存缓存。

        正在执行或等待回复的会话返回 409，避免删除后又被旧请求写回；
        删除不存在的会话按幂等成功处理。
        """
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            return JSONResponse(
                status_code=400,
                content={"detail": "会话编号不合法：只允许 1～64 位字母、数字、下划线或连字符。"},
            )

        remove = getattr(store, "delete", None)
        with lock:
            # 活动计数大于 0 说明有请求正在执行或等待会话锁
            if active_counts.get(session_id, 0) > 0:
                return JSONResponse(
                    status_code=409,
                    content={"detail": "会话正在处理消息，请等待回复完成后再清空。"},
                )
            # 先删数据库：失败时保留内存会话，避免上下文丢失或快照复活
            if callable(remove) and not remove(session_id):
                logger.warning("会话清除失败，保留内存会话")
                return JSONResponse(
                    status_code=503,
                    content={"detail": "会话清除失败，请稍后再试。"},
                )
            drop_session_locked(session_id)
        return {"deleted": True, "session_id": session_id}

    return app


# uvicorn 启动入口：agent.web_app:app
app = create_app()