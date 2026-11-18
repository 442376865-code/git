"""常用就医地点的 SQLite 持久化。

表结构：
- navigation_places：用户主动录入的就医地点
  （名称、类型、地址、电话、备注、创建与更新时间）

说明：
- 只保存用户自己录入的信息，不联网、不定位，不声称是"附近"的真实地点；
- 类型限定为：医院、诊所、药房、卫生院、其他；
- 全部 SQL 使用参数化，注入内容只会作为普通文本保存；
- 不保存任何地图服务商返回的数据（没有经纬度、来源、外部编号等字段）。

线程安全：
- 默认 check_same_thread=True，与原有调用方式完全一致；
- Web 服务由多个请求线程共用同一连接时传 check_same_thread=False，
  并由调用方加锁串行访问（与 health_db / medication_db 的约定相同）。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# 默认数据库文件放在项目根目录的 data/ 下（运行期生成，已加入 .gitignore）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "navigation.db"

# 支持的就医地点类型
CATEGORIES = ("医院", "诊所", "药房", "卫生院", "其他")

# 允许编辑的列名白名单：拼进 SQL 的列名只能来自这里，用户输入一律走占位符
_UPDATABLE_COLUMNS = ("name", "category", "address", "phone", "note")

# 时间格式与其他业务数据库保持一致（精确到秒）
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_limit(limit):
    """把 limit 规范化为非负整数。

    参数：
        limit: None 表示不限制；非负整数表示最多返回多少条。

    异常：
        ValueError: limit 不是非负整数（布尔值同样拒绝）。
    """
    if limit is None:
        return None
    # bool 是 int 的子类，必须单独排除，避免 True/False 被当成 1/0
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是非负整数")
    if limit < 0:
        raise ValueError("limit 必须是非负整数")
    return limit


class MedicalNavigationDB:
    """常用就医地点的存取。"""

    def __init__(self, db_path=None, check_same_thread=True):
        """创建数据库连接。

        参数：
            db_path: 数据库文件路径；传 ":memory:" 使用内存库（测试推荐）。
                     不传时使用项目根目录下的 data/navigation.db。
            check_same_thread: 是否只允许创建连接的线程使用。Web 服务由
                     多个请求线程共用同一连接时需传 False，并由调用方加锁串行访问。
        """
        self.is_memory = db_path == ":memory:"
        path = db_path if db_path is not None else DEFAULT_DB_PATH
        if not self.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._conn.row_factory = sqlite3.Row
        # 标记连接是否已关闭，保证 close() 可以安全地重复调用
        self._closed = False

    def close(self):
        """关闭数据库连接；重复调用不会抛异常。"""
        if self._closed:
            return
        self._conn.close()
        self._closed = True

    def initialize(self):
        """建表（可重复调用，幂等）。"""
        # with 语句在成功时自动提交、异常时自动回滚
        with self._conn:
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

    def create_place(self, name, category="其他", address=None, phone=None, note=None):
        """新增就医地点，返回新地点编号。"""
        now = datetime.now().strftime(_TIME_FORMAT)
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO navigation_places "
                "(name, category, address, phone, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, category, address, phone, note, now, now),
            )
        return cursor.lastrowid

    def get_place(self, place_id):
        """按编号读取地点；不存在时返回 None。"""
        row = self._conn.execute(
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places WHERE id = ?",
            (place_id,),
        ).fetchone()
        return dict(row) if row else None
    def list_places(self, category=None, limit=None):
        """按编号顺序返回地点，可按类型过滤并限制返回条数。

        参数：
            category: 可选类型，必须是 CATEGORIES 之一；None 表示不过滤。
            limit: 可选条数上限；None 表示不限制，0 表示返回空列表。

        异常：
            ValueError: category 不在支持范围内，或 limit 不是非负整数。
        """
        if category is not None and category not in CATEGORIES:
            raise ValueError(f"类型仅支持：{'、'.join(CATEGORIES)}。")
        limit_value = _parse_limit(limit)

        sql = (
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places"
        )
        params = []
        if category is not None:
            sql += " WHERE category = ?"
            params.append(category)
        sql += " ORDER BY id"
        if limit_value is not None:
            sql += " LIMIT ?"
            params.append(limit_value)

        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def search_places(self, keyword):
        """按名称、类型或地址模糊搜索；关键词为空时返回空列表。"""
        if not keyword:
            return []
        pattern = f"%{keyword}%"
        rows = self._conn.execute(
            "SELECT id, name, category, address, phone, note, created_at, updated_at "
            "FROM navigation_places "
            "WHERE name LIKE ? OR category LIKE ? OR COALESCE(address, '') LIKE ? "
            "ORDER BY id",
            (pattern, pattern, pattern),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_place(
        self,
        place_id,
        name=None,
        category=None,
        address=None,
        phone=None,
        note=None,
    ):
        """按编号编辑地点，返回是否有记录被更新。

        参数：
            place_id: 要编辑的地点编号。
            name: None 表示不修改；非空字符串表示新名称，空字符串视为非法。
            category: None 表示不修改；否则必须是 CATEGORIES 之一。
            address / phone / note: None 表示不修改；
                传空字符串表示清空该字段（存为 NULL）。

        返回：
            True: 记录存在且已写入；
            False: 记录不存在，或没有任何字段需要更新（不写库）。

        异常：
            ValueError: 名称为空，或类型不在支持范围内。抛出前不会写库，
                因此编辑失败不会破坏原有数据。
        """
        updates = {}

        if name is not None:
            cleaned_name = str(name).strip()
            if not cleaned_name:
                raise ValueError("地点名称不能为空")
            updates["name"] = cleaned_name

        if category is not None:
            if category not in CATEGORIES:
                raise ValueError(f"类型仅支持：{'、'.join(CATEGORIES)}。")
            updates["category"] = category

        for column, value in (("address", address), ("phone", phone), ("note", note)):
            if value is None:
                continue
            # 空字符串表示清空，与"不修改"区分开
            cleaned = str(value).strip()
            updates[column] = cleaned or None

        if not updates:
            return False

        updates["updated_at"] = datetime.now().strftime(_TIME_FORMAT)

        # 列名只从白名单取，用户输入全部通过 ? 占位符传入
        columns = [
            column
            for column in updates
            if column in _UPDATABLE_COLUMNS or column == "updated_at"
        ]
        assignments = ", ".join(f"{column} = ?" for column in columns)
        params = [updates[column] for column in columns]
        params.append(place_id)

        with self._conn:
            cursor = self._conn.execute(
                f"UPDATE navigation_places SET {assignments} WHERE id = ?",
                tuple(params),
            )
        return cursor.rowcount > 0

    def delete_place(self, place_id):
        """按编号删除地点，返回是否删除成功。"""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM navigation_places WHERE id = ?",
                (place_id,),
            )
        return cursor.rowcount > 0