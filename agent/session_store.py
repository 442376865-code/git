"""SQLite 会话存储：保存 Web 聊天的会话快照。

设计约束：
- 只保存 session_id 与消息快照，不保存健康数据或其他用户信息；
- 所有 SQL 都使用参数化传参，不做字符串拼接；
- 使用短连接，用完立刻关闭，避免 Windows 下文件句柄长期占用；
- 只适用于单进程部署。多个 worker 会各自持有内存会话缓存，
  无法保证同一会话的一致性，本模块不宣称支持多进程或多 worker。
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 默认数据库位置：项目根目录下的 data/sessions.db
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "sessions.db"

# 超过这个天数没有更新的会话会在保存时被清理
SESSION_TTL_DAYS = 7

# 建表语句：session_id 主键，保证同一会话只有一份快照
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

# 写入快照：同一 session_id 只更新消息和更新时间，保留最初的创建时间
UPSERT_SQL = """
INSERT INTO sessions (session_id, messages_json, created_at, updated_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(session_id) DO UPDATE SET
    messages_json = excluded.messages_json,
    updated_at = excluded.updated_at
"""

# 容量淘汰顺序：先按更新时间从旧到新；同一时间用 rowid（插入顺序）保证真正最早
ORDER_SQL = "SELECT session_id FROM sessions ORDER BY updated_at ASC, rowid ASC"
COUNT_SQL = "SELECT COUNT(*) FROM sessions"
DELETE_SQL = "DELETE FROM sessions WHERE session_id = ?"


def _now():
    """返回 UTC 微秒级 ISO 时间字符串。

    微秒精度保证同一秒内的多次写入也能区分先后；
    旧数据里的秒级字符串仍然可以正常读取和比较。
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value):
    """解析 ISO 时间字符串，无法解析时返回 None。"""
    if not isinstance(value, str):
        return None
    # 兼容部分工具写出的 Z 后缀
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # 没有时区的旧数据按 UTC 处理
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_expired(value, cutoff):
    """判断时间是否早于截止时间；无法解析的时间按过期处理。"""
    parsed = _parse_time(value)
    if parsed is None:
        # 损坏时间无法判断新旧，安全起见视为过期，但调用方会跳过受保护会话
        return True
    return parsed < cutoff


def _normalize_protected(protected_ids):
    """把保护集合整理成字符串集合，忽略非法值。"""
    if not protected_ids:
        return set()
    return {sid for sid in protected_ids if isinstance(sid, str)}


class SessionStore:
    """基于 SQLite 的会话快照存储。"""

    def __init__(self, db_path=None):
        # 只记录路径，不在这里建目录或开连接，避免仅导入就产生文件
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    def _connect(self):
        """建立短连接，并确保表存在。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute(CREATE_TABLE_SQL)
        return conn

    def load(self, session_id):
        """读取会话快照。

        返回消息列表；数据库不存在、记录不存在、内容损坏或会话已过期时
        返回 None，由调用方按新会话处理，绝不因为坏数据抛异常。
        过期会话不能恢复旧历史，后续保存会用本次新消息覆盖旧快照。
        """
        # 文件不存在时直接返回，避免只为读取就创建空数据库
        if not self.db_path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute("PRAGMA busy_timeout = 3000")
                row = conn.execute(
                    "SELECT messages_json, updated_at FROM sessions "
                    "WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
        except (sqlite3.Error, OSError):
            return None
        if row is None:
            return None

        # 超过 TTL 或时间无法解析时一律视为过期，不恢复旧历史
        cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)
        if _is_expired(row[1], cutoff):
            return None

        try:
            messages = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        # 只接受消息列表，其他结构一律视为损坏
        return messages if isinstance(messages, list) else None

    def save(self, session_id, messages):
        """原子写入会话快照，成功返回 True。

        保留原有接口：只写入，不做容量清理。
        单条 UPSERT 语句本身就是一个事务；写入失败时返回 False，
        调用方原有的快照不会被删除或破坏。
        """
        if not isinstance(messages, list):
            return False
        try:
            payload = json.dumps(messages, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
        now = _now()
        try:
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(UPSERT_SQL, (session_id, payload, now, now))
        except (sqlite3.Error, OSError):
            return False
        return True

    def delete(self, session_id):
        """删除会话快照，成功返回 True。

        数据库文件不存在时视为已删除；任何失败都返回 False，
        调用方据此决定是否保留内存中的会话，避免快照复活。
        """
        if not self.db_path.exists():
            return True
        try:
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(DELETE_SQL, (session_id,))
        except (sqlite3.Error, OSError):
            return False
        return True

    def save_and_prune(self, session_id, messages, max_rows, protected_ids=()):
        """在同一个事务里写入当前会话，并清理过期与超额会话。

        顺序：
        1. UPSERT 当前会话，同时刷新它的 updated_at（微秒级）；
        2. 删除超过 SESSION_TTL_DAYS 天未更新的会话；
        3. 超过 max_rows 时按 updated_at 最旧优先删除，同一时间用 rowid 排序。

        当前会话和 protected_ids 永远不会被删除；如果剩余的都是受保护会话，
        允许数据库暂时超过上限。任何一步失败都会整体回滚。
        """
        if not isinstance(messages, list):
            return False
        if not isinstance(max_rows, int) or max_rows < 0:
            return False
        try:
            payload = json.dumps(messages, ensure_ascii=False)
        except (TypeError, ValueError):
            return False

        protected = _normalize_protected(protected_ids)
        protected.add(session_id)
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="microseconds")
        cutoff = now_dt - timedelta(days=SESSION_TTL_DAYS)
        try:
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(UPSERT_SQL, (session_id, payload, now, now))
                    self._prune_expired(conn, protected, cutoff)
                    self._prune_capacity(conn, max_rows, protected)
        except (sqlite3.Error, OSError):
            return False
        return True

    @staticmethod
    def _prune_expired(conn, protected, cutoff):
        """删除超过 TTL 的会话；受保护会话和当前会话不会被删。"""
        rows = conn.execute("SELECT session_id, updated_at FROM sessions").fetchall()
        for session_id, updated_at in rows:
            if session_id in protected:
                continue
            if _is_expired(updated_at, cutoff):
                conn.execute(DELETE_SQL, (session_id,))

    @staticmethod
    def _prune_capacity(conn, max_rows, protected):
        """删除超额且不受保护的旧快照。"""
        total = conn.execute(COUNT_SQL).fetchone()[0]
        excess = total - max_rows
        if excess <= 0:
            return
        rows = conn.execute(ORDER_SQL).fetchall()
        victims = [row[0] for row in rows if row[0] not in protected][:excess]
        for victim in victims:
            conn.execute(DELETE_SQL, (victim,))