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
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .main import Agent
from .modules.amap_client import (
    AmapClient,
    CITY_MAX_LEN as NAVIGATION_SEARCH_CITY_MAX_LEN,
    DATA_SOURCE as NAVIGATION_SEARCH_DATA_SOURCE,
    KEYWORD_MAX_LEN as NAVIGATION_SEARCH_KEYWORD_MAX_LEN,
    TYPES_MAX_LEN as NAVIGATION_SEARCH_TYPES_MAX_LEN,
    TYPES_PATTERN as NAVIGATION_SEARCH_TYPES_PATTERN,
)
from .modules.health_db import HealthDB
from .modules.medication_db import MedicationDB
from .modules.medical_navigation_db import (
    CATEGORIES as NAVIGATION_CATEGORIES,
    MedicalNavigationDB,
)
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

# 健康指标允许的类型与合理范围（血压 mmHg、血糖 mmol/L、心率 次/分、体温 ℃、血氧 %）
METRIC_RANGES = {
    "systolic_bp": (60.0, 260.0),
    "diastolic_bp": (30.0, 160.0),
    "blood_glucose": (1.0, 40.0),
    "heart_rate": (30.0, 220.0),
    "temperature": (30.0, 45.0),
    "spo2": (50.0, 100.0),
}

# 健康档案与体重记录的合理范围（与聊天模块保持一致）
HEIGHT_RANGE = (100.0, 250.0)
WEIGHT_RANGE = (20.0, 300.0)

# 文本字段上限与档案取值约束
NAME_MAX_LEN = 20
NOTE_MAX_LEN = 200
GENDERS = ("男", "女")

# 列表查询的默认与最大条数
DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# 日期与时间只接受固定数字格式，避免任意字符串直接写库
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?$")
TIME_PATTERN = re.compile(r"^\d{1,2}:\d{2}$")

# 用药计划字段长度与用药时间点数量上限
MEDICATION_NAME_MAX_LEN = 50
INSTRUCTION_MAX_LEN = 200
MEDICATION_NOTE_MAX_LEN = 200
MAX_SCHEDULE_TIMES = 6

# 用药接口统一附带的安全提示：只做手动记录，不提供任何用药建议或提醒
MEDICATION_NOTICE = (
    "以上记录不能替代医生诊疗；本功能仅支持手动记录，"
    "不提供系统提醒，也不提供药物、剂量、停药或补服建议。"
)

# 删除计划时允许的两种明确行为：stop 停用并保留历史，delete 物理删除
PLAN_DELETE_MODES = ("stop", "delete")

# 就医地点的字段长度上限（名称按产品要求固定为 1～100 字）
NAVIGATION_NAME_MAX_LEN = 100
NAVIGATION_ADDRESS_MAX_LEN = 200
NAVIGATION_PHONE_MAX_LEN = 50
NAVIGATION_NOTE_MAX_LEN = 200

# 高德实时搜索失败时，内部错误分类到 HTTP 状态码的映射
NAVIGATION_SEARCH_ERROR_STATUS = {
    "INVALID_REQUEST": 400,
    "AUTH_FAILED": 502,
    "QUOTA_EXCEEDED": 429,
    "CONFIG_MISSING": 503,
    "NETWORK_ERROR": 502,
    "TIMEOUT": 504,
    "HTTP_ERROR": 502,
    "INVALID_RESPONSE": 502,
    "CLIENT_CLOSED": 503,
}

# 实时搜索的固定提示：明确数据来源，说明结果不落库、不构成医疗建议
NAVIGATION_SEARCH_NOTICE = (
    "以下为高德地图实时搜索结果，仅供参考，不构成医疗建议；"
    "结果仅在本次展示，不会保存到本地。"
)


class ChatRequest(BaseModel):
    """聊天请求体；字段都有默认值，避免缺字段时报 422。"""

    message: str = ""
    session_id: str = ""


class ProfileRequest(BaseModel):
    """健康档案请求体；字段都可选，只更新明确传入的字段。"""

    name: Any = None
    gender: Any = None
    birth_date: Any = None
    height_cm: Any = None


class WeightRequest(BaseModel):
    """体重记录请求体；weight_kg 必填，其余可选。"""

    weight_kg: Any = None
    record_date: Any = None
    note: Any = None


class MetricRequest(BaseModel):
    """健康指标请求体；metric 与 value 必填。"""

    metric: Any = None
    value: Any = None
    recorded_at: Any = None


class MedicationPlanRequest(BaseModel):
    """用药计划请求体；药名与医嘱用法必填。"""

    medication_name: Any = None
    doctor_instruction: Any = None
    schedule_times: Any = None
    start_date: Any = None
    end_date: Any = None
    note: Any = None


class MedicationLogRequest(BaseModel):
    """服药记录请求体；plan_id 必填，时间为空时使用当前时间。"""

    plan_id: Any = None
    taken_at: Any = None
    note: Any = None


class NavigationPlaceRequest(BaseModel):
    """就医地点请求体；新增时 name 与 category 必填，编辑时只处理传入字段。"""

    name: Any = None
    category: Any = None
    address: Any = None
    phone: Any = None
    note: Any = None


def _parse_float(value):
    """把输入转成 float；空值、布尔和非法文本一律返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value, minimum=1):
    """把输入转成不小于 minimum 的整数；小数、布尔和非法文本返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value.strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= minimum else None


def _parse_date(value):
    """校验 YYYY-MM-DD 日期，非法或晚于今天时返回 None。"""
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None
    return None if parsed > date.today() else value


def _parse_datetime(value):
    """校验 YYYY-MM-DD HH:MM[:SS] 时间，非法时返回 None。"""
    if not isinstance(value, str) or not DATETIME_PATTERN.match(value):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            datetime.strptime(value, fmt)
            return value
        except ValueError:
            continue
    return None


def _parse_plan_date(value, label):
    """校验可选的计划日期（YYYY-MM-DD），不限制未来日期。

    返回 (日期字符串或 None, 错误提示或 None)。
    """
    if value is None:
        return None, None
    if not isinstance(value, str) or not DATE_PATTERN.match(value):
        return None, f"{label}格式应为 YYYY-MM-DD，例如 2026-11-01。"
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None, f"{label}不是有效日期，应为 YYYY-MM-DD。"
    return value, None


def _parse_schedule_times(value):
    """校验用药时间点，返回 (逗号拼接字符串或 None, 错误提示或 None)。

    只记录医嘱里已有的时间点，不生成、不推断任何用药方案。
    """
    if value is None:
        return None, None
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    elif isinstance(value, str):
        items = [item.strip() for item in re.split(r"[,，\s]+", value) if item.strip()]
    else:
        return None, "用药时间必须是 HH:MM 文本或文本数组。"
    if not items:
        return None, "用药时间不能为空。"
    if len(items) > MAX_SCHEDULE_TIMES:
        return None, f"用药时间最多 {MAX_SCHEDULE_TIMES} 个。"
    for item in items:
        if not TIME_PATTERN.match(item):
            return None, "用药时间格式应为 HH:MM，例如 08:00。"
        try:
            datetime.strptime(item, "%H:%M")
        except ValueError:
            return None, "用药时间不是有效时刻，例如 08:00。"
    return ",".join(items), None


def _parse_taken_at(value):
    """校验服药时间且不得晚于当前时间，返回 (时间字符串或 None, 错误) 。"""
    if value is None:
        return None, None
    parsed = _parse_datetime(value)
    if parsed is None:
        return None, "服药时间格式应为 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。"
    moment = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            moment = datetime.strptime(parsed, fmt)
            break
        except ValueError:
            continue
    if moment is None:
        return None, "服药时间格式不正确。"
    if moment > datetime.now():
        return None, "服药时间不能晚于当前时间。"
    return parsed, None


def _parse_medication_note(value):
    """校验可选的用药备注，返回 (备注或 None, 错误提示或 None)。"""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "备注必须是文本。"
    note = value.strip() or None
    if note is not None and len(note) > MEDICATION_NOTE_MAX_LEN:
        return None, f"备注长度最多 {MEDICATION_NOTE_MAX_LEN} 个字符。"
    return note, None


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


def create_app(
    agent_factory=None,
    session_store=None,
    health_db=None,
    medication_db=None,
    navigation_db=None,
    amap_client=None,
):
    """创建 FastAPI 应用。

    参数：
        agent_factory: 可选的 Agent 工厂函数，默认使用 Agent；测试可注入假实现。
        session_store: 可选的会话存储，默认使用 data/sessions.db；测试应传入临时目录。
        health_db: 可选的健康数据库实例；不传时在首次访问健康接口时才创建，
                   保持启动阶段不产生数据库文件的原有行为。
        medication_db: 可选的用药数据库实例；不传时在首次访问用药接口时才创建，
                       同样保持启动阶段不产生数据库文件。
        navigation_db: 可选的导航数据库实例；不传时在首次访问导航接口时才创建。
        amap_client: 可选的高德地图客户端；不传时在首次实时搜索时创建，
                     并从环境变量 AMAP_KEY 读取 Key。测试可注入离线客户端。
    """
    app = FastAPI(title="医知有据", description="医学知识、健康管理、用药与就医地点助手")
    factory = agent_factory or Agent
    store = session_store if session_store is not None else SessionStore()

    # 健康数据库懒加载：只有第一次调用健康接口时才建库建表
    health_holder = {"db": health_db}
    # 可重入锁：health_session 与 get_health_db 可能嵌套获取同一把锁
    health_lock = RLock()

    def get_health_db():
        """返回健康数据库实例，首次使用时完成初始化。"""
        db = health_holder["db"]
        if db is None:
            with health_lock:
                db = health_holder["db"]
                if db is None:
                    # Web 请求由线程池执行，允许跨线程共用连接，访问时统一加锁
                    db = HealthDB(check_same_thread=False)
                    db.initialize()
                    health_holder["db"] = db
        return db

    @contextmanager
    def health_session():
        """在锁内提供健康数据库实例，避免多线程并发操作同一连接。"""
        with health_lock:
            yield get_health_db()

    # 用药数据库同样懒加载：首次访问用药接口时才建库建表
    medication_holder = {"db": medication_db}
    medication_lock = RLock()

    def get_medication_db():
        """返回用药数据库实例，首次使用时完成初始化。"""
        db = medication_holder["db"]
        if db is None:
            with medication_lock:
                db = medication_holder["db"]
                if db is None:
                    # 与健康模块同构：同一连接被多个请求线程共用，访问时统一加锁
                    db = MedicationDB(check_same_thread=False)
                    db.initialize()
                    medication_holder["db"] = db
        return db

    @contextmanager
    def medication_session():
        """在锁内提供用药数据库实例，保证计划与记录的读写串行。"""
        with medication_lock:
            yield get_medication_db()

    # 导航数据库同样懒加载：首次访问导航接口时才建库建表
    navigation_holder = {"db": navigation_db}
    navigation_lock = RLock()

    def get_navigation_db():
        """返回导航数据库实例，首次使用时完成初始化。"""
        db = navigation_holder["db"]
        if db is None:
            with navigation_lock:
                db = navigation_holder["db"]
                if db is None:
                    # 与健康、用药模块同构：多个请求线程共用连接，访问时统一加锁
                    db = MedicalNavigationDB(check_same_thread=False)
                    db.initialize()
                    navigation_holder["db"] = db
        return db

    @contextmanager
    def navigation_session():
        """在锁内提供导航数据库实例，保证读写串行。"""
        with navigation_lock:
            yield get_navigation_db()

    # 高德地图客户端懒加载：只有第一次调用实时搜索时才创建。
    # 未注入时从环境变量 AMAP_KEY 读取 Key，代码中不硬编码任何密钥。
    amap_holder = {"client": amap_client}
    amap_lock = RLock()

    def get_amap_client():
        """返回高德地图客户端，首次使用时完成创建。"""
        client = amap_holder["client"]
        if client is None:
            with amap_lock:
                client = amap_holder["client"]
                if client is None:
                    client = AmapClient()
                    amap_holder["client"] = client
        return client

    # 注册健康管理结构化接口；下面四个原有接口的行为保持不变
    _register_health_api(app, health_session)
    # 注册用药管理结构化接口，同样只做手动记录，不涉及 Agent 与大模型
    _register_medication_api(app, medication_session)
    # 注册就医地点结构化接口：手动地点 CRUD + 高德实时搜索（结果不落库）
    _register_navigation_api(app, navigation_session, get_amap_client)

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


def _register_health_api(app, health_session):
    """注册健康管理结构化接口。

    所有数据库异常都转换为中文提示，不返回堆栈；
    health_session 是在锁内提供 HealthDB 实例的上下文管理器。
    """

    def fail(status_code, message):
        """统一失败响应：只包含中文提示，不含内部异常信息。"""
        return JSONResponse(status_code=status_code, content={"detail": message})

    def read_error():
        """数据库读取失败的统一响应。"""
        logger.warning("健康数据读取失败")
        return fail(500, "健康数据暂时不可用，请稍后再试。")

    def write_error():
        """数据库写入失败的统一响应。"""
        logger.warning("健康数据写入失败")
        return fail(500, "健康数据暂时不可用，请稍后再试。")

    def limit_error():
        """列表条数非法时的统一响应。"""
        return fail(400, f"limit 应为 1～{MAX_LIMIT} 之间的整数。")

    @app.get("/api/health/profile")
    def get_profile():
        """读取健康档案；未建立时返回空对象。"""
        try:
            with health_session() as db:
                profile = db.get_profile()
        except sqlite3.Error:
            return read_error()
        return {"profile": profile, "exists": bool(profile)}

    @app.put("/api/health/profile")
    def put_profile(payload: ProfileRequest):
        """创建或更新健康档案，只更新明确传入的字段。"""
        fields = {}

        if payload.name is not None:
            if not isinstance(payload.name, str):
                return fail(400, "姓名必须是文本。")
            name = payload.name.strip()
            if not name or len(name) > NAME_MAX_LEN:
                return fail(400, f"姓名长度应为 1～{NAME_MAX_LEN} 个字符。")
            fields["name"] = name

        if payload.gender is not None:
            if payload.gender not in GENDERS:
                return fail(400, "性别只能是「男」或「女」。")
            fields["gender"] = payload.gender

        if payload.birth_date is not None:
            birth_date = _parse_date(payload.birth_date)
            if birth_date is None:
                return fail(400, "出生日期格式应为 YYYY-MM-DD，且不能晚于今天。")
            fields["birth_date"] = birth_date

        if payload.height_cm is not None:
            height = _parse_float(payload.height_cm)
            if height is None or not HEIGHT_RANGE[0] <= height <= HEIGHT_RANGE[1]:
                return fail(400, "身高应为 100～250 cm 之间的数字。")
            fields["height_cm"] = height

        # 空请求体没有可更新内容，直接拒绝，避免误以为保存成功
        if not fields:
            return fail(400, "请至少提供一个要更新的档案字段。")

        try:
            with health_session() as db:
                profile = db.update_profile(**fields)
        except sqlite3.Error:
            return write_error()
        return {"profile": profile}

    @app.get("/api/health/weights")
    def list_weights(limit: str = str(DEFAULT_LIMIT)):
        """按记录日期倒序返回体重历史。"""
        parsed_limit = _parse_int(limit)
        if parsed_limit is None or parsed_limit > MAX_LIMIT:
            return limit_error()
        try:
            with health_session() as db:
                records = db.list_weights(parsed_limit)
        except sqlite3.Error:
            return read_error()
        return {"records": records, "count": len(records)}

    @app.post("/api/health/weights", status_code=201)
    def add_weight(payload: WeightRequest):
        """新增体重记录，日期缺省为当天。"""
        weight = _parse_float(payload.weight_kg)
        if weight is None or not WEIGHT_RANGE[0] <= weight <= WEIGHT_RANGE[1]:
            return fail(400, "体重应为 20～300 kg 之间的数字。")

        record_date = None
        if payload.record_date is not None:
            record_date = _parse_date(payload.record_date)
            if record_date is None:
                return fail(400, "记录日期格式应为 YYYY-MM-DD，且不能晚于今天。")

        note = None
        if payload.note is not None:
            if not isinstance(payload.note, str):
                return fail(400, "备注必须是文本。")
            note = payload.note.strip() or None
            if note is not None and len(note) > NOTE_MAX_LEN:
                return fail(400, f"备注长度最多 {NOTE_MAX_LEN} 个字符。")

        try:
            with health_session() as db:
                record_id = db.add_weight(weight, record_date, note)
                records = db.list_weights(DEFAULT_LIMIT)
        except sqlite3.Error:
            return write_error()
        return JSONResponse(
            status_code=201, content={"id": record_id, "records": records}
        )

    @app.delete("/api/health/weights/{record_id}")
    def delete_weight(record_id: str):
        """按编号删除体重记录；不存在时返回 404。"""
        parsed_id = _parse_int(record_id)
        if parsed_id is None:
            return fail(400, "记录编号必须是正整数。")
        try:
            with health_session() as db:
                deleted = db.delete_weight(parsed_id)
        except sqlite3.Error:
            return write_error()
        if not deleted:
            return fail(404, "未找到该体重记录。")
        return {"deleted": True, "record_id": parsed_id}

    @app.get("/api/health/metrics")
    def list_metrics(metric: str = "", limit: str = str(DEFAULT_LIMIT)):
        """按记录时间倒序返回健康指标，可按类型过滤。"""
        if metric and metric not in METRIC_RANGES:
            return fail(400, "metric 不在支持的指标类型中。")
        parsed_limit = _parse_int(limit)
        if parsed_limit is None or parsed_limit > MAX_LIMIT:
            return limit_error()
        try:
            with health_session() as db:
                records = db.list_metrics(metric or None, parsed_limit)
        except sqlite3.Error:
            return read_error()
        return {"records": records, "count": len(records)}

    @app.post("/api/health/metrics", status_code=201)
    def add_metric(payload: MetricRequest):
        """新增健康指标记录，按类型校验合理范围。"""
        metric = payload.metric
        if not isinstance(metric, str) or metric not in METRIC_RANGES:
            return fail(
                400, "metric 必须是：" + "、".join(METRIC_RANGES) + " 之一。"
            )
        low, high = METRIC_RANGES[metric]
        value = _parse_float(payload.value)
        if value is None or not low <= value <= high:
            return fail(400, f"该指标数值应在 {low}～{high} 之间。")

        recorded_at = None
        if payload.recorded_at is not None:
            recorded_at = _parse_datetime(payload.recorded_at)
            if recorded_at is None:
                return fail(
                    400,
                    "记录时间格式应为 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS。",
                )

        try:
            with health_session() as db:
                record_id = db.add_metric(metric, value, recorded_at)
                records = db.list_metrics(metric, DEFAULT_LIMIT)
        except sqlite3.Error:
            return write_error()
        return JSONResponse(
            status_code=201, content={"id": record_id, "records": records}
        )


def _register_medication_api(app, medication_session):
    """注册用药管理结构化接口。

    只提供手动记录能力：不生成用药方案、不提供系统提醒、不调用大模型。
    所有数据库异常都转换为中文提示，不返回堆栈或内部字段。
    """

    def fail(status_code, message):
        """统一失败响应：只包含中文提示，不含内部异常信息。"""
        return JSONResponse(status_code=status_code, content={"detail": message})

    def read_error():
        """数据库读取失败的统一响应。"""
        logger.warning("用药数据读取失败")
        return fail(500, "用药数据暂时不可用，请稍后再试。")

    def write_error():
        """数据库写入失败的统一响应。"""
        logger.warning("用药数据写入失败")
        return fail(500, "用药数据暂时不可用，请稍后再试。")

    def limit_error():
        """列表条数非法时的统一响应。"""
        return fail(400, f"limit 应为 1～{MAX_LIMIT} 之间的整数。")

    def recent_plans(db):
        """返回最近若干条计划，按编号升序，便于前端直接渲染。"""
        return db.list_plans()[-DEFAULT_LIMIT:]

    @app.get("/api/medication/plans")
    def list_plans(status: str = "", limit: str = str(DEFAULT_LIMIT)):
        """返回用药计划列表，可按生效/停用状态过滤。

        变更为停用或删除后，历史计划默认保留，便于用户核对既往医嘱。
        """
        if status and status not in ("active", "stopped"):
            return fail(400, "status 只能是 active（生效中）或 stopped（已停用）。")
        parsed_limit = _parse_int(limit)
        if parsed_limit is None or parsed_limit > MAX_LIMIT:
            return limit_error()
        try:
            with medication_session() as db:
                plans = db.list_plans()
        except sqlite3.Error:
            return read_error()
        if status:
            plans = [plan for plan in plans if plan.get("status") == status]
        # 只返回编号最大的若干条，避免一次返回全部历史
        plans = plans[-parsed_limit:]
        return {
            "plans": plans,
            "count": len(plans),
            "notice": MEDICATION_NOTICE,
        }

    @app.post("/api/medication/plans", status_code=201)
    def create_plan(payload: MedicationPlanRequest):
        """新增一条用药计划；药名与医嘱用法必须由用户提供。

        接口只做格式校验与存档，不推断剂量、频次或疗程。
        """
        name = payload.medication_name
        if not isinstance(name, str):
            return fail(400, "药名必须是文本。")
        name = name.strip()
        if not name or len(name) > MEDICATION_NAME_MAX_LEN:
            return fail(
                400, f"药名长度应为 1～{MEDICATION_NAME_MAX_LEN} 个字符。"
            )

        instruction = payload.doctor_instruction
        if not isinstance(instruction, str):
            return fail(400, "医嘱用法必须是文本。")
        instruction = instruction.strip()
        if not instruction or len(instruction) > INSTRUCTION_MAX_LEN:
            return fail(
                400, f"医嘱用法长度应为 1～{INSTRUCTION_MAX_LEN} 个字符。"
            )

        schedule_times, error = _parse_schedule_times(payload.schedule_times)
        if error:
            return fail(400, error)

        start_date, error = _parse_plan_date(payload.start_date, "开始日期")
        if error:
            return fail(400, error)
        end_date, error = _parse_plan_date(payload.end_date, "结束日期")
        if error:
            return fail(400, error)
        if start_date and end_date and start_date > end_date:
            return fail(400, "结束日期不能早于开始日期。")

        note, error = _parse_medication_note(payload.note)
        if error:
            return fail(400, error)

        try:
            with medication_session() as db:
                plan_id = db.create_plan(
                    name,
                    instruction,
                    schedule_times=schedule_times,
                    start_date=start_date,
                    end_date=end_date,
                    note=note,
                )
                plans = recent_plans(db)
        except sqlite3.Error:
            return write_error()
        return JSONResponse(
            status_code=201,
            content={
                "id": plan_id,
                "plans": plans,
                "count": len(plans),
                "notice": MEDICATION_NOTICE,
            },
        )

    @app.delete("/api/medication/plans/{plan_id}")
    def delete_plan(plan_id: str, mode: str = "stop"):
        """按编号处理用药计划，必须明确区分两种行为。

        mode=stop（默认）：只把状态改为已停用，保留计划与历史记录；
        mode=delete：物理删除计划及其关联服药记录，不可恢复。
        """
        parsed_id = _parse_int(plan_id)
        if parsed_id is None:
            return fail(400, "计划编号必须是正整数。")
        if mode not in PLAN_DELETE_MODES:
            return fail(
                400, "mode 只能是 stop（停用并保留历史）或 delete（物理删除）。"
            )

        try:
            with medication_session() as db:
                if mode == "stop":
                    if not db.stop_plan(parsed_id):
                        if db.get_plan(parsed_id) is None:
                            return fail(404, "未找到该用药计划。")
                        return fail(409, "该用药计划已停用，无需重复停用。")
                else:
                    if not db.delete_plan(parsed_id):
                        return fail(404, "未找到该用药计划。")
                plans = recent_plans(db)
        except sqlite3.Error:
            return write_error()
        return {
            "mode": mode,
            "plan_id": parsed_id,
            "plans": plans,
            "count": len(plans),
            "notice": MEDICATION_NOTICE,
        }

    @app.get("/api/medication/logs")
    def list_logs(limit: str = str(DEFAULT_LIMIT)):
        """按服药时间倒序返回最近的手动记录。"""
        parsed_limit = _parse_int(limit)
        if parsed_limit is None or parsed_limit > MAX_LIMIT:
            return limit_error()
        try:
            with medication_session() as db:
                logs = db.list_logs(parsed_limit)
        except sqlite3.Error:
            return read_error()
        return {
            "logs": logs,
            "count": len(logs),
            "notice": MEDICATION_NOTICE,
        }

    @app.post("/api/medication/logs", status_code=201)
    def create_log(payload: MedicationLogRequest):
        """新增一条手动服药记录；时间为空时使用服务器当前时间。

        数据库层会再次校验计划存在且处于生效状态，避免并发停用产生脏记录。
        """
        plan_id = _parse_int(payload.plan_id)
        if plan_id is None:
            return fail(400, "plan_id 必须是正整数。")

        taken_at, error = _parse_taken_at(payload.taken_at)
        if error:
            return fail(400, error)
        if taken_at is None:
            taken_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        note, error = _parse_medication_note(payload.note)
        if error:
            return fail(400, error)

        try:
            with medication_session() as db:
                log_id = db.add_log(plan_id, taken_at, note)
                if log_id is None:
                    return fail(400, "该用药计划不存在或已停用，无法记录服药。")
                logs = db.list_logs(DEFAULT_LIMIT)
        except sqlite3.Error:
            return write_error()
        return JSONResponse(
            status_code=201,
            content={
                "id": log_id,
                "logs": logs,
                "count": len(logs),
                "notice": MEDICATION_NOTICE,
            },
        )

    @app.delete("/api/medication/logs/{log_id}")
    def delete_log(log_id: str):
        """按编号删除一条服药记录；不存在时返回 404。"""
        parsed_id = _parse_int(log_id)
        if parsed_id is None:
            return fail(400, "记录编号必须是正整数。")
        try:
            with medication_session() as db:
                deleted = db.delete_log(parsed_id)
        except sqlite3.Error:
            return write_error()
        if not deleted:
            return fail(404, "未找到该服药记录。")
        return {"deleted": True, "log_id": parsed_id}


def _register_navigation_api(app, navigation_session, get_amap_client):
    """注册医疗导航结构化接口。

    - /api/navigation/places（及子路径）只管理用户手动保存的地点；
    - /api/navigation/search 调用高德 Web 服务做实时关键字搜索，
      结果只在本次响应中返回，不写数据库、不缓存、不转成收藏地点。
    所有异常都转换为中文提示，不返回堆栈或内部字段。
    """

    def fail(status_code, message):
        """统一失败响应：只包含中文提示，不含内部异常信息。"""
        return JSONResponse(status_code=status_code, content={"detail": message})

    def read_error():
        """数据库读取失败的统一响应。"""
        logger.warning("就医地点读取失败")
        return fail(500, "就医地点数据暂时不可用，请稍后再试。")

    def write_error():
        """数据库写入失败的统一响应。"""
        logger.warning("就医地点写入失败")
        return fail(500, "就医地点数据暂时不可用，请稍后再试。")

    def limit_error():
        """列表条数非法时的统一响应。"""
        return fail(400, f"limit 应为 1～{MAX_LIMIT} 之间的整数。")

    def category_error():
        """类别不在支持范围时的统一响应。"""
        return fail(
            400, "类型必须是：" + "、".join(NAVIGATION_CATEGORIES) + " 之一。"
        )

    def name_error():
        """名称非法时的统一响应。"""
        return fail(400, f"地点名称必须是 1～{NAVIGATION_NAME_MAX_LEN} 个字符的文本。")

    def parse_optional_text(value, label, max_len):
        """规范化可选文本，返回 (文本或 None, 错误提示或 None)。

        空白字符串视为未填写，统一转成 None，避免空串被当成有效内容。
        """
        if value is None:
            return None, None
        if not isinstance(value, str):
            return None, f"{label}必须是文本。"
        text = value.strip()
        if not text:
            return None, None
        if len(text) > max_len:
            return None, f"{label}最多 {max_len} 个字符。"
        return text, None

    def search_parameter_error(message):
        """实时搜索参数非法时的统一响应。"""
        return JSONResponse(
            status_code=400,
            content={"detail": message, "error_code": "INVALID_REQUEST"},
        )

    @app.get("/api/navigation/search")
    def search_realtime_places(
        keywords: str = "",
        city: str = "",
        citylimit: str = "true",
        types: str = "",
    ):
        """调用高德地图实时搜索地点。

        只做关键字搜索，结果不保存、不缓存、不写数据库。
        失败时返回明确的错误分类与中文提示，不伪装成成功或无结果。
        """
        keyword_text = keywords.strip()
        if not keyword_text or len(keyword_text) > NAVIGATION_SEARCH_KEYWORD_MAX_LEN:
            return search_parameter_error(
                f"keywords 必须是 1～{NAVIGATION_SEARCH_KEYWORD_MAX_LEN} 个字符的文本。"
            )

        city_text = city.strip()
        if not city_text or len(city_text) > NAVIGATION_SEARCH_CITY_MAX_LEN:
            return search_parameter_error(
                f"city 必须是 1～{NAVIGATION_SEARCH_CITY_MAX_LEN} 个字符的文本。"
            )

        limit_text = citylimit.strip().lower()
        if limit_text not in ("true", "false"):
            return search_parameter_error("citylimit 只能是 true 或 false。")

        types_text = types.strip()
        if types_text:
            if len(types_text) > NAVIGATION_SEARCH_TYPES_MAX_LEN:
                return search_parameter_error(
                    f"types 总长度不能超过 {NAVIGATION_SEARCH_TYPES_MAX_LEN} 个字符。"
                )
            if not NAVIGATION_SEARCH_TYPES_PATTERN.match(types_text):
                return search_parameter_error(
                    "types 必须是 6 位数字的高德 POI 分类编码，多个用 | 分隔。"
                )

        result = get_amap_client().search_places(
            keyword_text,
            city=city_text,
            citylimit=limit_text == "true",
            types=types_text or None,
        )
        if not result.get("success"):
            error_code = result.get("error_code") or "INVALID_RESPONSE"
            status_code = NAVIGATION_SEARCH_ERROR_STATUS.get(error_code, 502)
            return JSONResponse(
                status_code=status_code,
                content={
                    "detail": result.get("message") or "实时地点搜索失败，请稍后再试。",
                    "error_code": error_code,
                },
            )

        results = result.get("results") or []
        return {
            "results": results,
            "count": len(results),
            "data_source": NAVIGATION_SEARCH_DATA_SOURCE,
            "notice": NAVIGATION_SEARCH_NOTICE,
        }

    @app.get("/api/navigation/places")
    def list_places(
        keyword: str = "", category: str = "", limit: str = str(DEFAULT_LIMIT)
    ):
        """按关键词、类别查询已保存的就医地点。

        keyword 为空时走列表查询，否则先在名称/类别/地址中模糊搜索，
        再按类别过滤并截断条数，保持与 DAO 相同的排序（编号升序）。
        """
        if category and category not in NAVIGATION_CATEGORIES:
            return category_error()
        parsed_limit = _parse_int(limit)
        if parsed_limit is None or parsed_limit > MAX_LIMIT:
            return limit_error()

        keyword = keyword.strip()
        try:
            with navigation_session() as db:
                if keyword:
                    places = db.search_places(keyword)
                    if category:
                        places = [
                            place for place in places if place["category"] == category
                        ]
                    places = places[:parsed_limit]
                else:
                    places = db.list_places(
                        category=category or None, limit=parsed_limit
                    )
        except sqlite3.Error:
            return read_error()
        return {"places": places, "count": len(places)}

    @app.post("/api/navigation/places", status_code=201)
    def create_place(payload: NavigationPlaceRequest):
        """新增一个手动保存的就医地点，成功返回 201 与地点详情。"""
        if not isinstance(payload.name, str):
            return name_error()
        name = payload.name.strip()
        if not name or len(name) > NAVIGATION_NAME_MAX_LEN:
            return name_error()

        if (
            not isinstance(payload.category, str)
            or payload.category not in NAVIGATION_CATEGORIES
        ):
            return category_error()

        address, error = parse_optional_text(
            payload.address, "地址", NAVIGATION_ADDRESS_MAX_LEN
        )
        if error:
            return fail(400, error)
        phone, error = parse_optional_text(
            payload.phone, "电话", NAVIGATION_PHONE_MAX_LEN
        )
        if error:
            return fail(400, error)
        note, error = parse_optional_text(
            payload.note, "备注", NAVIGATION_NOTE_MAX_LEN
        )
        if error:
            return fail(400, error)

        try:
            with navigation_session() as db:
                place_id = db.create_place(
                    name,
                    category=payload.category,
                    address=address,
                    phone=phone,
                    note=note,
                )
                place = db.get_place(place_id)
        except sqlite3.Error:
            return write_error()
        return JSONResponse(status_code=201, content={"place": place})

    @app.get("/api/navigation/places/{place_id}")
    def get_place(place_id: str):
        """按编号查询单个地点；不存在返回 404。"""
        parsed_id = _parse_int(place_id)
        if parsed_id is None:
            return fail(400, "地点编号必须是正整数。")
        try:
            with navigation_session() as db:
                place = db.get_place(parsed_id)
        except sqlite3.Error:
            return read_error()
        if place is None:
            return fail(404, "未找到该就医地点。")
        return {"place": place}

    @app.put("/api/navigation/places/{place_id}")
    def update_place(place_id: str, payload: NavigationPlaceRequest):
        """编辑手动地点。

        未传入的字段保持不变；可选字段传空字符串表示清空；
        id 与 created_at 不允许修改（请求模型里也没有这两个字段）。
        """
        parsed_id = _parse_int(place_id)
        if parsed_id is None:
            return fail(400, "地点编号必须是正整数。")

        fields = {}

        if payload.name is not None:
            if not isinstance(payload.name, str):
                return name_error()
            name = payload.name.strip()
            if not name or len(name) > NAVIGATION_NAME_MAX_LEN:
                return name_error()
            fields["name"] = name

        if payload.category is not None:
            if (
                not isinstance(payload.category, str)
                or payload.category not in NAVIGATION_CATEGORIES
            ):
                return category_error()
            fields["category"] = payload.category

        for field, label, max_len in (
            ("address", "地址", NAVIGATION_ADDRESS_MAX_LEN),
            ("phone", "电话", NAVIGATION_PHONE_MAX_LEN),
            ("note", "备注", NAVIGATION_NOTE_MAX_LEN),
        ):
            value = getattr(payload, field)
            if value is None:
                continue
            if not isinstance(value, str):
                return fail(400, f"{label}必须是文本。")
            text = value.strip()
            if len(text) > max_len:
                return fail(400, f"{label}最多 {max_len} 个字符。")
            # 空字符串交给 DAO 按“清空该字段”处理
            fields[field] = text

        # 没有任何字段需要更新时直接拒绝，避免误以为保存成功
        if not fields:
            return fail(400, "请至少提供一个要更新的地点字段。")

        try:
            with navigation_session() as db:
                if db.get_place(parsed_id) is None:
                    return fail(404, "未找到该就医地点。")
                updated = db.update_place(parsed_id, **fields)
                place = db.get_place(parsed_id)
        except sqlite3.Error:
            return write_error()
        if not updated:
            return fail(404, "未找到该就医地点。")
        return {"place": place}

    @app.delete("/api/navigation/places/{place_id}")
    def delete_place(place_id: str):
        """按编号删除手动地点；不存在返回 404。"""
        parsed_id = _parse_int(place_id)
        if parsed_id is None:
            return fail(400, "地点编号必须是正整数。")
        try:
            with navigation_session() as db:
                deleted = db.delete_place(parsed_id)
        except sqlite3.Error:
            return write_error()
        if not deleted:
            return fail(404, "未找到该就医地点。")
        return {"deleted": True, "place_id": parsed_id}


# uvicorn 启动入口：agent.web_app:app
app = create_app()