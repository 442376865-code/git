"""LLM 客户端重试与退避的单元测试。

覆盖：首次成功、重试后成功、重试耗尽、不可重试状态码、
超时与连接错误、指数退避、响应正文不外泄、非法 JSON、接口兼容。
全部 mock 网络与 sleep，不访问网络、不真实等待。
"""

import inspect
import io
import json
import unittest
import urllib.error
from http.client import IncompleteRead
from unittest import mock

from agent.llm import (
    DEFAULT_BACKOFF,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT,
    RETRYABLE_STATUS,
    LLMClient,
    call_llm,
)

SECRET_BODY = b"vendor-secret-body"


def http_error(code, body=SECRET_BODY):
    """构造带响应正文的 HTTPError，用于验证正文不会出现在异常里。"""
    return urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions",
        code,
        "error",
        {},
        io.BytesIO(body),
    )


class FakeResponse:
    """模拟 urlopen 返回的上下文管理器。"""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def ok_response(text="ok"):
    payload = {"choices": [{"message": {"role": "assistant", "content": text}}]}
    return FakeResponse(json.dumps(payload).encode("utf-8"))


class BrokenReadResponse(FakeResponse):
    """模拟 response.read() 时抛异常的响应对象。"""

    def __init__(self, error):
        self._error = error

    def read(self):
        raise self._error


class LLMClientRetryTest(unittest.TestCase):
    """重试策略与错误转换。"""

    def setUp(self):
        self.client = LLMClient(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test-model",
            timeout=5,
            max_attempts=2,
            backoff=1.0,
        )
        sleep_patch = mock.patch("agent.llm.time.sleep")
        self.sleep = sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def urlopen(self, **kwargs):
        """替换网络调用，返回 mock 以便断言调用次数。"""
        patcher = mock.patch("agent.llm.urllib.request.urlopen", **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_first_attempt_success(self):
        """首次成功不重试、不等待。"""
        mocked = self.urlopen(return_value=ok_response("你好"))
        message = self.client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(message["content"], "你好")
        self.assertEqual(mocked.call_count, 1)
        self.sleep.assert_not_called()

    def test_retry_then_success(self):
        """临时故障后重试成功。"""
        mocked = self.urlopen(side_effect=[http_error(503), ok_response("恢复")])
        message = self.client.chat([])
        self.assertEqual(message["content"], "恢复")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.sleep.call_args_list[0].args, (1.0,))

    def test_retry_exhausted(self):
        """两次临时故障后返回中文错误，且只等待一次。"""
        mocked = self.urlopen(side_effect=[http_error(503), http_error(502)])
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("暂时不可用", str(ctx.exception))
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_non_retryable_statuses(self):
        """400/401/403 不重试，直接返回中文错误。"""
        for code in (400, 401, 403):
            with self.subTest(code=code):
                mocked = self.urlopen(side_effect=http_error(code))
                with self.assertRaises(RuntimeError) as ctx:
                    self.client.chat([])
                self.assertIn("被拒绝", str(ctx.exception))
                self.assertEqual(mocked.call_count, 1)
                self.sleep.assert_not_called()

    def test_response_body_not_leaked(self):
        """异常信息里不能出现供应商响应正文。"""
        self.urlopen(side_effect=http_error(500, SECRET_BODY))
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertNotIn(SECRET_BODY.decode("utf-8"), str(ctx.exception))

    def test_timeout_is_retried(self):
        """直接抛出的 TimeoutError 属于临时故障，会重试。"""
        mocked = self.urlopen(side_effect=[TimeoutError("timed out"), ok_response("恢复")])
        message = self.client.chat([])
        self.assertEqual(message["content"], "恢复")
        self.assertEqual(mocked.call_count, 2)

    def test_timeout_exhausted(self):
        """两次超时后返回中文超时提示。"""
        self.urlopen(side_effect=[TimeoutError("t1"), TimeoutError("t2")])
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("超时", str(ctx.exception))

    def test_urlerror_with_timeout_reason(self):
        """URLError 包裹的超时按超时处理。"""
        error = urllib.error.URLError(TimeoutError("slow"))
        self.urlopen(side_effect=[error, error])
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("超时", str(ctx.exception))

    def test_connection_error_exhausted(self):
        """连接错误重试耗尽后返回中文连接失败提示。"""
        error = urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        mocked = self.urlopen(side_effect=[error, error])
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("无法连接", str(ctx.exception))
        self.assertEqual(mocked.call_count, 2)

    def test_exponential_backoff(self):
        """三次尝试的等待时间按 1、2 倍递增。"""
        client = LLMClient(
            api_key="test-key",
            timeout=5,
            max_attempts=3,
            backoff=1.0,
        )
        mocked = self.urlopen(
            side_effect=[http_error(503), http_error(503), http_error(503)]
        )
        with self.assertRaises(RuntimeError):
            client.chat([])
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.0, 2.0])

    def test_retryable_rate_limit_and_request_timeout(self):
        """408 与 429 也属于可重试的临时故障。"""
        for code in (408, 429):
            with self.subTest(code=code):
                mocked = self.urlopen(side_effect=[http_error(code), ok_response("ok")])
                self.assertEqual(self.client.chat([])["content"], "ok")
                self.assertEqual(mocked.call_count, 2)

    def test_invalid_json_not_retried(self):
        """响应不是合法 JSON 时不重试，转为中文错误。"""
        mocked = self.urlopen(return_value=FakeResponse(b"not-json"))
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("无法解析", str(ctx.exception))
        self.assertEqual(mocked.call_count, 1)

    def test_unexpected_payload_shape(self):
        """响应缺少 choices 时转为中文错误。"""
        self.urlopen(return_value=FakeResponse(b"{}"))
        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])
        self.assertIn("格式异常", str(ctx.exception))

    def test_connection_refused_directly_is_retried(self):
        """urlopen 直接抛 ConnectionRefusedError 时按次数重试，耗尽后统一 RuntimeError。"""
        error = ConnectionRefusedError(111, "refused")
        mocked = self.urlopen(side_effect=[error, error])

        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])

        self.assertEqual(type(ctx.exception), RuntimeError)
        self.assertIn("无法连接", str(ctx.exception))
        self.assertNotIn("refused", str(ctx.exception))
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_connection_reset_directly_is_retried(self):
        """urlopen 直接抛 ConnectionResetError 后重试成功。"""
        error = ConnectionResetError(104, "reset by peer")
        mocked = self.urlopen(side_effect=[error, ok_response("恢复")])

        message = self.client.chat([])

        self.assertEqual(message["content"], "恢复")
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_read_connection_reset_is_retried(self):
        """response.read() 抛 ConnectionResetError 时同样重试。"""
        error = ConnectionResetError(104, "reset during read")
        mocked = self.urlopen(
            side_effect=[BrokenReadResponse(error), ok_response("恢复")]
        )

        message = self.client.chat([])

        self.assertEqual(message["content"], "恢复")
        self.assertEqual(mocked.call_count, 2)

    def test_incomplete_read_is_wrapped(self):
        """response.read() 抛 IncompleteRead 时重试，耗尽后返回中文错误且不带原始异常。"""
        error = IncompleteRead(b"partial", 100)
        mocked = self.urlopen(
            side_effect=[BrokenReadResponse(error), BrokenReadResponse(error)]
        )

        with self.assertRaises(RuntimeError) as ctx:
            self.client.chat([])

        self.assertEqual(type(ctx.exception), RuntimeError)
        self.assertIn("无法连接", str(ctx.exception))
        self.assertNotIn("IncompleteRead", str(ctx.exception))
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)

class LLMInterfaceTest(unittest.TestCase):
    """默认参数与既有接口保持不变。"""

    def test_default_timeout_and_attempts(self):
        """默认单次超时 45 秒、最多 2 次尝试。"""
        client = LLMClient(api_key="test-key")
        self.assertEqual(DEFAULT_TIMEOUT, 45)
        self.assertEqual(DEFAULT_MAX_ATTEMPTS, 2)
        self.assertEqual(DEFAULT_BACKOFF, 1.0)
        self.assertEqual(client.timeout, 45)
        self.assertEqual(client.max_attempts, 2)

    def test_chat_signature_unchanged(self):
        """chat(messages, tools=None, temperature=0.7) 签名不变。"""
        params = list(inspect.signature(LLMClient.chat).parameters)
        self.assertEqual(params, ["self", "messages", "tools", "temperature"])

    def test_call_llm_signature_unchanged(self):
        """call_llm(messages, tools, temperature, client) 签名不变。"""
        params = list(inspect.signature(call_llm).parameters)
        self.assertEqual(params, ["messages", "tools", "temperature", "client"])

    def test_call_llm_uses_given_client(self):
        """call_llm 透传三条参数到指定客户端。"""
        fake = mock.Mock()
        fake.chat.return_value = {"content": "ok"}
        messages = [{"role": "user", "content": "hi"}]
        result = call_llm(messages, tools=None, temperature=0.2, client=fake)
        self.assertEqual(result, {"content": "ok"})
        fake.chat.assert_called_once_with(messages, tools=None, temperature=0.2)

    def test_missing_api_key_does_not_call_network(self):
        """缺少 API Key 时直接报中文错误，不发网络请求。"""
        client = LLMClient(api_key="test-key")
        client.api_key = ""
        with mock.patch("agent.llm.urllib.request.urlopen") as mocked:
            with self.assertRaises(RuntimeError) as ctx:
                client.chat([])
        self.assertIn("OPENAI_API_KEY", str(ctx.exception))
        mocked.assert_not_called()

    def test_retryable_status_set(self):
        """可重试状态码集合必须是临时故障集合。"""
        self.assertEqual(set(RETRYABLE_STATUS), {408, 429, 500, 502, 503, 504})

    def test_max_attempts_never_below_one(self):
        """尝试次数下限为 1，避免配置为 0 时完全不发请求。"""
        client = LLMClient(api_key="test-key", max_attempts=0)
        self.assertEqual(client.max_attempts, 1)


if __name__ == "__main__":
    unittest.main()