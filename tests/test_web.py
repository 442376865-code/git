"""Web 接口与聊天页面的单元测试。

覆盖：页面可访问且包含关键元素、健康检查、正常聊天回复、
空消息校验、会话内记忆复用、不同会话相互隔离、Agent 异常时的中文错误提示、
Agent 初始化失败的兜底、接口不改变 Agent.run 的调用方式、
以及"等待回复时点击新会话"的竞态守卫。
全部使用注入的假 Agent，不调用大模型、不访问网络。
"""

import inspect
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agent.main import Agent
from agent.memory import Memory
from agent.router import Router
from agent.session_store import SESSION_TTL_DAYS, SessionStore
from agent.web_app import INDEX_FILE, create_app

NODE = shutil.which("node")

# 测试统一使用临时目录，绝不读写项目真实的 data/
TEMP_ROOT = Path(tempfile.mkdtemp(prefix="demo3-web-test-"))


def make_test_app(agent_factory=None):
    """创建使用临时 SQLite 数据库的应用，避免读写真实 data/。"""
    return create_app(
        agent_factory=agent_factory,
        session_store=SessionStore(TEMP_ROOT / "sessions.db"),
    )


class FakeAgent:
    """假 Agent：记录收到的消息，返回可预测的回复。"""

    def __init__(self):
        self.messages = []

    def run(self, user_input):
        self.messages.append(user_input)
        return f"回复：{user_input}（第 {len(self.messages)} 条）"


class BrokenAgent:
    """总是抛异常的假 Agent，用于验证错误兜底。"""

    def run(self, user_input):
        raise RuntimeError("模型调用失败")


class RoutingAgent:
    """只带真实 Router 的假 Agent，用于验证响应里的 module 字段。"""

    def __init__(self):
        self.router = Router()

    def run(self, user_input):
        return "已处理"


class WebPageTest(unittest.TestCase):
    """聊天页面的静态内容。"""

    def setUp(self):
        self.client = TestClient(make_test_app(agent_factory=FakeAgent))

    def test_index_page_available(self):
        """根路径返回 HTML 页面。"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_index_page_contains_chat_elements(self):
        """页面包含标题、输入框、发送按钮与免责提示。"""
        html = self.client.get("/").text
        self.assertIn("医知有据", html)
        self.assertIn("chat-form", html)
        self.assertIn("发送", html)
        self.assertIn("sessionStorage", html)
        self.assertIn("120", html)

    def test_index_file_exists(self):
        """静态页面文件真实存在，避免部署后 500。"""
        self.assertTrue(INDEX_FILE.is_file())


class ChatApiTest(unittest.TestCase):
    """聊天接口的请求校验、会话记忆与异常兜底。"""

    def setUp(self):
        self.created = []

        def factory():
            agent = FakeAgent()
            self.created.append(agent)
            return agent

        self.client = TestClient(make_test_app(agent_factory=factory))

    def test_health(self):
        """健康检查返回 ok。"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_chat_returns_reply_and_session(self):
        """正常消息返回回复，并生成会话编号。"""
        response = self.client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["reply"], "回复：你好（第 1 条）")
        self.assertTrue(data["session_id"])

    def test_chat_empty_message_rejected(self):
        """空消息返回 400 与中文提示。"""
        response = self.client.post("/api/chat", json={"message": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能为空", response.json()["detail"])

    def test_chat_missing_message_field(self):
        """缺字段按空消息处理，不抛 500。"""
        response = self.client.post("/api/chat", json={})
        self.assertEqual(response.status_code, 400)

    def test_same_session_reuses_agent(self):
        """同一会话复用同一个 Agent，实现会话内记忆。"""
        first = self.client.post("/api/chat", json={"message": "你好"}).json()
        second = self.client.post(
            "/api/chat",
            json={"message": "继续", "session_id": first["session_id"]},
        ).json()

        self.assertEqual(second["session_id"], first["session_id"])
        self.assertIn("第 2 条", second["reply"])
        self.assertEqual(len(self.created), 1)

    def test_different_sessions_isolated(self):
        """不同会话使用不同 Agent，记忆互不影响。"""
        self.client.post("/api/chat", json={"message": "甲", "session_id": "s1"})
        self.client.post("/api/chat", json={"message": "乙", "session_id": "s2"})
        self.assertEqual(len(self.created), 2)
        self.assertEqual(self.created[0].messages, ["甲"])
        self.assertEqual(self.created[1].messages, ["乙"])

    def test_agent_exception_returns_500(self):
        """Agent 抛异常时返回 500 与中文提示。"""
        client = TestClient(make_test_app(agent_factory=BrokenAgent))
        response = client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("请稍后再试", response.json()["detail"])

    def test_agent_run_signature_unchanged(self):
        """Web 层只按 run(user_input) 单参数调用，不改变 Agent 接口。"""
        self.assertEqual(list(inspect.signature(FakeAgent.run).parameters), ["self", "user_input"])
        response = self.client.post("/api/chat", json={"message": "查看用药计划"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.created[0].messages, ["查看用药计划"])


class ModuleFieldTest(unittest.TestCase):
    """响应中的 module 字段必须反映实际命中的业务模块。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.client = TestClient(
            create_app(
                agent_factory=RoutingAgent,
                session_store=SessionStore(self.db_path),
            )
        )

    def test_chat_returns_routed_module(self):
        """四条基准请求与通用兜底分别返回对应模块名。"""
        cases = {
            "什么是高血压": Router.MEDICAL_KNOWLEDGE,
            "查看健康档案": Router.HEALTH_MANAGEMENT,
            "查看用药计划": Router.MEDICATION,
            "附近药房在哪": Router.MEDICAL_NAVIGATION,
            "你好": Router.GENERAL,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                response = self.client.post("/api/chat", json={"message": message})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["module"], expected)


class AgentInitFailureTest(unittest.TestCase):
    """Agent 初始化失败时同样走中文 500 兜底。"""

    def test_init_failure_returns_500(self):
        """工厂初始化抛异常时返回 500 与中文提示，不暴露内部异常。"""

        def broken_factory():
            raise RuntimeError("数据库打开失败")

        client = TestClient(make_test_app(agent_factory=broken_factory))
        response = client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("请稍后再试", response.json()["detail"])

    def test_init_failure_not_cached(self):
        """初始化失败不写入会话缓存，下一次请求可以恢复。"""
        attempts = []

        def flaky_factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("首次初始化失败")
            return FakeAgent()

        client = TestClient(make_test_app(agent_factory=flaky_factory))
        first = client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(first.status_code, 500)

        second = client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["reply"], "回复：你好（第 1 条）")
        self.assertEqual(len(attempts), 2)

    def test_init_failure_keeps_existing_session(self):
        """新会话初始化失败时，已有会话的记忆不受影响。"""
        created = []

        def factory():
            if len(created) == 1:
                raise RuntimeError("新会话初始化失败")
            agent = FakeAgent()
            created.append(agent)
            return agent

        client = TestClient(make_test_app(agent_factory=factory))
        client.post("/api/chat", json={"message": "甲", "session_id": "s1"})

        failed = client.post("/api/chat", json={"message": "乙", "session_id": "s2"})
        self.assertEqual(failed.status_code, 500)

        again = client.post("/api/chat", json={"message": "丙", "session_id": "s1"})
        self.assertEqual(again.status_code, 200)
        self.assertIn("第 2 条", again.json()["reply"])


class FrontendRaceTest(unittest.TestCase):
    """等待回复时点击“新会话”的竞态守卫。"""

    @classmethod
    def setUpClass(cls):
        cls.html = INDEX_FILE.read_text(encoding="utf-8")
        match = re.search(
            r"function isStaleReply\([^)]*\)\s*\{[\s\S]*?\n\s*\}",
            cls.html,
        )
        if match is None:
            raise AssertionError("页面中找不到 isStaleReply 竞态判定函数")
        cls.function_source = match.group(0)

    def test_new_session_bumps_generation(self):
        """点击新会话时递增代号，使旧请求过期。"""
        self.assertIn("generation += 1", self.html)

    def test_reply_guard_precedes_session_write(self):
        """回写 session_id 之前必须先判断回复是否属于当前会话。"""
        guard = self.html.index("isStaleReply(requestGeneration, generation)")
        write = self.html.index("sessionStorage.setItem(SESSION_KEY")
        self.assertLess(guard, write)

    def test_loading_released_only_for_current_generation(self):
        """旧请求的 finally 不能解锁新会话的等待状态。"""
        finally_block = self.html[self.html.index("} finally {") :]
        self.assertIn("isStaleReply(requestGeneration, generation)", finally_block)

    def test_stale_reply_function_executable(self):
        """用 Node 执行页面中的判定函数：不同代号为过期，相同代号为有效。"""
        if NODE is None:
            self.skipTest("未找到 node，跳过可执行断言")
        script = self.function_source + (
            "\nprocess.stdout.write(JSON.stringify("
            "[isStaleReply(0, 1), isStaleReply(1, 1), isStaleReply(2, 2)]));"
        )
        result = subprocess.run(
            [NODE, "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[true,false,false]")

class ProbeAgent:
    """记录自身被并发调用的峰值，并给每条消息编号。"""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.running = 0
        self.peak = 0
        self.messages = []
        self._lock = threading.Lock()

    def run(self, user_input):
        with self._lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.messages.append(user_input)
            index = len(self.messages)
        time.sleep(self.delay)
        with self._lock:
            self.running -= 1
        return f"{user_input}#{index}"


class BarrierAgent:
    """两次 run 必须同时到达栅栏；被全局大锁串行化时会超时报错。"""

    def __init__(self, barrier):
        self.barrier = barrier

    def run(self, user_input):
        self.barrier.wait(timeout=5)
        return "并行:" + user_input


class FailOnceAgent:
    """第一次调用抛异常，之后正常返回。"""

    def __init__(self):
        self.calls = 0

    def run(self, user_input):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return "恢复:" + user_input


class WebSessionConcurrencyTest(unittest.TestCase):
    """同一会话串行、不同会话并行的并发行为。"""

    def _post_concurrently(self, app, session_ids, messages):
        """用独立 TestClient 与栅栏让多个请求尽量同时发出。"""
        clients = [TestClient(app) for _ in messages]
        barrier = threading.Barrier(len(messages))

        def worker(client, message, session_id):
            barrier.wait(timeout=5)
            return client.post(
                "/api/chat",
                json={"message": message, "session_id": session_id},
            )

        with ThreadPoolExecutor(max_workers=len(messages)) as pool:
            futures = [
                pool.submit(worker, client, message, session_id)
                for client, message, session_id in zip(clients, messages, session_ids)
            ]
            return [future.result(timeout=15) for future in futures]

    def test_same_session_agent_runs_serially(self):
        """同一 session_id 的两条请求不能同时进入同一个 Agent。"""
        probe = ProbeAgent()
        app = make_test_app(agent_factory=lambda: probe)

        results = self._post_concurrently(app, ["s1", "s1"], ["甲", "乙"])

        self.assertEqual([response.status_code for response in results], [200, 200])
        self.assertEqual(probe.peak, 1)

    def test_same_session_replies_do_not_cross_talk(self):
        """串行处理保证每条回复对应自己的输入与序号。"""
        probe = ProbeAgent()
        app = make_test_app(agent_factory=lambda: probe)

        results = self._post_concurrently(app, ["s1", "s1"], ["甲", "乙"])

        self.assertEqual(len(probe.messages), 2)
        self.assertEqual(sorted(probe.messages), ["乙", "甲"])
        for response in results:
            message, index = response.json()["reply"].split("#")
            self.assertEqual(probe.messages[int(index) - 1], message)

    def test_different_sessions_run_in_parallel(self):
        """不同 session_id 使用各自的锁，不会被同一把执行锁阻塞。"""
        barrier = threading.Barrier(2)
        created = []

        def factory():
            agent = BarrierAgent(barrier)
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)

        results = self._post_concurrently(app, ["a", "b"], ["一", "二"])

        self.assertEqual([response.status_code for response in results], [200, 200])
        self.assertEqual(len(created), 2)

    def test_session_lock_released_after_agent_error(self):
        """Agent 抛异常后 finally 释放会话锁，后续请求仍可处理。"""
        agent = FailOnceAgent()
        app = make_test_app(agent_factory=lambda: agent)
        client = TestClient(app)

        first = client.post("/api/chat", json={"message": "一", "session_id": "s1"})
        self.assertEqual(first.status_code, 500)

        # 上一请求若没有释放锁，这里会一直等待
        second = client.post("/api/chat", json={"message": "二", "session_id": "s1"})
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["reply"], "恢复:二")

    def test_eviction_removes_agent_and_session_lock(self):
        """会话淘汰时同时删除 Agent 与会话锁，旧会话会重新创建。"""
        created = []

        def factory():
            agent = FakeAgent()
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)
        client = TestClient(app)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            client.post("/api/chat", json={"message": "甲", "session_id": "s1"})
            client.post("/api/chat", json={"message": "乙", "session_id": "s2"})
            client.post("/api/chat", json={"message": "丙", "session_id": "s1"})

        self.assertEqual(len(created), 3)

class GateAgent:
    """run 会阻塞到 gate 放行，用于制造"执行中"的会话。"""

    def __init__(self, gate, started=None):
        self.gate = gate
        self.started = started
        self.calls = 0

    def run(self, user_input):
        self.calls += 1
        if self.started is not None:
            self.started.set()
        self.gate.wait(timeout=10)
        return f"{user_input}:{self.calls}"


class WebSessionCapacityTest(unittest.TestCase):
    """活动会话保护与 LRU 淘汰行为。"""

    def test_active_session_survives_eviction(self):
        """活跃会话不会因为创建新会话而被淘汰。"""
        gate = threading.Event()
        started = threading.Event()
        created = []

        def factory():
            if not created:
                agent = GateAgent(gate, started)
            else:
                agent = FakeAgent()
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)
        client = TestClient(app)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=1) as pool:
                running = pool.submit(
                    client.post,
                    "/api/chat",
                    json={"message": "一", "session_id": "s1"},
                )
                self.assertTrue(started.wait(timeout=5))

                # s1 正在执行，创建 s2 时不能淘汰 s1
                other = TestClient(app).post(
                    "/api/chat",
                    json={"message": "二", "session_id": "s2"},
                )
                self.assertEqual(other.status_code, 200)

                gate.set()
                self.assertEqual(running.result(timeout=15).status_code, 200)

            # s1 仍然复用同一个 Agent，序号从 1 增加到 2
            again = client.post("/api/chat", json={"message": "三", "session_id": "s1"})

        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["reply"], "三:2")
        self.assertEqual(len(created), 2)

    def test_waiting_request_protects_session(self):
        """正在等待同一会话锁的请求也会保护该会话。"""
        gate = threading.Event()
        started = threading.Event()
        created = []

        def factory():
            if not created:
                agent = GateAgent(gate, started)
            else:
                agent = FakeAgent()
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    TestClient(app).post,
                    "/api/chat",
                    json={"message": "一", "session_id": "s1"},
                )
                self.assertTrue(started.wait(timeout=5))

                second = pool.submit(
                    TestClient(app).post,
                    "/api/chat",
                    json={"message": "二", "session_id": "s1"},
                )
                # 留出时间让第二个请求进入会话锁等待
                time.sleep(0.3)

                other = TestClient(app).post(
                    "/api/chat",
                    json={"message": "三", "session_id": "s2"},
                )
                self.assertEqual(other.status_code, 200)

                gate.set()
                self.assertEqual(first.result(timeout=15).status_code, 200)
                self.assertEqual(second.result(timeout=15).status_code, 200)

            again = TestClient(app).post(
                "/api/chat",
                json={"message": "四", "session_id": "s1"},
            )

        self.assertEqual(again.json()["reply"], "四:3")
        self.assertEqual(len(created), 2)

    def test_all_active_allows_temporary_overflow(self):
        """所有会话都活跃时允许暂时超额，结束后自动清理到上限。"""
        gates = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        created = []

        def factory():
            index = len(created)
            if index < 2:
                agent = GateAgent(gates[index], started[index])
            else:
                free = threading.Event()
                free.set()
                agent = GateAgent(free)
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    TestClient(app).post,
                    "/api/chat",
                    json={"message": "一", "session_id": "s1"},
                )
                self.assertTrue(started[0].wait(timeout=5))

                second = pool.submit(
                    TestClient(app).post,
                    "/api/chat",
                    json={"message": "二", "session_id": "s2"},
                )
                self.assertTrue(started[1].wait(timeout=5))

                # 两个会话都在执行，允许暂时超过 MAX_SESSIONS
                self.assertEqual(len(created), 2)

                gates[0].set()
                self.assertEqual(first.result(timeout=15).status_code, 200)

                gates[1].set()
                self.assertEqual(second.result(timeout=15).status_code, 200)

            # s1 最久未使用且已空闲，已被清理，重新请求会创建新 Agent
            again = TestClient(app).post(
                "/api/chat",
                json={"message": "三", "session_id": "s1"},
            )

        self.assertEqual(again.status_code, 200)
        self.assertEqual(len(created), 3)

    def test_eviction_chooses_least_recently_used_idle_session(self):
        """超额时淘汰最久未使用的空闲会话，而不是最近访问过的会话。"""
        created = []

        def factory():
            agent = FakeAgent()
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)
        client = TestClient(app)

        with mock.patch("agent.web_app.MAX_SESSIONS", 2):
            client.post("/api/chat", json={"message": "a", "session_id": "s1"})
            client.post("/api/chat", json={"message": "b", "session_id": "s2"})
            # 再次访问 s1，使 s2 成为最久未使用的空闲会话
            client.post("/api/chat", json={"message": "c", "session_id": "s1"})
            # 创建 s3 触发淘汰
            client.post("/api/chat", json={"message": "d", "session_id": "s3"})

            again_s1 = client.post(
                "/api/chat",
                json={"message": "e", "session_id": "s1"},
            )
            again_s2 = client.post(
                "/api/chat",
                json={"message": "f", "session_id": "s2"},
            )

        self.assertIn("第 3 条", again_s1.json()["reply"])
        self.assertIn("第 1 条", again_s2.json()["reply"])
        self.assertEqual(len(created), 4)

    def test_active_count_decreases_after_agent_error(self):
        """Agent 异常后活动计数仍会减少，空闲会话可被正常淘汰。"""
        created = []

        def factory():
            agent = FailOnceAgent() if not created else FakeAgent()
            created.append(agent)
            return agent

        app = make_test_app(agent_factory=factory)
        client = TestClient(app)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            first = client.post(
                "/api/chat",
                json={"message": "一", "session_id": "s1"},
            )
            self.assertEqual(first.status_code, 500)

            # s1 已空闲，创建 s2 时应当可以淘汰 s1
            other = client.post(
                "/api/chat",
                json={"message": "二", "session_id": "s2"},
            )
            self.assertEqual(other.status_code, 200)

            # s1 已被淘汰，重新创建后序号回到 1
            again = client.post(
                "/api/chat",
                json={"message": "三", "session_id": "s1"},
            )

        self.assertEqual(again.status_code, 200)
        self.assertIn("第 1 条", again.json()["reply"])
        self.assertEqual(len(created), 3)

class EchoRouter:
    """把任意输入都路由到 echo 模块，避免测试调用大模型。"""

    def route(self, user_input):
        return "echo"


class EchoModule:
    """返回当前记忆条数，便于断言历史是否被恢复。"""

    name = "echo"

    def run(self, user_input, context=None):
        return f"历史{len(context.messages)}条"


def make_echo_agent():
    """创建不调用大模型的真实 Agent，用于验证记忆持久化。"""
    return Agent(router=EchoRouter(), modules={"echo": EchoModule()})


class FlakyEchoAgent:
    """带 Memory 的假 Agent，可切换成下一轮抛异常。"""

    def __init__(self):
        self.memory = Memory()
        self.fail = False

    def run(self, user_input):
        self.memory.add("user", user_input)
        if self.fail:
            raise RuntimeError("boom")
        reply = f"回复:{user_input}"
        self.memory.add("assistant", reply)
        return reply


class SessionIdValidationTest(unittest.TestCase):
    """session_id 格式校验：非法值直接 400，且不触碰数据库。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.store = SessionStore(self.db_path)
        self.client = TestClient(
            create_app(agent_factory=FakeAgent, session_store=self.store)
        )

    def test_invalid_session_ids_rejected_without_writing(self):
        """含斜杠、空格、中文、超长等非法编号返回中文 400。"""
        invalid = ["a/b", "a b", "会话一号", "a;b", "a.b", "x" * 65]
        for session_id in invalid:
            with self.subTest(session_id=session_id):
                response = self.client.post(
                    "/api/chat",
                    json={"message": "你好", "session_id": session_id},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("会话编号不合法", response.json()["detail"])
        # 非法请求在创建 Agent 之前就被拒绝，数据库文件不应出现
        self.assertFalse(self.db_path.exists())

    def test_valid_session_id_accepted(self):
        """64 位字母、数字、下划线、连字符都按正常请求处理。"""
        session_id = "A-z_0" + "x" * 59
        self.assertEqual(len(session_id), 64)
        response = self.client.post(
            "/api/chat",
            json={"message": "你好", "session_id": session_id},
        )
        self.assertEqual(response.status_code, 200)

    def test_empty_session_id_is_generated(self):
        """不带会话编号时自动生成合法编号。"""
        response = self.client.post("/api/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.json()["session_id"], r"^[A-Za-z0-9_-]{1,64}$")


class SessionPersistenceTest(unittest.TestCase):
    """同一数据库重启恢复、会话隔离与失败不覆盖旧快照。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"

    def _new_client(self, factory):
        # 每次调用都新建 store 与应用，模拟服务重启
        return TestClient(
            create_app(
                agent_factory=factory,
                session_store=SessionStore(self.db_path),
            )
        )

    def test_restart_restores_history(self):
        """同一 session_id 在重启后能读取上次保存的历史。"""
        first = self._new_client(make_echo_agent)
        first_reply = first.post(
            "/api/chat",
            json={"message": "第一", "session_id": "restart-1"},
        )
        self.assertEqual(first_reply.json()["reply"], "历史1条")

        second = self._new_client(make_echo_agent)
        second_reply = second.post(
            "/api/chat",
            json={"message": "第二", "session_id": "restart-1"},
        )
        # 恢复 2 条历史后加入本条用户消息，模块看到 3 条
        self.assertEqual(second_reply.json()["reply"], "历史3条")

    def test_different_sessions_isolated_after_restart(self):
        """重启后不同 session 的记忆仍然互不叠加。"""
        first = self._new_client(make_echo_agent)
        first.post("/api/chat", json={"message": "甲", "session_id": "iso-a"})
        first.post("/api/chat", json={"message": "乙", "session_id": "iso-b"})

        second = self._new_client(make_echo_agent)
        again_a = second.post("/api/chat", json={"message": "丙", "session_id": "iso-a"})
        again_b = second.post("/api/chat", json={"message": "丁", "session_id": "iso-b"})

        self.assertEqual(again_a.json()["reply"], "历史3条")
        self.assertEqual(again_b.json()["reply"], "历史3条")

    def test_agent_error_keeps_old_snapshot(self):
        """Agent 抛异常时不保存，数据库里的旧快照保持不变。"""
        agent = FlakyEchoAgent()
        client = self._new_client(lambda: agent)

        ok = client.post("/api/chat", json={"message": "第一", "session_id": "keep-1"})
        self.assertEqual(ok.status_code, 200)

        agent.fail = True
        failed = client.post("/api/chat", json={"message": "第二", "session_id": "keep-1"})
        self.assertEqual(failed.status_code, 500)

        self.assertEqual(
            SessionStore(self.db_path).load("keep-1"),
            [
                {"role": "user", "content": "第一"},
                {"role": "assistant", "content": "回复:第一"},
            ],
        )


class SessionExpiryTest(unittest.TestCase):
    """过期会话再次访问时不得恢复旧历史。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.store = SessionStore(self.db_path)

    def test_expired_session_restarts_from_empty_history(self):
        """8 天前的会话再次请求时从 1 条历史开始，旧消息不会复活。"""
        client = TestClient(
            create_app(agent_factory=make_echo_agent, session_store=self.store)
        )
        first = client.post("/api/chat", json={"message": "旧一", "session_id": "exp-1"})
        self.assertEqual(first.json()["reply"], "历史1条")
        second = client.post("/api/chat", json={"message": "旧二", "session_id": "exp-1"})
        self.assertEqual(second.json()["reply"], "历史3条")

        # 把更新时间改成 8 天前，模拟长期未访问的会话
        expired = (
            datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS + 1)
        ).isoformat(timespec="microseconds")
        with closing(sqlite3.connect(self.db_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (expired, "exp-1"),
                )

        # 模拟服务重启后再次访问同一 session_id
        restarted = TestClient(
            create_app(
                agent_factory=make_echo_agent,
                session_store=SessionStore(self.db_path),
            )
        )
        again = restarted.post("/api/chat", json={"message": "新", "session_id": "exp-1"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["reply"], "历史1条")

        # 保存后的快照只包含本次请求的 user 与 assistant 两条消息
        self.assertEqual(
            SessionStore(self.db_path).load("exp-1"),
            [
                {"role": "user", "content": "新"},
                {"role": "assistant", "content": "历史1条"},
            ],
        )


class MemoryGateAgent:
    """带 Memory 的阻塞 Agent，用于制造同时活跃的会话。"""

    def __init__(self, gate, started):
        self.memory = Memory()
        self.gate = gate
        self.started = started

    def run(self, user_input):
        self.memory.add("user", user_input)
        self.started.set()
        self.gate.wait(timeout=10)
        reply = f"回复:{user_input}"
        self.memory.add("assistant", reply)
        return reply


class SpyStore(SessionStore):
    """记录 save_and_prune 的保护集合，便于断言活动会话被传入。"""

    def __init__(self, db_path):
        super().__init__(db_path)
        self.calls = []

    def save_and_prune(self, session_id, messages, max_rows, protected_ids=()):
        self.calls.append((session_id, set(protected_ids)))
        return super().save_and_prune(session_id, messages, max_rows, protected_ids)


class SessionCapacityTest(unittest.TestCase):
    """SQLite 行数与内存 LRU 一起受 MAX_SESSIONS 限制。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"

    def _count_rows(self):
        """统计数据库中保存的会话行数。"""
        if not self.db_path.exists():
            return 0
        with closing(sqlite3.connect(self.db_path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def test_database_never_exceeds_max_sessions(self):
        """MAX_SESSIONS=1 时连续创建多个会话，数据库始终不超过 1 行。"""
        store = SessionStore(self.db_path)
        client = TestClient(create_app(agent_factory=make_echo_agent, session_store=store))

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            for index in range(5):
                response = client.post(
                    "/api/chat",
                    json={"message": "你好", "session_id": f"cap-{index}"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertLessEqual(self._count_rows(), 1)

        self.assertEqual(self._count_rows(), 1)
        self.assertIsNotNone(store.load("cap-4"))

    def test_evicted_session_starts_fresh(self):
        """被容量淘汰的 session 再访问时不得恢复旧历史。"""
        store = SessionStore(self.db_path)
        client = TestClient(create_app(agent_factory=make_echo_agent, session_store=store))

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            first = client.post("/api/chat", json={"message": "一", "session_id": "fresh-1"})
            self.assertEqual(first.json()["reply"], "历史1条")

            second = client.post("/api/chat", json={"message": "二", "session_id": "fresh-2"})
            self.assertEqual(second.json()["reply"], "历史1条")

            # fresh-1 已被淘汰，重新访问必须从零开始
            again = client.post("/api/chat", json={"message": "三", "session_id": "fresh-1"})
            self.assertEqual(again.json()["reply"], "历史1条")

    def test_active_sessions_protected_during_save(self):
        """保存快照时把所有活动会话传给数据库作为保护集合。"""
        gates = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        created = []

        def factory():
            index = len(created)
            agent = MemoryGateAgent(gates[index], started[index])
            created.append(agent)
            return agent

        store = SpyStore(self.db_path)
        app = create_app(agent_factory=factory, session_store=store)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    lambda: TestClient(app).post(
                        "/api/chat",
                        json={"message": "一", "session_id": "prot-1"},
                    )
                )
                self.assertTrue(started[0].wait(timeout=5))

                second = pool.submit(
                    lambda: TestClient(app).post(
                        "/api/chat",
                        json={"message": "二", "session_id": "prot-2"},
                    )
                )
                self.assertTrue(started[1].wait(timeout=5))

                # prot-1 先完成并保存；此时 prot-2 仍活跃，必须被保护
                gates[0].set()
                self.assertEqual(first.result(timeout=15).status_code, 200)
                gates[1].set()
                self.assertEqual(second.result(timeout=15).status_code, 200)

        self.assertEqual(store.calls[0][0], "prot-1")
        self.assertIn("prot-2", store.calls[0][1])
        self.assertLessEqual(self._count_rows(), 1)

    def test_save_failure_does_not_break_request(self):
        """快照写入失败时请求仍正常返回，且不写数据库。"""
        store = SessionStore(self.db_path)
        client = TestClient(create_app(agent_factory=make_echo_agent, session_store=store))

        with mock.patch.object(store, "save_and_prune", return_value=False):
            response = client.post(
                "/api/chat",
                json={"message": "你好", "session_id": "fail-save"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "历史1条")
        self.assertFalse(self.db_path.exists())

    def test_delete_failure_keeps_memory_session(self):
        """数据库删除失败时保留内存会话，不崩溃也不丢失上下文。"""
        created = []

        def factory():
            agent = FakeAgent()
            created.append(agent)
            return agent

        store = SessionStore(self.db_path)
        client = TestClient(create_app(agent_factory=factory, session_store=store))

        with mock.patch.object(store, "delete", return_value=False), \
                mock.patch("agent.web_app.MAX_SESSIONS", 1):
            client.post("/api/chat", json={"message": "甲", "session_id": "keep-a"})
            client.post("/api/chat", json={"message": "乙", "session_id": "keep-b"})
            again = client.post("/api/chat", json={"message": "丙", "session_id": "keep-a"})

        self.assertEqual(again.status_code, 200)
        # keep-a 未被淘汰，仍是原来的 Agent，序号继续累加
        self.assertEqual(len(created), 2)
        self.assertIn("第 2 条", again.json()["reply"])

    def test_overflow_falls_back_after_requests_finish(self):
        """全部活跃时允许暂时超限，请求结束后数据库回落到上限。"""
        gates = [threading.Event(), threading.Event()]
        started = [threading.Event(), threading.Event()]
        created = []

        def factory():
            index = len(created)
            agent = MemoryGateAgent(gates[index], started[index])
            created.append(agent)
            return agent

        store = SessionStore(self.db_path)
        app = create_app(agent_factory=factory, session_store=store)

        with mock.patch("agent.web_app.MAX_SESSIONS", 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    lambda: TestClient(app).post(
                        "/api/chat",
                        json={"message": "一", "session_id": "ov-1"},
                    )
                )
                self.assertTrue(started[0].wait(timeout=5))

                second = pool.submit(
                    lambda: TestClient(app).post(
                        "/api/chat",
                        json={"message": "二", "session_id": "ov-2"},
                    )
                )
                self.assertTrue(started[1].wait(timeout=5))

                gates[0].set()
                self.assertEqual(first.result(timeout=15).status_code, 200)
                gates[1].set()
                self.assertEqual(second.result(timeout=15).status_code, 200)

        self.assertLessEqual(self._count_rows(), 1)

class SecondCallGateAgent:
    """第一次正常返回，第二次阻塞的 Agent，用于测试活动会话保护。"""

    def __init__(self, gate, started):
        self.memory = Memory()
        self.gate = gate
        self.started = started
        self.calls = 0

    def run(self, user_input):
        self.calls += 1
        self.memory.add("user", user_input)
        if self.calls >= 2:
            self.started.set()
            self.gate.wait(timeout=10)
        reply = f"历史{len(self.memory.messages)}条"
        self.memory.add("assistant", reply)
        return reply


class MessageLimitTest(unittest.TestCase):
    """消息长度上限：4000 通过，4001 返回中文 400。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.store = SessionStore(self.db_path)
        self.client = TestClient(
            create_app(agent_factory=make_echo_agent, session_store=self.store)
        )

    def test_4000_chars_accepted(self):
        """正好 4000 个字符可以通过。"""
        response = self.client.post(
            "/api/chat",
            json={"message": "字" * 4000, "session_id": "len-4000"},
        )
        self.assertEqual(response.status_code, 200)

    def test_4001_chars_rejected(self):
        """4001 个字符返回 400，并提示允许的最大字符数。"""
        response = self.client.post(
            "/api/chat",
            json={"message": "字" * 4001, "session_id": "len-4001"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("4000", response.json()["detail"])

    def test_length_checked_after_strip(self):
        """首尾空白不计入长度。"""
        message = "  " + "字" * 4000 + "  "
        response = self.client.post(
            "/api/chat",
            json={"message": message, "session_id": "len-strip"},
        )
        self.assertEqual(response.status_code, 200)


class SessionDeleteTest(unittest.TestCase):
    """DELETE /api/session/{session_id} 的完整行为。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.db_path = Path(self.tempdir.name) / "sessions.db"
        self.store = SessionStore(self.db_path)

    def test_delete_then_revisit_is_fresh(self):
        """删除成功后同一 session_id 再访问是全新历史。"""
        client = TestClient(
            create_app(agent_factory=make_echo_agent, session_store=self.store)
        )
        first = client.post("/api/chat", json={"message": "一", "session_id": "del-1"})
        self.assertEqual(first.json()["reply"], "历史1条")

        deleted = client.delete("/api/session/del-1")
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(self.store.load("del-1"))

        again = client.post("/api/chat", json={"message": "二", "session_id": "del-1"})
        self.assertEqual(again.json()["reply"], "历史1条")

    def test_delete_missing_is_idempotent(self):
        """删除不存在的会话按成功处理。"""
        client = TestClient(
            create_app(agent_factory=make_echo_agent, session_store=self.store)
        )
        response = client.delete("/api/session/never-exists")
        self.assertEqual(response.status_code, 200)

    def test_delete_invalid_session_id(self):
        """非法编号返回中文 400。"""
        client = TestClient(
            create_app(agent_factory=make_echo_agent, session_store=self.store)
        )
        response = client.delete("/api/session/bad.id")
        self.assertEqual(response.status_code, 400)
        self.assertIn("会话编号不合法", response.json()["detail"])

    def test_delete_failure_returns_503_and_keeps_memory(self):
        """数据库删除失败时返回 503，内存上下文不能丢。"""
        created = []

        def factory():
            agent = FakeAgent()
            created.append(agent)
            return agent

        client = TestClient(create_app(agent_factory=factory, session_store=self.store))
        client.post("/api/chat", json={"message": "甲", "session_id": "del-fail"})

        with mock.patch.object(self.store, "delete", return_value=False):
            response = client.delete("/api/session/del-fail")

        self.assertEqual(response.status_code, 503)
        again = client.post("/api/chat", json={"message": "乙", "session_id": "del-fail"})
        self.assertEqual(len(created), 1)
        self.assertIn("第 2 条", again.json()["reply"])

    def test_delete_active_session_returns_409(self):
        """会话正在执行时返回 409，不删除数据库或内存数据。"""
        gate = threading.Event()
        started = threading.Event()
        agent = SecondCallGateAgent(gate, started)
        app = create_app(agent_factory=lambda: agent, session_store=self.store)
        client = TestClient(app)

        first = client.post("/api/chat", json={"message": "一", "session_id": "del-active"})
        self.assertEqual(first.json()["reply"], "历史1条")
        self.assertIsNotNone(self.store.load("del-active"))

        with ThreadPoolExecutor(max_workers=1) as pool:
            second = pool.submit(
                lambda: TestClient(app).post(
                    "/api/chat",
                    json={"message": "二", "session_id": "del-active"},
                )
            )
            self.assertTrue(started.wait(timeout=5))

            response = client.delete("/api/session/del-active")
            self.assertEqual(response.status_code, 409)
            self.assertIsNotNone(self.store.load("del-active"))

            gate.set()
            self.assertEqual(second.result(timeout=15).status_code, 200)

        # 内存会话未被删除，历史继续累加
        third = client.post("/api/chat", json={"message": "三", "session_id": "del-active"})
        self.assertEqual(third.json()["reply"], "历史5条")


class FrontendSessionControlsTest(unittest.TestCase):
    """页面上的清空按钮、确认提示与输入长度限制。"""

    def setUp(self):
        self.html = INDEX_FILE.read_text(encoding="utf-8")

    def test_textarea_has_maxlength(self):
        """输入框限制 4000 字符。"""
        self.assertIn('maxlength="4000"', self.html)

    def test_clear_button_uses_confirm_and_delete(self):
        """清空按钮有确认提示，并调用删除接口。"""
        self.assertIn("清空对话", self.html)
        self.assertIn("confirm(", self.html)
        self.assertIn("/api/session/", self.html)

    def test_new_session_does_not_delete_history(self):
        """新会话按钮只切换前端，不发删除请求。"""
        start = self.html.index("newSessionBtn.addEventListener")
        end = self.html.index("clearSessionBtn.addEventListener")
        new_session_block = self.html[start:end]
        self.assertNotIn("DELETE", new_session_block)