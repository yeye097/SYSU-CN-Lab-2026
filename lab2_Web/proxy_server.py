"""成员 A：HTTP 代理接入层。一连接一请求，缓存策略由 B 的 handle() 决定。"""
import argparse
import logging
import re
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http import HTTPStatus
from urllib.parse import urlsplit

if __package__:
    from .config import DEFAULT_CONFIG
    from .errors import (ClientRequestError, ClientDisconnected, OriginClientError,
                         OriginTimeoutError, OriginProtocolError)
    from .http_types import HTTPRequest, HTTPResponse
    from .log_context import configure_logging, request_id, request_url
    from .origin_client import OriginClient
else:
    from config import DEFAULT_CONFIG
    from errors import (ClientRequestError, ClientDisconnected, OriginClientError,
                        OriginTimeoutError, OriginProtocolError)
    from http_types import HTTPRequest, HTTPResponse
    from log_context import configure_logging, request_id, request_url
    from origin_client import OriginClient

logger = logging.getLogger("ProxyServer")
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
# 以下首部只适用于相邻连接，不能原样转交另一条 TCP 连接。
HOP_HEADERS = {"connection", "proxy-connection", "keep-alive", "te", "trailer",
               "transfer-encoding", "upgrade", "proxy-authenticate", "proxy-authorization"}


def _valid_value(value):
    return all(ord(c) >= 32 or c == "\t" for c in value) and "\x7f" not in value


def _without_hop_headers(headers):
    nominated = {token.strip().lower()
                 for name, value in headers if name.lower() in ("connection", "proxy-connection")
                 for token in value.split(",")}
    return [(name, value) for name, value in headers
            if name.lower() not in HOP_HEADERS | nominated]


def _target_parts(target, host_header):
    """absolute-form 以 URL 为准；也接受 origin-form，便于本地 socket 联调。"""
    if not target.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in target) or "\\" in target:
        raise ClientRequestError("URL 必须是合法 ASCII URI；中文请使用百分号编码")
    if "#" in target:
        raise ClientRequestError("HTTP 请求目标不能包含片段标识 #")
    if host_header is not None:
        try:
            # Host 只能是 authority，不能借路径、查询串或片段改变原始请求目标。
            if (not host_header or not host_header.isascii()
                    or re.search(r"[\s/?#@\\]", host_header) or host_header.endswith(":")):
                raise ValueError("Host 必须是主机及可选端口")
            authority = urlsplit("http://" + host_header)
            if not authority.hostname or not 1 <= (authority.port or 80) <= 65535 or authority.port == 0:
                raise ValueError("Host 主机或端口无效")
        except ValueError as error:
            raise ClientRequestError(f"非法 Host：{error}") from error
    if target.startswith("/"):
        if target.startswith("//") or not host_header:
            raise ClientRequestError("相对请求目标需要有效 Host")
        full_url = "http://" + host_header + target
    else:
        full_url = target
    try:
        parts = urlsplit(full_url)
        if parts.scheme.lower() != "http":
            raise ClientRequestError("仅支持普通 HTTP，不支持 HTTPS/CONNECT", 501)
        host = parts.hostname
        port = parts.port if parts.port is not None else 80
        if (not host or parts.username is not None or parts.password is not None
                or parts.netloc.endswith(":") or not 1 <= port <= 65535):
            raise ValueError("主机或端口无效")
        if any(c in host for c in " /?#@\\") or not host.isascii():
            raise ValueError("主机名无效")
    except ValueError as error:
        raise ClientRequestError(f"非法 URL：{error}") from error
    host = host.lower()
    authority = f"[{host}]" if ":" in host else host
    if port != 80:
        authority += f":{port}"
    path = parts.path or "/"
    if parts.query or "?" in target:
        path += "?" + parts.query
    return host, port, path, "http://" + authority + path, authority


def read_client_request(sock, *, config=DEFAULT_CONFIG):
    """循环读取到 CRLF CRLF，返回公共 HTTPRequest；超时是整个首部读取期限。"""
    buffer = bytearray()
    deadline = time.monotonic() + config.client_timeout
    previous_timeout = sock.gettimeout()
    try:
        while True:
            end = buffer.find(b"\r\n\r\n")
            if end >= 0:
                if end + 4 > config.max_request_header_bytes:
                    raise ClientRequestError("请求头过大", 431)
                break
            if len(buffer) >= config.max_request_header_bytes:
                raise ClientRequestError("请求头过大", 431)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientRequestError("读取请求超时", 408)
            sock.settimeout(remaining)
            chunk = sock.recv(min(4096, config.max_request_header_bytes - len(buffer)))
            if not chunk:
                if not buffer:
                    raise ClientDisconnected()
                raise ClientRequestError("请求首部尚未完整，客户端已结束发送")
            buffer.extend(chunk)
    except socket.timeout as error:
        raise ClientRequestError("读取请求超时", 408) from error
    finally:
        sock.settimeout(previous_timeout)

    # Latin-1 一字节映射一个字符；正文不在这里解码，也不参与 HTTP 首部解析。
    lines = bytes(buffer[:end]).decode("latin1").split("\r\n")
    items = lines[0].split(" ")
    if len(items) != 3 or not TOKEN.fullmatch(items[0]):
        raise ClientRequestError("非法请求行")
    method, target, version = items
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise ClientRequestError("仅支持 HTTP/1.0 与 HTTP/1.1", 505)
    if method != "GET":
        raise ClientRequestError("仅支持 GET 请求", 501, head_request=(method == "HEAD"))

    headers = []
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator or not TOKEN.fullmatch(name) or not _valid_value(value):
            raise ClientRequestError("非法首部行（不支持折行首部）")
        headers.append((name, value.strip(" \t")))
    hosts = [v for k, v in headers if k.lower() == "host"]
    if len(hosts) > 1 or (version == "HTTP/1.1" and (not hosts or not hosts[0])):
        raise ClientRequestError("HTTP/1.1 需要一个非空 Host 首部")
    # GET 带正文不在实验范围；先检查定界字段，再剥离 hop-by-hop 首部。
    lengths = [v for k, v in headers if k.lower() == "content-length"]
    if (any(k.lower() == "transfer-encoding" for k, _ in headers)
            or len(lengths) > 1
            or (lengths and (not re.fullmatch(r"0+", lengths[0])))
            or buffer[end + 4:]):
        raise ClientRequestError("不支持带请求体的 GET 或请求流水线")
    if any(k.lower() == "expect" for k, _ in headers):
        raise ClientRequestError("不支持 Expect 请求头", 417)

    host, port, path, url, authority = _target_parts(target, hosts[0] if hosts else None)
    # absolute-form 的目标来自 URL，避免旧 Host 使源站选错虚拟主机。
    headers = [(k, v) for k, v in _without_hop_headers(headers)
               if k.lower() not in ("host", "content-length")]
    headers.append(("Host", authority))
    return HTTPRequest(method, url, host, port, path, headers)


def _response_bytes(response, config):
    """先验证、重建完整报文，再发送，避免发了一半才发现首部错误。"""
    if not 200 <= response.status_code <= 599 or not isinstance(response.body, bytes):
        raise OriginProtocolError("最终响应状态码或正文类型无效")
    if not _valid_value(response.reason):
        raise OriginProtocolError("响应原因短语包含控制字符")
    if len(response.body) > config.max_response_body_bytes:
        raise OriginProtocolError("响应正文超过配置上限")
    for name, value in response.headers:
        if not TOKEN.fullmatch(name) or not _valid_value(value):
            raise OriginProtocolError("响应首部不合法")
    headers = [(k, v) for k, v in _without_hop_headers(response.headers)
               if k.lower() != "content-length"]
    body = b"" if response.status_code in (204, 205, 304) else response.body
    # 204/304 不带正文，也不写容易误导的 Content-Length: 0。
    if response.status_code not in (204, 304):
        headers.append(("Content-Length", str(len(body))))
    headers.append(("Connection", "close"))
    text = f"HTTP/1.1 {response.status_code} {response.reason}\r\n"
    text += "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
    try:
        head = text.encode("latin1")
    except UnicodeEncodeError as error:
        raise OriginProtocolError("响应首部不能按 Latin-1 编码") from error
    if len(head) > config.max_response_header_bytes:
        raise OriginProtocolError("响应首部超过配置上限")
    return head + body


def write_client_response(sock, response, *, config=DEFAULT_CONFIG):
    """不修改原响应（可能正被其他缓存命中线程共享），只生成当前连接的报文。"""
    sock.sendall(_response_bytes(response, config))


def _error_response(code):
    reason = HTTPStatus(code).phrase
    return HTTPResponse(code, reason, [("Content-Type", "text/plain; charset=utf-8")],
                        f"{code} {reason}\n".encode("utf-8"))


class PassthroughController:
    """仅用于 A/C 联调；每次访问源站，不查缓存，不生成虚假的 HIT/MISS。"""
    def __init__(self, origin_client):
        self.origin_client = origin_client

    def handle(self, request):
        logger.info("不缓存联调：访问源站", extra={"cache_status": "BYPASS"})
        return self.origin_client.fetch(request)


class ProxyServer:
    """监听套接字只归主循环；已接收连接归工作线程。控制器全实例共享。"""
    def __init__(self, controller, *, config=DEFAULT_CONFIG):
        self.controller = controller
        self.config = config
        self._stop = threading.Event()
        self._slots = threading.BoundedSemaphore(config.max_workers + config.max_pending_connections)
        self._clients = set()
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind((config.host, config.port))
            self._listener.listen(config.listen_backlog)
            self._listener.settimeout(0.2)  # 定期检查退出标志，也使 Windows 的 Ctrl+C 有机会被处理。
        except BaseException:
            self._listener.close()
            raise
        self.server_address = self._listener.getsockname()

    @property
    def active_connections(self):
        """已接收且正在处理或排队的连接数；用于运行观察及测试。"""
        with self._lock:
            return len(self._clients)

    def shutdown(self):
        """停止接入，并唤醒等待客户端数据的工作线程；源站请求受 C 的超时限制。"""
        self._stop.set()
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _send_error(self, client, code, *, head_only=False):
        try:
            # 错误报文使用默认上限，避免用户为正常响应设置的极小上限阻止错误通知。
            raw = _response_bytes(_error_response(code), DEFAULT_CONFIG)
            if head_only:
                raw = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
            client.sendall(raw)
        except OSError:
            pass  # 对方可能已断开，无需再返回第二条错误。

    def handle_client(self, client, address):
        id_token = request_id.set(uuid.uuid4().hex[:12])
        url_token = request_url.set("-")
        status = None
        with client:
            try:
                if self._stop.is_set():
                    return
                client.settimeout(self.config.client_timeout)
                request = read_client_request(client, config=self.config)
                request_url.set(request.url)
                logger.info("接收请求：%s %s，客户端 %s", request.method, request.url, address)
                # B 的 handle 负责缓存决策；A 不直接调用缓存存储。
                response = self.controller.handle(request)
                status = response.status_code
                # 公共输出接口先校验、重建报文，再 sendall；校验失败可安全映射为 502。
                write_client_response(client, response, config=self.config)
            except ClientDisconnected:
                logger.info("客户端未发送请求即断开：%s", address)
            except ClientRequestError as error:
                status = error.status_code
                logger.warning("请求错误：%s", error)
                self._send_error(client, status, head_only=error.head_request)
            except OriginTimeoutError as error:
                status = 504
                logger.warning("源站超时：%s", error)
                self._send_error(client, status)
            except OriginClientError as error:
                status = 502
                logger.warning("源站通信或响应错误：%s", error)
                self._send_error(client, status)
            except OSError as error:
                # 此处是客户端接收/发送异常；不向已发出部分响应的连接再拼一个错误响应。
                logger.warning("客户端通信结束：%s", error)
            except Exception:
                status = 500
                logger.exception("请求处理发生内部错误")
                self._send_error(client, status)
            finally:
                self._finish_client(client)
                logger.info("结束请求，状态=%s", status if status is not None else "disconnected")
                request_url.reset(url_token)
                request_id.reset(id_token)

    def _run_client(self, client, address):
        try:
            self.handle_client(client, address)
        finally:
            with self._lock:
                self._clients.discard(client)
            self._slots.release()

    def _finish_client(self, client):
        """先结束发送，再有限度排空请求数据；最多 0.1 秒/1 MiB，避免无限等待。"""
        try:
            client.shutdown(socket.SHUT_WR)
            remaining = self.config.max_discard_bytes
            deadline = time.monotonic() + 0.1
            while remaining > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                client.settimeout(wait)
                data = client.recv(min(4096, remaining))
                if not data:
                    break
                remaining -= len(data)
        except OSError:
            pass

    def _reject_busy(self, client):
        with client:
            client.settimeout(0.1)
            self._send_error(client, 503)
            self._finish_client(client)

    def serve_forever(self):
        logger.info("代理监听 %s:%s", *self.server_address)
        try:
            with ThreadPoolExecutor(max_workers=self.config.max_workers, thread_name_prefix="proxy") as pool:
                try:
                    while not self._stop.is_set():
                        try:
                            client, address = self._listener.accept()
                        except socket.timeout:
                            continue
                        if not self._slots.acquire(blocking=False):
                            # 槽位涵盖工作线程和有限排队连接；避免线程池内部无限积压 socket。
                            self._reject_busy(client)
                            logger.warning("连接容量已满，返回 503：%s", address)
                            continue
                        with self._lock:
                            self._clients.add(client)
                        try:
                            pool.submit(self._run_client, client, address)
                        except BaseException:
                            with self._lock:
                                self._clients.discard(client)
                            client.close()
                            self._slots.release()
                            raise
                finally:
                    self.shutdown()
        finally:
            self._listener.close()


def build_controller(config, *, no_cache=False):
    origin = OriginClient(default_timeout=config.origin_timeout,
                          max_response_header_bytes=config.max_response_header_bytes,
                          max_response_body_bytes=config.max_response_body_bytes)
    if no_cache:
        return PassthroughController(origin)
    # 正式模式必须装有 B 的实现，不能静默降级成每次都访问源站。
    if __package__:
        from .cache_controller import CacheController
        from .cache_store import CacheStore
    else:
        from cache_controller import CacheController
        from cache_store import CacheStore
    return CacheController(origin_client=origin, store=CacheStore(max_bytes=config.max_cache_bytes))


def main(argv=None):
    parser = argparse.ArgumentParser(description="HTTP GET 缓存代理接入层（成员 A）")
    parser.add_argument("--host", default=DEFAULT_CONFIG.host)
    parser.add_argument("--port", type=int, default=DEFAULT_CONFIG.port)
    parser.add_argument("--workers", type=int, default=DEFAULT_CONFIG.max_workers)
    parser.add_argument("--pending", type=int, default=DEFAULT_CONFIG.max_pending_connections)
    parser.add_argument("--client-timeout", type=float, default=DEFAULT_CONFIG.client_timeout)
    parser.add_argument("--origin-timeout", type=float, default=DEFAULT_CONFIG.origin_timeout)
    parser.add_argument("--no-cache", action="store_true", help="仅用于 A/C 转发联调，每次都访问源站")
    args = parser.parse_args(argv)
    configure_logging()
    try:
        config = replace(DEFAULT_CONFIG, host=args.host, port=args.port, max_workers=args.workers,
                         max_pending_connections=args.pending, client_timeout=args.client_timeout,
                         origin_timeout=args.origin_timeout)
        controller = build_controller(config, no_cache=args.no_cache)
        server = ProxyServer(controller, config=config)
    except ImportError as error:
        parser.error(f"缓存模块尚未就绪：{error}。B 完成后放入同目录；A/C 联调可显式添加 --no-cache")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.no_cache:
        logger.warning("当前为 --no-cache 联调模式，不提供缓存命中功能")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("代理已停止接入并完成连接清理")

if __name__ == "__main__":
    main()
