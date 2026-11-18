"""Memory 的恢复、裁剪、快照与损坏数据回退测试。

覆盖新增能力，同时确认原有 JSON 文件持久化仍然兼容。
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.memory import Memory


class MemoryRestoreTest(unittest.TestCase):
    """历史恢复、裁剪与安全快照。"""

    def test_initial_messages_restored(self):
        """构造时可以直接传入历史消息。"""
        memory = Memory(initial_messages=[{"role": "user", "content": "甲"}])
        self.assertEqual(memory.messages, [{"role": "user", "content": "甲"}])

    def test_initial_messages_trimmed_to_limit(self):
        """恢复的历史仍按 max_messages 保留最后若干条。"""
        history = [{"role": "user", "content": f"第{i}条"} for i in range(60)]
        memory = Memory(max_messages=50, initial_messages=history)

        self.assertEqual(len(memory.messages), 50)
        self.assertEqual(memory.messages[0]["content"], "第10条")
        self.assertEqual(memory.messages[-1]["content"], "第59条")

    def test_trim_drops_leading_tool_messages(self):
        """裁剪后如果开头是工具结果，要一直弹到不以 tool 开头。"""
        history = [{"role": "user", "content": "问题"}] + [
            {"role": "tool", "tool_call_id": f"t{i}", "content": "结果"}
            for i in range(50)
        ]
        memory = Memory(max_messages=50, initial_messages=history)
        self.assertEqual(memory.messages, [])

    def test_invalid_initial_messages_falls_back_to_empty(self):
        """非列表或字段非法的历史按空记忆处理。"""
        self.assertEqual(Memory(initial_messages="坏数据").messages, [])
        broken = [
            1,
            "x",
            {"role": "system", "content": "越权角色"},
            {"role": "user", "content": 123},
        ]
        self.assertEqual(Memory(initial_messages=broken).messages, [])

    def test_restore_replaces_and_trims(self):
        """restore 会整体替换记忆，并沿用裁剪规则。"""
        memory = Memory(max_messages=2)
        memory.add("user", "旧")
        memory.restore([
            {"role": "user", "content": "新一"},
            {"role": "assistant", "content": "新二"},
            {"role": "user", "content": "新三"},
        ])
        self.assertEqual([m["content"] for m in memory.messages], ["新二", "新三"])

    def test_snapshot_is_independent_copy(self):
        """修改快照不会影响当前记忆。"""
        memory = Memory()
        memory.add("user", "甲")

        snapshot = memory.snapshot()
        snapshot.append({"role": "user", "content": "乙"})
        snapshot[0]["content"] = "被改"

        self.assertEqual(memory.messages, [{"role": "user", "content": "甲"}])

    def test_snapshot_filters_broken_entries(self):
        """快照会丢弃损坏条目，避免坏数据写进数据库。"""
        memory = Memory()
        memory.messages = [
            {"role": "user", "content": "正常"},
            {"role": "assistant"},
            {"role": "tool", "content": "缺 tool_call_id"},
            "坏数据",
        ]
        self.assertEqual(memory.snapshot(), [{"role": "user", "content": "正常"}])

    def test_snapshot_excludes_system_prompt(self):
        """快照只保存对话消息，不重复保存系统提示。"""
        memory = Memory(system_prompt="系统提示")
        memory.add("user", "甲")

        self.assertEqual(memory.snapshot(), [{"role": "user", "content": "甲"}])
        self.assertEqual(len(memory.get_messages()), 2)

    def test_add_still_trims_to_limit(self):
        """原有 add 的裁剪行为不回归。"""
        memory = Memory(max_messages=3)
        for index in range(5):
            memory.add("user", str(index))
        self.assertEqual([m["content"] for m in memory.messages], ["2", "3", "4"])

    def test_assistant_tool_calls_are_kept(self):
        """带工具调用的 assistant 消息 content 为 None 时也要保留。"""
        memory = Memory()
        memory.add_assistant({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "t1", "function": {"name": "f"}}],
        })
        self.assertEqual(
            memory.snapshot(),
            [{
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "t1", "function": {"name": "f"}}],
            }],
        )


class MemoryFilePersistenceTest(unittest.TestCase):
    """原有 JSON 文件持久化保持兼容。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "memory.json"

    def test_json_roundtrip(self):
        """写入后重新创建 Memory 能读回历史。"""
        first = Memory(persist_path=str(self.path))
        first.add("user", "甲")

        second = Memory(persist_path=str(self.path))
        self.assertEqual(second.messages, [{"role": "user", "content": "甲"}])

    def test_corrupted_json_falls_back_to_empty(self):
        """文件内容不是 JSON 时回退为空记忆，不抛异常。"""
        self.path.write_text("不是 JSON", encoding="utf-8")
        self.assertEqual(Memory(persist_path=str(self.path)).messages, [])

    def test_non_list_json_falls_back_to_empty(self):
        """文件内容是对象而不是列表时也回退为空记忆。"""
        self.path.write_text(json.dumps({"role": "user"}), encoding="utf-8")
        self.assertEqual(Memory(persist_path=str(self.path)).messages, [])