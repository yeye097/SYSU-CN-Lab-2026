import argparse
import sys
import time
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class ServerState:
    """可控测试源站的状态管理器"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.version = 1
        # 初始最后修改时间设为 1 小时前
        # 故意设成 1 小时前：这样客户端拿旧时间戳来请求时，才能测出"过期"的效果
        self.last_modified_time = int(time.time()) - 3600
        # formatdate 把时间戳转成 HTTP 标准格式的字符串，比如 "Wed, 21 Oct 2026 10:00:00 GMT"
        self.last_modified_str = formatdate(self.last_modified_time, usegmt=True)
        # etag 用版本号和时间戳拼一个唯一标识，资源一变它就跟着变
        self.etag = f'"v{self.version}-{self.last_modified_time}"'
        self.title = "SYSU CN Lab2 - 可控测试源站"
        self.content = (
            "<h1>【初始版本 v1】中山大学计算机网络实验 - Web缓存测试页面</h1>\n"
            "<p>状态：初始页面已就绪。</p>\n"
            f"<p>当前资源版本：<b>v{self.version}</b></p>\n"
            f"<p>资源最后修改时间 (Last-Modified)：<code>{self.last_modified_str}</code></p>\n"
            "<hr>\n"
            "<p>提示：可以通过访问 <code>/update</code> 触发源站资源更新，测试 200 更新场景。</p>"
        )

    def update_resource(self):
        """模拟源站资源更新"""
        self.version += 1
        self.last_modified_time = int(time.time())
        self.last_modified_str = formatdate(self.last_modified_time, usegmt=True)
        self.etag = f'"v{self.version}-{self.last_modified_time}"'
        self.content = (
            f"<h1>【更新版本 v{self.version}】中山大学计算机网络实验 - 页面已修改</h1>\n"
            f"<p style='color:red;'>状态：源站资源已在 {self.last_modified_str} 发生更新！</p>\n"
            f"<p>当前资源版本：<b>v{self.version}</b></p>\n"
            f"<p>全新资源修改时间 (Last-Modified)：<code>{self.last_modified_str}</code></p>\n"
            "<hr>\n"
            "<p>说明：此页面用于验证代理服务器在缓存过期后向源站获取新正文的场景 (200 OK)。</p>"
        )


# 全局状态单例
# 所有请求线程共享同一个资源状态，这样 /update 的修改对所有后续请求都可见
state = ServerState()


class MockOriginHandler(BaseHTTPRequestHandler):
    """可控测试源站请求处理器（由 ThreadingHTTPServer 为每个连接分配独立线程）"""

    # 声明用 HTTP/1.1 协议，这样才能正确设置 Content-Length、Connection 等首部
    protocol_version = "HTTP/1.1"

    def log_message(self, format_str, *args):
        # 重写默认日志方法：加上时间戳和 [OriginServer] 标记，
        # 这样日志时间能和 Wireshark 抓包时间对照着看
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sys.stdout.write(f"[{now}] [OriginServer] {format_str % args}\n")
        sys.stdout.flush()

    def do_GET(self):
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        # query 是查询参数字典，比如 /delay?sec=3 会得到 {"sec": ["3"]}
        query = parse_qs(parsed_url.query)

        # 1. 模拟源站慢响应 / 超时接口: /delay?sec=3
        if path == "/delay":
            delay_sec = float(query.get("sec", [3])[0])
            self.log_message("[SIMULATE DELAY] 模拟源站慢响应，睡眠等待 %.1f 秒...", delay_sec)
            time.sleep(delay_sec)
            body = f"Delayed response after {delay_sec} seconds.".encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                # 客户端等不及已经断开，写响应会报错，这里捕获住不让服务器崩掉
                self.log_message("[CLIENT DISCONNECTED] 客户端在延迟响应期间已断开连接")
            return

        # 2. 模拟分块传输编码 (Chunked Transfer Encoding) 响应: /chunked
        # 用于测试 C 模块对 Transfer-Encoding: chunked 的定界解析与解码还原
        if path == "/chunked":
            self.log_message("[CHUNKED RESPONSE] 发送分块传输测试响应")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()

            # 按照分块格式输出：<十六进制长度>\r\n<数据>\r\n
            chunks = [b"Chunk1: Hello ", b"Chunk2: Network ", b"Chunk3: World!"]
            try:
                for c in chunks:
                    # 每块前面要加上这一块的字节数（十六进制）和 CRLF
                    size_hex = f"{len(c):X}\r\n".encode("latin1")
                    self.wfile.write(size_hex + c + b"\r\n")
                    # 每发一块就强制刷新，让客户端真的分多次收到
                    self.wfile.flush()
                # 发送分块结束标识：0\r\n\r\n
                # 块大小为 0 表示正文发完了，这是分块传输的结束标志
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            return

        # 3. 模拟连接关闭定界 (EOF 定界，无 Content-Length，无 Chunked): /eof
        if path == "/eof":
            body = b"Response delimited strictly by connection close (EOF)."
            self.log_message("[EOF RESPONSE] 发送无 Content-Length、通过关闭连接定界的响应")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            # 注意：故意不发 Content-Length，让客户端只能靠"连接关闭"来判断正文结束
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            return

        # 4. 模拟源站资源更新: /update
        if path == "/update":
            state.update_resource()
            self.log_message(">>> [RESOURCE UPDATED] 触发源站更新: 版本升级为 v%d, 新 Last-Modified: %s",
                             state.version, state.last_modified_str)
            info = (
                f"OK: Resource updated to v{state.version}.\n"
                f"New Last-Modified: {state.last_modified_str}\n"
                f"New ETag: {state.etag}\n"
            ).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(info)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(info)
            return

        # 5. 资源重置接口: /reset
        if path == "/reset":
            # 把资源恢复成初始版本，用于场景之间复位，保证每次测试从同一状态开始
            state.reset()
            self.log_message(">>> [RESOURCE RESET] 资源已重置为初始状态 v1")
            info = b"OK: Reset to v1 initial state.\n"
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(info)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(info)
            return

        # 6. 源站状态接口: /status
        if path == "/status":
            # 返回当前资源状态，方便调试和写报告时核对源站到底是多少版本
            info = (
                f"Version: v{state.version}\n"
                f"Last-Modified: {state.last_modified_str}\n"
                f"ETag: {state.etag}\n"
            ).encode("utf-8")
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(info)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(info)
            return

        # 7. 核心测试网页: / 或 /index.html（采用 Content-Length 定界）
        if path in ["/", "/index.html"]:
            # 取出请求里的两个条件首部，用来判断该返回 304 还是 200
            ims_header = self.headers.get("If-Modified-Since")
            inm_header = self.headers.get("If-None-Match")

            not_modified = False

            # 优先检查 ETag：请求带的 If-None-Match 和当前 etag 完全相等，就说明没变过
            if inm_header and inm_header.strip() == state.etag:
                not_modified = True
                self.log_message("[CONDITIONAL MATCH] If-None-Match 匹配: %s", inm_header)

            # 没有 If-None-Match 时，才退而求其次检查 If-Modified-Since
            elif ims_header:
                try:
                    # 把 HTTP 日期字符串解析成带时区的时间对象，才能和源站时间比较
                    client_time = parsedate_to_datetime(ims_header)
                    # 源站的修改时间也转成 UTC 时间对象，保证两边时区一致
                    server_time = datetime.fromtimestamp(state.last_modified_time, tz=timezone.utc)
                    # 如果客户端时间没带时区信息，按 UTC 补上，避免比较出错
                    if client_time.tzinfo is None:
                        client_time = client_time.replace(tzinfo=timezone.utc)
                    # 客户端时间 >= 源站修改时间，说明客户端手里的已经是新的，返回 304
                    if client_time >= server_time:
                        not_modified = True
                        self.log_message("[CONDITIONAL MATCH] If-Modified-Since 命中: %s >= %s",
                                         ims_header, state.last_modified_str)
                    else:
                        self.log_message("[CONDITIONAL MISS] If-Modified-Since 过期: 客户端 %s < 源站 %s",
                                         ims_header, state.last_modified_str)
                except Exception as e:
                    # 日期格式解析失败不能影响服务，记一笔日志后按普通请求处理
                    self.log_message("[CONDITIONAL PARSE ERROR] 解析 If-Modified-Since 失败: %s", e)

            # 304 Not Modified
            if not_modified:
                self.log_message("<<< [RESPONSE 304] 内容未改变，返回 304 Not Modified")
                self.send_response(304, "Not Modified")
                self.send_header("Date", formatdate(time.time(), usegmt=True))
                self.send_header("Last-Modified", state.last_modified_str)
                self.send_header("ETag", state.etag)
                self.send_header("Connection", "close")
                self.end_headers()
                return

            # 200 OK 完整页面（采用 Content-Length 定长定界）
            # 用当前状态拼出完整 HTML，再转成字节数好设置 Content-Length
            body_bytes = (
                "<!DOCTYPE html>\n"
                "<html>\n"
                "<head><meta charset='utf-8'><title>" + state.title + "</title></head>\n"
                "<body>\n" + state.content + "\n</body>\n"
                "</html>"
            ).encode("utf-8")

            self.log_message("<<< [RESPONSE 200] 返回 200 OK, 正文长度: %d 字节, 版本: v%d",
                             len(body_bytes), state.version)
            self.send_response(200, "OK")
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            # Last-Modified 和 ETag 是给代理做条件验证用的验证标识
            self.send_header("Last-Modified", state.last_modified_str)
            self.send_header("ETag", state.etag)
            self.send_header("Date", formatdate(time.time(), usegmt=True))
            # max-age=10 让代理可以测试"缓存 10 秒内有效"的新鲜度逻辑
            self.send_header("Cache-Control", "public, max-age=10")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body_bytes)
            return

        self.send_error(404, "File Not Found on Mock Origin")


def run_server(host="127.0.0.1", port=8000):
    server_address = (host, port)
    # 多线程服务器：每个连接分配独立线程。
    # 场景 5（多客户端并发）时代理会向源站并行建立多条 TCP 连接，
    # 单线程服务器会将其串行化，导致抓包中看不到并发的源站侧 TCP 流。
    httpd = ThreadingHTTPServer(server_address, MockOriginHandler)
    print(f"可控测试源站已启动: http://{host}:{port}（多线程并发）")
    print(f"标准测试页 (Content-Length 定界): http://{host}:{port}/index.html")
    print(f"分块传输测试页 (Chunked 定界): http://{host}:{port}/chunked")
    print(f"连接关闭测试页 (EOF 定界): http://{host}:{port}/eof")
    print(f"更新端点: http://{host}:{port}/update")
    print(f"重置端点: http://{host}:{port}/reset")
    try:
        # serve_forever 让服务器一直轮询处理新连接，阻塞在这里不退出
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭测试源站...")
        httpd.server_close()
        print("测试源站已停止。")


if __name__ == "__main__":
    # 直接运行本文件时才启动服务器；被 import 时不启动（比如被测试文件引用时）
    parser = argparse.ArgumentParser(description="可控 HTTP 测试源站")
    parser.add_argument("--host", default="127.0.0.1", help="主机地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args()
    run_server(args.host, args.port)
