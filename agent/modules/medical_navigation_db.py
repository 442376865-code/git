"""常用就医地点的 SQLite 持久化。

表结构：
- navigation_places：用户主动录入的就医地点
  （名称、类型、地址、电话、备注、创建与更新时间）

说明：
- 只保存用户自己录入的信息，不联网、不定位，不声称是"附近"的真实地点；
- 类型限定为：医院、诊所、药房、卫生院、其他；
- 全部 SQL 使用参数化，注入内容只会作为普通文本保存。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# 默认数据库文件放在项目根目录的 data/ 下（运行期生成，已加入 .gitignore）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "navigation.db"

# 支持的就医地点类型
CATEGORIES = ("医院", "诊所", "药房", "卫生院", "其他")


class MedicalNavigationDB:
    """常用就医地点的存取。"""

    def __init__(self, db_path=None):
        """创建数据库连接。

        参数：
            db_path: 数据库文件路径；传 ":memory:" 使用内存库（测试推荐）。
                     不传时使用项目根目录下的 data/navigation.db。
        """
        self.is_memory = db_path == ":memory:"
        path = db_path if db_path is not None else DEFAULT_DB_PATH
        if not self.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()

    def initialize(self):
        """建表（可重复调用，幂等）。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS navigation_places (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '其他'
                    CHECK (category IN ('医院', '诊所', '药房', '卫生院', '其他')),
                address TEXT,
                phone TEXT,
                note TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def create_place(self, name, category="其他", address=None, phone=None, note=None):
        """新增就医地点，返回新地点编号。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = self._conn.execute(
            "INSERT INTO navigation_places "
            "(name, category, address, phone, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, category, address, phone, note, now, now),
        )
        self._conn.commit()
        return cursor.lastrowid

    def get_place(self, place_id):
        """按编号读取地点；不存在时返回 None。"""
        row = self._conn.execute(
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places WHERE id = ?",
            (place_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_places(self):
        """按编号顺序返回全部就医地点。"""
        rows = self._conn.execute(
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def search_places(self, keyword):
        """按名称或类型模糊搜索；关键词为空时返回空列表。"""
        if not keyword:
            return []
        pattern = f"%{keyword}%"
        rows = self._conn.execute(
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places "
            "WHERE name LIKE ? OR category LIKE ? "
            "ORDER BY id",
            (pattern, pattern),
        ).fetchall()
        return [dict(row) for row in rows]

    def delete_place(self, place_id):
        """按编号删除地点，返回是否删除成功。"""
        cursor = self._conn.execute(
            "DELETE FROM navigation_places WHERE id = ?",
            (place_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0