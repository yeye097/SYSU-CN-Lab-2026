"""整合回归：源站定界错误不能变成成功缓存，真实 TCP 验证 A→B→C。"""
import io
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.utils import formatdate
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

from config import DEFAULT_CONFIG
from cache_controller import CacheController
from http_types import HTTPRequest, HTTPResponse
from origin_client import OriginClient, OriginProtocolError
from proxy_server import build_controller, ProxyServer
from mock_origin_server import MockOriginHandler, state
from traffic_generator import raw_http_get_via_proxy, direct_origin_call, run_scene_3


class ByteSocket:
    """提供真实字节流及 recv 分段语义，定界测试无需网络或计时。"""
    def __init__(self, raw):
        self.stream = io.BytesIO(raw)

    def recv(self, size):
        return self.stream.read(size)


class OriginBoundaryTests(unittest.TestCase):
    def parse(self, raw, client=None):
        return (client or OriginClient())._receive_and_parse_response(ByteSocket(raw), "GET")

    def test_truncated_content_length_rejected(self):
        with self.assertRaises(OriginProtocolError):
            self.parse(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n\r\nshort")

    def test_content_length_does_not_include_surplus_bytes(self):
        response = self.parse(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nokEXTRA")
        self.assertEqual(response.body, b"ok")

    def test_ambiguous_or_invalid_framing_rejected(self):
        for headers in (b"Content-Length: -1", b"Content-Length: nope",
                        b"Content-Length: 2\r\nContent-Length: 3",
                        b"Transfer-Encoding: notchunked",
                        b"Transfer-Encoding: gzip, chunked",
                        b"Transfer-Encoding: chunked\r\nContent-Length: 2"):
            with self.subTest(headers=headers), self.assertRaises(OriginProtocolError):
                self.parse(b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n\r\n0\r\n\r\n")

    def test_invalid_status_and_header_rejected(self):
        for raw in (b"NOTHTTP 200 OK\r\n\r\nok", b"HTTP/1.1 999 Bad\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nmissing-colon\r\n\r\nok"):
            with self.subTest(raw=raw), self.assertRaises(OriginProtocolError):
                self.parse(raw)

    def test_receive_limits_for_all_body_boundaries(self):
        client = OriginClient(max_response_header_bytes=128, max_response_body_bytes=4)
        for raw in (b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n12345",
                    b"HTTP/1.1 200 OK\r\n\r\n12345",
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\n12345\r\n0\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nX: " + b"x" * 128 + b"\r\n\r\n",
                    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nX: " + b"x" * 128 + b"\r\n\r\n"):
            with self.subTest(raw=raw), self.assertRaises(OriginProtocolError):
                self.parse(raw, client)

    def test_config_reaches_origin_and_cache(self):
        config = replace(DEFAULT_CONFIG, max_cache_bytes=512, max_response_body_bytes=123,
                         max_response_header_bytes=256)
        controller = build_controller(config)
        self.assertEqual(controller.store.max_bytes, 512)
        self.assertEqual(controller.origin_client.max_response_body_bytes, 123)
        self.assertEqual(controller.origin_client.max_response_header_bytes, 256)

    def test_large_delta_seconds_do_not_raise_internal_errors(self):
        for value in ("9" * 5000, "0" * 5000 + "10"):
            with self.subTest(value_length=len(value)):
                origin = Mock()
                origin.fetch.return_value = HTTPResponse(200, "OK", [
                    ("Cache-Control", "max-age=" + value), ("Age", value)], b"ok")
                controller = CacheController(origin, clock=iter([1000, 1000.1]).__next__)
                response = controller.handle(HTTPRequest("GET", "http://localhost/", "localhost", 80, "/"))
                self.assertEqual(response.body, b"ok")
                expected = 2 ** 31 if value.startswith("9") else 10
                self.assertEqual(controller.store.get("http://localhost/").freshness_lifetime, expected)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        state.reset()
        self.records = []
        self.records_lock = threading.Lock()
        self.parallel = threading.Barrier(4)
        owner = self

        class RecordingOrigin(MockOriginHandler):
            def log_message(self, *args):
                pass

            def send_response(self, code, message=None):
                with owner.records_lock:
                    owner.records.append((self.path, dict(self.headers), code))
                super().send_response(code, message)

            def send_header(self, name, value):
                # 源站 Date 与缓存时钟必须一起推进，否则新响应也会显得已过期。
                if name.lower() == "date":
                    value = formatdate(owner.now, usegmt=True)
                super().send_header(name, value)

            def do_GET(self):
                if self.path == "/broken":
                    with owner.records_lock:
                        owner.records.append((self.path, dict(self.headers), 200))
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nCache-Control: max-age=60\r\n"
                                     b"Content-Length: 99\r\n\r\npartial")
                    self.close_connection = True
                elif self.path.startswith("/parallel/"):
                    owner.parallel.wait(timeout=3)
                    body = self.path.encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "max-age=60")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    super().do_GET()

        self.origin = ThreadingHTTPServer(("127.0.0.1", 0), RecordingOrigin)
        self.origin_thread = threading.Thread(target=self.origin.serve_forever, daemon=True)
        self.origin_thread.start()
        self.addCleanup(self.stop_origin)
        self.config = replace(DEFAULT_CONFIG, port=0, origin_timeout=0.15)
        self.controller = build_controller(self.config)
        # 使用真实源站与 TCP，仅推进缓存时钟来确定性触发过期。
        self.now = time.time()
        self.controller.clock = lambda: self.now
        self.proxy = ProxyServer(self.controller, config=self.config)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.proxy_thread.start()
        self.addCleanup(self.stop_proxy)
        self.base = f"http://127.0.0.1:{self.origin.server_port}"

    def stop_origin(self):
        self.origin.shutdown()
        self.origin.server_close()
        self.origin_thread.join(timeout=3)
        self.assertFalse(self.origin_thread.is_alive())

    def stop_proxy(self):
        self.proxy.shutdown()
        self.proxy_thread.join(timeout=3)
        self.assertFalse(self.proxy_thread.is_alive())

    def get(self, path="/index.html", headers=None):
        raw = raw_http_get_via_proxy(*self.proxy.server_address, self.base + path,
                                     extra_headers=headers, timeout=4).encode("latin1")
        head, body = raw.split(b"\r\n\r\n", 1)
        lines = head.decode("latin1").split("\r\n")
        code = int(lines[0].split()[1])
        fields = dict((k.lower(), v.strip()) for k, v in
                      (line.split(":", 1) for line in lines[1:]))
        if code != 304:
            self.assertEqual(int(fields["content-length"]), len(body))
        self.assertEqual(fields["connection"], "close")
        return code, fields, body

    def test_miss_hit_expired_304_and_updated_200(self):
        with self.assertLogs("cache_controller", level="INFO") as logs:
            first = self.get()
            self.assertEqual(first[0], 200)
            hit = self.get()
            self.assertEqual(hit[2], first[2])
            self.assertEqual(len(self.records), 1, "HIT 必须完全不访问源站")
            self.assertIn("age", hit[1])
            self.now += 11
            validated = self.get()
            self.assertEqual(validated[0], 200)
            self.assertEqual(validated[2], first[2])
            self.assertEqual(self.records[-1][2], 304)
            self.assertEqual(self.records[-1][1]["If-None-Match"], first[1]["etag"])
            direct_origin_call("127.0.0.1", self.origin.server_port, "/update")
            self.now += 11
            updated = self.get()
            self.assertEqual(updated[0], 200)
            self.assertNotEqual(updated[2], first[2])
            self.assertNotEqual(updated[1]["etag"], first[1]["etag"])
            count = len(self.records)
            self.assertEqual(self.get()[2], updated[2])
            self.assertEqual(len(self.records), count)
        statuses = [record.cache_status for record in logs.records]
        for expected in ("MISS", "HIT", "REVALIDATE", "UPDATE"):
            self.assertIn(expected, statuses)

    def test_concurrent_misses_and_hits(self):
        paths = [f"/parallel/{i}" for i in range(4)]
        for _ in range(2):
            with ThreadPoolExecutor(max_workers=4) as pool:
                responses = list(pool.map(self.get, paths))
            self.assertEqual([r[2] for r in responses], [p.encode() for p in paths])
            self.assertEqual(len(self.records), 4, "第二轮并发必须全部命中")

    def test_three_body_boundaries(self):
        for path, content in (("/index.html", "中山大学".encode()),
                              ("/chunked", b"Chunk1: Hello"),
                              ("/eof", b"Response delimited")):
            with self.subTest(path=path):
                code, headers, body = self.get(path)
                self.assertEqual(code, 200)
                self.assertIn(content, body)
                self.assertNotIn("transfer-encoding", headers)

    def test_truncated_response_is_502_and_never_cached(self):
        for _ in range(2):
            self.assertEqual(self.get("/broken")[0], 502)
        self.assertEqual(self.controller.store.size(), 0)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.get()[0], 200, "异常之后代理仍能工作")

    def test_timeout_is_504_and_server_recovers(self):
        self.assertEqual(self.get("/delay?sec=0.4")[0], 504)
        self.assertEqual(self.controller.store.size(), 0)
        self.assertEqual(self.get()[0], 200)

    def test_client_conditions_and_cookie_bypass(self):
        first = self.get()
        self.assertEqual(self.get(headers={"If-None-Match": first[1]["etag"]})[0], 304)
        self.assertEqual(self.get(headers={"Cookie": "user=one"})[0], 200)
        self.assertEqual(len(self.records), 3)
        self.assertEqual(self.controller.store.size(), 1)

    def test_origin_etag_takes_priority_over_modified_since(self):
        first = self.get()
        response = self.get(headers={"If-None-Match": '"different"',
                                     "If-Modified-Since": first[1]["last-modified"]})
        self.assertEqual(response[0], 200)

    def test_capture_scene_3_always_revalidates(self):
        self.now = int(self.now)
        self.get()
        run_scene_3(*self.proxy.server_address, "127.0.0.1", self.origin.server_port)
        self.assertEqual(len(self.records), 2)
        self.assertEqual(self.records[-1][2], 304)


if __name__ == "__main__":
    unittest.main(verbosity=2)
