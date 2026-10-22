"""线程安全的缓存存储；网络访问必须在本模块锁外执行。"""
from collections import OrderedDict
from copy import deepcopy
import threading
if __package__:
    from .config import DEFAULT_CONFIG
    from .http_types import CacheEntry
else:
    from config import DEFAULT_CONFIG
    from http_types import CacheEntry


def response_size(response):
    """响应状态行、首部、正文的字节总量，不包含 Python 对象开销。"""
    head = f"HTTP/1.1 {response.status_code} {response.reason}\r\n"
    head += "".join(f"{k}: {v}\r\n" for k, v in response.headers) + "\r\n"
    return len(head.encode("iso-8859-1")) + len(response.body)


class CacheStore:
    """只管理 CacheEntry 和 LRU，不负责新鲜度判断或网络请求。"""
    def __init__(self, max_entries=100, max_bytes=DEFAULT_CONFIG.max_cache_bytes):
        for name, value in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        # 顺序从最久未使用到最近使用；查到或写入条目时移到末尾。
        self._cache = OrderedDict()
        self._total_bytes = 0
        self.lock = threading.Lock()

    def _remove(self, key):
        """内部删除方法：调用方必须持锁，统一维护条目和总字节数。"""
        entry = self._cache.pop(key, None)
        if entry is not None:
            self._total_bytes -= entry.entry_size
        return entry is not None

    def get(self, key):
        """返回独立快照，包括过期条目，供控制器生成条件请求。"""
        with self.lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            self._cache.move_to_end(key)
            # 响应含可变的首部列表，浅拷贝不能防止外部修改缓存。
            return deepcopy(entry)

    def put(self, key, entry):
        """保存或替换完整条目；超出单条容量时返回 False。"""
        if not isinstance(entry, CacheEntry):
            raise TypeError("entry must be CacheEntry")
        if not isinstance(entry.response.body, bytes):
            raise TypeError("response body must be bytes")
        # 复制和计量放在锁外，减少占锁时间；不相信调用方填写的 entry_size。
        saved = deepcopy(entry)
        saved.entry_size = response_size(saved.response)
        with self.lock:
            # 先扣掉旧大小；超大新响应也不能让同键旧版本继续留在缓存。
            self._remove(key)
            if saved.entry_size > self.max_bytes:
                return False
            self._cache[key] = saved
            self._total_bytes += saved.entry_size
            # 两项上限同时满足才停止，每次淘汰字典最前面的 LRU 条目。
            while len(self._cache) > self.max_entries or self._total_bytes > self.max_bytes:
                self._remove(next(iter(self._cache)))
            return True

    def delete(self, key):
        """删除条目并扣减容量，返回该键原先是否存在。"""
        with self.lock:
            return self._remove(key)

    def clear(self):
        """同时清空条目与容量统计，供场景测试复位。"""
        with self.lock:
            self._cache.clear()
            self._total_bytes = 0

    def size(self):
        """返回缓存条目数量，与 total_bytes 的字节总量区分。"""
        with self.lock:
            return len(self._cache)

    def total_bytes(self):
        """返回已存响应的字节数，不是 Python 进程的实际内存用量。"""
        with self.lock:
            return self._total_bytes

    def keys(self):
        """返回 LRU 顺序快照，不暴露内部字典迭代器。"""
        with self.lock:
            return list(self._cache)
