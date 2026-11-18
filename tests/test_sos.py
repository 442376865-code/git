"""独立 SOS 急救程序的单元测试。

覆盖：首页可访问、120 呼救入口、健康检查、关键急救引导、
无外部资源、不引用 Agent 或 LLM、导入 SOS 时不加载 agent 包。
全部离线执行，不访问网络。
"""

import subprocess
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from sos.web_app import INDEX_FILE, app

SOS_DIR = Path(__file__).resolve().parents[1] / "sos"
WEB_APP_FILE = SOS_DIR / "web_app.py"
PAGE_FILE = SOS_DIR / "static" / "index.html"


class SosPageTest(unittest.TestCase):
    """SOS 页面内容与关键元素。"""

    def setUp(self):
        self.client = TestClient(app)

    def test_index_available(self):
        """首页返回 HTML。"""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])

    def test_index_has_immediate_call_action(self):
        """首屏必须突出“立即拨打 120”，并提供 tel:120 链接。"""
        html = self.client.get("/").text
        self.assertIn("立即拨打 120", html)
        self.assertIn('href="tel:120"', html)

    def test_health(self):
        """健康检查返回 ok。"""
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_page_contains_key_guidance(self):
        """页面包含急救流程与紧急情况识别要点。"""
        html = self.client.get("/").text
        keywords = [
            "确认现场安全",
            "判断意识与呼吸",
            "立即呼救",
            "安全体位",
            "120 调度员",
            "意识不清",
            "呼吸异常",
            "胸痛",
            "卒中",
            "严重出血",
            "气道异物",
        ]
        for keyword in keywords:
            with self.subTest(keyword=keyword):
                self.assertIn(keyword, html)

    def test_page_contains_disclaimer(self):
        """页面明确说明不替代专业急救，不提供诊断、药名、剂量。"""
        html = self.client.get("/").text
        self.assertIn("不能替代专业急救", html)
        self.assertIn("不提供诊断", html)
        self.assertIn("药名", html)
        self.assertIn("剂量", html)

    def test_page_has_no_external_resources(self):
        """页面不得引用任何外部 http/https 资源或外部脚本。"""
        html = self.client.get("/").text
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<link", html)
        self.assertNotIn("@import", html)

    def test_page_does_not_mention_agent_or_llm(self):
        """页面内容不得出现 Agent 或 LLM 相关字样。"""
        html = PAGE_FILE.read_text(encoding="utf-8")
        for word in ("Agent", "agent", "LLM", "llm", "call_llm"):
            with self.subTest(word=word):
                self.assertNotIn(word, html)

    def test_index_file_exists(self):
        """静态页面文件真实存在，避免部署后 500。"""
        self.assertTrue(INDEX_FILE.is_file())


class SosIsolationTest(unittest.TestCase):
    """SOS 应用与主控 Agent 完全隔离。"""

    def test_web_app_does_not_import_agent(self):
        """web_app.py 不得导入 agent 包或路由、模型相关模块。"""
        source = WEB_APP_FILE.read_text(encoding="utf-8")
        forbidden = [
            "from agent",
            "import agent",
            "call_llm",
            "Router",
            "router",
            "openai",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_importing_sos_does_not_load_agent_package(self):
        """在全新解释器中导入 SOS，不应把 agent 包加载进内存。"""
        code = (
            "import sys, sos.web_app; "
            "print('agent' in sys.modules, 'agent.llm' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "False False")

    def test_sos_uses_fastapi_app(self):
        """SOS 使用独立的 FastAPI 应用实例。"""
        self.assertEqual(app.title, "SOS 急救指引")
        paths = {route.path for route in app.routes}
        self.assertIn("/", paths)
        self.assertIn("/api/health", paths)