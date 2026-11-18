"""用药计划与服药记录 SQLite 持久化。

表结构：
- medication_plans：用药计划（药名、医嘱用法、时间、起止日期、状态、备注）
- medication_logs：服药记录（关联计划、服药时间、备注）

说明：
- 连接上启用 SQLite 外键，服药记录必须关联有效计划；
- 计划停用只改状态，不物理删除，历史计划保留；
- 全部 SQL 使用参数化，避免注入。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# 默认数据库文件放在项目根目录的 data/ 下（运行期生成，已加入 .gitignore）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "medication.db"


class MedicationDB:
    """用药计划与服药记录的存取。"""

    def __init__(self, db_path=None):
        """创建数据库连接并启用外键。

        参数：
            db_path: 数据库文件路径；传 ":memory:" 使用内存库（测试推荐）。
                     不传时使用项目根目录下的 data/medication.db。
        """
        self.is_memory = db_path == ":memory:"
        path = db_path if db_path is not None else DEFAULT_DB_PATH
        if not self.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        # 每个连接都要单独启用外键
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()

    def initialize(self):
        """建表（可重复调用，幂等）。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS medication_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                medication_name TEXT NOT NULL,
                doctor_instruction TEXT NOT NULL,
                schedule_times TEXT,
                start_date TEXT,
                end_date TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'stopped')),
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS medication_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                taken_at TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (plan_id) REFERENCES medication_plans(id)
            );
            """
        )
        self._conn.commit()

    def create_plan(
        self,
        medication_name,
        doctor_instruction,
        schedule_times=None,
        start_date=None,
        end_date=None,
        note=None,
    ):
        """新增用药计划，状态固定为 active，返回新计划编号。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self._conn.execute(
            "INSERT INTO medication_plans "
            "(medication_name, doctor_instruction, schedule_times, start_date, "
            " end_date, status, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (
                medication_name,
                doctor_instruction,
                schedule_times,
                start_date,
                end_date,
                note,
                now,
                now,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_plan(self, plan_id):
        """按编号读取计划；不存在时返回 None。"""
        row = self._conn.execute(
            "SELECT id, medication_name, doctor_instruction, schedule_times, "
            "start_date, end_date, status, note, created_at, updated_at "
            "FROM medication_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_plans(self):
        """按编号顺序返回全部用药计划（包括已停用）。"""
        rows = self._conn.execute(
            "SELECT id, medication_name, doctor_instruction, schedule_times, "
            "start_date, end_date, status, note, created_at, updated_at "
            "FROM medication_plans ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def stop_plan(self, plan_id):
        """把生效中的计划改为已停用，返回是否变更成功。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self._conn.execute(
            "UPDATE medication_plans SET status = 'stopped', updated_at = ? "
            "WHERE id = ? AND status = 'active'",
            (now, plan_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def add_log(self, plan_id, taken_at, note=None):
        """新增服药记录，返回新记录编号；计划不存在或已停用时返回 None。

        校验与写入在同一条 INSERT ... SELECT 语句中完成（原子操作）：
        只有存在 status = 'active' 的计划时才会真正插入，
        避免"先查询再写入"之间计划被停用导致的脏记录。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self._conn.execute(
            "INSERT INTO medication_logs (plan_id, taken_at, note, created_at) "
            "SELECT ?, ?, ?, ? "
            "WHERE EXISTS ("
            "    SELECT 1 FROM medication_plans WHERE id = ? AND status = 'active'"
            ")",
            (plan_id, taken_at, note, now, plan_id),
        )
        self._conn.commit()
        # 没有插入任何行，说明计划不存在或已停用
        if cursor.rowcount == 0:
            return None
        return cursor.lastrowid

    def list_logs(self, limit=10):
        """按服药时间倒序返回最近若干条记录，并带出药名。"""
        rows = self._conn.execute(
            "SELECT l.id, l.plan_id, p.medication_name, l.taken_at, l.note, l.created_at "
            "FROM medication_logs l "
            "JOIN medication_plans p ON p.id = l.plan_id "
            "ORDER BY l.taken_at DESC, l.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
    def delete_plan(self, plan_id):
        """删除指定用药计划及其关联服药记录，返回是否删除成功。

        当前外键没有配置级联删除，所以在同一事务里先删服药记录、再删计划；
        计划不存在时返回 False，数据库异常时回滚并返回 False，均不抛异常。
        """
        try:
            self._conn.execute(
                "DELETE FROM medication_logs WHERE plan_id = ?", (plan_id,)
            )
            cursor = self._conn.execute(
                "DELETE FROM medication_plans WHERE id = ?", (plan_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error:
            self._conn.rollback()
            return False

    def delete_log(self, log_id):
        """删除指定服药记录，返回是否删除成功；不存在或异常时返回 False。"""
        try:
            cursor = self._conn.execute(
                "DELETE FROM medication_logs WHERE id = ?", (log_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0
        except sqlite3.Error:
            self._conn.rollback()
            return False