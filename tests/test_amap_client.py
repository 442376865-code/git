"""高德地图 Web 服务客户端（仅关键字搜索）的单元测试。

全部使用 httpx.MockTransport 模拟响应，离线执行：
不访问高德、不读取 .env、不使用真实 Key、不写数据库、不缓存数据。
"""

import json
import os
import unittest
from unittest import mock

import httpx

from agent.modules.amap_client import AmapClient

# 仅用于测试的假 Key，不是真实密钥
TEST_KEY = "test-amap-key-for-unit-test"


def default_poi(**overrides):
    """构造一个典型的高德 POI 响应条目。"""
    poi = {
        "id": "B0FFTEST001",
        "name": "杭州市第一人民医院",
        "type": "医疗保健服务;综合医院;综合医院",
        "typecode": "090101",
        "address": "浣纱路 261 号",
        "location": "120.164902,30.253000",
        "pname": "浙江省",
        "cityname": "杭州市",
        "adname": "上城区",
        "business": {"tel": "0571-56005600"},
    }
    poi.update(overrides)
    return poi


def success_payload(pois=None, count=None):
    """构造高德成功响应。"""
    if pois is None:
        pois = [default_poi()]
    if count is None:
        count = str(len(pois))
    return {
        "status": "1",
        "info": "OK",
        "infocode": "10000",
        "count": count,
        "pois": pois,
    }


def error_payload(infocode, info="错误", status="0", count="0"):
    """构造高德业务错误响应。"""
    return {
        "status": status,
        "info": info,
        "infocode": infocode,
        "count": count,
        "pois": [],
    }


def json_response(payload, status_code=200):
    """构造 JSON 响应。"""
    return httpx.Response(status_code, json=payload)


class Handler:
    """记录请求并按顺序返回预设响应。"""

    def __init__(self, response=None, exception=None, responses=None):
        self.requests = []
        self._response = response
        self._exception = exception
        self._responses = list(responses or [])

    def __call__(self, request):
        self.requests.append(request)
        if self._responses:
            item = self._responses.pop(0)
        elif self._exception is not None:
            item = self._exception
        else:
            item = self._response
        if isinstance(item, Exception):
            raise item
        return item


class AmapClientTestBase(unittest.TestCase):
    """创建带 MockTransport 的客户端。"""

    def make_client(self, response=None, exception=None, responses=None, api_key=TEST_KEY):
        self.handler = Handler(
            response=response, exception=exception, responses=responses
        )
        client = AmapClient(
            api_key=api_key, transport=httpx.MockTransport(self.handler)
        )
        self.addCleanup(client.close)
        return client

    def params(self, index=0):
        """返回第 index 次请求的查询参数。"""
        return self.handler.requests[index].url.params


class SearchSuccessTest(AmapClientTestBase):
    """正常搜索与参数传递。"""

    def test_search_success(self):
        """正常返回成功结构。"""
        client = self.make_client(response=json_response(success_payload()))
        result = client.search_places("医院")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(len(result["results"]), 1)
        self.assertIsNone(result["error_code"])
        self.assertIn("1", result["message"])

    def test_city_passed_as_region(self):
        """指定城市时以 region 传参。"""
        client = self.make_client(response=json_response(success_payload()))
        client.search_places("医院", city="杭州市")
        self.assertEqual(self.params()["region"], "杭州市")

    def test_city_limit_true(self):
        """指定城市时同时传 city_limit=true。"""
        client = self.make_client(response=json_response(success_payload()))
        client.search_places("医院", city="杭州市")
        self.assertEqual(self.params()["city_limit"], "true")

    def test_no_region_without_city(self):
        """未指定城市时不传 region 与 city_limit。"""
        client = self.make_client(response=json_response(success_payload()))
        client.search_places("医院")
        params = self.params()
        self.assertNotIn("region", params)
        self.assertNotIn("city_limit", params)

    def test_request_uses_official_parameters(self):
        """请求方法与参数符合高德 POI 2.0 关键字搜索。"""
        client = self.make_client(response=json_response(success_payload()))
        client.search_places("药店", page=2, page_size=5)
        request = self.handler.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.scheme, "https")
        self.assertEqual(request.url.host, "restapi.amap.com")
        self.assertEqual(request.url.path, "/v5/place/text")
        params = self.params()
        self.assertEqual(params["keywords"], "药店")
        self.assertEqual(params["page_num"], "2")
        self.assertEqual(params["page_size"], "5")
        self.assertEqual(params["show_fields"], "business")
        self.assertEqual(params["key"], TEST_KEY)

    def test_poi_fields_normalized(self):
        """POI 只保留约定字段并完成规范化。"""
        client = self.make_client(response=json_response(success_payload()))
        poi = client.search_places("医院")["results"][0]
        self.assertEqual(poi["amap_poi_id"], "B0FFTEST001")
        self.assertEqual(poi["name"], "杭州市第一人民医院")
        self.assertEqual(poi["category"], "医院")
        self.assertEqual(poi["address"], "浣纱路 261 号")
        self.assertEqual(poi["phone"], "0571-56005600")
        self.assertEqual(poi["location"], "120.164902,30.253000")
        self.assertEqual(poi["city"], "杭州市")
        self.assertEqual(poi["district"], "上城区")
        self.assertEqual(poi["data_source"], "高德地图")
        self.assertEqual(
            set(poi),
            {
                "amap_poi_id",
                "name",
                "category",
                "address",
                "phone",
                "location",
                "city",
                "district",
                "data_source",
            },
        )

    def test_poi_category_mapping(self):
        """高德 type 映射为可用类别，无法判断时归入其他。"""
        pois = [
            default_poi(id="1", name="甲", type="医疗保健服务;综合医院;综合医院"),
            default_poi(id="2", name="乙", type="购物服务;医药保健销售店;药房"),
            default_poi(id="3", name="丙", type="医疗保健服务;诊所;诊所"),
            default_poi(id="4", name="丁", type="医疗保健服务;卫生院;卫生院"),
            default_poi(id="5", name="戊", type="生活服务;其他;其他"),
        ]
        client = self.make_client(response=json_response(success_payload(pois=pois)))
        categories = [item["category"] for item in client.search_places("医疗")["results"]]
        self.assertEqual(categories, ["医院", "药房", "诊所", "卫生院", "其他"])

    def test_missing_location_returns_none(self):
        """无坐标或坐标格式异常时 location 为 None。"""
        pois = [
            default_poi(id="1", name="甲", location=[]),
            default_poi(id="2", name="乙", location="bad"),
            default_poi(id="3", name="丙", location="120.1,30.2,40.3"),
        ]
        client = self.make_client(response=json_response(success_payload(pois=pois)))
        results = client.search_places("医院")["results"]
        self.assertEqual([item["location"] for item in results], [None, None, None])

    def test_empty_fields_become_none(self):
        """高德返回空数组的字段统一转成 None。"""
        poi = default_poi(address=[], cityname=[], adname=[], business=[])
        client = self.make_client(
            response=json_response(success_payload(pois=[poi]))
        )
        result = client.search_places("医院")["results"][0]
        self.assertIsNone(result["address"])
        self.assertIsNone(result["phone"])
        self.assertIsNone(result["city"])
        self.assertIsNone(result["district"])

    def test_empty_result(self):
        """高德返回 0 条时属于正常结果，不是错误。"""
        payload = success_payload(pois=[], count="0")
        client = self.make_client(response=json_response(payload))
        result = client.search_places("不存在的医院")
        self.assertTrue(result["success"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["error_code"])
        self.assertIn("没有找到", result["message"])

    def test_poi_without_id_or_name_is_dropped(self):
        """缺少编号或名称的条目直接丢弃，不返回半成品数据。"""
        pois = [
            default_poi(id=[], name="无编号医院"),
            default_poi(id="2", name=[]),
            default_poi(id="3", name="正常医院"),
        ]
        client = self.make_client(response=json_response(success_payload(pois=pois)))
        results = client.search_places("医院")["results"]
        self.assertEqual([item["name"] for item in results], ["正常医院"])


class InputValidationTest(AmapClientTestBase):
    """参数校验：非法输入不发起请求。"""

    def test_missing_key_does_not_send_request(self):
        """环境变量为空时不请求网络。"""
        handler = Handler(response=json_response(success_payload()))
        with mock.patch.dict(os.environ, {"AMAP_KEY": ""}, clear=False):
            client = AmapClient(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CONFIG_MISSING")
        self.assertEqual(handler.requests, [])

    def test_blank_injected_key_does_not_send_request(self):
        """显式传入空白 Key 时同样视为未配置。"""
        client = self.make_client(response=json_response(success_payload()), api_key="   ")
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "CONFIG_MISSING")
        self.assertEqual(self.handler.requests, [])

    def test_invalid_keyword_rejected_without_request(self):
        """关键词为空、空白、超长或非文本时直接拒绝。"""
        client = self.make_client(response=json_response(success_payload()))
        for keyword in ("", "   ", "医" * 81, None, 123):
            with self.subTest(keyword=keyword):
                result = client.search_places(keyword)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.handler.requests, [])

    def test_keyword_at_max_length_accepted(self):
        """关键词恰好 80 字可以正常请求。"""
        client = self.make_client(response=json_response(success_payload()))
        result = client.search_places("医" * 80)
        self.assertTrue(result["success"])
        self.assertEqual(len(self.handler.requests), 1)

    def test_invalid_city_rejected(self):
        """城市为空或超长时直接拒绝。"""
        client = self.make_client(response=json_response(success_payload()))
        for city in ("", "   ", "城" * 21, 123):
            with self.subTest(city=city):
                result = client.search_places("医院", city=city)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.handler.requests, [])

    def test_invalid_page_rejected(self):
        """page 不是 1～100 的整数时拒绝。"""
        client = self.make_client(response=json_response(success_payload()))
        for page in (0, -1, 101, "1", 1.5, True, None):
            with self.subTest(page=page):
                result = client.search_places("医院", page=page)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.handler.requests, [])

    def test_invalid_page_size_rejected(self):
        """page_size 不是 1～25 的整数时拒绝。"""
        client = self.make_client(response=json_response(success_payload()))
        for size in (0, 26, -1, "10", 10.5, True, None):
            with self.subTest(page_size=size):
                result = client.search_places("医院", page_size=size)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertEqual(self.handler.requests, [])

class BusinessErrorMappingTest(AmapClientTestBase):
    """高德 status / infocode 的业务错误映射。"""

    def search_with(self, payload):
        """用给定响应体执行一次搜索。"""
        client = self.make_client(response=json_response(payload))
        return client.search_places("医院")

    def test_invalid_key_maps_to_auth_failed(self):
        """Key 无效（10001）映射为 AUTH_FAILED。"""
        result = self.search_with(error_payload("10001", "INVALID_USER_KEY"))
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertIn("Key", result["message"])

    def test_platform_mismatch_maps_to_auth_failed(self):
        """平台不匹配（10009）映射为 AUTH_FAILED，并提示应为 Web 服务类型。"""
        result = self.search_with(error_payload("10009", "USERKEY_PLAT_NOMATCH"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertIn("Web 服务", result["message"])

    def test_quota_exceeded_mapping(self):
        """日配额用尽（10003）映射为 QUOTA_EXCEEDED。"""
        result = self.search_with(error_payload("10003", "DAILY_QUERY_OVER_LIMIT"))
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_frequency_limit_mapping(self):
        """访问过于频繁（10004）同样映射为 QUOTA_EXCEEDED。"""
        result = self.search_with(error_payload("10004", "ACCESS_TOO_FREQUENT"))
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_invalid_params_mapping(self):
        """参数非法（20000）映射为 INVALID_REQUEST。"""
        result = self.search_with(error_payload("20000", "INVALID_PARAMS"))
        self.assertEqual(result["error_code"], "INVALID_REQUEST")

    def test_route_planning_error_codes_not_reported_as_no_results(self):
        """20800～20803 属于路径规划错误，不能当成“没有搜索结果”。"""
        for infocode in ("20800", "20801", "20802", "20803"):
            with self.subTest(infocode=infocode):
                result = self.search_with(error_payload(infocode, "ROUTE_ERROR"))
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_RESPONSE")
                self.assertNotIn("没有找到", result["message"])
                self.assertNotIn("未找到", result["message"])

    def test_service_not_available_maps_to_auth_failed(self):
        """10002 是权限不足或接口路径错误，映射为 AUTH_FAILED。"""
        result = self.search_with(error_payload("10002", "SERVICE_NOT_AVAILABLE"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertIn("权限", result["message"])

    def test_https_not_supported_maps_to_http_error(self):
        """10011 是服务不支持 HTTPS，不应描述成 Key 问题。"""
        result = self.search_with(error_payload("10011", "NOT_SUPPORT_HTTPS"))
        self.assertEqual(result["error_code"], "HTTP_ERROR")
        self.assertIn("HTTPS", result["message"])

    def test_resource_unavailable_maps_to_http_error(self):
        """10017 所请求资源不可用，映射为 HTTP_ERROR。"""
        result = self.search_with(error_payload("10017", "RESOURCE_UNAVAILABLE"))
        self.assertEqual(result["error_code"], "HTTP_ERROR")

    def test_insufficient_privileges_maps_to_auth_failed(self):
        """10012 是权限不足，映射为 AUTH_FAILED，提示不应提到 HTTPS。"""
        result = self.search_with(error_payload("10012", "INSUFFICIENT_PRIVILEGES"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertIn("权限", result["message"])
        self.assertNotIn("HTTPS", result["message"])

    def test_missing_required_params_mapping(self):
        """20001 缺少必填参数，提示中明确说明是缺参数。"""
        result = self.search_with(error_payload("20001", "MISSING_REQUIRED_PARAMS"))
        self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertIn("必填", result["message"])

    def test_illegal_request_not_described_as_param_value_error(self):
        """20002 是请求协议非法，不能描述成参数值错误。"""
        result = self.search_with(error_payload("20002", "ILLEGAL_REQUEST"))
        self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertIn("请求方式", result["message"])
        self.assertNotIn("参数的值", result["message"])

    def test_illegal_content_maps_to_invalid_request(self):
        """20012 查询内容非法映射为 INVALID_REQUEST。"""
        result = self.search_with(error_payload("20012", "ILLEGAL_CONTENT"))
        self.assertEqual(result["error_code"], "INVALID_REQUEST")

    def test_unknown_error_code_maps_to_invalid_response(self):
        """20003 未知错误按 INVALID_RESPONSE 处理。"""
        result = self.search_with(error_payload("20003", "UNKNOWN_ERROR"))
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_qps_limit_codes_map_to_quota(self):
        """10019/10020/10021 都是 QPS 超限，归入配额类。"""
        for infocode in ("10019", "10020", "10021"):
            with self.subTest(infocode=infocode):
                result = self.search_with(error_payload(infocode, "QPS_LIMIT"))
                self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_account_daily_limit_maps_to_quota(self):
        """10044 账号维度日调用量超限归入配额类。"""
        result = self.search_with(
            error_payload("10044", "USER_DAILY_QUERY_OVER_LIMIT")
        )
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_account_banned_maps_to_auth_failed(self):
        """10026 账号被封禁映射为 AUTH_FAILED。"""
        result = self.search_with(error_payload("10026", "INVALID_REQUEST"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")

    def test_interface_privilege_expired_maps_to_auth_failed(self):
        """10041 接口权限过期映射为 AUTH_FAILED。"""
        result = self.search_with(error_payload("10041", "NO_EFFECTIVE_INTERFACE"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")

    def test_status_zero_with_unknown_infocode(self):
        """status=0 且 infocode 未知时按 INVALID_RESPONSE 处理，不猜测含义。"""
        result = self.search_with(error_payload("99999", "UNKNOWN"))
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_missing_infocode_is_safe(self):
        """缺少 infocode 时不抛异常，返回通用错误。"""
        payload = {"status": "0", "info": "", "pois": []}
        result = self.search_with(payload)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_http_200_but_status_missing(self):
        """HTTP 200 但缺少 status 时不能当成成功。"""
        result = self.search_with({"pois": [default_poi()]})
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_undocumented_22000_falls_back_to_invalid_response(self):
        """22000 不在官方错误码表中，不能猜成 AUTH_FAILED。"""
        result = self.search_with(error_payload("22000", "UNKNOWN_CODE"))
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_ip_query_over_limit_maps_to_quota(self):
        """10010 是 IP 访问超限，归入配额类。"""
        result = self.search_with(error_payload("10010", "IP_QUERY_OVER_LIMIT"))
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_qps_has_exceeded_maps_to_quota_and_mentions_qps(self):
        """10014 是云图服务 QPS 超限，提示中应明确 QPS。"""
        result = self.search_with(error_payload("10014", "QPS_HAS_EXCEEDED_THE_LIMIT"))
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")
        self.assertIn("QPS", result["message"])

    def test_gateway_timeout_limit_maps_to_quota(self):
        """10015 是受单机 QPS 限流，归入配额类。"""
        result = self.search_with(error_payload("10015", "GATEWAY_TIMEOUT"))
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")

    def test_user_key_recycled_maps_to_auth_failed(self):
        """10013 是 Key 被删除，映射为 AUTH_FAILED。"""
        result = self.search_with(error_payload("10013", "USER_KEY_RECYCLED"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")

    def test_abroad_privilege_maps_to_auth_failed_and_mentions_abroad(self):
        """20011 是海外无权限，提示中应明确说明海外。"""
        result = self.search_with(error_payload("20011", "INSUFFICIENT_ABROAD_PRIVILEGES"))
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertIn("海外", result["message"])

    def test_unmapped_official_codes_fall_back_safely(self):
        """300xx 与 40000～40003 未做专门映射时按安全通用分类处理。"""
        for infocode in ("30001", "40000", "40002", "40003"):
            with self.subTest(infocode=infocode):
                result = self.search_with(error_payload(infocode, "UNMAPPED"))
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "INVALID_RESPONSE")
                self.assertNotIn("没有找到", result["message"])


class TransportErrorTest(AmapClientTestBase):
    """网络、超时、HTTP 状态与响应格式错误。"""

    def test_network_error(self):
        """连接失败映射为 NETWORK_ERROR，并重试一次。"""
        client = self.make_client(exception=httpx.ConnectError("连接失败"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "NETWORK_ERROR")
        self.assertEqual(len(self.handler.requests), 2)

    def test_timeout(self):
        """请求超时映射为 TIMEOUT，并重试一次。"""
        client = self.make_client(exception=httpx.ReadTimeout("超时"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "TIMEOUT")
        self.assertEqual(len(self.handler.requests), 2)

    def test_http_server_error(self):
        """HTTP 5xx 映射为 HTTP_ERROR，并重试一次。"""
        client = self.make_client(response=httpx.Response(503, text="busy"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "HTTP_ERROR")
        self.assertEqual(len(self.handler.requests), 2)

    def test_http_client_error_not_retried(self):
        """HTTP 4xx 属于确定性失败，不重试。"""
        client = self.make_client(response=httpx.Response(403, text="forbidden"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "HTTP_ERROR")
        self.assertEqual(len(self.handler.requests), 1)

    def test_invalid_json(self):
        """响应不是合法 JSON 时返回 INVALID_RESPONSE。"""
        client = self.make_client(response=httpx.Response(200, text="not-json"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")
        self.assertEqual(len(self.handler.requests), 1)

    def test_non_dict_json(self):
        """响应是合法 JSON 但不是对象时返回 INVALID_RESPONSE。"""
        client = self.make_client(response=httpx.Response(200, text="[1, 2]"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")

    def test_pois_not_a_list_is_tolerated(self):
        """pois 字段类型异常时按空结果处理，不抛异常。"""
        payload = success_payload()
        payload["pois"] = "unexpected"
        client = self.make_client(response=json_response(payload))
        result = client.search_places("医院")
        self.assertTrue(result["success"])
        self.assertEqual(result["results"], [])


class RetryPolicyTest(AmapClientTestBase):
    """重试策略：只重试临时故障，认证与参数错误不重试。"""

    def test_auth_error_not_retried(self):
        """认证错误只请求一次。"""
        client = self.make_client(
            response=json_response(error_payload("10001", "INVALID_USER_KEY"))
        )
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertEqual(len(self.handler.requests), 1)

    def test_quota_error_not_retried(self):
        """配额错误只请求一次。"""
        client = self.make_client(
            response=json_response(error_payload("10003", "DAILY_QUERY_OVER_LIMIT"))
        )
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "QUOTA_EXCEEDED")
        self.assertEqual(len(self.handler.requests), 1)

    def test_invalid_request_not_retried(self):
        """参数错误只请求一次。"""
        client = self.make_client(
            response=json_response(error_payload("20000", "INVALID_PARAMS"))
        )
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "INVALID_REQUEST")
        self.assertEqual(len(self.handler.requests), 1)

    def test_service_not_available_not_retried(self):
        """10002 属于权限或接口配置问题，重试没有意义。"""
        client = self.make_client(
            response=json_response(error_payload("10002", "SERVICE_NOT_AVAILABLE"))
        )
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "AUTH_FAILED")
        self.assertEqual(len(self.handler.requests), 1)

    def test_route_planning_error_not_retried(self):
        """20800 系列错误不重试，也不返回“无结果”。"""
        client = self.make_client(
            response=json_response(error_payload("20800", "OUT_OF_SERVICE"))
        )
        result = client.search_places("医院")
        self.assertEqual(result["error_code"], "INVALID_RESPONSE")
        self.assertEqual(len(self.handler.requests), 1)

    def test_network_error_retried_at_most_once(self):
        """持续网络故障时总共请求 2 次，不会无限重试。"""
        client = self.make_client(exception=httpx.ConnectError("连接失败"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(len(self.handler.requests), 2)

    def test_retry_then_success(self):
        """首次临时故障、第二次成功时返回成功结果。"""
        client = self.make_client(
            responses=[
                httpx.ConnectError("连接失败"),
                json_response(success_payload()),
            ]
        )
        result = client.search_places("医院")
        self.assertTrue(result["success"])
        self.assertEqual(len(self.handler.requests), 2)

    def test_server_busy_infocode_retried(self):
        """高德服务繁忙（10016）属于临时故障，允许重试一次。"""
        client = self.make_client(
            responses=[
                json_response(error_payload("10016", "SERVER_IS_BUSY")),
                json_response(success_payload()),
            ]
        )
        result = client.search_places("医院")
        self.assertTrue(result["success"])
        self.assertEqual(len(self.handler.requests), 2)

    def test_server_busy_twice_returns_error(self):
        """连续两次服务繁忙时返回错误，且只请求两次。"""
        client = self.make_client(
            response=json_response(error_payload("10016", "SERVER_IS_BUSY"))
        )
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "NETWORK_ERROR")
        self.assertEqual(len(self.handler.requests), 2)


class KeySecurityTest(AmapClientTestBase):
    """Key 绝不出现在返回值、错误提示或日志中。"""

    def test_error_message_never_contains_key(self):
        """网络错误时返回值不含 Key。"""
        client = self.make_client(exception=httpx.ConnectError("连接失败"))
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertNotIn(TEST_KEY, repr(result))
        self.assertNotIn(TEST_KEY, json.dumps(result, ensure_ascii=False))

    def test_business_error_never_contains_key(self):
        """业务错误时返回值不含 Key。"""
        client = self.make_client(
            response=json_response(error_payload("10001", "INVALID_USER_KEY"))
        )
        result = client.search_places("医院")
        self.assertNotIn(TEST_KEY, repr(result))

    def test_logs_never_contain_key(self):
        """失败日志只记录错误类型，不含 Key 与请求参数。"""
        client = self.make_client(exception=httpx.ConnectError("连接失败"))
        with self.assertLogs("agent.modules.amap_client", level="WARNING") as captured:
            client.search_places("医院")
        logs = "\n".join(captured.output)
        self.assertNotIn(TEST_KEY, logs)
        self.assertNotIn("keywords", logs)
        self.assertNotIn("restapi.amap.com", logs)

    def test_key_is_sent_in_query_parameters(self):
        """Key 确实通过查询参数发送，未被放到 URL 路径等可被日志记录的位置。"""
        client = self.make_client(response=json_response(success_payload()))
        client.search_places("医院")
        request = self.handler.requests[0]
        self.assertEqual(request.url.params["key"], TEST_KEY)
        self.assertNotIn(TEST_KEY, request.url.path)

    def test_client_does_not_expose_key_attribute(self):
        """客户端不提供读取 Key 的公开方法。"""
        client = self.make_client(response=json_response(success_payload()))
        public_names = [name for name in dir(client) if not name.startswith("_")]
        self.assertNotIn("api_key", public_names)
        self.assertNotIn("key", public_names)


class LifecycleTest(AmapClientTestBase):
    """close 与上下文管理器。"""

    def test_close_is_idempotent(self):
        """重复关闭不抛异常。"""
        client = self.make_client(response=json_response(success_payload()))
        client.close()
        client.close()

    def test_search_after_close_returns_client_closed(self):
        """关闭后搜索返回 CLIENT_CLOSED，且不发起请求。"""
        client = self.make_client(response=json_response(success_payload()))
        client.close()
        result = client.search_places("医院")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "CLIENT_CLOSED")
        self.assertEqual(self.handler.requests, [])

    def test_context_manager_closes_client(self):
        """with 语句退出后客户端处于关闭状态。"""
        client = self.make_client(response=json_response(success_payload()))
        with client as active:
            self.assertTrue(active.search_places("医院")["success"])
        self.assertEqual(client.search_places("医院")["error_code"], "CLIENT_CLOSED")

    def test_injected_client_is_closed(self):
        """注入的 HTTP 客户端由本对象负责关闭，不泄漏连接。"""
        http_client = httpx.Client(transport=httpx.MockTransport(lambda r: json_response(success_payload())))
        client = AmapClient(api_key=TEST_KEY, client=http_client)
        client.close()
        self.assertTrue(http_client.is_closed)