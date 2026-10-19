"""公共异常入口：B 从此处导入源站异常，A 才能统一捕获同一异常类型。"""
if __package__:
    from .origin_client import (OriginClientError, OriginTimeoutError,
                                OriginConnectionError, OriginProtocolError)
else:
    from origin_client import (OriginClientError, OriginTimeoutError,
                               OriginConnectionError, OriginProtocolError)


class ClientRequestError(Exception):
    """能够返回给客户端的请求错误；默认 400，也用于 408/431/501。"""
    def __init__(self, message, status_code=400, *, head_request=False):
        super().__init__(message)
        self.status_code = status_code
        self.head_request = head_request


class ClientDisconnected(Exception):
    """尚未发送请求就关闭连接，例如浏览器的预连接。无需返回错误。"""
