import copy
import json
import os


class Memory:
    def __init__(self, system_prompt=None, max_messages=50, persist_path=None, initial_messages=None):
        self.system_prompt = system_prompt
        self.max_messages = max_messages
        self.persist_path = persist_path
        self.messages = []
        if initial_messages is not None:
            # 优先使用外部传入的历史（如 SQLite 会话快照），并沿用同样的裁剪规则
            self.messages = self._sanitize(initial_messages)
            self._trim()
        elif persist_path and os.path.exists(persist_path):
            self._load()

    def add(self, role, content):
        self.messages.append({"role": role, "content": content})
        self._trim()
        self._save()

    def add_assistant(self, message):
        kept = {k: v for k, v in message.items() if k in ("role", "content", "tool_calls")}
        self.messages.append(kept)
        self._trim()
        self._save()

    def add_tool_result(self, tool_call_id, content):
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })
        self._trim()
        self._save()

    def get_messages(self):
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}] + list(self.messages)
        return list(self.messages)

    def restore(self, messages):
        """用外部快照替换当前记忆，并沿用同样的清洗和裁剪规则。"""
        self.messages = self._sanitize(messages)
        self._trim()

    def snapshot(self):
        """返回可安全持久化的消息快照。

        返回的是新的列表和新的字典对象，调用方修改它不会影响当前记忆；
        同时过滤掉损坏条目，避免坏数据写进数据库。
        """
        return self._sanitize(self.messages)

    @staticmethod
    def _sanitize(messages):
        """把任意输入清洗成合法的消息列表。

        规则：
        - 只保留 user / assistant / tool 三种角色；
        - content 必须是字符串；assistant 带 tool_calls 时允许 content 为 None；
        - tool 消息必须带字符串 tool_call_id；
        - 非列表、非字典或字段类型错误的条目直接丢弃。
        """
        if not isinstance(messages, list):
            return []
        cleaned = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            content = item.get("content")
            if content is not None and not isinstance(content, str):
                continue
            tool_calls = item.get("tool_calls")
            if tool_calls is not None and not isinstance(tool_calls, list):
                tool_calls = None
            # 没有正文、也没有工具调用时，这条消息没有意义
            if content is None and not (role == "assistant" and tool_calls):
                continue
            # tool 消息必须能对应到某次工具调用
            if role == "tool":
                tool_call_id = item.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
            entry = {"role": role, "content": content}
            if role == "assistant" and tool_calls:
                entry["tool_calls"] = copy.deepcopy(tool_calls)
            if role == "tool":
                entry["tool_call_id"] = tool_call_id
            cleaned.append(entry)
        return cleaned

    def _trim(self):
        if len(self.messages) <= self.max_messages:
            return
        self.messages = self.messages[-self.max_messages:]
        while self.messages and self.messages[0].get("role") == "tool":
            self.messages.pop(0)

    def _save(self):
        if not self.persist_path:
            return
        directory = os.path.dirname(self.persist_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(self.persist_path, "w", encoding="utf-8") as f:
            json.dump(self.messages, f, ensure_ascii=False, indent=2)

    def _load(self):
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.messages = []
            return
        # 旧文件可能被手工改坏，加载后统一清洗并按上限裁剪
        self.messages = self._sanitize(data)
        self._trim()