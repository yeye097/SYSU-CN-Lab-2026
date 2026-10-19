"""A 的模块测试：真实 socket / 本地源站，不依赖外网或第三方库。"""
import concurrent.futures
import socket
import threading
import time
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer

try:
    from . import proxy_server as proxy
    from .config import DEFAULT_CONFIG
    from .http_types import HTTPResponse
    from .origin_client import OriginClient, OriginTimeoutError, OriginConnectionError, OriginProtocolError
    from .C.mock_origin_server import MockOriginHandler
except ImportError:
    import proxy_server as proxy
    from config import DEFAULT_CONFIG
    from http_types import HTTPResponse
    from origin_client import OriginClient, OriginTimeoutError, OriginConnectionError, OriginProtocolError
    from C.mock_origin_server import MockOriginHandler


def request_bytes(target="http://Example.COM:80/a?q=1", extra=b"", method="GET"):
    return (f"{method} {target} HTTP/1.1\r\nHost: ignored.example\r\n".encode("ascii")
            + extra + b"\r\n")


class RequestTests(unittest.TestCase):
    def parse(self, data, config=DEFAULT_CONFIG):
        left, right = socket.socketpair()
        with left, right:
            right.sendall(data)
            right.shutdown(socket.SHUT_WR)
            return proxy.read_client_request(left, config=config)

    def test_absolute_url_normalization_and_duplicate_headers(self):
        req = self.parse(request_bytes(extra=b"X-Test: one\r\nx-test: two\r\n"))
        self.assertEqual((req.host, req.port, req.path), ("example.com", 80, "/a?q=1"))
        self.assertEqual(req.url, "http://example.com/a?q=1")
        self.assertEqual(req.get_headers("X-Test"), ["one", "two"])
        self.assertEqual(req.get_header("Host"), "example.com")

    def test_segmented_header_and_timeout_restored(self):
        left, right = socket.socketpair()
        with left, right:
            left.settimeout(3)
            def send():
                for part in [b"GET http://localhost:8000/ HTTP/1.1\r\nHo", b"st: localhost:8000\r\n", b"\r\n"]:
                    right.sendall(part)
                    time.sleep(0.01)
            sender = threading.Thread(target=send)
            sender.start()
            req = proxy.read_client_request(left)
            sender.join()
            self.assertEqual(req.path, "/")
            self.assertEqual(left.gettimeout(), 3)

    def test_origin_form_and_ipv6(self):
        for target, host, url in [
            ("/x?a=2", "LOCALHOST:8000", "http://localhost:8000/x?a=2"),
            ("http://[::1]:8000/", "ignored", "http://[::1]:8000/"),
        ]:
            with self.subTest(target=target):
                req = self.parse(f"GET {target} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
                self.assertEqual(req.url, url)

    def test_invalid_host_cannot_change_origin_form_path(self):
        for host in ["localhost:8000/prefix", "localhost?query", "localhost#fragment",
                     "user@localhost", "local host", "localhost:"]:
            with self.subTest(host=host):
                with self.assertRaises(proxy.ClientRequestError):
                    self.parse(f"GET /x HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())

    def test_request_hop_headers_removed_and_end_to_end_preserved(self):
        req = self.parse(request_bytes(extra=(
            b"Connection: keep-alive, X-Hop\r\nX-Hop: secret\r\n"
            b"Proxy-Connection: keep-alive\r\nProxy-Authorization: secret\r\n"
            b"Cache-Control: no-cache\r\nCookie: a=b\r\nContent-Length: 0\r\n")))
        for key in ["Connection", "X-Hop", "Proxy-Connection", "Proxy-Authorization", "Content-Length"]:
            self.assertIsNone(req.get_header(key))
        self.assertEqual(req.get_header("Cache-Control"), "no-cache")
        self.assertEqual(req.get_header("Cookie"), "a=b")

    def test_invalid_requests_rejected(self):
        cases = [
            b"broken\r\n\r\n", b"GET / HTTP/1.1\r\n\r\n",
            request_bytes(extra=b"Host: duplicate\r\n"),
            request_bytes(extra=b"Bad Header: value\r\n"),
            request_bytes(extra=b"X-Test : value\r\n"),
            request_bytes(extra=b" folded\r\n"),
            request_bytes(extra=b"X-Test: a\x00b\r\n"),
            request_bytes("http://user:pass@example.com/"),
            request_bytes("http://example.com:bad/"),
            request_bytes("http://example.com:0/"),
            request_bytes("http://example.com:/"),
            request_bytes("http://example.com/#fragment"),
            request_bytes(extra=b"Content-Length: 1\r\n") + b"a",
            request_bytes(extra=b"Content-Length: -1\r\n"),
            request_bytes(extra=b"Content-Length: 0\r\nContent-Length: 0\r\n"),
            request_bytes(extra=b"Transfer-Encoding: chunked\r\n"),
            request_bytes() + b"unexpected body",
        ]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(proxy.ClientRequestError) as error:
                    self.parse(data)
                self.assertEqual(error.exception.status_code, 400)

    def test_unsupported_method_and_scheme(self):
        for data in [request_bytes(method="POST"), request_bytes(method="CONNECT"), request_bytes("https://example.com/")]:
            with self.subTest(data=data):
                with self.assertRaises(proxy.ClientRequestError) as error:
                    self.parse(data)
                self.assertEqual(error.exception.status_code, 501)

    def test_header_limit_partial_eof_and_empty_connection(self):
        config = replace(DEFAULT_CONFIG, max_request_header_bytes=64)
        with self.assertRaises(proxy.ClientRequestError) as error:
            self.parse(request_bytes(extra=b"X-Large: " + b"a" * 100 + b"\r\n"), config)
        self.assertEqual(error.exception.status_code, 431)
        with self.assertRaises(proxy.ClientRequestError):
            self.parse(b"GET / HTTP/1.1\r\n")
        with self.assertRaises(proxy.ClientDisconnected):
            self.parse(b"")

    def test_header_deadline(self):
        left, right = socket.socketpair()
        with left, right:
            with self.assertRaises(proxy.ClientRequestError) as error:
                proxy.read_client_request(left, config=replace(DEFAULT_CONFIG, client_timeout=0.05))
            self.assertEqual(error.exception.status_code, 408)


class ResponseTests(unittest.TestCase):
    def serialize(self, response, config=DEFAULT_CONFIG):
        left, right = socket.socketpair()
        with left, right:
            proxy.write_client_response(left, response, config=config)
            left.shutdown(socket.SHUT_WR)
            return b"".join(iter(lambda: right.recv(4096), b""))

    def test_reframe_binary_body_and_preserve_duplicate_headers(self):
        response = HTTPResponse(200, "OK", [
            ("Content-Length", "999"), ("content-length", "888"),
            ("Transfer-Encoding", "chunked"), ("Trailer", "X-Trailer"),
            ("Connection", "keep-alive, X-Hop"), ("X-Hop", "secret"),
            ("Content-Encoding", "gzip"), ("Set-Cookie", "a=1"), ("Set-Cookie", "b=2"),
        ], b"\x00\xff\xe4\xbd\xa0")
        original = list(response.headers)
        head, body = self.serialize(response).split(b"\r\n\r\n", 1)
        self.assertEqual(body, response.body)
        self.assertEqual(head.lower().count(b"content-length:"), 1)
        self.assertIn(b"Content-Length: 5", head)
        self.assertIn(b"Connection: close", head)
        self.assertIn(b"Content-Encoding: gzip", head)
        self.assertEqual(head.count(b"Set-Cookie:"), 2)
        for key in [b"transfer-encoding:", b"x-hop:", b"trailer:"]:
            self.assertNotIn(key, head.lower())
        self.assertEqual(response.headers, original)

    def test_bodyless_statuses(self):
        for code in [204, 304]:
            with self.subTest(code=code):
                head, body = self.serialize(HTTPResponse(code, "No Body", [("Content-Length", "999")], b"wrong")).split(b"\r\n\r\n", 1)
                self.assertEqual(body, b"")
                self.assertNotIn(b"Content-Length", head)

    def test_bad_response_and_size_limits(self):
        for response in [HTTPResponse(200, "OK\r\nInjected: yes"),
                         HTTPResponse(200, "OK", [("X", "a\r\nb")]),
                         HTTPResponse(200, "OK", [("Bad Name", "value")]),
                         HTTPResponse(100, "Continue")]:
            with self.subTest(response=response):
                with self.assertRaises(OriginProtocolError):
                    self.serialize(response)
        with self.assertRaises(OriginProtocolError):
            self.serialize(HTTPResponse(200, "OK", body=b"12345"), replace(DEFAULT_CONFIG, max_response_body_bytes=4))
        with self.assertRaises(OriginProtocolError):
            self.serialize(HTTPResponse(200, "OK", [("X", "a" * 100)]), replace(DEFAULT_CONFIG, max_response_header_bytes=64))


class EchoController:
    """仅供测试：回显路径，不冒充正式缓存实现。"""
    def handle(self, request):
        return HTTPResponse(200, "OK", body=request.path.encode("utf-8"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def start(self, controller=None, **changes):
        config = replace(DEFAULT_CONFIG, port=0, client_timeout=0.3, **changes)
        server = proxy.ProxyServer(controller or EchoController(), config=config)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.servers.append((server, thread))
        return server

    def exchange(self, server, data=None):
        with socket.create_connection(server.server_address, timeout=3) as sock:
            sock.sendall(data or request_bytes())
            return b"".join(iter(lambda: sock.recv(4096), b""))

    def test_complete_response_and_close(self):
        raw = self.exchange(self.start())
        self.assertTrue(raw.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertEqual(raw.split(b"\r\n\r\n", 1)[1], b"/a?q=1")

    def test_concurrent_clients_are_not_serialized_or_mixed(self):
        barrier = threading.Barrier(4)
        class ConcurrentController:
            def handle(self, request):
                barrier.wait(timeout=2)
                return HTTPResponse(200, "OK", body=request.path.encode())
        server = self.start(ConcurrentController(), max_workers=4)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.exchange, server, request_bytes(f"http://localhost/{i}")) for i in range(4)]
            for i, future in enumerate(futures):
                self.assertEqual(future.result().split(b"\r\n\r\n", 1)[1], f"/{i}".encode())

    def test_errors_mapped_and_server_survives(self):
        class ErrorController:
            def handle(self, request):
                errors = {"/timeout": OriginTimeoutError, "/connection": OriginConnectionError,
                          "/protocol": OriginProtocolError, "/bug": RuntimeError}
                if request.path in errors:
                    raise errors[request.path]("test error")
                return HTTPResponse(200, "OK")
        server = self.start(ErrorController())
        for path, code in [("/timeout", 504), ("/connection", 502), ("/protocol", 502), ("/bug", 500), ("/ok", 200)]:
            with self.subTest(path=path):
                self.assertTrue(self.exchange(server, request_bytes("http://localhost" + path)).startswith(f"HTTP/1.1 {code} ".encode()))
        self.assertTrue(self.exchange(server, b"broken\r\n\r\n").startswith(b"HTTP/1.1 400 "))
        self.assertTrue(self.exchange(server, request_bytes(method="POST")).startswith(b"HTTP/1.1 501 "))

    def test_early_disconnect_and_read_timeout(self):
        server = self.start()
        with socket.create_connection(server.server_address, timeout=2):
            pass
        with socket.create_connection(server.server_address, timeout=2) as sock:
            sock.sendall(b"GET /")
            self.assertTrue(sock.recv(4096).startswith(b"HTTP/1.1 408 "))
        self.assertTrue(self.exchange(server).startswith(b"HTTP/1.1 200 "))

    def test_head_error_has_headers_but_no_body(self):
        raw = self.exchange(self.start(), request_bytes(method="HEAD"))
        head, body = raw.split(b"\r\n\r\n", 1)
        self.assertTrue(head.startswith(b"HTTP/1.1 501 "))
        self.assertEqual(body, b"")

    def test_rejected_body_is_drained_before_close(self):
        server = self.start()
        data = request_bytes(extra=b"Content-Length: 200000\r\n") + b"x" * 200000
        self.assertTrue(self.exchange(server, data).startswith(b"HTTP/1.1 400 "))

    def test_shutdown_unblocks_idle_connection(self):
        server = self.start()
        with socket.create_connection(server.server_address, timeout=2) as sock:
            limit = time.monotonic() + 2
            while not server.active_connections and time.monotonic() < limit:
                time.sleep(0.005)
            server.shutdown()
            self.servers[-1][1].join(timeout=2)
            self.assertFalse(self.servers[-1][1].is_alive())
            self.assertEqual(sock.recv(4096), b"")

    def test_capacity_bound_and_recovery(self):
        server = self.start(max_workers=1, max_pending_connections=0)
        with socket.create_connection(server.server_address, timeout=2) as first:
            # 等首个连接真正占有处理槽位，避免依赖固定 sleep。
            limit = time.monotonic() + 2
            while server.active_connections != 1 and time.monotonic() < limit:
                time.sleep(0.005)
            self.assertEqual(server.active_connections, 1)
            self.assertTrue(self.exchange(server).startswith(b"HTTP/1.1 503 "))
            first.sendall(request_bytes())
            self.assertTrue(first.recv(4096).startswith(b"HTTP/1.1 200 "))
        limit = time.monotonic() + 2
        while server.active_connections and time.monotonic() < limit:
            time.sleep(0.005)
        self.assertTrue(self.exchange(server).startswith(b"HTTP/1.1 200 "))

    def test_real_c_origin_client_three_response_boundaries(self):
        origin = ThreadingHTTPServer(("127.0.0.1", 0), MockOriginHandler)
        thread = threading.Thread(target=origin.serve_forever)
        thread.start()
        try:
            server = self.start(proxy.PassthroughController(OriginClient(default_timeout=1)))
            for path, text in [("/index.html", "中山大学"), ("/chunked", "Chunk1: Hello"), ("/eof", "Response delimited")]:
                with self.subTest(path=path):
                    raw = self.exchange(server, request_bytes(f"http://127.0.0.1:{origin.server_port}{path}"))
                    head, body = raw.split(b"\r\n\r\n", 1)
                    self.assertTrue(head.startswith(b"HTTP/1.1 200 "))
                    self.assertIn(text.encode(), body)
                    self.assertIn(f"Content-Length: {len(body)}".encode(), head)
                    self.assertNotIn(b"Transfer-Encoding", head)
        finally:
            origin.shutdown()
            origin.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main(verbosity=2)
