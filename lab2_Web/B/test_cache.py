"""B 的模块测试：不启动网络服务，用模拟源站验证缓存行为。"""
from run_tests import prepare_workspace
prepare_workspace()

import unittest
from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from unittest.mock import Mock

from cache_controller import CacheController
from cache_store import CacheStore, response_size
from errors import OriginTimeoutError
from http_types import CacheEntry, HTTPRequest, HTTPResponse


def response(policy="max-age=10", body=b"v1", status=200, headers=()):
    """构造源站响应；额外首部用于测试 ETag、Date 等信息。"""
    return HTTPResponse(status, "Not Modified" if status == 304 else "OK",
                        [("Cache-Control", policy), *headers], body)


def entry(body=b"a"):
    """构造最小缓存条目，用于存储层测试。"""
    return CacheEntry(HTTPResponse(200, "OK", [], body), 0, 0, 0, 10)


class CacheTests(unittest.TestCase):
    def setUp(self):
        """每个测试重新创建控制器和模拟源站，避免缓存状态互相影响。"""
        self.now = 1000
        self.request = HTTPRequest("GET", "http://localhost/a", "localhost", 80, "/a")
        # Mock 替代 C 的源站客户端：测试只验证 B 的决策，不实际联网。
        self.origin = Mock()
        # 改变 now 即可模拟过期，不需要 sleep。
        self.controller = CacheController(self.origin, clock=lambda: self.now)

    def test_hit_expiry_and_replacement(self):
        """首次访问保存 v1，新鲜时命中；过期后获取并保存 v2。"""
        # side_effect 列表让两次源站调用依次返回 v1、v2。
        self.origin.fetch.side_effect = [response(), response(body=b"v2")]
        self.controller.handle(self.request)
        self.now += 3
        hit = self.controller.handle(self.request)
        self.assertEqual(hit.get_header("Age"), "3")
        self.assertEqual(self.origin.fetch.call_count, 1)  # HIT 不访问源站
        self.now += 10
        self.assertEqual(self.controller.handle(self.request).body, b"v2")
        self.assertEqual(self.controller.handle(self.request).body, b"v2")
        self.assertEqual(self.origin.fetch.call_count, 2)

    def test_304_and_validator_priority(self):
        """验证 ETag 优先、Last-Modified 备用，以及 304 复用正文并刷新有效期。"""
        # 两种情况独立运行：同时有标识时选 ETag，只有时间戳时选 Last-Modified。
        for validators, expected in [
            ([("ETag", '"v1"'), ("Last-Modified", "stamp")], [("If-None-Match", '"v1"')]),
            ([("Last-Modified", "stamp")], [("If-Modified-Since", "stamp")]),
        ]:
            with self.subTest(validators=validators):
                self.controller.store.clear()  # 每个子场景从空缓存开始
                self.origin.fetch.reset_mock()
                self.origin.fetch.side_effect = [response(headers=validators),
                    response("max-age=30", b"", 304, [("Content-Length", "0")])]
                self.controller.handle(self.request)
                self.now += 11
                result = self.controller.handle(self.request)
                self.assertEqual(self.origin.fetch.call_args.kwargs["conditional_headers"], expected)
                self.assertEqual((result.status_code, result.body), (200, b"v1"))
                self.now += 20
                self.controller.handle(self.request)  # 304 已刷新新鲜期
                self.assertEqual(self.origin.fetch.call_count, 2)

    def test_no_cache_no_store_private(self):
        """no-cache 可保存但需验证；no-store 和 private 不能保存。"""
        for policy in ("no-cache, max-age=60", "no-store", "private"):
            with self.subTest(policy=policy):
                self.controller.store.clear()  # 每个子场景从空缓存开始
                self.origin.fetch.reset_mock()
                self.origin.fetch.side_effect = [response(policy), response(policy)]
                self.controller.handle(self.request)
                self.controller.handle(self.request)
                self.assertEqual(self.origin.fetch.call_count, 2)
                # no-cache 留下 1 条供下次验证，另两种策略应为 0 条。
                expected_size = 1 if policy.startswith("no-cache") else 0
                self.assertEqual(self.controller.store.size(), expected_size)

    def test_bypass_and_response_filter(self):
        """检查请求绕过规则，以及 Set-Cookie/Vary 响应不进入缓存。"""
        self.origin.fetch.return_value = response()
        for name in ("Authorization", "Cookie", "Range", "If-None-Match"):
            self.request.headers = [(name, "x")]
            self.controller.handle(self.request)
            self.assertEqual(self.controller.store.size(), 0)
        self.request.headers = []
        for name in ("Set-Cookie", "Vary"):
            self.origin.fetch.return_value = response(headers=[(name, "x")])
            self.controller.handle(self.request)
            self.assertEqual(self.controller.store.size(), 0)

    def test_freshness_and_age(self):
        """检查新鲜期优先级，以及上游 Age 加本地驻留时间。"""
        cases = [([], 0), ([("Cache-Control", "max-age=10, s-maxage=20")], 20),
                 ([("Expires", formatdate(1030, usegmt=True)),
                   ("Date", formatdate(1000, usegmt=True))], 30)]
        for headers, expected in cases:
            self.assertEqual(self.controller.freshness_lifetime(headers, 1000), expected)
        self.origin.fetch.return_value = response("max-age=60", headers=[
            ("Date", formatdate(980, usegmt=True)), ("Age", "30")])
        self.controller.handle(self.request)
        self.now += 2
        # Date 推算年龄为 20 秒，取较大的上游 Age=30，再加本地 2 秒。
        self.assertEqual(self.controller.handle(self.request).get_header("Age"), "32")

    def test_source_failure_does_not_return_stale(self):
        """过期验证遇到超时应抛异常，不能用旧正文代替失败。"""
        self.origin.fetch.side_effect = [response(), OriginTimeoutError("timeout")]
        self.controller.handle(self.request)
        self.now += 11
        with self.assertRaises(OriginTimeoutError):
            self.controller.handle(self.request)

    def test_lru_and_capacity(self):
        """检查 LRU 淘汰，以及删除和清空后的容量统计。"""
        size = response_size(entry().response)
        # 分别让条目数和字节数成为限制，均应淘汰最久未使用的 b。
        for store in (CacheStore(max_entries=2), CacheStore(max_bytes=size * 2)):
            store.put("a", entry())
            store.put("b", entry())
            store.get("a")
            store.put("c", entry())
            self.assertEqual(store.keys(), ["a", "c"])
            self.assertEqual(store.total_bytes(), size * 2)
            store.delete("a")
            self.assertEqual(store.total_bytes(), size)
            store.clear()
            self.assertEqual((store.size(), store.total_bytes()), (0, 0))

    def test_replacement_oversize_and_snapshot(self):
        """检查快照隔离、同键替换计量和超大条目的拒绝保存。"""
        store = CacheStore(max_bytes=100)
        store.put("a", entry())
        snapshot = store.get("a")
        snapshot.response.body = b"external"  # 修改快照不能影响存储中的原响应
        self.assertEqual(store.get("a").response.body, b"a")
        store.put("a", entry(b"new"))
        self.assertEqual(store.total_bytes(), response_size(entry(b"new").response))
        self.assertFalse(store.put("a", entry(b"x" * 101)))
        self.assertEqual((store.size(), store.total_bytes()), (0, 0))

    def test_concurrent_reads_and_writes(self):
        """多个线程同时读写，最终不能突破容量或破坏字节统计。"""
        store = CacheStore(max_entries=5, max_bytes=150)

        def worker(number):
            for i in range(100):
                key = str((i + number) % 20)
                store.put(key, entry())
                store.get(key)  # 其他线程可能已淘汰此键，允许返回 None
                if i % 3 == 0:
                    store.delete(key)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(worker, number) for number in range(4)]
            for future in futures:
                future.result()  # 将子线程异常传回主线程，避免测试假通过
        self.assertLessEqual(store.size(), 5)
        self.assertLessEqual(store.total_bytes(), 150)
        # 等线程全部结束后核对精确总量，避免读到不同时间点的状态。
        self.assertEqual(store.total_bytes(), sum(store.get(k).entry_size for k in store.keys()))


if __name__ == "__main__":
    unittest.main()
