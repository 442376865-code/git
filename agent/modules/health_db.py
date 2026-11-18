"""健康档案与体重记录 SQLite 持久化。

表结构：
- health_profile：姓名、性别、出生日期、身高（固定单行记录）
- weight_records：体重记录明细（自动编号、体重、记录日期、备注、创建时间）
- schema_migrations：迁移元数据，保证每个迁移只执行一次

兼容旧版 health.db：
- 初始化时幂等补齐 name、birth_date 两列；
- 旧版存在 health_profile.weight_kg 时，迁移为一条体重记录；
- 迁移是否已执行由 schema_migrations 的固定键 legacy_weight_v1 判断，
  因此即使用户删除了迁移记录，重启后也不会重新插入；
- health_metrics 为历史旧表，本次补充读写方法，既有数据不丢失。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# 默认数据库文件放在项目根目录的 data/ 下（运行期生成，已加入 .gitignore）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "health.db"

# 允许更新的档案字段，避免写入未知列
PROFILE_FIELDS = ("name", "gender", "birth_date", "height_cm")

# 旧版体重迁移的固定键与备注标记
LEGACY_MIGRATION_KEY = "legacy_weight_v1"
LEGACY_NOTE = "由旧版本迁移"


class HealthDB:
    """健康档案与体重记录的存取。"""

    def __init__(self, db_path=None, check_same_thread=True):
        """创建数据库连接。

        参数：
            db_path: 数据库文件路径；传 ":memory:" 使用内存库（测试推荐）。
                     不传时使用项目根目录下的 data/health.db。
            check_same_thread: 是否只允许创建连接的线程使用。Web 服务由
                     多个请求线程共用同一连接时需传 False，并由调用方加锁串行访问。
        """
        self.is_memory = db_path == ":memory:"
        path = db_path if db_path is not None else DEFAULT_DB_PATH
        if not self.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._conn.row_factory = sqlite3.Row

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()

    def initialize(self):
        """建表并执行幂等迁移（可重复调用）。"""
        self._create_tables()
        self._migrate_profile_columns()
        self._migrate_legacy_weight()
        self._conn.commit()

    def _create_tables(self):
        """创建新表；已存在的旧表保持原样。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                key TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_profile (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name TEXT,
                gender TEXT,
                birth_date TEXT,
                height_cm REAL,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS weight_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                weight_kg REAL NOT NULL,
                record_date TEXT NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS health_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metric TEXT NOT NULL,
                value REAL NOT NULL,
                recorded_at TEXT NOT NULL
            );
            """
        )

    def _migrate_profile_columns(self):
        """旧库缺少 name / birth_date 时补列，已有数据保持不动。"""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(health_profile)")
        }
        if "name" not in columns:
            self._conn.execute("ALTER TABLE health_profile ADD COLUMN name TEXT")
        if "birth_date" not in columns:
            self._conn.execute("ALTER TABLE health_profile ADD COLUMN birth_date TEXT")

    def _migration_applied(self, key):
        """判断某个迁移是否已经执行过。"""
        row = self._conn.execute(
            "SELECT 1 FROM schema_migrations WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def _record_migration(self, key):
        """写入迁移标记（需在调用方的事务中执行）。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (key, applied_at) VALUES (?, ?)",
            (key, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    def _migrate_legacy_weight(self):
        """把旧版 profile.weight_kg 迁移为一条体重记录，只执行一次。

        迁移记录插入与迁移标记写入在同一个事务内完成：
        任何一步失败都会整体回滚，不会留下半完成状态。
        """
        # 已有迁移标记：直接跳过，即使迁移记录被删除也不会重新生成
        if self._migration_applied(LEGACY_MIGRATION_KEY):
            return

        # 先提交之前的 DDL，让下面的迁移在独立事务中完成
        self._conn.commit()

        try:
            row = self._conn.execute(
                "SELECT weight_kg, updated_at FROM health_profile "
                "WHERE id = 1 AND weight_kg IS NOT NULL"
            ).fetchone()
        except sqlite3.OperationalError:
            # 新库没有 weight_kg 旧列，直接记录迁移标记，避免每次启动重试
            with self._conn:
                self._record_migration(LEGACY_MIGRATION_KEY)
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._conn:
            if row:
                # 兼容「已迁移但尚无标记」的旧库：已有迁移记录时只补标记
                already = self._conn.execute(
                    "SELECT 1 FROM weight_records WHERE note = ? LIMIT 1",
                    (LEGACY_NOTE,),
                ).fetchone()
                if not already:
                    weight = row["weight_kg"]
                    created = row["updated_at"] or now
                    record_date = created[:10] if len(created) >= 10 else created
                    self._conn.execute(
                        "INSERT INTO weight_records "
                        "(weight_kg, record_date, note, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (weight, record_date, LEGACY_NOTE, created),
                    )
            self._record_migration(LEGACY_MIGRATION_KEY)

    def get_profile(self):
        """读取档案；未建立时返回空字典。"""
        row = self._conn.execute(
            "SELECT name, gender, birth_date, height_cm, updated_at "
            "FROM health_profile WHERE id = 1"
        ).fetchone()
        return dict(row) if row else {}

    def update_profile(self, **fields):
        """部分更新档案，未传入的字段保持原值。"""
        updates = {
            key: value
            for key, value in fields.items()
            if key in PROFILE_FIELDS and value is not None
        }
        if not updates:
            return self.get_profile()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = self.get_profile()
        if not existing:
            columns = ["id"] + list(updates) + ["updated_at"]
            values = [1] + list(updates.values()) + [now]
            placeholders = ", ".join("?" for _ in columns)
            self._conn.execute(
                f"INSERT INTO health_profile ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        else:
            assignments = ", ".join(f"{key} = ?" for key in updates)
            self._conn.execute(
                f"UPDATE health_profile SET {assignments}, updated_at = ? WHERE id = 1",
                list(updates.values()) + [now],
            )
        self._conn.commit()
        return self.get_profile()

    def clear_all(self):
        """清空档案与体重记录（旧表 health_metrics 保留不动）。"""
        self._conn.execute("DELETE FROM health_profile")
        self._conn.execute("DELETE FROM weight_records")
        self._conn.commit()

    def add_weight(self, weight_kg, record_date=None, note=None):
        """新增一条体重记录，日期缺省为当天，返回新记录编号。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        record_date = record_date or now[:10]
        cursor = self._conn.execute(
            "INSERT INTO weight_records (weight_kg, record_date, note, created_at) "
            "VALUES (?, ?, ?, ?)",
            (weight_kg, record_date, note, now),
        )
        self._conn.commit()
        return cursor.lastrowid

    def list_weights(self, limit=10):
        """按记录日期倒序返回最近若干条体重记录。"""
        rows = self._conn.execute(
            "SELECT id, weight_kg, record_date, note, created_at FROM weight_records "
            "ORDER BY record_date DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_weight(self, record_id):
        """按编号删除体重记录，返回是否删除成功。"""
        cursor = self._conn.execute(
            "DELETE FROM weight_records WHERE id = ?", (record_id,)
        )
        self._conn.commit()
        return cursor.rowcount > 0
    def add_metric(self, metric, value, recorded_at=None):
        """新增一条健康指标记录，返回新记录编号。

        参数：
            metric: 指标类型，由调用方校验后传入；
            value: 指标数值；
            recorded_at: 记录时间，缺省为当前时间。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        recorded_at = recorded_at or now
        cursor = self._conn.execute(
            "INSERT INTO health_metrics (metric, value, recorded_at) "
            "VALUES (?, ?, ?)",
            (metric, value, recorded_at),
        )
        self._conn.commit()
        return cursor.lastrowid

    def list_metrics(self, metric=None, limit=20):
        """按记录时间倒序返回健康指标，可按类型过滤。"""
        if metric:
            rows = self._conn.execute(
                "SELECT id, metric, value, recorded_at FROM health_metrics "
                "WHERE metric = ? ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (metric, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, metric, value, recorded_at FROM health_metrics "
                "ORDER BY recorded_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]