import threading
import time
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from mock_origin_server import MockOriginHandler, state as origin_state
from http_types import HTTPRequest
from origin_client import (
    OriginClient,
    OriginTimeoutError,
    OriginConnectionError
)

# 测试用的源站地址，端口取 18000/18001，避免和正式环境的 8000 冲突
TEST_HOST = "127.0.0.1"
TEST_PORT = 18000


class TestOriginClient(unittest.TestCase):
    """
    符合分工方案规范的 OriginClient 单元测试集
    重点覆盖：三种响应定界方式、条件请求、超时、源站异常断开
    """

    @classmethod
    def setUpClass(cls):
        # 启动可控测试源站后台线程
        # 用 mock 源站在本地起一个真实 HTTP 服务，测试就不依赖外部网络
        cls.server = HTTPServer((TEST_HOST, TEST_PORT), MockOriginHandler)
        # 把服务器放到后台线程里跑，这样测试代码可以继续往下执行
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        # 睡 0.3 秒等服务器真正就绪，否则第一个请求可能连不上
        time.sleep(0.3)
        cls.client = OriginClient(default_timeout=3.0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        # 每个用例执行前都重置源站资源，保证用例之间互不影响
        origin_state.reset()

    # --------------------------------------------------------------------------
    # 模块测试重点 1：三种响应定界方式测试
    # --------------------------------------------------------------------------

    def test_01_delimitation_content_length(self):
        """定界方式一：Content-Length 定长定界测试"""
        print("\n>>> [定界测试 1] Content-Length 定长定界验证")
        req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/index.html",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/index.html"
        )
        # 调用被测接口，把请求发给本地测试源站
        resp = self.client.fetch(req)
        print(f"    - 状态码: {resp.status_code} {resp.reason}")
        print(f"    - Content-Length 首部: {resp.get_header('content-length')}")
        print(f"    - 实际接收 Body 长度: {len(resp.body)} 字节")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(resp.get_header("content-length"))
        # 核心断言：实收正文长度必须和声明的长度一致，证明定长定界没出错
        self.assertEqual(len(resp.body), int(resp.get_header("content-length")))
        self.assertIn("中山大学计算机网络实验", resp.body.decode("utf-8"))

    def test_02_delimitation_chunked(self):
        """定界方式二：分块传输编码 (Transfer-Encoding: chunked) 解码定界测试"""
        print("\n>>> [定界测试 2] Transfer-Encoding: chunked 分块传输解码验证")
        req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/chunked",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/chunked"
        )
        resp = self.client.fetch(req)
        print(f"    - 状态码: {resp.status_code} {resp.reason}")
        print(f"    - 解码后 Body 长度: {len(resp.body)} 字节")
        print(f"    - 解码后正文内容: {resp.body.decode('utf-8')}")

        self.assertEqual(resp.status_code, 200)
        # 按照方案统一约束：“内部已去除分块传输编码，保留原有内容编码”
        # 解码后响应里不该再出现 Transfer-Encoding 首部
        self.assertIsNone(resp.get_header("transfer-encoding"))
        # 三段内容必须被完整拼接成一句原文，证明分块解码正确
        self.assertEqual(resp.body.decode("utf-8"), "Chunk1: Hello Chunk2: Network Chunk3: World!")
        # 解码后必须补上真实的 Content-Length，和正文长度对得上
        self.assertEqual(resp.get_header("content-length"), str(len(resp.body)))

    def test_03_delimitation_connection_close_eof(self):
        """定界方式三：连接关闭定界 (Connection: close 读到 EOF) 测试"""
        print("\n>>> [定界测试 3] 连接关闭定界 (EOF) 验证")
        # 请求 /eof，源站不发 Content-Length，只能靠关闭连接表示正文结束
        req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/eof",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/eof"
        )
        resp = self.client.fetch(req)
        print(f"    - 状态码: {resp.status_code} {resp.reason}")
        print(f"    - 实际接收 Body: {resp.body.decode('utf-8')}")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("EOF", resp.body.decode("utf-8"))

    # --------------------------------------------------------------------------
    # 模块测试重点 2：条件请求与 304 / 内容更新测试
    # --------------------------------------------------------------------------

    def test_04_conditional_request_304(self):
        """条件请求验证：携带 If-Modified-Since，未修改时返回 304 Not Modified"""
        print("\n>>> [条件请求测试] 304 Not Modified 验证")
        # 先获取当前 Last-Modified
        # 第一次正常请求，从响应里拿到源站当前的 Last-Modified 值
        init_req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/index.html",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/index.html"
        )
        resp1 = self.client.fetch(init_req)
        last_modified = resp1.get_header("last-modified")

        # 模拟 B 传入条件首部 conditional_headers
        # 把刚拿到的 Last-Modified 作为条件首部再请求一次，内容没变就应返回 304
        cond_headers = [("If-Modified-Since", last_modified)]
        resp2 = self.client.fetch(init_req, conditional_headers=cond_headers)
        print(f"    - 条件请求返回状态码: {resp2.status_code} {resp2.reason}")
        print(f"    - 正文长度: {len(resp2.body)} (依据规范 304 必须无正文)")

        self.assertEqual(resp2.status_code, 304)
        self.assertTrue(resp2.is_not_modified)
        # 304 响应不允许带正文，长度必须是 0
        self.assertEqual(len(resp2.body), 0)

    def test_05_update_after_expiry_200(self):
        """内容变化后更新验证：源站更新后，条件请求返回新 200 OK 与新正文"""
        print("\n>>> [内容更新测试] 源站更新后拉取新页面 200 OK 验证")
        old_time = origin_state.last_modified_str

        # 触发源站 /update 变更资源
        update_req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/update",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/update"
        )
        update_resp = self.client.fetch(update_req)
        self.assertEqual(update_resp.status_code, 200)

        # 携带旧时间戳请求验证，此时源站应返回全新 200 OK
        # 旧时间戳早于新修改时间，源站判定内容已变，返回完整新页面
        req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/index.html",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/index.html"
        )
        resp = self.client.fetch(req, conditional_headers=[("If-Modified-Since", old_time)])
        print(f"    - 返回状态码: {resp.status_code} (期望 200)")
        print(f"    - 新正文包含更新标记: {'页面已修改' in resp.body.decode('utf-8')}")

        # 内容变了就不能返回 304，必须是带新正文的 200
        self.assertEqual(resp.status_code, 200)
        self.assertIn("页面已修改", resp.body.decode("utf-8"))

    # --------------------------------------------------------------------------
    # 模块测试重点 3：超时与源站异常断开测试
    # --------------------------------------------------------------------------

    def test_06_timeout_handling(self):
        """超时处理测试：请求延迟接口，验证准确触发 OriginTimeoutError 及 504 响应"""
        print("\n>>> [异常测试 1] 源站慢响应超时测试")
        # 请求 /delay?sec=2：源站要 2 秒后才响应，故意拖过超时时间
        delay_req = HTTPRequest(
            method="GET",
            url=f"http://{TEST_HOST}:{TEST_PORT}/delay?sec=2",
            host=TEST_HOST,
            port=TEST_PORT,
            path="/delay?sec=2"
        )
        start_t = time.time()
        # 超时设 0.5 秒 < 源站延迟 2 秒，必须抛出超时异常
        with self.assertRaises(OriginTimeoutError):
            self.client.fetch(delay_req, timeout=0.5)
        elapsed = time.time() - start_t
        print(f"    - 准确抛出 OriginTimeoutError，耗时: {elapsed:.2f}s")

        # 验证 safe_fetch 合成 504 Gateway Timeout
        safe_resp = self.client.safe_fetch(delay_req, timeout=0.5)
        print(f"    - safe_fetch 合成响应: {safe_resp.status_code} {safe_resp.reason}")
        self.assertEqual(safe_resp.status_code, 504)

    def test_07_origin_disconnect_connection_refused(self):
        """源站异常断开 / 连接失败测试：连接无效端口，验证触发 OriginConnectionError 及 502 响应"""
        print("\n>>> [异常测试 2] 目标端口未开放/连接拒绝测试")
        # 故意请求一个没人监听的端口 59999，模拟源站拒绝连接
        bad_req = HTTPRequest(
            method="GET",
            url="http://127.0.0.1:59999/",
            host="127.0.0.1",
            port=59999,
            path="/"
        )
        # 连接被拒时必须抛出连接异常
        with self.assertRaises(OriginConnectionError):
            self.client.fetch(bad_req)
        print("    - 准确抛出 OriginConnectionError")

        # 验证 safe_fetch 合成 502 Bad Gateway
        safe_resp = self.client.safe_fetch(bad_req)
        print(f"    - safe_fetch 合成响应: {safe_resp.status_code} {safe_resp.reason}")
        self.assertEqual(safe_resp.status_code, 502)


# --------------------------------------------------------------------------
# 健壮性与边界场景补充测试
# 对应架构文档"不假设一次 recv() 收到完整报文"及公共类型统一约束
# --------------------------------------------------------------------------

# 第二个测试源站用另一个端口，和第一组测试互不干扰
TEST_PORT2 = 18001


class RobustTestHandler(BaseHTTPRequestHandler):
    """健壮性测试源站：模拟分帧到达、Trailer、重复首部、请求回显等场景"""

    protocol_version = "HTTP/1.1"

    def log_message(self, format_str, *args):
        pass  # 静默日志，避免刷屏

    def _send_simple(self, body: bytes, extra_headers=None):
        # 封装一个"发普通响应"的小工具：状态行 + Content-Length + 可选额外首部
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path

        # 1. 响应分多次 TCP 报文到达：先发首部，停顿后分两段发正文，每段强制 flush
        if path == "/segmented":
            body_bytes = ("SEG-" + "A" * 20 + "-" + "B" * 20 + "-END").encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            # 先只把首部刷出去，模拟首部先到、正文后到的分帧情况
            self.wfile.flush()
            time.sleep(0.3)
            # 再发正文前半段并刷新，逼客户端分多次 recv 才能拼齐
            self.wfile.write(body_bytes[:12])
            self.wfile.flush()
            time.sleep(0.3)
            self.wfile.write(body_bytes[12:])
            self.wfile.flush()
            return

        # 2. 分块传输携带 Trailer 首部
        if path == "/chunked-trailer":
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            payload = b"Trailer-Test-"
            self.wfile.write(b"%X\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()
            time.sleep(0.1)
            # 结束块前附带一个 Trailer 首部
            # 0 块 + Trailer + 空行，测试客户端能否正确剥离 Trailer 并识别结束
            self.wfile.write(b"0\r\nX-Checksum: abc123\r\n\r\n")
            self.wfile.flush()
            return

        # 3. 响应包含同名重复首部
        if path == "/dupheaders":
            # 故意发两个同名 X-Multi 首部，测试重复首部是否被完整保留
            self._send_simple(
                b"duplicate-header-test",
                extra_headers=[("X-Multi", "first"), ("X-Multi", "second")]
            )
            return

        # 4. 回显代理→源站实际收到的请求首部
        if path == "/echo":
            # 把收到的请求首部原样当正文返回，测试就能检查客户端到底发了什么
            lines = [f"{k}: {v}" for k, v in self.headers.raw_items()]
            body = "\n".join(lines).encode("latin1", errors="replace")
            self._send_simple(body)
            return

        self.send_error(404, "Not Found on Robust Test Origin")

    def do_HEAD(self):
        # HEAD 请求：只返回首部（Content-Length 为假设 GET 时的长度），不返回正文
        # 按规范 HEAD 的 Content-Length 应等于 GET 时的长度，但实际不能带正文
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "999")
        self.end_headers()


class TestOriginClientRobustness(unittest.TestCase):
    """健壮性与边界场景测试：分帧到达、Trailer、HEAD、重复首部、请求构造"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer((TEST_HOST, TEST_PORT2), RobustTestHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)
        cls.client = OriginClient(default_timeout=3.0)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _make_request(self, path, method="GET", extra_headers=None):
        # 小工具：按路径快速构造 HTTPRequest，减少每个用例里的重复代码
        return HTTPRequest(
            method=method,
            url=f"http://{TEST_HOST}:{TEST_PORT2}{path}",
            host=TEST_HOST,
            port=TEST_PORT2,
            path=path,
            headers=extra_headers or []
        )

    def test_08_segmented_content_length_response(self):
        """响应经多次 TCP 报文分帧到达时，Content-Length 定界仍能完整读取"""
        print("\n>>> [健壮性测试 1] 响应分帧到达（首部与正文分多次 recv）")
        resp = self.client.fetch(self._make_request("/segmented"))
        print(f"    - 状态码: {resp.status_code} {resp.reason}")
        print(f"    - 声明 Content-Length: {resp.get_header('content-length')}, 实收: {len(resp.body)} 字节")

        self.assertEqual(resp.status_code, 200)
        # 分帧到达也必须收成完整正文，长度和声明值一致
        self.assertEqual(len(resp.body), int(resp.get_header("content-length")))
        # 开头结尾都在，证明中间没有丢字节
        self.assertTrue(resp.body.startswith(b"SEG-"))
        self.assertTrue(resp.body.endswith(b"-END"))

    def test_09_chunked_with_trailer(self):
        """分块传输携带 Trailer 首部时，解码应正确剥离 Trailer 并还原完整正文"""
        print("\n>>> [健壮性测试 2] 分块传输 Trailer 首部处理")
        resp = self.client.fetch(self._make_request("/chunked-trailer"))
        print(f"    - 解码后正文: {resp.body.decode('utf-8')}")

        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_header("transfer-encoding"))
        # Trailer 不属于正文，解码结果必须只有块数据本身
        self.assertEqual(resp.body, b"Trailer-Test-")
        self.assertEqual(resp.get_header("content-length"), str(len(resp.body)))

    def test_10_head_request_no_body(self):
        """HEAD 请求：源站返回带 Content-Length 的首部但无正文，客户端应返回空正文"""
        print("\n>>> [健壮性测试 3] HEAD 请求无正文处理")
        resp = self.client.fetch(self._make_request("/head", method="HEAD"))
        print(f"    - 状态码: {resp.status_code}, 正文长度: {len(resp.body)}")

        self.assertEqual(resp.status_code, 200)
        # 关键断言：HEAD 响应即使有 Content-Length，body 也必须是空的
        self.assertEqual(resp.body, b"")
        self.assertEqual(resp.get_header("content-length"), "999")

    def test_11_duplicate_response_headers_preserved(self):
        """响应重复首部应按 list[tuple[str, str]] 完整保留，且查找大小写无关"""
        print("\n>>> [健壮性测试 4] 响应重复首部保留")
        resp = self.client.fetch(self._make_request("/dupheaders"))
        # get_headers 要把两个同名首部都收集到，且大小写不影响查找
        multi = resp.get_headers("x-multi")
        print(f"    - X-Multi 全部值: {multi}")

        self.assertEqual(multi, ["first", "second"])
        # 用小写查、用大写查都应命中第一个值，验证大小写无关
        self.assertEqual(resp.get_header("X-MULTI"), "first")

    def test_12_outgoing_request_construction(self):
        """发往源站的请求：自动补全 Host、强制 Connection: close、条件首部去重覆盖"""
        print("\n>>> [健壮性测试 5] 请求构造（Host / Connection / 条件首部去重）")
        # 故意让请求里带上旧的条件首部和 keep-alive，看客户端会不会正确处理
        req = self._make_request(
            "/echo",
            extra_headers=[
                ("If-Modified-Since", "old-timestamp"),
                ("Connection", "keep-alive"),
                ("User-Agent", "test-agent")
            ]
        )
        resp = self.client.fetch(
            req,
            conditional_headers=[("If-Modified-Since", "new-timestamp")]
        )
        echoed = resp.body.decode("latin1")
        print("    - 源站实际收到的请求首部:")
        for line in echoed.split("\n"):
            print(f"        {line}")

        # 没传 Host 也要自动补全，且带上端口
        self.assertIn(f"Host: {TEST_HOST}:{TEST_PORT2}", echoed)
        # 原请求的 keep-alive 必须被强制改成 close
        self.assertIn("Connection: close", echoed)
        self.assertNotIn("keep-alive", echoed)
        # 条件首部必须用新值覆盖旧值，且只出现一次
        self.assertIn("If-Modified-Since: new-timestamp", echoed)
        self.assertNotIn("old-timestamp", echoed)
        self.assertIn("User-Agent: test-agent", echoed)
        self.assertEqual(echoed.count("If-Modified-Since"), 1)


if __name__ == "__main__":
    print("开始运行源站通信模块单元测试集（契合分工方案全部重点 + 健壮性补充）")
    # 把两个测试类的用例打包成一个测试集一起执行
    loader = unittest.TestLoader()
    suite = unittest.TestSuite([
        loader.loadTestsFromTestCase(TestOriginClient),
        loader.loadTestsFromTestCase(TestOriginClientRobustness),
    ])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("[ALL PASSED] 所有测试用例全部通过！")
    else:
        print("[FAILED] 有部分测试用例未通过")
