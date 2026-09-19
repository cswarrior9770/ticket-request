# -*- coding: utf-8 -*-
"""一键启动：起本地服务 + 自动打开浏览器

用法（一般由根目录的「启动.bat」双击调用）：
    python scripts/start.py                 # 默认 8765 端口
    python scripts/start.py --port 9000
    python scripts/start.py --no-browser    # 只起服务
    python scripts/start.py --date 2026-10-01   # 改默认乘车日期

只依赖 Python 标准库（3.10+），不需要 pip 装任何东西。

原理
----
1. 先探测端口：如果 8765 上已经有本服务在跑，就直接开浏览器，不再重复起一个；
2. 否则后台线程轮询端口，一通就调 webbrowser 打开页面；
3. 主线程跑 server.py 的 HTTP 服务（阻塞）。
   关闭本窗口 / Ctrl+C 即停止服务。
"""
import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

BANNER = """
  ============================================================
     列车运行图 · 一键启动
  ============================================================
"""


def port_open(port, host="127.0.0.1", timeout=0.4):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def wait_and_open(url, port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if port_open(port):
            print(f"\n  [OK] 服务已就绪，正在打开浏览器：{url}\n", flush=True)
            try:
                webbrowser.open(url)
            except Exception as e:
                print(f"  (自动打开浏览器失败：{e}，请手动访问上面的地址)", flush=True)
            return True
        time.sleep(0.3)
    print(f"\n  [!] 等待服务就绪超时，请手动访问 {url}\n", flush=True)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--date", default=None, help="默认乘车日期 YYYY-MM-DD")
    a = ap.parse_args(argv)

    url = f"http://127.0.0.1:{a.port}/"

    print(BANNER)
    print(f"  项目目录：{BASE}")
    print(f"  网页地址：{url}")
    print("  停止服务：在本窗口按 Ctrl+C（或直接关闭窗口）")
    print("-" * 62)

    # 已经在跑就不再起第二个
    if port_open(a.port):
        print(f"  端口 {a.port} 上已有服务在运行，直接打开页面。")
        if not a.no_browser:
            webbrowser.open(url)
        time.sleep(1.2)
        return 0

    data = os.path.join(BASE, "data", "viz_data.json")
    if not os.path.exists(data):
        print("  [!] 还没有数据（data/viz_data.json 不存在）。")
        print("      页面打开后请点「刷新数据」，或先运行：")
        print("      python scripts/pipeline.py")
        print("-" * 62)

    if not a.no_browser:
        threading.Thread(target=wait_and_open, args=(url, a.port),
                         daemon=True).start()

    import server as S                      # noqa: E402  (scripts 已在 sys.path)
    if a.date:
        S.P.DEFAULT_DATE = a.date
        print(f"  默认乘车日期设为 {a.date}")
    return S.main(["--port", str(a.port)])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  已停止。")
