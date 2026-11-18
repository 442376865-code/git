"""SessionStore 的读写、时间字段与损坏数据回退测试。

全部使用临时目录中的数据库，不读写项目真实的 data/。
"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agent.session_store import SESSION_TTL_DAYS, SessionStore


class SessionStoreTest(unittest.TestCase):
    """SQLite 会话存储的基础行为。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.store = SessionStore(self.db_path)

    def _read_times(self, session_id):
        """读取某个会话的创建时间与更新时间。"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute(
                "SELECT created_at, updated_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

    def test_save_and_load_roundtrip(self):
        """保存中文消息后能原样读回。"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，请讲。"},
        ]
        self.assertTrue(self.store.save("s1", messages))
        self.assertEqual(self.store.load("s1"), messages)

    def test_load_missing_returns_none(self):
        """记录不存在时返回 None。"""
        self.assertIsNone(self.store.load("s1"))

    def test_read_does_not_create_database_file(self):
        """只读不存在的数据库时不应创建文件。"""
        self.assertIsNone(self.store.load("s1"))
        self.assertFalse(self.db_path.exists())

    def test_created_at_preserved_and_updated_at_refreshed(self):
        """重复保存保留创建时间，并刷新更新时间。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        created, _ = self._read_times("s1")

        # 手工把更新时间改成旧值，便于验证下次保存会刷新它
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    ("2000-01-01T00:00:00+00:00", "s1"),
                )

        self.store.save("s1", [{"role": "user", "content": "b"}])
        created_again, updated_again = self._read_times("s1")

        self.assertEqual(created_again, created)
        self.assertNotEqual(updated_again, "2000-01-01T00:00:00+00:00")

    def test_corrupted_json_returns_none(self):
        """消息内容不是合法 JSON 时安全返回 None。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET messages_json = ? WHERE session_id = ?",
                    ("{不是合法 JSON", "s1"),
                )
        self.assertIsNone(self.store.load("s1"))

    def test_non_list_json_returns_none(self):
        """合法 JSON 但不是列表时也按损坏处理。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET messages_json = ? WHERE session_id = ?",
                    (json.dumps({"role": "user"}), "s1"),
                )
        self.assertIsNone(self.store.load("s1"))

    def test_sql_is_parameterized(self):
        """带引号和分号的会话编号只当普通文本存储，不会被执行。"""
        dangerous = "x'; DROP TABLE sessions; --"
        messages = [{"role": "user", "content": "a"}]

        self.assertTrue(self.store.save(dangerous, messages))
        self.assertEqual(self.store.load(dangerous), messages)

        # 表仍然存在，说明输入没有拼进 SQL
        with closing(sqlite3.connect(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_messages_not_saved(self):
        """非列表消息直接拒绝，不写入数据库。"""
        self.assertFalse(self.store.save("s1", "不是列表"))
        self.assertIsNone(self.store.load("s1"))
    def _count_rows(self):
        """统计当前快照行数。"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def test_delete_removes_snapshot(self):
        """delete 成功后记录消失。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        self.assertTrue(self.store.delete("s1"))
        self.assertIsNone(self.store.load("s1"))

    def test_delete_missing_session_returns_true(self):
        """删除不存在的记录视为成功，便于内存淘汰继续执行。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        self.assertTrue(self.store.delete("s2"))
        self.assertIsNotNone(self.store.load("s1"))

    def test_delete_without_database_returns_true_and_no_file(self):
        """数据库文件不存在时删除直接成功，且不会创建空文件。"""
        self.assertTrue(self.store.delete("s1"))
        self.assertFalse(self.db_path.exists())

    def test_save_and_prune_enforces_max_rows(self):
        """上限为 1 时连续写入，数据库始终不超过 1 行。"""
        for session_id in ("s1", "s2", "s3"):
            self.assertTrue(
                self.store.save_and_prune(
                    session_id,
                    [{"role": "user", "content": session_id}],
                    1,
                )
            )
            self.assertLessEqual(self._count_rows(), 1)

        self.assertIsNotNone(self.store.load("s3"))
        self.assertIsNone(self.store.load("s1"))

    def test_save_and_prune_keeps_current_and_protected(self):
        """当前会话与受保护会话不会被清理，允许暂时超限。"""
        self.store.save_and_prune("s1", [{"role": "user", "content": "a"}], 2)
        self.assertTrue(
            self.store.save_and_prune(
                "s2",
                [{"role": "user", "content": "b"}],
                1,
                protected_ids={"s1"},
            )
        )
        self.assertEqual(self._count_rows(), 2)
        self.assertIsNotNone(self.store.load("s1"))
        self.assertIsNotNone(self.store.load("s2"))

    def test_save_and_prune_deletes_oldest_first(self):
        """超额时先删 updated_at 最旧的会话。"""
        for session_id in ("a", "b", "c"):
            self.store.save_and_prune(
                session_id,
                [{"role": "user", "content": session_id}],
                3,
            )
        # 手工把 b 改成最旧，写入 d 时应先删 b
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    ("2000-01-01T00:00:00+00:00", "b"),
                )

        self.store.save_and_prune("d", [{"role": "user", "content": "d"}], 2)

        self.assertIsNone(self.store.load("b"))
        self.assertLessEqual(self._count_rows(), 2)

    def test_save_and_prune_rolls_back_on_failure(self):
        """清理失败时 UPSERT 也一起回滚，不留下半个快照。"""
        with mock.patch.object(
            self.store,
            "_prune_capacity",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            self.assertFalse(
                self.store.save_and_prune(
                    "s1",
                    [{"role": "user", "content": "a"}],
                    1,
                )
            )
        self.assertIsNone(self.store.load("s1"))

    def test_save_and_prune_rejects_invalid_messages(self):
        """非法消息不写入，也不清理已有会话。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        self.assertFalse(self.store.save_and_prune("s2", "不是列表", 1))
        self.assertIsNotNone(self.store.load("s1"))
    def _set_updated_at(self, session_id, value):
        """测试辅助：直接改写某个会话的更新时间。"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (value, session_id),
                )

    def test_updated_at_has_microsecond_precision(self):
        """新写入的时间必须带 6 位微秒，避免同秒退化。"""
        self.store.save("s1", [{"role": "user", "content": "a"}])
        _, updated = self._read_times("s1")
        self.assertRegex(updated, r"\.\d{6}\+00:00$")

    def test_rowid_breaks_tie_for_same_timestamp(self):
        """同一时间戳下按插入顺序淘汰，删除真正最早的 z 而不是 a。"""
        for session_id in ("z", "a", "m"):
            self.store.save_and_prune(
                session_id,
                [{"role": "user", "content": session_id}],
                3,
            )

        # 用当前时间做固定时间戳，避免触发 7 天过期清理
        fixed = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute("UPDATE sessions SET updated_at = ?", (fixed,))

        # 写入 3 行上限内的新会话会多出 1 行，应删除 rowid 最小的 z
        self.store.save_and_prune("new", [{"role": "user", "content": "new"}], 3)

        self.assertIsNone(self.store.load("z"))
        self.assertIsNotNone(self.store.load("a"))
        self.assertIsNotNone(self.store.load("m"))
        self.assertIsNotNone(self.store.load("new"))

    def test_ttl_removes_expired_session(self):
        """超过 7 天未更新的会话在保存时被删除。"""
        self.store.save("old", [{"role": "user", "content": "old"}])
        expired = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS + 1)
        ).isoformat(timespec="microseconds")
        self._set_updated_at("old", expired)

        self.store.save_and_prune("new", [{"role": "user", "content": "new"}], 200)

        self.assertIsNone(self.store.load("old"))
        self.assertIsNotNone(self.store.load("new"))

    def test_ttl_keeps_recent_session(self):
        """未超过 7 天的会话保留。"""
        self.store.save("recent", [{"role": "user", "content": "recent"}])
        recent = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS - 1)
        ).isoformat(timespec="microseconds")
        self._set_updated_at("recent", recent)

        self.store.save_and_prune("new", [{"role": "user", "content": "new"}], 200)

        self.assertIsNotNone(self.store.load("recent"))
        self.assertIsNotNone(self.store.load("new"))

    def test_ttl_keeps_current_and_protected(self):
        """受保护会话即使过期也不会被 TTL 清理，但读取时不恢复旧历史。"""
        self.store.save("old", [{"role": "user", "content": "old"}])
        expired = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS + 1)
        ).isoformat(timespec="microseconds")
        self._set_updated_at("old", expired)

        self.store.save_and_prune(
            "new",
            [{"role": "user", "content": "new"}],
            200,
            protected_ids={"old"},
        )

        # 行仍然保留，说明没有被 TTL 删除
        self.assertEqual(self._count_rows(), 2)
        self.assertIsNotNone(self.store.load("new"))
        # 但过期会话不能恢复旧历史
        self.assertIsNone(self.store.load("old"))

    def test_ttl_corrupted_timestamp_is_pruned_safely(self):
        """无法解析的时间按过期处理，不崩溃且不误删当前会话。"""
        self.store.save("bad", [{"role": "user", "content": "bad"}])
        self._set_updated_at("bad", "坏时间")

        self.assertTrue(
            self.store.save_and_prune("new", [{"role": "user", "content": "new"}], 200)
        )

        self.assertIsNone(self.store.load("bad"))
        self.assertIsNotNone(self.store.load("new"))

    def test_seconds_precision_legacy_data_is_compatible(self):
        """旧的秒级时间字符串仍能正常解析，不会被误判为过期。"""
        self.store.save("legacy", [{"role": "user", "content": "legacy"}])
        recent = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS - 1)
        ).isoformat(timespec="seconds")
        self._set_updated_at("legacy", recent)

        self.store.save_and_prune("new", [{"role": "user", "content": "new"}], 200)

        self.assertIsNotNone(self.store.load("legacy"))
    def test_load_expired_session_returns_none(self):
        """超过 7 天未更新的会话读取时返回 None，不恢复旧历史。"""
        self.store.save("old", [{"role": "user", "content": "old"}])
        expired = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS + 1)
        ).isoformat(timespec="microseconds")
        self._set_updated_at("old", expired)

        self.assertIsNone(self.store.load("old"))

    def test_load_corrupted_timestamp_returns_none(self):
        """更新时间无法解析时按过期处理，读取返回 None 且不抛异常。"""
        self.store.save("bad", [{"role": "user", "content": "bad"}])
        self._set_updated_at("bad", "坏时间")

        self.assertIsNone(self.store.load("bad"))

    def test_load_recent_session_restored(self):
        """未超过 7 天的会话仍能正常恢复历史。"""
        messages = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，请讲。"},
        ]
        self.store.save("recent", messages)
        recent = (
            datetime.now(timezone.utc)
            - timedelta(days=SESSION_TTL_DAYS - 1)
        ).isoformat(timespec="microseconds")
        self._set_updated_at("recent", recent)

        self.assertEqual(self.store.load("recent"), messages)