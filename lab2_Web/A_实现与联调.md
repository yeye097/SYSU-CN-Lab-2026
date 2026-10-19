# 成员 A：代理接入实现与联调

基于仓库 `bf58e6d`、`架构.docx`、`项目指导路线.md` 和 `C/接口公布说明.md` 实现。

## 范围与文件

完成 A 的 HTTP GET 接入层：有限线程池与排队上限、请求流式接收与解析、调用 B、响应重建、错误映射、连接清理、日志与测试。不实现 B 的缓存策略。

| 文件 | 用途 |
| --- | --- |
| `proxy_server.py` | 启动入口、请求解析、响应发送、并发调度 |
| `config.py` | 全组配置，测试可用 `dataclasses.replace` 覆盖 |
| `http_types.py` | 公共类型入口，重导出 C 已有三个类，避免复制出不同类型 |
| `origin_client.py` | 重导出 C 实现和异常，保留 C 原文件不变 |
| `errors.py` | 公共异常入口及客户端请求异常 |
| `log_context.py` | 请求编号、URL、缓存状态日志格式 |
| `test_proxy_server.py` | A 模块测试与 A/C 真实 socket 联调测试 |
| `run_tests.py` | 运行 A 和 C 的完整测试集合 |

## 调用与接口约束（给 B）

调用链：`accept → handle_client → read_client_request → controller.handle → 响应校验与重建 → sendall → close`。

```python
from http_types import HTTPRequest, HTTPResponse, CacheEntry
from config import DEFAULT_CONFIG

class CacheController:
    def __init__(self, origin_client):
        # 保存注入的 C 客户端；在这里创建且只创建一次共享缓存。
        # 存储容量读取 DEFAULT_CONFIG.max_cache_bytes。
        ...

    def handle(self, request: HTTPRequest) -> HTTPResponse:
        # 完成缓存策略；必要时在缓存锁之外调用：
        # self.origin_client.fetch(request, conditional_headers=[...])
        ...
```

- B 文件放在 `lab2_Web/cache_controller.py`、`cache_store.py`。A 已固定构造方式 `CacheController(origin_client=origin)`，实例由所有工作线程共享。
- `handle()` 返回最终响应，内部验证得到 304 时由 B 合并缓存；客户端自己带条件头而绕过缓存时，最终 304 允许直接返回。
- A 的公开函数仍为 `read_client_request(sock)`、`write_client_response(sock, response)`；仅增加可选关键字 `config` 供测试覆盖，不影响原调用。
- `headers` 是保留重复项的列表，查找不区分大小写；正文是 `bytes`，已去除分块传输编码，仍保留 gzip 等内容编码。
- 源站异常使用 `errors.py` 中的 `OriginTimeoutError` / `OriginConnectionError` / `OriginProtocolError`，与 C 抛出的异常是同一对象。不要另定义同名的新异常类。
- B 不吞掉源站异常，不调用 C 的 `safe_fetch()`；A 将超时映射为 504，其他源站异常映射为 502。
- B 可记录 `logger.info("直接返回缓存", extra={"cache_status": "HIT"})`；其他取值为 MISS / REVALIDATE / BYPASS / UPDATE。A 自动附加线程内请求编号和 URL，不修改请求字段或 `handle()` 签名。

## 配置与协议行为

默认：代理 `127.0.0.1:8888`；测试源站 8000；8 个工作线程、16 个排队连接；请求读取总期限和客户端发送超时 10 秒；源站单次 socket 超时 5 秒；请求头和输出响应头各 64 KiB；输出正文上限 8 MiB；缓存建议上限 64 MiB。

- 一个连接处理一个请求，响应后关闭；多个连接同时处理。超过处理与排队容量返回 503。排队时间不计入开始处理后的首部读取期限。
- 接受代理 absolute-form URL，也接受带 Host 的 origin-form 路径；支持 HTTP/1.0、HTTP/1.1。仅支持 `http` GET，其他方法或 HTTPS/CONNECT 返回 501。
- URL 规范化为小写主机、去默认 80 端口、补 `/`，保留路径和查询串；Host 仅允许主机及端口，absolute-form 的 Host 按 URL 重建。
- 拒绝缺失/重复 Host、非法首部、非零/重复 Content-Length、Transfer-Encoding、请求体和流水线；零长度 GET 可接受。首部过大返回 431，读取超时返回 408。
- 输出移除 hop-by-hop 首部和 Connection 指定的字段，重建 Content-Length 和 `Connection: close`；不修改共享响应对象。
- 204/304 无正文且不生成 Content-Length；205 正文为空、长度为 0。HEAD 返回 501，但错误响应也遵守 HEAD 无正文规则。
- 非法请求返回 400；源站异常 502/504；意外内部错误 500。客户端提前关闭不影响其他请求。
- 返回后先 shutdown 发送方向，再限时 0.1 秒、限量 1 MiB 排空来包，减少未读数据导致的 RST；不是对任意恶意超大输入都保证正常挥手。
- 退出时停止接入，唤醒正在等客户端数据的连接，等待源站调用在其超时规则内返回，关闭监听和工作线程。

## Windows 运行（按顺序）

只用 Python 标准库，建议 Python 3.10 或更新版本。先进入仓库的 `lab2_Web` 目录，以下三个终端均在该目录操作。

1. **终端 1，启动源站**：

   ```powershell
   python C/mock_origin_server.py --port 8000
   ```

   预期源站开始监听 8000。

2. **终端 2，启动 A/C 联调代理**（当前 B 尚未提交）：

   ```powershell
   python proxy_server.py --port 8888 --no-cache
   ```

   预期显示监听地址，并明确提示当前不缓存；每次请求均访问源站。未加此参数且缺少 B 时，程序给出说明并退出，不冒充完整缓存代理。

3. **终端 3，通过指定代理发起请求**：

   ```powershell
   curl.exe --noproxy "" --proxy http://127.0.0.1:8888 http://127.0.0.1:8000/index.html -i
   ```

   `--noproxy ""` 清除这次命令的代理绕过列表，避免本机 NO_PROXY 使请求直连源站。Windows PowerShell 5.1 若不能正确传递空字符串，可改用 C 的原始 socket 工具（当前仅验证首次转发）：

   ```powershell
   python C/traffic_generator.py --scene 1
   ```

   预期收到 200 和页面内容，代理日志包含请求编号、URL、BYPASS。浏览器也可设置 HTTP 代理 `127.0.0.1:8888`，关闭本地地址绕过，访问明确以 `http://` 开头的地址。

4. **运行全部模块测试**：

   ```powershell
   python run_tests.py
   ```

   预期结果 `OK`。只测 A 可执行 `python -m unittest test_proxy_server -v`。A 使用空闲端口；C 原测试使用 18000/18001，请确保未被占用。

5. **B 完成后启用正式缓存模式**：把 B 的两个文件放到本目录，停止旧代理，再运行：

   ```powershell
   python proxy_server.py --port 8888
   ```

   再由 C 按五场景抓包验收。`--no-cache` 下不可证明 HIT、304 合并或缓存更新。

## 报告截图位置与注解要点

| 建议截图 | 解释重点 |
| --- | --- |
| `ProxyServer.serve_forever()` | accept 主循环、线程池提交、信号量限制已接收连接数量 |
| `read_client_request()` | TCP 字节流、CRLF CRLF 定界、Latin-1 首部解析、大小与总读取期限 |
| `ProxyServer.handle_client()` | 共享控制器调用、缓存职责归 B、错误映射和请求日志 |
| `_response_bytes()` 与 `write_client_response()` | hop-by-hop 过滤、重算正文长度、保留内容编码、sendall |
| `_run_client()`、`shutdown()` | with 关闭连接、finally 释放槽位、退出时唤醒等待线程 |

## 验证边界与 C 后续对接

本次只完成 A；B 的命中/新鲜度/304 合并尚不可验收。未执行 Windows 浏览器实机运行或 Wireshark 抓包，自动测试在 Linux/Python 环境完成。

交付检查：A 新增 21 项测试、C 原有 12 项测试，共 33 项通过；覆盖请求分段、URL/Host、错误映射、响应重建、并发、过载恢复、连接退出，以及 C 的定长/分块/EOF 三种真实转发。运行 `python run_tests.py` 可复现。

响应大小限制目前在 **A 拿到完整响应、输出之前**执行，不限制 C 接收期间的峰值内存。C 的接口说明已将“接收时限制首部/正文大小”列为后续工作，应由 C 在三种定界路径中读取同名配置。C 原实现对 Content-Length 正文提前 EOF 未严格报错，也应由 C 补齐；A 重算长度不能代替源站协议校验。

C 的流量工具场景 1 只重置源站，不清空代理缓存：正式验收首次 MISS 前应重启代理或使用新 URL。工具与报告中的条件头以实际抓包为准（方案优先 ETag，并非固定 If-Modified-Since）。
