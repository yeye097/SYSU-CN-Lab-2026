# Lab2 Web 缓存实验整合版

运行代码已整合到本目录，沿用「客户端 → A 代理接入 → B 缓存控制 → C 源站通信」架构。A、B、C 子目录保留各成员原始交付，后续运行和修改以本目录文件为准，不要再用原稿覆盖整合版。

仅使用 Python 标准库，Python 3.9 及以上可运行；本次在 Windows / Python 3.9.25 验证。支持普通 HTTP GET 和多客户端连接，每条连接处理一次请求后关闭；不支持 HTTPS/CONNECT 或带正文的请求。

## 启动

在本目录打开两个终端，分别执行：

```powershell
# 终端 1：可控源站，默认 127.0.0.1:8000
python -B mock_origin_server.py --port 8000
```

```powershell
# 终端 2：完整缓存代理，默认 127.0.0.1:8888
python -B proxy_server.py --port 8888
```

第三个终端验证首次获取和缓存命中：

```powershell
curl.exe --noproxy "" --proxy http://127.0.0.1:8888 http://127.0.0.1:8000/index.html -i
curl.exe --noproxy "" --proxy http://127.0.0.1:8888 http://127.0.0.1:8000/index.html -i
```

两次请求须在 10 秒内完成。代理日志应先出现 MISS，再出现 HIT；HIT 响应含 Age，且不产生对应回源请求。也可将客户端的 HTTP 代理配置为 `127.0.0.1:8888`，注意客户端可能默认绕过本地地址，或受自己的缓存影响。

在仓库根目录也可执行 `python -B -m lab2_Web.proxy_server --port 8888`。`--no-cache` 仅供转发对照实验，正式缓存验收不要添加此参数。Ctrl+C 停止服务，重新启动代理会清空内存缓存。

## 文件与公共接口

| 文件 | 来源与职责 |
| --- | --- |
| proxy_server.py、config.py、errors.py、log_context.py | A：接入、配置、错误映射、统一日志 |
| cache_controller.py、cache_store.py | B：新鲜度、条件验证、缓存存储和 LRU |
| origin_client.py、mock_origin_server.py、traffic_generator.py | C：源站通信、可控源站、抓包场景 |
| http_types.py | 采用 C 已实现、A 已认可的数据结构，作为整合版唯一公共类型定义 |
| test_proxy_server.py、test_cache.py、test_origin_client.py | 三位成员原有的 42 项模块测试，导入统一指向整合版 |
| test_integration.py | 新增跨模块、真实 TCP 和边界回归测试 |
| run_tests.py | 统一测试入口，默认执行所有测试 |

核心接口保持原约定：

```python
read_client_request(sock) -> HTTPRequest
CacheController(origin_client=origin).handle(request) -> HTTPResponse
OriginClient.fetch(request, conditional_headers=None) -> HTTPResponse
write_client_response(sock, response) -> None
```

headers 保留为元组列表，body 为 bytes，URL 由 A 规范化。B 在缓存锁之外调用 C；C 抛出的异常经 errors.py 共享，A 将超时映射为 504、源站连接或协议错误映射为 502。正式链路不调用 `safe_fetch()` 或 `HTTPResponse.to_http_bytes()`。

默认配置：8 个工作线程、16 个排队连接、64 KiB 请求/响应首部、8 MiB 响应正文、64 MiB 缓存以及最多 100 个条目。响应接收上限和缓存容量由 `build_controller(config)` 传到对应模块，调整 `ProxyConfig` 即可统一生效。

## 兼容性检查与修复

原三份代码通过 B 的测试入口可以共同导入，原有 42 项测试全部通过，但缺少独立运行的根目录整合版和完整 A→B→C 缓存联调证据。此次修复如下：

| 问题 | 整合版处理 |
| --- | --- |
| 文件分散、两个同名测试入口、A 公共入口仍引用 C 子目录 | 根目录独立运行，统一类型、异常、模块导入和测试入口；运行代码不依赖原稿目录 |
| C 收到不足 Content-Length 的正文仍成功返回，B 可能缓存残缺页面 | 提前 EOF 抛协议错误，客户端收到 502，残缺响应不入缓存；超过声明长度的尾部字节不计入正文 |
| C 接收阶段无头体大小上限，A 只在发送前检查 | 定长、chunked、EOF 均在接收期间限制大小，分块行和 Trailer 也有限额 |
| 无效/重复 Content-Length、错误传输编码和非法状态行被宽松接受 | 在 C 层拒绝歧义或不支持的报文并映射为 502，保留有效重复首部 |
| 非默认缓存容量、响应限额未向 B/C 传递 | 启动入口显式注入运行配置 |
| 超长 Age/max-age 数字可引发 ValueError/OverflowError | 去除前导零，过大的 delta-seconds 饱和到 2^31，避免内部 500 |
| 测试源站在 ETag 不匹配时仍可能按 Last-Modified 返回 304 | 存在 If-None-Match 时优先按 ETag 判断；去掉重复 Date |
| 抓包脚本 max-age=0 在年龄为零时可能直接 HIT | 场景 3/4 改用 no-cache，确保执行条件验证 |
| C 导入时修改全局日志格式 | 日志仅由代理启动入口统一配置，保留请求编号、URL 和缓存决策标记 |

## 测试

```powershell
python -B run_tests.py
```

测试失败返回非零退出码。必须允许本机 TCP 监听与连接；C 原有测试占用 18000/18001 端口，新增联调测试使用系统分配的临时端口，不依赖外网。各模块也可通过 `python -B -m unittest test_cache -v` 等命令单独执行。

本次验证结果见 `test-results.txt`，共 57 项：原有 42 项，加 15 项新增回归。新增测试覆盖：

- MISS → HIT，核对源站请求数不增加；过期后携带 ETag，源站返回 304，客户端仍得到完整 200 正文。
- 源站更新后取得新 200 和新正文，后续命中更新后的内容。
- 四个不同 URL 同时回源并核对各自正文，再次并发访问全部命中。
- 三种正文定界、客户端条件头/Cookie 绕过、截断返回 502 且不缓存、超时返回 504 后服务可继续。
- 配置传递、接收限额、非法报文、超长缓存时间值、测试源站条件头优先级及抓包场景强制验证。

测试日志中的预期超时、502 和模拟内部异常用于验证错误分支，最终以 unittest 汇总的 OK/FAILED 为准。过期联调用同步推进的源站 Date 与缓存时钟实现确定性验证，网络收发使用真实本机 TCP。

另已将 10 个运行文件单独复制到临时目录，不带 A/B/C 原稿目录，从命令行启动源站与代理，验证首次获取、本地命中和更新正文均成功；测试后已停止进程并清理临时目录。

## 抓包步骤与边界

```powershell
python -B traffic_generator.py --all
# 或按顺序单独执行 --scene 1 至 --scene 5
```

1. 开始场景 1 前重启代理。`/reset` 只重置源站，不会清除代理缓存。
2. 场景 1 后 10 秒内执行场景 2；交互等待超过新鲜期时应重做这两个场景，否则会出现正常的过期验证，不能作为 HIT 证据。
3. 场景 3/4 通过 no-cache 强制验证，条件首部优先为 If-None-Match。若报告需要自然到期的抓包，等待超过 10 秒后发送普通 GET 即可。
4. Wireshark 选择 loopback 网卡，使用过滤器 `http and (tcp.port == 8888 or tcp.port == 8000)`；必要时把相应端口 Decode As HTTP。`/reset`、`/update` 是脚本直连源站的控制流量，分析时应与代理回源流量区分。

此次完成代码整合和自动化测试，没有生成 Wireshark `.pcapng`、截图或实验报告。未验证外部浏览器、外网源站及 Linux/macOS 环境。源站超时是单次 socket 操作超时，未实现整个下载过程的总期限。源站传输编码仅支持 chunked，不支持其它 Transfer-Encoding 或 1xx 中间响应；这些情况返回 502。

并发测试验证请求不串流、不同 URL 可同时回源，以及缓存存储的锁与容量一致性；同一 URL 的并发未命中可各自回源，未实现请求合并，也未保证并发重验证按资源版本顺序提交。这些边界不应表述为已通过完整 HTTP 标准或所有并发时序验证。
