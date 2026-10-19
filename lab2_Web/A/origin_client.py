"""根目录接入适配：复用 C 已提交的实现，保留原异常对象的身份。

C 后续将实现迁至根目录时可以替换此文件；当前无需复制或修改 C 的代码。
"""
if __package__:
    from .C.origin_client import (OriginClient, OriginClientError, OriginTimeoutError,
                                  OriginConnectionError, OriginProtocolError)
else:
    from C.origin_client import (OriginClient, OriginClientError, OriginTimeoutError,
                                 OriginConnectionError, OriginProtocolError)

__all__ = ["OriginClient", "OriginClientError", "OriginTimeoutError",
           "OriginConnectionError", "OriginProtocolError"]
