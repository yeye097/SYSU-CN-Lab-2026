# B 缓存模块

基于远端 main 提交 `430b845`，复用 A/C 的实际公共接口。B 目录只保留五个文件：

| 文件 | 作用 |
| --- | --- |
| cache_controller.py | 缓存决策、新鲜度、条件验证、304 合并和 Age 更新 |
| cache_store.py | 内存存储、字节容量、LRU 和线程保护 |
| test_cache.py | 必要的缓存策略、存储和并发测试 |
| run_tests.py | 准备导入路径并运行测试 |
| README.md | 接口、运行方式和实现说明 |

建议先读控制器的 handle，再读存储层的 get/put，最后查看对应测试。代码注释重点解释策略选择和并发边界。

## 测试

在 `lab2_Web` 目录运行：

```powershell
python -B B/run_tests.py
python -B B/run_tests.py --all
```

前者运行 B 的 9 项核心模块测试，后者加上 A 原有 21 项、C 原有 12 项，共 42 项。也可执行 `python -B B/test_cache.py` 直接运行 B 测试。

B 使用模拟源站和可控时钟，覆盖命中与过期、正文替换、ETag/Last-Modified 验证、304 刷新、缓存指令、绕过规则、Date/Age、新鲜期、LRU、字节容量、快照隔离及并发读写。

为保持文件简短，已删除之前的真实 A/B/C TCP 联调测试代码；此前的联调结果仅作为历史验证，不属于当前测试集合。`--all` 包含 A/C 自带的网络测试，需允许本机 TCP 连接，C 使用 18000/18001 端口。`-B` 禁止生成 __pycache__。

## 最终集成与启动

按 A 的交付说明，把 A 文件复制到 lab2_Web 根目录，保留 C 子目录；再把 B 的两个核心文件复制到同一根目录：

```powershell
Copy-Item A/*.py .
Copy-Item B/cache_controller.py, B/cache_store.py .
```

两个终端分别执行：

```powershell
# 终端 1：源站
python -B C/mock_origin_server.py --port 8000
```

```powershell
# 终端 2：启用 B 缓存的代理
python -B proxy_server.py --port 8888
```

不再提供单独的 B 代理启动脚本，完整代理统一由 A 的入口启动。首次 MISS 验收前重启代理；源站 /reset 不会清空代理缓存。

## 公共接口

```python
controller = CacheController(origin_client=origin)
response = controller.handle(request)
```

请求、响应和缓存条目统一从 A 的 http_types 入口导入，复用 C 中的同一份类；headers 为保留重复项的元组列表，body 为 bytes，URL 由 A 规范化并保留查询串。CacheEntry 使用实际字段 entry_size，不扩展公共类型字段。

源站异常从 A/errors.py 导入，与 C 抛出的异常具有相同身份。B 不调用 safe_fetch，不合成错误响应；源站异常向上传递，由 A 映射 502/504。B 使用 extra 中的 cache_status 记录决策，A 自动补充线程内请求编号和 URL。

存储接口为 put(url, CacheEntry)、get(url)、delete、clear、size、total_bytes、keys；get 返回深拷贝快照。默认字节容量读取 DEFAULT_CONFIG.max_cache_bytes（64 MiB），另有默认 100 条的数量上限，可通过 CacheStore 构造参数调整。

## 缓存策略

- 请求含 Authorization、Cookie、Range、If-* 或 no-store 时绕过，既不查也不存；请求 no-cache 强制验证，max-age 限制可接受年龄。
- 仅保存满足策略的 GET 200。Set-Cookie、Vary、no-store、private 不保存；不要求 public。
- 新鲜期按 s-maxage、max-age、Expires 的顺序计算，默认零；重复或无效的时效指令按零处理。
- 初始年龄取 apparent_age 与 corrected_age 的较大值；前者是接收时间减 Date，后者是 Age 加源站请求耗时。当前年龄再加本代理驻留时间。命中时更新返回副本的 Age。
- ETag 优先生成 If-None-Match，否则 Last-Modified 生成 If-Modified-Since；均不存在时重新获取。
- 304 合并新首部、复用旧正文，忽略其 Content-Length 和逐跳首部，重新计算年龄与容量。缺少 Cache-Control 时保留旧策略；缺少 Date 时用接收时间补全。新策略禁止缓存时，删除条目并为本次请求返回完整正文。
- 新 200 替换旧内容；不可缓存的新响应移除旧条目。源站异常时不返回过期内容。没有对应条件缓存却收到 304 时抛出公共协议异常。

## 存储与并发

响应字节数包含状态行、首部及正文，不含 Python 对象开销。替换、删除、LRU 淘汰同步维护总量。超大条目拒绝保存并删除同键旧条目，不淘汰无关条目。

存储操作加锁，所有网络访问在锁外。深拷贝防止调用方修改共享首部列表。允许同一 URL 的并发未命中分别访问源站，未实现请求合并。

自动化联调不能替代 C 的 Wireshark 抓包交付。C 接收期间的大小限制和协议健壮性仍由 C 后续处理，B 不修改 A/C 源文件。
