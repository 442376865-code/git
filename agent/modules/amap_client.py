"""高德地图 Web 服务 API 客户端（仅关键字搜索）。

本期范围：
- 只实现 POI 2.0 关键字搜索（GET https://restapi.amap.com/v5/place/text）；
- 只返回当前搜索需要的最小字段，不保存、不缓存、不写数据库；
- 不实现周边搜索、地理编码、逆地理编码、路线规划、距离计算与地图渲染。

安全约束：
- Key 只从环境变量 AMAP_KEY 或构造参数读取，不写入日志、异常、返回值；
- 任何错误提示都是固定中文文案，绝不拼接请求 URL、查询参数或响应原文；
- 缺少 Key 时直接返回配置缺失，不发起任何网络请求。

医疗边界：
- 搜索结果只是“按关键词查到的地点”，没有用户坐标与距离依据，
  因此任何文案都不使用“最近”“离你多远”之类的表述。
"""

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

# 高德 POI 2.0 关键字搜索接口（只用 HTTPS）
AMAP_SEARCH_URL = "https://restapi.amap.com/v5/place/text"

# 请求超时：连接与读取合计不超过 5 秒
DEFAULT_TIMEOUT = 5.0

# 可重试的临时故障最多额外重试 1 次，总请求次数不超过 2 次
MAX_RETRIES = 1

# 参数上限（与高德官方文档一致）
KEYWORD_MAX_LEN = 80
CITY_MAX_LEN = 20
PAGE_MIN = 1
PAGE_MAX = 100
PAGE_SIZE_MIN = 1
PAGE_SIZE_MAX = 25

# types 参数（高德 POI 分类编码 typecode）：
# 官方要求每个编码为 6 位数字，多个编码用 "|" 分隔，取值参考官方 POI 分类码表。
TYPES_MAX_LEN = 120
TYPES_PATTERN = re.compile(r"^\d{6}(?:\|\d{6})*$")

# 数据来源标识，前端展示时必须与“用户手动保存的地点”区分开
DATA_SOURCE = "高德地图"

# 内部错误类型
ERROR_CONFIG_MISSING = "CONFIG_MISSING"
ERROR_AUTH_FAILED = "AUTH_FAILED"
ERROR_QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_NETWORK_ERROR = "NETWORK_ERROR"
ERROR_TIMEOUT = "TIMEOUT"
ERROR_HTTP_ERROR = "HTTP_ERROR"
ERROR_INVALID_RESPONSE = "INVALID_RESPONSE"
# 说明：20800～20803 是路径规划接口专用错误码，不属于“没有搜索结果”，
# 因此不映射到该类型；本常量预留给“请求成功但没有匹配结果”的语义，
# 当前用 success=True + 空 results 表达，不产生 NO_RESULTS 错误码。
ERROR_NO_RESULTS = "NO_RESULTS"
ERROR_CLIENT_CLOSED = "CLIENT_CLOSED"

# 高德 infocode 到内部错误类型的映射。
# 依据官方《错误码说明》：https://lbs.amap.com/api/webservice/guide/tools/info
# 只收录官方文档明确给出的编码，未收录的一律按 INVALID_RESPONSE 处理，避免猜测含义。
_INFOCODE_ERRORS = {
    # ---- 认证、权限与配置类：重试没有意义 ----
    "10001": (ERROR_AUTH_FAILED, "高德地图 Key 无效或已过期，请检查服务端配置。"),
    # 10002 官方含义是“没有权限使用相应的服务，或者请求接口的路径拼写错误”。
    # 前者是服务权限问题，后者是接口地址配置问题，两者都属于确定性配置错误，
    # 重试没有意义，因此归入 AUTH_FAILED，并在提示中同时给出两条排查方向。
    "10002": (ERROR_AUTH_FAILED, "高德地图无使用该服务的权限，或接口地址配置有误，请检查 Key 权限与服务地址。"),
    "10005": (ERROR_AUTH_FAILED, "当前服务器 IP 不在高德地图 Key 的白名单内。"),
    "10006": (ERROR_AUTH_FAILED, "高德地图 Key 的域名配置不匹配。"),
    "10007": (ERROR_AUTH_FAILED, "高德地图请求签名验证未通过。"),
    "10008": (ERROR_AUTH_FAILED, "高德地图安全码验证未通过。"),
    "10009": (ERROR_AUTH_FAILED, "高德地图 Key 与请求平台不匹配，请确认是 Web 服务类型。"),
    # 10012 官方含义是“权限不足，服务请求被拒绝”，没有限定一定是 Key 的问题
    "10012": (ERROR_AUTH_FAILED, "高德地图权限不足，服务请求被拒绝。"),
    "10013": (ERROR_AUTH_FAILED, "高德地图 Key 已被删除或回收。"),
    "10026": (ERROR_AUTH_FAILED, "高德地图账号处于封禁状态，暂时无法调用服务。"),
    "10041": (ERROR_AUTH_FAILED, "高德地图接口权限已过期，请检查 Key 权限。"),
    # 20011 官方含义是“查询区域在海外，但没有海外地图权限”
    "20011": (ERROR_AUTH_FAILED, "查询区域在海外，当前 Key 没有海外地图权限。"),
    # 说明：22000 不在官方错误码表中，属于历史猜测码，已移除；
    # 官方未收录的编码（如 300xx、40000～40003）统一走默认 INVALID_RESPONSE，
    # 保证“无法识别”时不会误报成认证失败、无结果等具体语义。
    # ---- 配额与频率类：重试同样没有意义 ----
    "10003": (ERROR_QUOTA_EXCEEDED, "高德地图今日调用次数已用完，请明天再试。"),
    "10004": (ERROR_QUOTA_EXCEEDED, "请求过于频繁，请稍后再试。"),
    "10010": (ERROR_QUOTA_EXCEEDED, "高德地图当前 IP 调用次数已超限。"),
    # 10014 官方含义是“云图服务 QPS 超限”，属于频率类
    "10014": (ERROR_QUOTA_EXCEEDED, "高德地图云图服务 QPS 超出限制，请稍后再试。"),
    "10015": (ERROR_QUOTA_EXCEEDED, "受单机限流限制，请求过于频繁，请稍后再试。"),
    "10019": (ERROR_QUOTA_EXCEEDED, "高德地图该服务总调用频率超出限制，请稍后再试。"),
    "10020": (ERROR_QUOTA_EXCEEDED, "该 Key 调用该服务的频率超出限制，请稍后再试。"),
    "10021": (ERROR_QUOTA_EXCEEDED, "账号调用该服务的频率超出限制，请稍后再试。"),
    # 10029 官方名称与描述不完全一致，但两种解释都属于调用量/频率限制，按配额类保守归类
    "10029": (ERROR_QUOTA_EXCEEDED, "高德地图调用频率超出限制，请稍后再试。"),
    "10044": (ERROR_QUOTA_EXCEEDED, "高德地图账号今日调用量已用完，请明天再试。"),
    "10045": (ERROR_QUOTA_EXCEEDED, "高德地图账号今日海外服务调用量已用完，请稍后再试。"),
    # ---- 服务能力与协议类 ----
    # 10011 官方含义是“服务不支持 HTTPS 请求”，属于协议/服务能力问题，不是 Key 问题
    "10011": (ERROR_HTTP_ERROR, "高德地图服务不支持当前的 HTTPS 请求方式。"),
    # 10016 是服务器负载过高，属于临时故障，允许重试一次
    "10016": (ERROR_NETWORK_ERROR, "高德地图服务繁忙，请稍后再试。"),
    "10017": (ERROR_HTTP_ERROR, "高德地图所请求的资源暂时不可用，请稍后再试。"),
    # ---- 参数与请求方式类 ----
    # 20000 是参数“值”不符合规范
    "20000": (ERROR_INVALID_REQUEST, "搜索参数的值不符合高德地图要求。"),
    "20001": (ERROR_INVALID_REQUEST, "缺少高德地图搜索所需的必填参数。"),
    # 20002 是请求协议/请求方式非法（例如只支持 GET 却用了 POST），不是参数值错误
    "20002": (ERROR_INVALID_REQUEST, "请求方式不符合高德地图接口要求。"),
    "20003": (ERROR_INVALID_RESPONSE, "高德地图返回了未知错误，请稍后再试。"),
    # 20012 是查询内容非法，搜索接口可能出现
    "20012": (ERROR_INVALID_REQUEST, "搜索关键词包含高德地图不接受的内容，请修改后重试。"),
    # ---- 路径规划专用错误码 ----
    # 20800～20803 只属于路径规划接口（规划点不在陆地范围、附近无路、路线计算失败、起终点过远）。
    # 当前调用的是 POI 关键字搜索，出现这些码说明上游返回异常，
    # 按 INVALID_RESPONSE 处理；绝不能误报成“没有找到地点”。
    "20800": (ERROR_INVALID_RESPONSE, "高德地图返回了与地点搜索不匹配的错误，请稍后再试。"),
    "20801": (ERROR_INVALID_RESPONSE, "高德地图返回了与地点搜索不匹配的错误，请稍后再试。"),
    "20802": (ERROR_INVALID_RESPONSE, "高德地图返回了与地点搜索不匹配的错误，请稍后再试。"),
    "20803": (ERROR_INVALID_RESPONSE, "高德地图返回了与地点搜索不匹配的错误，请稍后再试。"),
}

# 高德 type 到本系统可用类别的映射，顺序敏感（先匹配更具体的类型）
_CATEGORY_RULES = (
    ("卫生院", "卫生院"),
    ("社区卫生", "卫生院"),
    ("药房", "药房"),
    ("药店", "药房"),
    ("医药保健销售", "药房"),
    ("诊所", "诊所"),
    ("卫生所", "诊所"),
    ("门诊", "诊所"),
    ("医院", "医院"),
)


def _failure(error_code, message):
    """构造失败结果。"""
    return {
        "success": False,
        "results": [],
        "count": 0,
        "message": message,
        "error_code": error_code,
    }


def _success(results, count, message):
    """构造成功结果。"""
    return {
        "success": True,
        "results": results,
        "count": count,
        "message": message,
        "error_code": None,
    }


def _clean_text(value):
    """清洗高德返回的文本字段。

    高德对空字段常返回空数组 [] 而不是空字符串，这里只接受非空字符串，
    其余情况（包括 []、None、数字）一律返回 None。
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _to_count(value, fallback):
    """把高德返回的 count 字符串转成非负整数，失败时使用兜底值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if number >= 0 else fallback


def _map_category(amap_type):
    """把高德 type 映射为本系统类别，无法判断时归入“其他”。"""
    if not amap_type:
        return "其他"
    for keyword, category in _CATEGORY_RULES:
        if keyword in amap_type:
            return category
    return "其他"


def _normalize_location(value):
    """把“经度,纬度”字符串规范化为保留 6 位小数；无法解析时返回 None。"""
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        longitude = float(parts[0])
        latitude = float(parts[1])
    except ValueError:
        return None
    return f"{longitude:.6f},{latitude:.6f}"


def _extract_phone(poi):
    """从 business.tel 取联系电话；高德未提供时返回 None。"""
    business = poi.get("business")
    if not isinstance(business, dict):
        return None
    return _clean_text(business.get("tel"))


def _normalize_poi(poi):
    """把单个 POI 规范化成前端需要的字段；缺少编号或名称时丢弃。"""
    poi_id = _clean_text(poi.get("id"))
    name = _clean_text(poi.get("name"))
    if not poi_id or not name:
        return None
    return {
        "amap_poi_id": poi_id,
        "name": name,
        "category": _map_category(_clean_text(poi.get("type")) or ""),
        "address": _clean_text(poi.get("address")),
        "phone": _extract_phone(poi),
        "location": _normalize_location(poi.get("location")),
        "city": _clean_text(poi.get("cityname")),
        "district": _clean_text(poi.get("adname")),
        "data_source": DATA_SOURCE,
    }

class AmapClient:
    """高德地图 Web 服务客户端，当前只提供关键字搜索。

    说明：
        - 只读取环境变量 AMAP_KEY，或由调用方显式注入；
        - 注入的 HTTP 客户端由本对象负责关闭，调用方不要再重复使用；
        - 本类不保存任何搜索结果，返回的数据只在本次调用中有效。
    """

    def __init__(self, api_key=None, client=None, transport=None, timeout=DEFAULT_TIMEOUT):
        """创建客户端。

        参数：
            api_key: 测试时可显式传入；不传时从环境变量 AMAP_KEY 读取。
            client: 可选的 httpx.Client 实例（测试或复用连接时使用）。
            transport: 可选的 httpx transport（例如 MockTransport），
                       只在未注入 client 时使用。
            timeout: 自建客户端时的总超时，默认 5 秒。
        """
        raw_key = api_key if api_key is not None else os.getenv("AMAP_KEY", "")
        # Key 只保存在私有属性里，不提供任何读取接口，也不参与日志与异常
        self._api_key = raw_key.strip() if isinstance(raw_key, str) else ""

        if client is not None:
            self._client = client
        else:
            self._client = httpx.Client(transport=transport, timeout=timeout)

    def __enter__(self):
        """支持 with 语句。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """退出 with 时关闭连接。"""
        self.close()
        return False

    def close(self):
        """关闭底层 HTTP 客户端；重复调用安全。"""
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.close()
        except Exception:  # 关闭失败不影响调用方，只记一条不含敏感信息的日志
            logger.warning("关闭高德地图 HTTP 客户端时出错")

    def search_places(
        self, keyword, city=None, page=1, page_size=10, citylimit=True, types=None
    ):
        """按关键词搜索医疗相关地点。

        参数：
            keyword: 搜索关键词，1～80 个字符，例如“医院”“药店”“某医院”。
            city: 可选城市名；传入时按 citylimit 决定是否严格限制在该城市内。
            page: 页码，从 1 开始，最大 100。
            page_size: 每页条数，1～25。
            citylimit: 是否把结果严格限制在 city 范围内，默认 True。
            types: 可选高德 POI 分类编码（6 位数字），多个用“|”分隔；
                   取值参考官方 POI 分类码表，不传时不限制地点类型。

        返回：
            dict：包含 success、results、count、message、error_code 五个键。
            参数非法、Key 缺失、网络异常等都不会抛异常。
        """
        if self._client is None:
            return _failure(ERROR_CLIENT_CLOSED, "高德地图客户端已关闭，请重新创建。")

        if not self._api_key:
            # 配置缺失时直接返回，绝不发起请求
            return _failure(
                ERROR_CONFIG_MISSING,
                "未配置高德地图 Web 服务 Key，暂时无法搜索实时地点。",
            )

        keyword_text = keyword.strip() if isinstance(keyword, str) else ""
        if not keyword_text or len(keyword_text) > KEYWORD_MAX_LEN:
            return _failure(
                ERROR_INVALID_REQUEST,
                f"搜索关键词应为 1～{KEYWORD_MAX_LEN} 个字符。",
            )

        city_text = None
        if city is not None:
            city_text = city.strip() if isinstance(city, str) else ""
            if not city_text or len(city_text) > CITY_MAX_LEN:
                return _failure(
                    ERROR_INVALID_REQUEST,
                    f"城市名称应为 1～{CITY_MAX_LEN} 个字符。",
                )

        if not isinstance(citylimit, bool):
            return _failure(ERROR_INVALID_REQUEST, "citylimit 必须是布尔值。")

        types_text = None
        if types is not None:
            types_text = types.strip() if isinstance(types, str) else ""
            if not types_text or len(types_text) > TYPES_MAX_LEN:
                return _failure(
                    ERROR_INVALID_REQUEST,
                    f"types 应为高德 POI 分类编码，多个用 | 分隔，总长度不超过 {TYPES_MAX_LEN} 个字符。",
                )
            if not TYPES_PATTERN.match(types_text):
                return _failure(
                    ERROR_INVALID_REQUEST,
                    "types 必须是 6 位数字的高德 POI 分类编码，多个用 | 分隔。",
                )

        if isinstance(page, bool) or not isinstance(page, int):
            return _failure(ERROR_INVALID_REQUEST, f"page 应为 {PAGE_MIN}～{PAGE_MAX} 之间的整数。")
        if not PAGE_MIN <= page <= PAGE_MAX:
            return _failure(ERROR_INVALID_REQUEST, f"page 应为 {PAGE_MIN}～{PAGE_MAX} 之间的整数。")

        if isinstance(page_size, bool) or not isinstance(page_size, int):
            return _failure(
                ERROR_INVALID_REQUEST,
                f"page_size 应为 {PAGE_SIZE_MIN}～{PAGE_SIZE_MAX} 之间的整数。",
            )
        if not PAGE_SIZE_MIN <= page_size <= PAGE_SIZE_MAX:
            return _failure(
                ERROR_INVALID_REQUEST,
                f"page_size 应为 {PAGE_SIZE_MIN}～{PAGE_SIZE_MAX} 之间的整数。",
            )

        # 只请求当前搜索需要的字段：business 仅用于取联系电话
        params = {
            "key": self._api_key,
            "keywords": keyword_text,
            "page_size": page_size,
            "page_num": page,
            "show_fields": "business",
        }
        if city_text:
            params["region"] = city_text
            # city_limit 只在指定城市时才有意义；默认严格限制在指定城市内
            params["city_limit"] = "true" if citylimit else "false"
        if types_text:
            params["types"] = types_text

        data = None
        last_error = (ERROR_NETWORK_ERROR, "无法获取实时地点，请稍后再试。")
        for attempt in range(MAX_RETRIES + 1):
            data, error_code, message, retryable = self._request_once(params)
            if data is not None:
                break
            last_error = (error_code, message)
            # 认证、配额、参数错误不重试；临时故障最多重试一次
            if not retryable or attempt >= MAX_RETRIES:
                break

        if data is None:
            # 只记录内部错误类型，绝不记录请求参数与 Key
            logger.warning("高德地图地点搜索失败：%s", last_error[0])
            return _failure(*last_error)

        pois = data.get("pois")
        if not isinstance(pois, list):
            pois = []

        results = []
        for poi in pois:
            if not isinstance(poi, dict):
                continue
            normalized = _normalize_poi(poi)
            if normalized is not None:
                results.append(normalized)

        count = _to_count(data.get("count"), len(results))
        if results:
            message = f"找到 {len(results)} 个地点。"
        else:
            message = "没有找到匹配的地点。"
        return _success(results, count, message)

    def _request_once(self, params):
        """执行一次请求。

        返回 (数据, 错误类型, 提示, 是否可重试)：
        - 成功时数据为 dict，其余三项为 None / None / False；
        - 失败时数据为 None，由调用方决定是否重试。
        """
        try:
            response = self._client.get(AMAP_SEARCH_URL, params=params)
        except httpx.TimeoutException:
            return None, ERROR_TIMEOUT, "高德地图服务响应超时，请稍后再试。", True
        except httpx.RequestError:
            return None, ERROR_NETWORK_ERROR, "无法连接高德地图服务，请检查网络后重试。", True

        if response.status_code != 200:
            if response.status_code >= 500:
                return None, ERROR_HTTP_ERROR, "高德地图服务暂时不可用，请稍后再试。", True
            return None, ERROR_HTTP_ERROR, "高德地图服务返回异常状态，请稍后再试。", False

        try:
            data = response.json()
        except ValueError:
            # 响应不是合法 JSON，同样不把响应内容带进提示
            return None, ERROR_INVALID_RESPONSE, "高德地图返回的数据无法解析。", False

        if not isinstance(data, dict):
            return None, ERROR_INVALID_RESPONSE, "高德地图返回的数据格式不正确。", False

        # 不能只看 HTTP 200，必须同时判断业务状态
        if str(data.get("status", "")).strip() != "1":
            infocode = str(data.get("infocode", "")).strip()
            code, message = _INFOCODE_ERRORS.get(
                infocode,
                (ERROR_INVALID_RESPONSE, "高德地图返回了无法识别的结果。"),
            )
            # 10016 是高德服务繁忙，属于临时故障，允许重试一次
            return None, code, message, infocode == "10016"

        return data, None, None, False