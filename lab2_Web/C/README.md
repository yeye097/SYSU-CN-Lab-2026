# 角色 C 实施指南与模块接口文档 (SYSU CN Lab2)

本文档由**角色 C（源站通信与抓包分析）**维护，完全遵循团队《Web 缓存代理实验分工与接口方案》规范编写。

---

## 一、 文件清单与架构说明

| 文件名 | 功能定位 | 说明 |
| :--- | :--- | :--- |
| `http_types.py` | 公共数据类型模块 | 定义 `HTTPRequest`、`HTTPResponse` 及 `CacheEntry`；统一 `headers` 为 `list[tuple[str, str]]`。**注意：此文件按架构约定由 A 维护权威版，C 目录内为本地自测副本、不提交仓库，以 A 阶段 0 定稿为准。** |
| `origin_client.py` | 源站通信客户端模块 | 实现 `fetch(request, conditional_headers=None)`；支持三种响应定界方式（Content-Length、分块传输解码、连接关闭 EOF）；支持连接与读取超时控制。 |
| `mock_origin_server.py` | 可控 HTTP 测试源站 | 监听本地 `8000` 端口，多线程并发（`ThreadingHTTPServer`，每连接独立线程）。支持 200 OK、识别条件请求返回 304、分块传输 `/chunked`、连接关闭 `/eof`、动态更新 `/update`、资源重置 `/reset`、状态查询 `/status`、慢响应 `/delay`。 |
| `test_origin_client.py` | 模块单元测试脚本 | 覆盖三种响应定界方式、条件请求 304、内容更新 200、超时控制与源站异常断开，以及分帧到达、Trailer、HEAD 无正文、重复首部保留、请求构造等健壮性场景，**12 项测试 100% 通过**。 |
| `traffic_generator.py` | 抓包流量触发工具 | 一键式触发 Wireshark 所需的 5 大测试场景流量，避免浏览器强缓存干扰。 |

---

## 二、 供组员 A / B 调用的公共接口规范

### 1. 公共数据结构 (`http_types.py`)

```python
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

@dataclass
class HTTPRequest:
    method: str
    url: str               # 规范化完整 URL（含查询串，用于缓存索引）
    host: str
    port: int
    path: str              # 路径（含查询串，如 /index.html?a=1）
    headers: List[Tuple[str, str]] = field(default_factory=list)

@dataclass
class HTTPResponse:
    status_code: int
    reason: str
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: bytes = b""      # 内部已去除分块传输编码，保留原有内容编码
    
    # 辅助方法
    is_not_modified: bool  # 是否为 304
    is_success: bool       # 是否为 200
    get_header(name, default=None)  # 大小写无关查找首部值
    to_http_bytes() -> bytes        # 重构为 HTTP 字节流发给客户端
```

### 2. 角色 B（缓存控制）如何调用角色 C（源站通信）

方案规定调用签名：
```python
OriginClient.fetch(request: HTTPRequest, conditional_headers: list[tuple[str, str]] = None) -> HTTPResponse
```

#### 调用示例 1：未命中缓存或无需验证，拉取完整资源
```python
from http_types import HTTPRequest
from origin_client import OriginClient

client = OriginClient(default_timeout=5.0)

# 传入 A 解析出的 request 对象
resp = client.fetch(request)

if resp.is_success:
    print(f"成功从源站获取，正文长度: {len(resp.body)}")
```

#### 调用示例 2：缓存过期，带条件首部验证
```python
# B 提供条件验证首部（ETag 优先，其次 Last-Modified）
cond_headers = [("If-Modified-Since", cached_last_modified)]

resp = client.fetch(request, conditional_headers=cond_headers)

if resp.is_not_modified:
    print("[CacheController] 收到源站 304，复用本地缓存正文！")
elif resp.is_success:
    print("[CacheController] 收到源站新 200，更新本地缓存正文与元数据！")
```

---

## 三、 模块自测运行指南（符合验收标准）

运行单元测试：
```bash
python test_origin_client.py
```

**测试重点覆盖证明：**
1. `test_01_delimitation_content_length`: Content-Length 定长定界解析测试（通过）
2. `test_02_delimitation_chunked`: 分块传输 Transfer-Encoding: chunked 解码与定界测试（通过）
3. `test_03_delimitation_connection_close_eof`: 连接关闭 EOF 定界测试（通过）
4. `test_04_conditional_request_304`: 条件请求 304 Not Modified 验证（通过）
5. `test_05_update_after_expiry_200`: 资源更新后拉取全新 200 OK 测试（通过）
6. `test_06_timeout_handling`: 超时异常抛出与 504 响应合成测试（通过）
7. `test_07_origin_disconnect_connection_refused`: 异常断开/拒绝连接与 502 响应合成测试（通过）
8. `test_08_segmented_content_length_response`: 响应分帧到达（首部与正文分多次 `recv` 拼齐）测试（通过）
9. `test_09_chunked_with_trailer`: 分块传输 Trailer 首部剥离与正文还原测试（通过）
10. `test_10_head_request_no_body`: HEAD 请求无正文处理测试（通过）
11. `test_11_duplicate_response_headers_preserved`: 响应重复首部按 `list[tuple[str, str]]` 保留测试（通过）
12. `test_12_outgoing_request_construction`: 请求构造（Host 补全 / `Connection: close` / 条件首部去重覆盖）测试（通过）

---

## 四、 Wireshark 5 大场景联合抓包操作指南

### 1. 抓包环境准备
* 打开 Wireshark，选择 **`Adapter for loopback traffic`**（本地回环网卡）。
* 显示过滤器：
  ```text
  http and (tcp.port == 8888 or tcp.port == 8000)
  ```
* 启动服务：
  * 终端 1：`python mock_origin_server.py --port 8000`
  * 终端 2：`python proxy_server.py --port 8888`

### 2. 抓包执行命令
使用流量工具交互式触发：
```bash
python traffic_generator.py --all
```
分别保存为：
* 场景 1：`scene1_miss.pcapng`
* 场景 2：`scene2_hit.pcapng`
* 场景 3：`scene3_304.pcapng`
* 场景 4：`scene4_update.pcapng`
* 场景 5：`scene5_concurrency.pcapng`

---

## 五、 报告文字撰写模板（供 C 填入最终报告）

### 场景 1（首次访问 / MISS）分析文字
> 客户端首次向代理请求页面。代理检索本地缓存未命中（Cache MISS），代理向测试源站（127.0.0.1:8000）发起 HTTP GET 请求。源站返回 200 OK 及完整正文。代理将响应保存至本地缓存，并将响应转发给客户端。抓包完整记录了客户端到代理、代理到源站的 4 次交互报文。

### 场景 2（本地响应 / HIT）分析文字
> 客户端在缓存有效期内再次请求相同 URL。代理查询本地缓存判定新鲜有效（Cache HIT），直接从本地读取缓存并返回 200 OK。在 Wireshark 过滤中，同一时间窗内只有客户端与代理之间的报文，代理与源站间完全无流量，验证了缓存减少源站负载的效果。

### 场景 3（过期验证 / 304）分析文字
> 缓存过期后，代理向源站发起条件请求，请求头中携带 `If-Modified-Since`。源站判定资源未被修改，返回 `304 Not Modified`（无正文）。代理确认本地缓存依然有效，刷新新鲜度，并取出本地正文包装成 200 OK 返回给客户端。

### 场景 4（过期更新 / 200）分析文字
> 源站通过 `/update` 修改了页面内容与时间戳。代理再次向源站发起条件验证时，源站判定时间戳过期，返回全新 `200 OK` 响应及新网页正文。代理收到后覆盖本地旧缓存，并将新内容返回给客户端。

### 场景 5（多客户端并发）分析文字
> 启动多线程模拟并发客户端请求，Wireshark 中可清晰看到多个独立的 TCP Stream。代理服务器多线程并发处理各个连接，数据流交错传输且互不干扰，响应正确无串流。
