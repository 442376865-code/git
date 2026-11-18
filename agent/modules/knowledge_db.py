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

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .knowledge_cards import KNOWLEDGE_CARDS
from .knowledge_chunks import KNOWLEDGE_CHUNKS

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

# 停用 2-gram：只由提问、判断等通用成分组成，几乎没有主题区分度。
# 例如「病是」「什么」「怎么」「么办」会在各种问题里反复出现，
# 如果参与打分就会把无关知识卡强行召回。
STOP_TOKENS = frozenset(
    {
        "病是", "是什", "什么",
        "怎么", "么办", "如何",
        "可以", "能否", "是否", "严重",
        "治疗", "么治", "感染",
    }
)

# 强证据字段：疾病名、别名与概述。它们直接描述"这张卡讲的是什么主题"，
# 只有命中这里的检索词才算可靠线索；症状、风险因素等字段属于弱证据。
STRONG_FIELDS = ("disease_name", "aliases", "overview")

# 明确指向儿童人群的词语。只有出现这些词，才允许返回 scope 含"儿童"的片段，
# 避免「肺炎严重吗」这类未说明人群的问题被儿童限定资料冒充全人群知识。
CHILD_HINT_WORDS = ("儿童", "小儿", "小孩", "孩子", "宝宝", "婴幼儿", "新生儿", "儿科")

# 明确指向非儿童人群的词语。出现任意一个就强制排除儿童限定片段，
# 例如「成人肺炎」「老年人肺炎」「医院获得性肺炎」都不能命中儿童肺炎片段。
NON_CHILD_HINT_WORDS = (
    "成人",
    "大人",
    "成年人",
    "老年",
    "老人",
    "老年人",
    "社区获得性",
    "医院获得性",
)

# 同一个来源最多返回的片段数，防止单一来源占满结果
CHUNKS_PER_SOURCE_LIMIT = 2

# 知识片段检索的主题别名映射：俗名 → 已收录的规范疾病名。
# 仅登记有明确来源依据的别名：WHO《卒中（实况报道）》正文明确写
# "卒中（俗称中风）"，因此"中风"→"脑卒中"是安全映射。
# 映射只作用于检索与强证据比对，不写入数据库、不进入 FTS 索引，
# 未登记的相近说法（如"脑梗"）一律不猜测扩展。
TOPIC_ALIASES = {
    "中风": "脑卒中",
}


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
        """建表并把种子知识卡与知识片段同步进库（可重复调用，幂等）。"""
        self._create_tables()
        self._sync_cards(KNOWLEDGE_CARDS)
        self._sync_chunks(KNOWLEDGE_CHUNKS)

    def _create_tables(self):
        """创建卡片表、片段表以及两张 FTS5 虚拟表。"""
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

            CREATE TABLE IF NOT EXISTS knowledge_chunks (
                chunk_id INTEGER PRIMARY KEY,
                disease_name TEXT NOT NULL,
                scope TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                content TEXT NOT NULL,
                source_title TEXT NOT NULL,
                source_publisher TEXT NOT NULL DEFAULT '',
                source_edition TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source_section TEXT NOT NULL DEFAULT '',
                source_page TEXT NOT NULL DEFAULT '',
                source_level INTEGER NOT NULL CHECK (source_level BETWEEN 1 AND 4),
                accessed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (
                    disease_name,
                    scope,
                    source_title,
                    source_section,
                    chunk_index
                )
            );

            CREATE INDEX IF NOT EXISTS idx_chunks_topic
                ON knowledge_chunks (disease_name, scope);

            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                disease_name,
                source_section,
                content,
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

    @classmethod
    def _strong_tokens(cls, card):
        """提取一张知识卡的强证据检索词（疾病名、别名、概述）。"""
        parts = []
        for field in STRONG_FIELDS:
            value = card.get(field, "")
            if isinstance(value, (list, tuple)):
                parts.extend(str(item) for item in value)
            else:
                parts.append(str(value))
        return set(cls._tokenize(" ".join(parts)))

    def match_fuzzy(self, text):
        """通过 FTS5 做关键词模糊召回，只返回最高分的卡片。

        打分规则：
        - 先过滤停用 2-gram，只保留有区分度的检索词；
        - FTS5 负责粗召回候选卡；
        - 候选卡必须至少命中一个"强证据"检索词（疾病名/别名/概述），
          否则说明只是命中了症状、风险因素等弱线索，直接丢弃；
        - 强证据命中数、总命中数都相同时才会并列返回多张卡。
        """
        if not isinstance(text, str) or not text.strip():
            return []

        # 单字区分度太低，同时丢掉停用 2-gram
        tokens = [
            token
            for token in self._tokenize(text)
            if len(token) >= 2 and token not in STOP_TOKENS
        ]
        tokens = list(dict.fromkeys(tokens))
        if not tokens:
            # 过滤后没有有效检索词，直接返回空结果，不做任何猜测
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
            card = json.loads(row["card_json"])
            strong = self._strong_tokens(card)
            strong_hit = sum(1 for token in tokens if token in strong)
            if strong_hit == 0:
                # 只命中弱证据，不足以判断主题，宁可返回空结果
                continue
            stored = set(row["search_text"].split())
            total_hit = sum(1 for token in tokens if token in stored)
            scored.append((strong_hit, total_hit, row["card_json"]))
        if not scored:
            return []

        best = max((item[0], item[1]) for item in scored)
        return [
            json.loads(item[2])
            for item in scored
            if (item[0], item[1]) == best
        ]

    def list_cards(self):
        """返回库中全部卡片，便于后续扩容与调试。"""
        rows = self._conn.execute(
            "SELECT card_json FROM knowledge_cards ORDER BY id"
        ).fetchall()
        return [json.loads(row["card_json"]) for row in rows]
    @staticmethod
    def _stable_chunk_id(chunk):
        """按业务键生成稳定的 chunk_id。

        同一主题、同一来源、同一章节、同一位次始终得到同一个编号，
        因此重复导入只会更新原有行，不会产生重复片段。
        """
        key = "|".join(
            [
                str(chunk["disease_name"]),
                str(chunk["scope"]),
                str(chunk["source_title"]),
                str(chunk.get("source_section", "")),
                str(int(chunk.get("chunk_index", 0))),
            ]
        )
        return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:12], 16)

    def _sync_chunks(self, chunks):
        """逐条同步知识片段，并重建对应的 FTS5 索引行（可重复调用，幂等）。"""
        for chunk in chunks:
            chunk_id = self._stable_chunk_id(chunk)
            values = (
                chunk_id,
                chunk["disease_name"],
                chunk["scope"],
                int(chunk.get("chunk_index", 0)),
                chunk["content"],
                chunk["source_title"],
                chunk.get("source_publisher", ""),
                chunk.get("source_edition", ""),
                chunk.get("source_url", ""),
                chunk.get("source_section", ""),
                chunk.get("source_page", ""),
                int(chunk["source_level"]),
                chunk["accessed_at"],
                chunk["updated_at"],
            )
            self._conn.execute(
                "INSERT INTO knowledge_chunks ("
                "chunk_id, disease_name, scope, chunk_index, content, "
                "source_title, source_publisher, source_edition, source_url, "
                "source_section, source_page, source_level, accessed_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chunk_id) DO UPDATE SET "
                "disease_name = excluded.disease_name, "
                "scope = excluded.scope, "
                "chunk_index = excluded.chunk_index, "
                "content = excluded.content, "
                "source_title = excluded.source_title, "
                "source_publisher = excluded.source_publisher, "
                "source_edition = excluded.source_edition, "
                "source_url = excluded.source_url, "
                "source_section = excluded.source_section, "
                "source_page = excluded.source_page, "
                "source_level = excluded.source_level, "
                "accessed_at = excluded.accessed_at, "
                "updated_at = excluded.updated_at",
                values,
            )
            # 先删旧索引行再写新行，避免正文修订后残留旧的 2-gram 索引
            self._conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (chunk_id,))
            self._conn.execute(
                "INSERT INTO chunks_fts (rowid, disease_name, source_section, content) "
                "VALUES (?, ?, ?, ?)",
                (
                    chunk_id,
                    " ".join(self._tokenize(chunk["disease_name"])),
                    " ".join(self._tokenize(chunk.get("source_section", ""))),
                    " ".join(self._tokenize(chunk["content"])),
                ),
            )
        self._conn.commit()

    def import_chunks(self, chunks=None):
        """导入知识片段（离线、可重复执行、幂等）。

        参数：
            chunks: 待导入的片段列表；不传时使用内置的 KNOWLEDGE_CHUNKS。
        """
        self._create_tables()
        self._sync_chunks(KNOWLEDGE_CHUNKS if chunks is None else chunks)

    def list_chunks(self):
        """返回库中全部知识片段，便于调试与测试。"""
        rows = self._conn.execute(
            "SELECT * FROM knowledge_chunks "
            "ORDER BY disease_name, chunk_index, chunk_id"
        ).fetchall()
        return [dict(row) for row in rows]
    @staticmethod
    def _mentions_child(text):
        """判断问题是否明确提到儿童人群。"""
        return any(word in text for word in CHILD_HINT_WORDS)

    @staticmethod
    def _mentions_non_child(text):
        """判断问题是否明确指向非儿童人群。"""
        return any(word in text for word in NON_CHILD_HINT_WORDS)

    def match_chunks(self, text):
        """在知识片段中检索，返回按匹配度排序的片段列表。

        规则：
        - 与卡片级检索一致，必须命中主题强证据（疾病名的 2-gram）才保留；
        - 只有明确提到儿童时才返回 scope 含"儿童"的片段；
        - 提到成人、老人、社区或医院获得性等词语时强制排除儿童片段。
        """
        if not isinstance(text, str) or not text.strip():
            return []

        # 别名归一化：把已登记的俗名替换成规范疾病名后再分词，
        # 让「中风」这类有来源依据的俗称也能命中「脑卒中」片段。
        # 只作用于检索词生成，人群判断仍使用原始输入。
        normalized = text
        for alias, disease in TOPIC_ALIASES.items():
            if alias in normalized:
                normalized = normalized.replace(alias, disease)

        tokens = [
            token
            for token in self._tokenize(normalized)
            if len(token) >= 2 and token not in STOP_TOKENS
        ]
        tokens = list(dict.fromkeys(tokens))
        if not tokens:
            return []

        expr = " OR ".join('"%s"' % token for token in tokens)
        sql = (
            "SELECT c.* FROM chunks_fts f "
            "JOIN knowledge_chunks c ON c.chunk_id = f.rowid "
            "WHERE chunks_fts MATCH ?"
        )
        if self._mentions_child(text) and not self._mentions_non_child(text):
            # 明确问儿童：只保留儿童限定片段
            sql += " AND c.scope LIKE '%儿童%'"
        else:
            # 未说明人群或明确指向成人：一律排除儿童限定片段
            sql += " AND c.scope NOT LIKE '%儿童%'"

        try:
            rows = self._conn.execute(sql, [expr]).fetchall()
        except sqlite3.OperationalError:
            # 查询表达式异常时安全回退为空结果
            return []

        scored = []
        for row in rows:
            strong = set(self._tokenize(row["disease_name"]))
            strong_hit = sum(1 for token in tokens if token in strong)
            if strong_hit == 0:
                # 只命中正文关键词、没有命中主题强证据，不足以判断主题
                continue
            stored = set(
                self._tokenize(
                    " ".join(
                        [
                            row["disease_name"],
                            row["source_section"] or "",
                            row["content"],
                        ]
                    )
                )
            )
            total_hit = sum(1 for token in tokens if token in stored)
            scored.append((strong_hit, total_hit, row))

        if not scored:
            return []

        scored.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                item[2]["source_level"],
                item[2]["disease_name"],
                item[2]["chunk_index"],
            )
        )

        # 同一来源最多保留若干条，避免单一来源占满结果
        limited = []
        counter = {}
        for _strong, _total, row in scored:
            key = (row["disease_name"], row["source_title"])
            if counter.get(key, 0) >= CHUNKS_PER_SOURCE_LIMIT:
                continue
            counter[key] = counter.get(key, 0) + 1
            limited.append(dict(row))
        return limited