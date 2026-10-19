"""全组公共类型入口，复用 C 已有定义，避免两份数据类产生不同类型身份。

A/B 统一从本文件导入。HTTPResponse.to_http_bytes() 仅供调试；
正式客户端输出使用 write_client_response()，重建连接和长度首部。
"""
if __package__:
    from .C.http_types import HTTPRequest, HTTPResponse, CacheEntry
else:
    from C.http_types import HTTPRequest, HTTPResponse, CacheEntry

__all__ = ["HTTPRequest", "HTTPResponse", "CacheEntry"]
