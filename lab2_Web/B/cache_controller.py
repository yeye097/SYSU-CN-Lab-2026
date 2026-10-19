"""HTTP GET 共享缓存控制；源站异常原样交给 A。"""
import logging
import math
import re
import time
from copy import deepcopy
from email.utils import parsedate_to_datetime, formatdate
from cache_store import CacheStore
from http_types import CacheEntry, HTTPResponse
from errors import OriginProtocolError


class CacheController:
    """A 的工作线程共享一个控制器；策略在此判断，存储加锁由 CacheStore 负责。"""
    def __init__(self, origin_client, store=None, *, clock=None, logger=None):
        # 注入 C 的客户端，单元测试可换成模拟对象；clock 用于控制测试时间。
        self.origin_client = origin_client
        self.store = store if store is not None else CacheStore()
        self.clock = clock if clock is not None else time.time
        self.logger = logger if logger is not None else logging.getLogger(__name__)

    @staticmethod
    def get_header(headers, name):
        """首部名忽略大小写；保留原列表及重复首部，不转换为字典。"""
        return next((v for k, v in headers if k.lower() == name.lower()), None)

    @staticmethod
    def parse_cache_control(headers):
        """合并多行 Cache-Control，并将指令解析为名称到取值的映射。"""
        directives = {}
        for name, value in headers:
            if name.lower() != "cache-control":
                continue
            # 只在引号外的逗号处分割，例如 no-cache="X,Y" 仍是一个指令。
            for item in re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', value):
                key, sep, val = item.strip().partition("=")
                key = key.strip().lower()
                if key:
                    # 重复时效指令按无效处理，避免歧义导致错误延长新鲜期。
                    if key in directives and key in ("max-age", "s-maxage"):
                        directives[key] = None
                    else:
                        directives[key] = val.strip().strip('"') if sep else True
        return directives

    @staticmethod
    def _seconds(value):
        """时效值只接受非负整数字符串，无效值保守按零处理。"""
        return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]+", value) else 0

    @staticmethod
    def _date(value):
        """将 HTTP 日期转为时间戳；缺失或解析失败时由调用方处理。"""
        try:
            return parsedate_to_datetime(value).timestamp() if value else None
        except (ValueError, TypeError, OverflowError):
            return None

    def freshness_lifetime(self, headers, response_time):
        """共享缓存优先 s-maxage，其次 max-age，再用 Expires，默认零。"""
        directives = self.parse_cache_control(headers)
        for name in ("s-maxage", "max-age"):
            if name in directives:
                return self._seconds(directives[name])
        expires = self._date(self.get_header(headers, "Expires"))
        date = self._date(self.get_header(headers, "Date"))
        return max(0, expires - (date if date is not None else response_time)) if expires is not None else 0

    def should_bypass(self, request):
        """按实验约定绕过个性化、范围和客户端条件请求，既不查也不存。"""
        names = {k.lower() for k, _ in request.headers}
        return (request.method.upper() != "GET"
                or bool(names & {"authorization", "cookie", "range"})
                or any(name.startswith("if-") for name in names))

    def should_cache(self, method, status_code, headers):
        """只保存允许共享的 GET 200；本实验不实现 Vary 变体缓存。"""
        directives = self.parse_cache_control(headers)
        names = {k.lower() for k, _ in headers}
        return (method.upper() == "GET" and status_code == 200
                and not names & {"set-cookie", "vary"}
                and not any(k in directives for k in ("no-store", "private")))

    def _entry(self, response, request_time, response_time):
        """记录收到响应时的初始年龄，不能把收到响应当成资源刚刚生成。"""
        headers = response.headers
        date = self._date(self.get_header(headers, "Date"))
        # Date 反映生成时间；Age 反映上游已驻留时间，并加上本次网络耗时。
        apparent_age = max(0, response_time - date) if date is not None else 0
        corrected_age = self._seconds(self.get_header(headers, "Age")) + max(0, response_time - request_time)
        return CacheEntry(
            response=deepcopy(response), request_time=request_time,
            response_time=response_time, initial_age=max(apparent_age, corrected_age),
            freshness_lifetime=self.freshness_lifetime(headers, response_time),
            etag=self.get_header(headers, "ETag"),
            last_modified=self.get_header(headers, "Last-Modified"))

    @staticmethod
    def build_conditional_headers(entry):
        """验证标识只选一个：ETag 优先；没有标识时无条件重新获取。"""
        if entry is not None and entry.etag:
            return [("If-None-Match", entry.etag)]
        if entry is not None and entry.last_modified:
            return [("If-Modified-Since", entry.last_modified)]
        return []

    def _cached_response(self, entry):
        # 修改副本的 Age，避免不同请求更新同一份首部列表。
        response = deepcopy(entry.response)
        response.headers = [(k, v) for k, v in response.headers if k.lower() != "age"]
        response.headers.append(("Age", str(math.floor(self.current_age(entry, self.clock())))))
        return response

    @staticmethod
    def current_age(entry, now):
        """当前年龄 = 收到响应时的年龄 + 在本代理中的驻留时间。"""
        return entry.initial_age + max(0, now - entry.response_time)

    def _log(self, request, result):
        # A 的 RequestContextFilter 自动填充线程内 request_id 和 URL。
        self.logger.info("缓存决策 %s: %s", result, request.url,
                         extra={"cache_status": result})

    def _merge_304(self, entry, validation, response_time):
        # 304 无正文；忽略其正文长度和逐跳首部，保留缓存实体正文。
        excluded = {"connection", "keep-alive", "proxy-authenticate",
                    "proxy-authorization", "te", "trailer", "transfer-encoding",
                    "upgrade", "content-length"}
        for k, v in validation.headers:
            if k.lower() == "connection":
                excluded.update(token.strip().lower() for token in v.split(","))
        updates = [(k, v) for k, v in validation.headers if k.lower() not in excluded]
        if self.get_header(updates, "Date") is None:
            updates.append(("Date", formatdate(response_time, usegmt=True)))
        # 新首部按名称覆盖旧值；未更新的策略沿用旧值，旧 Age 不沿用。
        replaced = {k.lower() for k, _ in updates} | {"age"}
        headers = [(k, v) for k, v in entry.response.headers
                   if k.lower() not in replaced and k.lower() not in excluded - {"content-length"}]
        headers.extend(updates)
        return HTTPResponse(entry.response.status_code, entry.response.reason,
                            headers, entry.response.body)

    def handle(self, request):
        """处理顺序：BYPASS → 新鲜 HIT → MISS/REVALIDATE → 保存或更新。"""
        # 1. 客户端指定不存储，或请求涉及身份/条件信息时直接交给 C。
        directives = self.parse_cache_control(request.headers)
        if self.should_bypass(request) or "no-store" in directives:
            self._log(request, "BYPASS")
            return self.origin_client.fetch(request)
        # 2. URL 已由 A 规范化，含查询串；获取快照后存储锁已释放。
        entry = self.store.get(request.url)
        fresh = entry is not None and "no-cache" not in self.parse_cache_control(entry.response.headers)
        if fresh:
            age = self.current_age(entry, self.clock())
            fresh = age < entry.freshness_lifetime
            if "max-age" in directives:
                fresh = fresh and age <= self._seconds(directives["max-age"])
        if fresh and "no-cache" not in directives:
            self._log(request, "HIT")
            return self._cached_response(entry)
        # 3. 过期或要求验证时不能直接返回旧正文，需要访问源站。
        self._log(request, "REVALIDATE" if entry is not None else "MISS")
        conditional = self.build_conditional_headers(entry)
        request_time = self.clock()
        # 存储层已经释放锁，此处网络访问不会持有全局缓存锁。
        response = self.origin_client.fetch(request, conditional_headers=conditional or None)
        response_time = self.clock()
        # 4. 304 只有元数据，需要复用旧正文，再按更新后的策略决定是否保存。
        if response.status_code == 304:
            if entry is None or not conditional:
                raise OriginProtocolError("unexpected 304 without a conditional cached entry")
            response = self._merge_304(entry, response, response_time)
            refreshed = self._entry(response, request_time, response_time)
            if self.should_cache(request.method, response.status_code, response.headers):
                self.store.put(request.url, refreshed)
            else:
                self.store.delete(request.url)
            self._log(request, "UPDATE")
            return self._cached_response(refreshed)
        # 5. 新响应允许缓存则替换；否则移除旧条目，避免继续使用旧版本。
        if self.should_cache(request.method, response.status_code, response.headers):
            self.store.put(request.url, self._entry(response, request_time, response_time))
        else:
            self.store.delete(request.url)
        if entry is not None:
            self._log(request, "UPDATE")
        return response
