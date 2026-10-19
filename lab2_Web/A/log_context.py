"""线程内请求上下文：B/C 的日志自动关联 A 分配的编号与 URL。"""
import logging
from contextvars import ContextVar

request_id = ContextVar("request_id", default="-")
request_url = ContextVar("request_url", default="-")


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        record.url = request_url.get()
        if not hasattr(record, "cache_status"):
            record.cache_status = "-"
        return True


def configure_logging():
    # 配置放在入口，覆盖 C 导入时设置的默认格式；不在每个请求里反复配置。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(threadName)s] [%(name)s] "
               "[id=%(request_id)s] [%(cache_status)s] [url=%(url)s] %(message)s",
        force=True,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestContextFilter())
