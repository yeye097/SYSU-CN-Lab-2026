import socket
import logging
from typing import List, Tuple, Optional

try:
    from http_types import HTTPRequest, HTTPResponse
except ImportError:
    from .http_types import HTTPRequest, HTTPResponse

# 配置全局日志的输出格式，加上时间戳和模块名，方便出问题时对照排查
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [OriginClient] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("OriginClient")


# 自定义网络异常定义
class OriginClientError(Exception):
    """源站通信基类异常"""
    # 这是所有源站异常的父类，上层只要捕获它就能抓住下面三种具体异常
    pass


class OriginTimeoutError(OriginClientError):
    """源站超时异常（连接或读取超时，对应 HTTP 504）"""
    pass


class OriginConnectionError(OriginClientError):
    """源站连接失败异常（拒绝连接或无法连通，对应 HTTP 502）"""
    pass


class OriginProtocolError(OriginClientError):
    """源站协议解析异常（非标准 HTTP 报文）"""
    pass


class OriginClient:
    """
    源站通信客户端
    职责：
      1. 接收角色 B 传入的 HTTPRequest 及可选条件首部 conditional_headers；
      2. 构造规范的 HTTP/1.1 请求并向目标源站建立 Socket 连接；
      3. 支持三种响应定界方式：Content-Length、分块传输 (chunked) 解码、连接关闭定界 (EOF)；
      4. 严格按照方案规范返回 HTTPResponse 对象。
    """

    def __init__(self, default_timeout: float = 5.0):
        self.default_timeout = default_timeout

    def fetch(
        self,
        request: HTTPRequest,
        conditional_headers: Optional[List[Tuple[str, str]]] = None,
        timeout: Optional[float] = None
    ) -> HTTPResponse:
        """
        核心通信接口：向源站发起请求并获取 HTTPResponse
        符合分工方案规范：OriginClient.fetch(request, conditional_headers=None) -> HTTPResponse
        """
        timeout = timeout if timeout is not None else self.default_timeout

        # 1. 组装请求首部列表（保留传入的首部，并合并 B 提供的条件首部）
        # 先复制一份请求自带的首部，避免直接改动原始对象
        headers_list: List[Tuple[str, str]] = list(request.headers)

        # 追加或覆盖条件首部（如 If-Modified-Since、If-None-Match）
        if conditional_headers:
            for cond_k, cond_v in conditional_headers:
                # 移除已有的同名条件头，避免重复
                # 把列表里同名的旧头过滤掉，保证同一个条件头只出现一次
                headers_list = [(k, v) for k, v in headers_list if k.lower() != cond_k.lower()]
                headers_list.append((cond_k, cond_v))

        # 补全 Host 首部（HTTP/1.1 必须包含 Host）
        # 先检查请求里有没有 Host 头，端口不是 80 时才需要把端口号写上
        has_host = any(k.lower() == "host" for k, _ in headers_list)
        if not has_host:
            host_val = f"{request.host}:{request.port}" if request.port != 80 else request.host
            headers_list.append(("Host", host_val))

        # 代理与源站间强制声明 Connection: close，确保收完报文即可断开连接，避免 socket 阻塞挂起
        # 不管原请求是什么，这里都强制改成 close：收完响应就断连，不用等对方关
        headers_list = [(k, v) for k, v in headers_list if k.lower() != "connection"]
        headers_list.append(("Connection", "close"))

        # 默认 User-Agent
        if not any(k.lower() == "user-agent" for k, _ in headers_list):
            headers_list.append(("User-Agent", "SYSU-CN-ProxyClient/1.0"))

        # 2. 构造标准 HTTP/1.1 请求报文字节流
        # 请求行格式是"方法 路径 协议版本"，路径用 origin-form（不带主机名）
        request_line = f"{request.method.upper()} {request.path} HTTP/1.1\r\n"
        headers_str = "".join(f"{k}: {v}\r\n" for k, v in headers_list)
        raw_request = (request_line + headers_str + "\r\n").encode("latin1")

        # 记一条 INFO 日志，抓包对不上时可以用来核对发了什么请求
        logger.info("正在向源站 %s:%d 发起请求: %s %s", request.host, request.port, request.method, request.path)
        if conditional_headers:
            logger.info("携带条件请求首部: %s", conditional_headers)

        # 3. 建立 Socket 连接并收发数据
        sock = None
        try:
            # 建立 TCP 连接，超时时间一并设置，连不上会抛异常
            sock = socket.create_connection((request.host, request.port), timeout=timeout)
            # 再设一次读取超时，防止连接建好后对方一直不发数据
            sock.settimeout(timeout)
            sock.sendall(raw_request)

            # 流式接收并解析源站返回的响应
            return self._receive_and_parse_response(sock, request.method)

        except socket.timeout as e:
            # 连接或读取超时：记错误日志并抛自定义异常，上层据此返回 504
            logger.error("源站 %s:%d 通信超时 (%.1fs)", request.host, request.port, timeout)
            raise OriginTimeoutError(f"连接或读取源站超时: {e}") from e

        except (ConnectionRefusedError, socket.gaierror, OSError) as e:
            # 连接被拒、域名解析失败或其他系统级网络错误，统一归为连接失败，对应 502
            logger.error("无法连接源站 %s:%d: %s", request.host, request.port, e)
            raise OriginConnectionError(f"源站连接失败: {e}") from e

        finally:
            # 不管成功还是失败，最后都要把 socket 关掉，防止连接泄漏
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass

    def _receive_and_parse_response(self, sock: socket.socket, method: str) -> HTTPResponse:
        """
        从 Socket 字节流中读取首部并支持三种定界方式读取 Body：
        1. 304 / 204 / HEAD 无正文
        2. Transfer-Encoding: chunked 分块传输解码（解码后去除分块传输头）
        3. Content-Length 定长读取
        4. 连接关闭定界（EOF 读取）
        """
        # 用字节数组缓存收到的数据，因为要不断往里追加新收到的内容
        buffer = bytearray()
        header_end_idx = -1

        # 阶段 A：持续读取直到找到首部与正文的分隔标识 \r\n\r\n
        # TCP 是字节流，一次 recv 不一定能收全首部，所以循环接收
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)

            header_end_idx = buffer.find(b"\r\n\r\n")
            if header_end_idx != -1:
                break

        # 连接都关了还没找到首部结束符，说明响应根本不是合法 HTTP 报文
        if header_end_idx == -1:
            raise OriginProtocolError("未能找到 HTTP 响应首部结束符 (\\r\\n\\r\\n)")

        headers_bytes = buffer[:header_end_idx]
        # 结束符之后可能已经跟着一部分正文了，先取出来，后面直接接着用
        body_initial_bytes = bytes(buffer[header_end_idx + 4:])

        # 阶段 B：解析状态行与首部行
        try:
            # 首部按 HTTP 规定用 latin1 解码，保证每个字节都对应一个字符
            headers_text = headers_bytes.decode("latin1")
        except UnicodeDecodeError as e:
            raise OriginProtocolError(f"响应首部编码解析失败: {e}")

        lines = headers_text.split("\r\n")
        if not lines or not lines[0]:
            raise OriginProtocolError("空 HTTP 响应状态行")

        # 解析状态行: 例如 "HTTP/1.1 200 OK"
        status_line = lines[0].strip()
        status_parts = status_line.split(" ", 2)
        if len(status_parts) < 2:
            raise OriginProtocolError(f"非法的 HTTP 状态行格式: {status_line}")

        try:
            status_code = int(status_parts[1])
        except ValueError:
            raise OriginProtocolError(f"非法的 HTTP 状态码: {status_parts[1]}")

        reason = status_parts[2] if len(status_parts) > 2 else ""

        # 按照方案要求，headers 统一采用 list[tuple[str, str]]，保留原始大小写与重复项
        # 逐行拆"名字: 值"，只按第一个冒号切，防止值里带冒号被切错
        parsed_headers: List[Tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                parsed_headers.append((k.strip(), v.strip()))

        # 阶段 C：三种响应定界方式处理
        # 1. 根据 HTTP 规范：304 Not Modified, 204 No Content 或 HEAD 请求禁止包含正文
        # 这三种情况直接返回空正文，不用再读 socket
        if status_code == 304 or status_code == 204 or method.upper() == "HEAD":
            logger.info("源站返回状态码 %d，根据规范不包含正文", status_code)
            return HTTPResponse(status_code=status_code, reason=reason, headers=parsed_headers, body=b"")

        # 先扫描首部，判断该用哪种定界方式
        # 检查是否采用 Transfer-Encoding: chunked 分块传输
        is_chunked = False
        content_length_val = None

        for k, v in parsed_headers:
            # 首部值里出现 chunked 就按分块传输处理
            if k.lower() == "transfer-encoding" and "chunked" in v.lower():
                is_chunked = True
            elif k.lower() == "content-length":
                try:
                    content_length_val = int(v.strip())
                except ValueError:
                    # 长度不是合法数字就当没有，退回 EOF 定界
                    pass

        # 2. 定界方式一：分块传输 (Chunked Transfer Encoding) 解码
        if is_chunked:
            logger.info("源站采用分块传输编码 (Transfer-Encoding: chunked)，正在执行分块解码...")
            body = self._read_chunked_body(sock, body_initial_bytes)
            
            # 方案统一约束：“内部已去除分块传输编码，保留原有内容编码”
            # 去掉 Transfer-Encoding 首部，并补上解包后的真实 Content-Length
            # 这样上层拿到的就是"普通"响应，不用关心原来是不是分块发的
            filtered_headers = [(k, v) for k, v in parsed_headers if k.lower() != "transfer-encoding"]
            filtered_headers.append(("Content-Length", str(len(body))))
            return HTTPResponse(status_code=status_code, reason=reason, headers=filtered_headers, body=body)

        # 3. 定界方式二：Content-Length 定长传输
        if content_length_val is not None:
            # 声明多少字节就收多少字节，先用手头已有的，不够再继续 recv
            body_parts = [body_initial_bytes]
            current_len = len(body_initial_bytes)
            while current_len < content_length_val:
                needed = content_length_val - current_len
                chunk = sock.recv(min(4096, needed))
                if not chunk:
                    break
                body_parts.append(chunk)
                current_len += len(chunk)
            # 把分段收到的内容拼成完整正文
            body = b"".join(body_parts)
            return HTTPResponse(status_code=status_code, reason=reason, headers=parsed_headers, body=body)

        # 4. 定界方式三：连接关闭定界 (Connection: close 读到 EOF)
        # 既没有 Content-Length 也不是分块，就只能一直读到对方关连接为止
        body_parts = [body_initial_bytes]
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body_parts.append(chunk)
        body = b"".join(body_parts)

        return HTTPResponse(status_code=status_code, reason=reason, headers=parsed_headers, body=body)

    def _read_chunked_body(self, sock: socket.socket, initial_buffer: bytes) -> bytes:
        """
        分块传输解码实现：
        依次解析 <分块大小十六进制>\r\n + <分块数据>\r\n，直至遇到 0\r\n\r\n 结束块
        """
        # buf 存放还没解析完的数据，decoded_body 存放解码后的正文
        buf = bytearray(initial_buffer)
        decoded_body = bytearray()

        def read_line():
            # 从 buf 里找 \r\n 拆出一行；缓冲区没有完整的一行就再收点数据
            nonlocal buf
            while True:
                idx = buf.find(b"\r\n")
                if idx != -1:
                    line = buf[:idx]
                    buf = buf[idx + 2:]
                    return line
                chunk = sock.recv(4096)
                # 还没读到行就遇到 EOF，说明分块数据不完整
                if not chunk:
                    raise OriginProtocolError("分块传输在读取块大小时意外遇到 EOF")
                buf.extend(chunk)

        def read_exact(n):
            # 确保从 buf 里取出恰好 n 字节，不够就继续收
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(max(4096, n - len(buf)))
                if not chunk:
                    raise OriginProtocolError("分块传输在读取块数据时意外遇到 EOF")
                buf.extend(chunk)
            res = bytes(buf[:n])
            buf = buf[n:]
            return res

        while True:
            line = read_line()
            # 块大小为十六进制字符串（忽略可能存在的分块扩展参数，分号后内容）
            # 有的实现在大小后面带 ";" 加扩展参数，切掉只留大小部分
            size_hex = line.split(b";")[0].strip()
            try:
                # 十六进制字符串转成十进制整数，就是这个块有多少字节
                chunk_size = int(size_hex, 16)
            except ValueError:
                raise OriginProtocolError(f"非法的分块大小格式: {size_hex}")

            # 遇到大小为 0 的块，表示分块数据传输结束
            if chunk_size == 0:
                # 读取可能存在的 Trailer 首部直到遇到独立的 \r\n
                # 结束块后面可能还有 Trailer 首部，循环读到空行为止
                while True:
                    trailer_line = read_line()
                    if not trailer_line:
                        break
                break

            # 读取该分块的具体数据
            chunk_data = read_exact(chunk_size)
            # 把这一块的内容拼接到解码结果里
            decoded_body.extend(chunk_data)

            # 每个块数据结尾必须带一个 CRLF (\r\n)
            # 读完数据后再读 2 字节，校验是不是 \r\n，防止报文错位
            crlf = read_exact(2)
            if crlf != b"\r\n":
                raise OriginProtocolError("分块数据末尾缺少 CRLF 校验标识")

        return bytes(decoded_body)

    def safe_fetch(
        self,
        request: HTTPRequest,
        conditional_headers: Optional[List[Tuple[str, str]]] = None,
        timeout: Optional[float] = None
    ) -> HTTPResponse:
        """
        异常安全请求包装：超时返回 504 Gateway Timeout，连接失败返回 502 Bad Gateway
        """
        # 注意：本方法仅供模块自测验证"异常→响应"的对应关系，正式主链路由 A 合成错误响应
        try:
            # 正常走一遍 fetch，成功就原样返回
            return self.fetch(request, conditional_headers, timeout)
        except OriginTimeoutError as e:
            # 超时就地合成一个 504 响应，让测试能验证"异常→响应"的对应关系
            err_body = f"<html><body><h1>504 Gateway Timeout</h1><p>{e}</p></body></html>".encode("utf-8")
            return HTTPResponse(
                status_code=504,
                reason="Gateway Timeout",
                headers=[
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(err_body))),
                    ("Connection", "close")
                ],
                body=err_body
            )
        except OriginConnectionError as e:
            # 连接失败则合成 502 响应
            err_body = f"<html><body><h1>502 Bad Gateway</h1><p>{e}</p></body></html>".encode("utf-8")
            return HTTPResponse(
                status_code=502,
                reason="Bad Gateway",
                headers=[
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Length", str(len(err_body))),
                    ("Connection", "close")
                ],
                body=err_body
            )
