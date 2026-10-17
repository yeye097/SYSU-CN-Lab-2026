# 从标准库导入数据类相关的工具：dataclass 是装饰器，field 用来设置默认值
from dataclasses import dataclass, field
# 导入类型注解用的工具，它们只是给人看的提示，不影响程序运行
from typing import List, Tuple, Optional


# @dataclass 会自动帮这个类生成 __init__、__repr__ 等方法，
# 这样我就可以直接 HTTPRequest(method="GET", ...) 来创建对象
@dataclass
class HTTPRequest:
    """
    客户端请求规范数据结构
    统一放入 http_types.py，三人共同确定
    """
    method: str
    url: str  # 规范化完整 URL（含查询串），用于缓存索引
    host: str
    port: int
    path: str  # 资源路径（含查询串，如 /index.html?a=1）
    # headers 默认给空列表，用 field 是为了避免多个对象共享同一个默认列表
    headers: List[Tuple[str, str]] = field(default_factory=list)  # 保留重复项

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """大小写无关查找首部单值（返回首次出现的匹配项）"""
        # 先把要找的名字转成小写，比较时两边都转小写，这样就不分大小写了
        target = name.lower()
        # 逐个首部遍历对比，找到第一个同名的就返回它的值
        for k, v in self.headers:
            if k.lower() == target:
                return v
        # 都没找到就返回调用方给的默认值（没传的话就是 None）
        return default

    def get_headers(self, name: str) -> List[str]:
        """大小写无关查找首部所有值（支持多值首部）"""
        target = name.lower()
        # 用列表推导把所有同名首部的值一次性收集起来
        return [v for k, v in self.headers if k.lower() == target]


@dataclass
class HTTPResponse:
    """
    HTTP 响应规范数据结构
    统一放入 http_types.py
    """
    status_code: int
    reason: str
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""  # 内部已去除分块传输编码，保留原有内容编码

    def get_header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """大小写无关查找首部单值"""
        target = name.lower()
        for k, v in self.headers:
            if k.lower() == target:
                return v
        return default

    def get_headers(self, name: str) -> List[str]:
        """大小写无关查找首部所有值"""
        target = name.lower()
        return [v for k, v in self.headers if k.lower() == target]

    @property
    def is_not_modified(self) -> bool:
        """是否命中 304 条件请求"""
        # property 让我可以像 resp.is_not_modified 这样直接当属性用，不用加括号
        return self.status_code == 304

    @property
    def is_success(self) -> bool:
        """是否为 200 OK 成功响应"""
        return self.status_code == 200

    def to_http_bytes(self) -> bytes:
        """
        重构为完整的 HTTP 响应字节流
        供代理层 (A) 直接发送给客户端浏览器
        """
        # 先拼状态行，再把每个首部拼成 "名字: 值" 一行
        status_line = f"HTTP/1.1 {self.status_code} {self.reason}\r\n"
        header_lines = [f"{k}: {v}\r\n" for k, v in self.headers]
        # 首部拼完后补一个空行，表示首部结束、后面就是正文
        headers_block = "".join(header_lines) + "\r\n"
        # 首部按 HTTP 规定用 latin1 编码，正文本来就是 bytes，直接拼在后面
        return status_line.encode("latin1") + headers_block.encode("latin1") + self.body


@dataclass
class CacheEntry:
    """
    缓存条目数据结构（由角色 B 维护和使用）
    """
    response: HTTPResponse
    request_time: float
    response_time: float
    initial_age: float
    # freshness_lifetime 是新鲜期，超过这个时间缓存就算过期，需要重新验证
    freshness_lifetime: float
    # etag 是验证标识，做条件请求时优先用它（没有 etag 才用 last_modified）
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    entry_size: int = 0
