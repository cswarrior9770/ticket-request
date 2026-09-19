# -*- coding: utf-8 -*-
"""12306 MCP Server 的 stdio 客户端（纯标准库，零新增依赖）

用途：把 drfccv/mcp-server-12306 当作取数后端，
      由 pipeline.py 调用它的 query-tickets / query-ticket-price /
      get-train-route-stations 三个工具。

原理
----
MCP 的 stdio 传输 = 子进程 stdin/stdout 上跑「换行分隔的 JSON-RPC 2.0」。
握手顺序（notifications 不需要响应）：
    initialize  →  notifications/initialized  →  tools/call

为什么选 stdio 而不是 Streamable HTTP：
    不占端口、不用处理 session id 与 SSE 分帧，隔离性最好；
    代价是每实例一个子进程 —— 这里全程复用一个，用完即关。

依赖：`uvx`（本机已装，uv 0.12.5）；首次运行会联网下载包与依赖到
      uv 自己的缓存目录，不写入本仓库、不改动沙盒 Python。

命令行自测
----------
    python scripts/mcp_client.py list
    python scripts/mcp_client.py call search-stations '{"query":"郑州"}'
    python scripts/mcp_client.py raw query-tickets '{"from_station":"郑州","to_station":"常州","train_date":"2026-10-01"}'
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

DEFAULT_CMD = ["uvx", "mcp-server-12306"]
# 服务端会做协议协商，按新→旧依次尝试
PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]


class MCPError(RuntimeError):
    """MCP 通信或工具调用失败"""


class MCPClient:
    def __init__(self, cmd=None, timeout=120, log=None):
        self.cmd = list(cmd or DEFAULT_CMD)
        self.timeout = timeout
        self.log = log or (lambda m: None)
        self.proc = None
        self.server_info = {}
        self.negotiated = None
        self.tools = []
        self._q = queue.Queue()
        self._id = 0
        self._send_lock = threading.Lock()

    # ---------- 生命周期 ----------

    def _resolve(self):
        exe = shutil.which(self.cmd[0])
        if not exe:
            raise MCPError(f"PATH 中找不到「{self.cmd[0]}」。"
                           f"请先安装 uv（uvx 随 uv 一起提供）。")
        return [exe] + self.cmd[1:]

    def start(self):
        argv = self._resolve()
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.log("启动 MCP 服务：" + " ".join(argv) + "（首次运行需联网，请稍候）")
        kw = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, **kw)
        threading.Thread(target=self._reader, args=(self.proc.stdout,),
                         daemon=True).start()
        threading.Thread(target=self._drain, args=(self.proc.stderr,),
                         daemon=True).start()
        self._handshake()
        return self

    def close(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.proc = None

    def __enter__(self):
        # 已经启动过就直接复用，否则 `with open_client(...)` 会再起一个进程
        return self if self.proc else self.start()

    def __exit__(self, *a):
        self.close()

    # ---------- 收发 ----------

    def _reader(self, pipe):
        try:
            for raw in iter(pipe.readline, b""):
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    self._q.put(json.loads(s))
                except Exception:
                    self.log("忽略非 JSON 输出：" + s[:120])
        except Exception:
            pass

    def _drain(self, pipe):
        try:
            for raw in iter(pipe.readline, b""):
                s = raw.decode("utf-8", "replace").strip()
                if s:
                    self.log("[mcp] " + s[:300])
        except Exception:
            pass

    def _send(self, obj):
        if not self.proc or self.proc.poll() is not None:
            raise MCPError("MCP 服务进程已退出")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _wait(self, want_id, timeout=None):
        end = time.time() + (timeout or self.timeout)
        while True:
            left = end - time.time()
            if left <= 0:
                raise MCPError(f"等待响应超时（id={want_id}）")
            try:
                msg = self._q.get(timeout=left)
            except queue.Empty:
                raise MCPError(f"等待响应超时（id={want_id}）")
            if msg.get("id") != want_id:
                continue                      # 通知/日志/其他响应，跳过
            if msg.get("error"):
                raise MCPError(str(msg["error"])[:300])
            return msg.get("result")

    def request(self, method, params=None, timeout=None):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method,
                    "params": params or {}})
        return self._wait(mid, timeout)

    def notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    # ---------- MCP 语义 ----------

    def _handshake(self):
        err = None
        for pv in PROTOCOLS:
            try:
                r = self.request("initialize", {
                    "protocolVersion": pv,
                    "capabilities": {},
                    "clientInfo": {"name": "wb-train-chart", "version": "1.0"},
                })
            except MCPError as e:
                err = e
                continue
            self.server_info = r or {}
            self.negotiated = self.server_info.get("protocolVersion", pv)
            si = self.server_info.get("serverInfo") or {}
            self.log(f"MCP 握手成功：protocol={self.negotiated} "
                     f"server={si.get('name')} {si.get('version','')}".rstrip())
            self.notify("notifications/initialized")
            return
        raise MCPError(f"initialize 全部失败：{err}")

    def list_tools(self, refresh=False):
        if not self.tools or refresh:
            self.tools = (self.request("tools/list") or {}).get("tools", [])
        return self.tools

    def call(self, name, args=None, timeout=None):
        r = self.request("tools/call",
                         {"name": name, "arguments": args or {}},
                         timeout=timeout)
        if r and r.get("isError"):
            raise MCPError(f"工具 {name} 返回错误：" + str(r)[:300])
        return r

    def call_json(self, name, args=None, timeout=None):
        """调用工具并把结果解析成 Python 对象"""
        return tool_json(self.call(name, args, timeout=timeout))


def tool_json(result):
    """把 tools/call 的返回拆成 Python 对象

    MCP 把结果放在 content[].text（字符串）里，SDK v2 也可能给
    structuredContent。两种都兼容。
    """
    if result is None:
        return None
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    parts = []
    for c in (result.get("content") or []):
        if c.get("type") == "text":
            parts.append(c.get("text") or "")
    txt = "".join(parts).strip()
    if not txt:
        return None
    try:
        return json.loads(txt)
    except Exception:
        return {"_text": txt}


def open_client(cmd=None, timeout=120, log=None):
    """便捷入口：起进程 + 握手"""
    return MCPClient(cmd=cmd, timeout=timeout, log=log).start()


# ---------------- 命令行自测 ----------------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    action = argv[1]
    log = lambda m: print(m, file=sys.stderr)          # noqa: E731
    cmd = None
    if "--cmd" in argv:
        i = argv.index("--cmd")
        cmd = argv[i + 1].split()
        del argv[i:i + 2]
    args = {}
    if len(argv) > 3:
        args = json.loads(argv[3])
    with open_client(cmd=cmd, log=log) as c:
        if action == "list":
            for t in c.list_tools():
                print(f"{t['name']:<32} {t.get('description','')[:70]}")
                print("    " + json.dumps(t.get("inputSchema", {}).get("properties", {}),
                                          ensure_ascii=False)[:300])
        elif action in ("call", "raw"):
            name = argv[2]
            r = c.call(name, args)
            if action == "raw":
                print(json.dumps(r, ensure_ascii=False, indent=1))
            else:
                print(json.dumps(tool_json(r), ensure_ascii=False, indent=1))
        else:
            print(f"未知动作 {action}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
