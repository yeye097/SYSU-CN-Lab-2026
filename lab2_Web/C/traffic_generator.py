import argparse
import socket
import time
import threading
from urllib.parse import urlparse

# 默认网络配置：代理服务器运行在 8888 端口，测试源站运行在 8000 端口
DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8888
DEFAULT_ORIGIN_HOST = "127.0.0.1"
DEFAULT_ORIGIN_PORT = 8000


def raw_http_get_via_proxy(
    proxy_host: str,
    proxy_port: int,
    target_url: str,
    extra_headers: dict = None,
    timeout: float = 5.0
) -> str:
    """
    通过 Socket 向代理服务器发起纯净的 HTTP GET 请求
    为什么不用浏览器而是用脚本发包？
      1. 现代浏览器（Chrome/Edge）有激进的本地缓存策略，多次刷新可能根本不产生网络流量；
      2. 浏览器会自带很多无关的扩展首部（sec-ch-ua、Cookie 等），使 Wireshark 报文杂乱；
      3. 脚本可以精确控制请求行与首部，方便在 Wireshark 中定位和分析。
    """
    # 从完整 URL 里拆出主机部分（如 127.0.0.1:8000），用来填 Host 首部
    parsed = urlparse(target_url)
    host_header = parsed.netloc

    # 根据 HTTP 代理协议规范：客户端向代理发送请求时，请求行中必须使用绝对 URI (Absolute URI)
    # 例如：GET http://127.0.0.1:8000/index.html HTTP/1.1
    req_lines = [
        f"GET {target_url} HTTP/1.1",
        f"Host: {host_header}",
        "User-Agent: SYSU-Traffic-Generator/1.0",
        "Connection: close",
        "Accept: text/html, */*"
    ]
    # 如果调用方传了额外首部（比如 Cache-Control: max-age=0），就逐条追加进去
    if extra_headers:
        for k, v in extra_headers.items():
            req_lines.append(f"{k}: {v}")
    
    # 按照 HTTP 规范以 CRLF (\r\n) 换行，最后跟一个空行表示首部结束
    req_data = "\r\n".join(req_lines) + "\r\n\r\n"

    # 创建 TCP socket，设置超时，防止连不上时一直卡住
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((proxy_host, proxy_port))
        # sendall 确保整个请求完整发出，不会只发一半
        sock.sendall(req_data.encode("latin1"))

        # 循环接收代理返回的数据
        # TCP 是字节流，一次 recv 不一定收全，所以循环收到对方关闭连接为止
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        # 把分段收到的字节拼成完整响应，再按 latin1 解码成字符串
        return b"".join(chunks).decode("latin1", errors="replace")
    finally:
        sock.close()


def direct_origin_call(origin_host: str, origin_port: int, path: str):
    """直接向源站发送控制指令（绕过代理），用于重置源站或模拟内容更新"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3.0)
    try:
        # 直连源站，不经过代理，这样控制指令不会污染代理的抓包
        sock.connect((origin_host, origin_port))
        req = f"GET {path} HTTP/1.1\r\nHost: {origin_host}:{origin_port}\r\nConnection: close\r\n\r\n"
        sock.sendall(req.encode("latin1"))
        data = sock.recv(4096)
        return data.decode("latin1", errors="replace")
    finally:
        sock.close()


def run_scene_1(p_host, p_port, o_host, o_port):
    """
    场景 1：首次访问，拉取页面 (Cache MISS)
    抓包重点：
      客户端发给代理一个请求；代理未命中缓存，向源站发起新请求；
      源站返回 200 OK，代理再返回给客户端 200 OK。Wireshark 应该抓到 4 个交互报文。
    """
    print("\n【场景 1：首次访问，拉取页面 (Cache MISS)】")
    # 先把测试源站重置为初始状态 v1
    # 保证场景 1 之前缓存和资源都是干净的，MISS 效果才明显
    direct_origin_call(o_host, o_port, "/reset")
    time.sleep(0.2)

    target_url = f"http://{o_host}:{o_port}/index.html"
    print(f"[*] 客户端向代理发送请求: {target_url}")
    # 通过代理发起请求，代理此时没有缓存，必须去源站拉取
    response = raw_http_get_via_proxy(p_host, p_port, target_url)
    # 响应用 \r\n 分行，第一行就是状态行，打印出来确认是 200
    status_line = response.split("\r\n")[0] if response else "无响应"
    print(f"[+] 客户端收到响应: {status_line}")


def run_scene_2(p_host, p_port, o_host, o_port):
    """
    场景 2：本地响应 (Cache HIT)
    抓包重点：
      客户端再次请求相同 URL；代理本地缓存有效，直接返回 200 OK；
      此时代理与源站之间完全不产生网络流量，体现了 Web 缓存节省带宽的优势。
    """
    print("\n【场景 2：本地响应 (Cache HIT)】")
    target_url = f"http://{o_host}:{o_port}/index.html"
    print(f"[*] 客户端再次请求相同 URL: {target_url}")
    # 场景 1 已经缓存过这个 URL，且 max-age=10 还没过期，代理应该直接命中本地缓存
    response = raw_http_get_via_proxy(p_host, p_port, target_url)
    status_line = response.split("\r\n")[0] if response else "无响应"
    print(f"[+] 客户端收到响应: {status_line}")


def run_scene_3(p_host, p_port, o_host, o_port):
    """
    场景 3：过期验证，内容未变 (304 Not Modified)
    抓包重点：
      缓存到期或客户端强制验证（Cache-Control: max-age=0）；
      代理向源站发起带 If-Modified-Since 首部的条件请求；
      源站返回 304 Not Modified；代理复用本地缓存正文并返回 200 OK 给客户端。
    """
    print("\n【场景 3：过期验证，内容未变 (304 Not Modified)】")
    target_url = f"http://{o_host}:{o_port}/index.html"
    print("[*] 发起带条件验证的请求 (Cache-Control: max-age=0)...")
    # max-age=0 告诉代理"这个响应立即过期"，强制它去源站做条件验证
    response = raw_http_get_via_proxy(
        p_host, p_port, target_url,
        extra_headers={"Cache-Control": "max-age=0"}
    )
    status_line = response.split("\r\n")[0] if response else "无响应"
    print(f"[+] 客户端收到响应: {status_line}")


def run_scene_4(p_host, p_port, o_host, o_port):
    """
    场景 4：过期后拉取新页面 (200 OK 更新)
    抓包重点：
      先通知源站修改资源内容 (/update)；
      代理再次向源站发起条件验证，源站检测到内容变了，返回新的 200 OK 及新网页；
      代理更新本地缓存并把新内容送达客户端。
    """
    print("\n【场景 4：过期后拉取新页面 (200 OK 更新)】")
    print("[*] 正在向源站发送更新指令 (/update)...")
    # 先让源站把资源换成新版本，这样代理再来验证时就会发现内容变了
    direct_origin_call(o_host, o_port, "/update")
    time.sleep(0.5)

    target_url = f"http://{o_host}:{o_port}/index.html"
    print(f"[*] 客户端通过代理访问已更新的资源: {target_url}")
    response = raw_http_get_via_proxy(
        p_host, p_port, target_url,
        extra_headers={"Cache-Control": "max-age=0"}
    )
    status_line = response.split("\r\n")[0] if response else "无响应"
    print(f"[+] 客户端收到新响应: {status_line}")


def run_scene_5(p_host, p_port, o_host, o_port, concurrency=4):
    """
    场景 5：多客户端并发访问 (Concurrency)
    抓包重点：
      同时启动 4 个客户端线程向代理发起请求；
      在 Wireshark 中观察不同 TCP Stream 的握手、数据传输与独立响应，证明代理支持多并发连接。
    """
    print(f"\n【场景 5：多客户端并发访问 (并发数: {concurrency})】")
    target_url = f"http://{o_host}:{o_port}/index.html"
    # results 用来收集每个客户端的结果，最后对比看响应是否正确
    results = []

    def client_worker(cid):
        # 错开微秒级启动，模拟真实多客户端几乎同时到达
        # 每个线程按编号乘以 0.05 秒错开，避免完全同一时刻连接导致观察不清
        time.sleep(0.05 * cid)
        # 记录开始时间，用来统计每个客户端的响应耗时
        t_start = time.time()
        resp = raw_http_get_via_proxy(p_host, p_port, target_url)
        t_cost = time.time() - t_start
        status_line = resp.split("\r\n")[0] if resp else "无响应"
        # 把客户端编号、状态行、耗时存起来，方便最后检查有没有串流
        results.append((cid, status_line, t_cost))
        print(f"    - [客户端 #{cid}] 收到响应: {status_line} (耗时: {t_cost:.3f}s)")

    # 按并发数创建线程，每个线程模拟一个独立客户端
    threads = []
    for i in range(1, concurrency + 1):
        t = threading.Thread(target=client_worker, args=(i,))
        threads.append(t)
        # 创建后立即启动，让它们并发跑起来
        t.start()

    # 主线程等待所有客户端线程结束，保证本场景全部完成再返回
    for t in threads:
        t.join()


def main():
    # 命令行参数：支持单场景触发和 --all 交互式全流程
    parser = argparse.ArgumentParser(description="Wireshark 抓包流量触发工具")
    parser.add_argument("--scene", type=int, choices=[1, 2, 3, 4, 5], help="指定触发单个场景 (1~5)")
    parser.add_argument("--all", action="store_true", help="交互式逐步触发全部 5 个场景")
    parser.add_argument("--proxy-host", default=DEFAULT_PROXY_HOST)
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--origin-host", default=DEFAULT_ORIGIN_HOST)
    parser.add_argument("--origin-port", type=int, default=DEFAULT_ORIGIN_PORT)
    args = parser.parse_args()

    ph, pp = args.proxy_host, args.proxy_port
    oh, op = args.origin_host, args.origin_port

    if args.scene == 1:
        run_scene_1(ph, pp, oh, op)
    elif args.scene == 2:
        run_scene_2(ph, pp, oh, op)
    elif args.scene == 3:
        run_scene_3(ph, pp, oh, op)
    elif args.scene == 4:
        run_scene_4(ph, pp, oh, op)
    elif args.scene == 5:
        run_scene_5(ph, pp, oh, op)
    elif args.all:
        # --all 模式：把 5 个场景按验收顺序列成表，逐个交互式触发
        scenes = [
            ("场景 1 (MISS 首次获取)", run_scene_1),
            ("场景 2 (HIT 本地命中)", run_scene_2),
            ("场景 3 (304 过期未改)", run_scene_3),
            ("场景 4 (200 更新正文)", run_scene_4),
            ("场景 5 (多客户端并发)", run_scene_5),
        ]
        for name, func in scenes:
            print(f"\n准备触发 -> {name}")
            # 每触发一个场景前停一下，留时间让我在 Wireshark 里开始/切换捕获
            input("请确认 Wireshark 已开始捕获，按回车键执行此场景...")
            func(ph, pp, oh, op)
            time.sleep(1.0)
        print("\n[+] 全部 5 个场景已触发完毕！可以在 Wireshark 中停止抓包并保存 .pcapng 文件。")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
