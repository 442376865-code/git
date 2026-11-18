"""SQLite + FTS5 医学知识卡存储与检索。

设计说明：
1. knowledge_cards 主表保存完整知识卡（JSON 文本），字段可随卡片扩展；
2. knowledge_card_aliases 别名表用于「疾病名/别名」子串精确命中；
3. knowledge_fts 虚拟表保存中文 2-gram 分词后的检索文本，支持关键词模糊检索；
4. 全部使用标准库 sqlite3，不依赖第三方中文分词工具。

检索策略（由医学知识模块调用）：
- 先走别名精确匹配（优先级最高，结果稳定）；
- 精确未命中时再走 FTS5 模糊召回，只返回得分最高的卡片。
"""

import json
import re
import sqlite3
from pathlib import Path

from .knowledge_cards import KNOWLEDGE_CARDS

# 默认数据库文件放在项目根目录的 data/ 下（运行期生成，已加入 .gitignore）
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "knowledge.db"

# 参与全文检索的文本字段
SEARCH_FIELDS = (
    "overview",
    "common_signs",
    "risk_factors",
    "daily_management",
    "urgent_signs",
    "emergency_signs",
)


class KnowledgeDB:
    """知识卡数据库：建表、导入种子数据、精确与模糊检索。"""

    def __init__(self, db_path=None):
        """创建数据库连接。

        参数：
            db_path: 数据库文件路径；传 ":memory:" 使用内存库（测试推荐）。
                     不传时使用项目根目录下的 data/knowledge.db。
        """
        self.is_memory = db_path == ":memory:"
        path = db_path if db_path is not None else DEFAULT_DB_PATH
        # 文件库需要先确保父目录存在
        if not self.is_memory:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def close(self):
        """关闭数据库连接。"""
        self._conn.close()

    def initialize(self):
        """建表并把种子知识卡同步进库（可重复调用，幂等）。"""
        self._create_tables()
        self._sync_cards(KNOWLEDGE_CARDS)

    def _create_tables(self):
        """创建主表、别名表和 FTS5 虚拟表。"""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                disease_name TEXT NOT NULL UNIQUE,
                card_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_card_aliases (
                card_id INTEGER NOT NULL,
                alias TEXT NOT NULL,
                PRIMARY KEY (card_id, alias),
                FOREIGN KEY (card_id) REFERENCES knowledge_cards(id) ON DELETE CASCADE
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                disease_name,
                search_text,
                tokenize='unicode61'
            );
            """
        )
        self._conn.commit()

    def _sync_cards(self, cards):
        """逐张同步卡片：存在则更新，不存在则插入，并重建别名与索引。"""
        for card in cards:
            name = card["disease_name"]
            payload = json.dumps(card, ensure_ascii=False, sort_keys=True)
            updated_at = str(card.get("updated_at", ""))

            row = self._conn.execute(
                "SELECT id FROM knowledge_cards WHERE disease_name = ?", (name,)
            ).fetchone()
            if row is None:
                cursor = self._conn.execute(
                    "INSERT INTO knowledge_cards (disease_name, card_json, updated_at) "
                    "VALUES (?, ?, ?)",
                    (name, payload, updated_at),
                )
                card_id = cursor.lastrowid
            else:
                card_id = row["id"]
                self._conn.execute(
                    "UPDATE knowledge_cards SET card_json = ?, updated_at = ? WHERE id = ?",
                    (payload, updated_at, card_id),
                )
                # 更新前先清掉旧别名与旧索引行，避免残留
                self._conn.execute(
                    "DELETE FROM knowledge_card_aliases WHERE card_id = ?", (card_id,)
                )
                self._conn.execute("DELETE FROM knowledge_fts WHERE rowid = ?", (card_id,))

            aliases = [name] + [alias for alias in card.get("aliases", []) if alias]
            for alias in dict.fromkeys(aliases):
                self._conn.execute(
                    "INSERT OR IGNORE INTO knowledge_card_aliases (card_id, alias) "
                    "VALUES (?, ?)",
                    (card_id, alias),
                )

            self._conn.execute(
                "INSERT INTO knowledge_fts (rowid, disease_name, search_text) "
                "VALUES (?, ?, ?)",
                (card_id, " ".join(self._tokenize(name)), self._build_search_text(card)),
            )
        self._conn.commit()

    @classmethod
    def _build_search_text(cls, card):
        """把卡片正文切成 2-gram 检索文本，供 FTS5 建立索引。"""
        parts = [card["disease_name"]] + list(card.get("aliases", []))
        for field in SEARCH_FIELDS:
            value = card.get(field, [])
            if isinstance(value, str):
                parts.append(value)
            else:
                parts.extend(value)
        return " ".join(cls._tokenize(" ".join(str(part) for part in parts)))

    @staticmethod
    def _tokenize(text):
        """中文按 2-gram 切分，英文数字按单词切分，并去重保序。"""
        tokens = []
        for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+", str(text)):
            if chunk[0].isascii():
                tokens.append(chunk.lower())
            elif len(chunk) == 1:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        return list(dict.fromkeys(tokens))

    def match_exact(self, text):
        """按疾病名与别名做子串精确匹配，返回命中的卡片列表。"""
        if not isinstance(text, str) or not text.strip():
            return []
        rows = self._conn.execute(
            "SELECT c.id AS id, c.card_json AS card_json, a.alias AS alias "
            "FROM knowledge_card_aliases a "
            "JOIN knowledge_cards c ON c.id = a.card_id "
            "ORDER BY c.id"
        ).fetchall()
        matched = {}
        for row in rows:
            if row["alias"] in text:
                matched[row["id"]] = json.loads(row["card_json"])
        return list(matched.values())

    def match_fuzzy(self, text):
        """通过 FTS5 做关键词模糊召回，只返回得分最高的卡片。"""
        if not isinstance(text, str) or not text.strip():
            return []
        # 单字区分度太低，只用长度 >= 2 的检索词
        tokens = [token for token in self._tokenize(text) if len(token) >= 2]
        tokens = list(dict.fromkeys(tokens))
        if not tokens:
            return []

        expr = " OR ".join('"%s"' % token for token in tokens)
        try:
            rows = self._conn.execute(
                "SELECT c.id AS id, c.card_json AS card_json, f.search_text AS search_text "
                "FROM knowledge_fts f "
                "JOIN knowledge_cards c ON c.id = f.rowid "
                "WHERE knowledge_fts MATCH ?",
                (expr,),
            ).fetchall()
        except sqlite3.OperationalError:
            # 查询表达式异常时安全回退为空结果
            return []

        scored = []
        for row in rows:
            stored = set(row["search_text"].split())
            hit = sum(1 for token in tokens if token in stored)
            if hit:
                scored.append((hit, row["id"], row["card_json"]))
        if not scored:
            return []

        best_hit = max(item[0] for item in scored)
        return [json.loads(item[2]) for item in scored if item[0] == best_hit]

    def list_cards(self):
        """返回库中全部卡片，便于后续扩容与调试。"""
        rows = self._conn.execute(
            "SELECT card_json FROM knowledge_cards ORDER BY id"
        ).fetchall()
        return [json.loads(row["card_json"]) for row in rows]