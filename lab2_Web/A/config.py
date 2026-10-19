"""全组公共配置。B/C 可直接读取字段，不需要各自硬编码一份。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyConfig:
    host: str = "127.0.0.1"
    port: int = 8888
    origin_test_port: int = 8000
    max_workers: int = 8
    max_pending_connections: int = 16
    listen_backlog: int = 32
    client_timeout: float = 10.0
    origin_timeout: float = 5.0
    max_request_header_bytes: int = 64 * 1024
    max_response_header_bytes: int = 64 * 1024
    max_response_body_bytes: int = 8 * 1024 * 1024
    max_cache_bytes: int = 64 * 1024 * 1024
    max_discard_bytes: int = 1024 * 1024

    def __post_init__(self):
        if not 0 <= self.port <= 65535:
            raise ValueError("port 必须在 0～65535 之间")
        if self.max_pending_connections < 0:
            raise ValueError("max_pending_connections 不能为负数")
        for name in ("max_workers", "listen_backlog", "client_timeout", "origin_timeout",
                     "max_request_header_bytes", "max_response_header_bytes",
                     "max_response_body_bytes", "max_cache_bytes", "max_discard_bytes"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")


DEFAULT_CONFIG = ProxyConfig()
