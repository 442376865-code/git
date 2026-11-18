import http.client
import json
import os
import time
import urllib.error
import urllib.request

# 可重试的 HTTP 状态码：都是临时故障，稍后重试可能恢复
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# 默认单次超时（秒），避免请求长时间卡住
DEFAULT_TIMEOUT = 45

# 默认最多尝试次数（含首次请求），避免无限重试
DEFAULT_MAX_ATTEMPTS = 2

# 默认退避基数（秒），第 n 次失败后等待 backoff * 2^(n-1)
DEFAULT_BACKOFF = 1.0


def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


class LLMClient:
    """统一的 LLM 调用客户端，带有限次重试与指数退避。"""

    def __init__(
        self,
        api_key=None,
        base_url=None,
        model=None,
        timeout=DEFAULT_TIMEOUT,
        max_attempts=DEFAULT_MAX_ATTEMPTS,
        backoff=DEFAULT_BACKOFF,
    ):
        """创建客户端。

        参数：
            api_key / base_url / model: 不传时从环境变量读取；
            timeout: 单次请求超时秒数，默认 45；
            max_attempts: 最多尝试次数（含首次），默认 2；
            backoff: 退避基数秒数，默认 1.0，按 1、2、4 倍递增。
        """
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.backoff = max(0.0, float(backoff))

    def chat(self, messages, tools=None, temperature=0.7):
        """发送一次对话请求，临时故障自动重试。

        返回助手消息字典；失败时统一抛简洁的中文 RuntimeError，
        不会把供应商返回的响应正文透传给调用方。
        """
        if not self.api_key:
            raise RuntimeError(
                "未配置 OPENAI_API_KEY，无法接入 ChatGPT。"
                "请设置环境变量 OPENAI_API_KEY（可选 OPENAI_BASE_URL、LLM_MODEL）后重试"
            )
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        for attempt in range(1, self.max_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in RETRYABLE_STATUS:
                    if attempt < self.max_attempts:
                        time.sleep(self._delay(attempt))
                        continue
                    raise RuntimeError("LLM 服务暂时不可用，请稍后再试。") from None
                # 400/401/403 等配置或权限问题，重试也没有意义
                raise RuntimeError("LLM 请求被拒绝，请检查接口配置。") from None
            except (TimeoutError, urllib.error.URLError) as e:
                reason = e.reason if isinstance(e, urllib.error.URLError) else e
                timed_out = isinstance(reason, TimeoutError)
                if attempt < self.max_attempts:
                    time.sleep(self._delay(attempt))
                    continue
                if timed_out:
                    raise RuntimeError("LLM 请求超时，请稍后再试。") from None
                raise RuntimeError("无法连接 LLM 服务，请检查网络后重试。") from None
            except (OSError, http.client.HTTPException):
                # urlopen 或 response.read() 直接抛出的底层网络异常，
                # 例如 ConnectionRefusedError、ConnectionResetError、IncompleteRead
                if attempt < self.max_attempts:
                    time.sleep(self._delay(attempt))
                    continue
                raise RuntimeError("无法连接 LLM 服务，请检查网络后重试。") from None
            except ValueError:
                # 返回内容不是合法 JSON，重试通常也不会变好
                raise RuntimeError("LLM 返回内容无法解析，请稍后再试。") from None
            else:
                return self._extract_message(data)

    def _delay(self, attempt):
        """计算第 attempt 次失败后的等待秒数（指数退避）。"""
        return self.backoff * (2 ** (attempt - 1))

    @staticmethod
    def _extract_message(data):
        """从响应中取助手消息；结构异常时转成中文错误。"""
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("LLM 返回格式异常，请稍后再试。") from None


_default_client = None


def get_default_client():
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def set_default_client(client):
    global _default_client
    _default_client = client


def call_llm(messages, tools=None, temperature=0.7, client=None):
    target = client if client is not None else get_default_client()
    return target.chat(messages, tools=tools, temperature=temperature)