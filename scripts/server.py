# -*- coding: utf-8 -*-
"""
本地服务：静态托管运行图 + 数据刷新 API

用法：
    python scripts/server.py            # 默认 http://127.0.0.1:8765/
    python scripts/server.py --port 9000

API：
    GET /api/data              返回当前数据集
    GET /api/refresh?date=...  启动后台刷新（可带 from=/to= 换始发终到站）
    GET /api/status            查询刷新进度与日志
    GET /api/stations          可选车站列表（供起终点选择器用）
    GET /api/transfer?via=徐州 查询经该站中转的两段车次（同步返回，带缓存）
"""
import json
import os
import sys
import threading
import time
import traceback
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P  # noqa: E402

BASE = P.BASE
HTML_NAME = "运行图原型.html"

_LOCK = threading.Lock()
JOB = {"running": False, "log": [], "report": None,
       "started": None, "finished": None}

# 中转查询缓存：key = "via|date"；旁路取数，与主数据集互不影响
_TLOCK = threading.Lock()
TCACHE = {}


def log(msg):
    line = f"{time.strftime('%H:%M:%S')}  {msg}"
    JOB["log"].append(line)
    if len(JOB["log"]) > 400:
        del JOB["log"][:100]
    print(line, flush=True)


def _current_route():
    """当前数据集用的起终点。

    每个请求都会 reload pipeline，模块全局量会退回默认的 郑州→常州，
    所以路线一律以 data/viz_data.json 的 meta 为准（单一事实来源）。
    """
    try:
        with open(os.path.join(BASE, "data", "viz_data.json"),
                  encoding="utf-8") as f:
            m = json.load(f).get("meta") or {}
        return m.get("from_name"), m.get("to_name")
    except Exception:
        return None, None


def _worker(date, want_live, source, frm=None, to=None, conc=None):
    try:
        # 重载流水线：pipeline.py 改动后无需重启本服务即可生效
        # （否则本进程会一直用启动时加载的旧模块，写出结构过期的数据集）
        import importlib
        importlib.reload(P)
        log("流水线模块已重载")
        if not frm and not to:
            frm, to = _current_route()       # 只改日期时沿用当前路线
        data, rep = P.refresh(date=date, want_live=want_live, log=log,
                              source=source, from_name=frm, to_name=to,
                              conc=(conc or P.DEFAULT_CONC))
        JOB["report"] = rep
        if data:
            log(f"完成：{rep.get('route')} 共 {len(data['trains'])} 趟车次"
                f"（数据源 {rep.get('source_used')}）")
        else:
            log("刷新未取得完整数据，已保留原有数据集")
    except Exception as e:
        traceback.print_exc()
        JOB["report"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        log(f"刷新异常：{e}")
    finally:
        JOB["running"] = False
        JOB["finished"] = time.strftime("%H:%M:%S")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=BASE, **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _run_legstops(self, items, date, conc=None):
        """经停补全的公共实现（GET / POST 都走这里）"""
        if not items:
            return self._json({"ok": False, "error": "缺少 items"}, 400)
        cur_f, cur_t = _current_route()
        try:
            import importlib
            importlib.reload(P)
            if cur_f or cur_t:
                P.set_route(cur_f, cur_t, log=log)   # reload 后要还原当前路线
            r = P.leg_stops(items, date=date, log=log,
                            conc=(conc or P.DEFAULT_CONC))
        except Exception as e:
            traceback.print_exc()
            log(f"经停补全失败：{e}")
            return self._json({"ok": False,
                               "error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"ok": True, "count": len(items),
                           "stops": r["stops"], "missing": r["missing"]})

    def do_POST(self):
        """只用于 /api/legstops —— 条目里带 tn/fc/tc/城市，字段多，GET 的 URL 会过长"""
        u = urlparse(self.path)
        if u.path != "/api/legstops":
            return self._json({"ok": False, "error": "不支持该路径"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception as e:
            return self._json({"ok": False, "error": f"请求体解析失败：{e}"}, 400)
        items = body.get("items") or []
        for it in items:
            it["key"] = f"{it.get('code')}|{it.get('dep')}"
        return self._run_legstops(items, body.get("date") or P.DEFAULT_DATE,
                                  body.get("conc"))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/api/data":
            p = os.path.join(BASE, "data", "viz_data.json")
            if not os.path.exists(p):
                return self._json({"error": "尚无数据集，请先刷新"}, 404)
            with open(p, encoding="utf-8") as f:
                return self._json(json.load(f))

        if u.path == "/api/status":
            return self._json({"running": JOB["running"], "log": JOB["log"],
                               "report": JOB["report"], "started": JOB["started"],
                               "finished": JOB["finished"]})

        if u.path == "/api/stations":
            try:
                st = P.fetch_stations()
                return self._json({"ok": True,
                                   "names": sorted(st.keys()),
                                   "count": len(st)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)

        if u.path == "/api/refresh":
            date = (q.get("date") or [P.DEFAULT_DATE])[0]
            want_live = (q.get("live") or ["1"])[0] != "0"
            source = (q.get("source") or [os.environ.get(P.SOURCE_ENV, "auto")])[0]
            frm = (q.get("from") or [""])[0].strip()
            to = (q.get("to") or [""])[0].strip()
            try:
                conc = max(1, int((q.get("conc") or [""])[0] or P.DEFAULT_CONC))
            except ValueError:
                conc = P.DEFAULT_CONC
            with _LOCK:
                if JOB["running"]:
                    return self._json({"started": False, "running": True,
                                       "msg": "已有刷新任务在执行中"})
                JOB.update({"running": True, "log": [], "report": None,
                            "started": time.strftime("%H:%M:%S"),
                            "finished": None})
            log(f"开始刷新（乘车日期 {date}，实时余票={'开' if want_live else '关'}"
                f"，数据源 {source}，经停并发 {conc}）"
                + (f"，路线 {frm} → {to}" if (frm or to) else ""))
            threading.Thread(target=_worker,
                             args=(date, want_live, source, frm or None, to or None,
                                   conc),
                             daemon=True).start()
            return self._json({"started": True, "running": True, "date": date,
                               "source": source, "conc": conc,
                               "from": frm, "to": to})

        if u.path == "/api/legstops":
            date = (q.get("date") or [P.DEFAULT_DATE])[0]
            raw = (q.get("items") or [""])[0]
            items = []
            for part in raw.split(","):
                f = part.split("|")
                if len(f) != 4:
                    continue
                items.append({"code": f[0], "dep": f[1], "from": f[2], "to": f[3],
                              "key": f[0] + "|" + f[1]})
            return self._run_legstops(items, date)

        if u.path == "/api/transfer":
            via = (q.get("via") or [""])[0].strip()
            date = (q.get("date") or [P.DEFAULT_DATE])[0]
            if not via:
                return self._json({"ok": False, "error": "缺少 via 参数"}, 400)
            force = (q.get("force") or ["0"])[0] == "1"
            cur_f, cur_t = _current_route()
            ck = f"{cur_f}|{cur_t}|{via}|{date}"     # 路线也要进键：换路线后两程完全不同
            with _TLOCK:
                hit = None if force else TCACHE.get(ck)
            if hit:
                log(f"中转查询命中缓存：经 {via}")
                return self._json({"ok": True, "cached": True, "data": hit})
            log(f"中转查询：经 {via}（{date}）")
            try:
                import importlib
                importlib.reload(P)          # 同刷新：改动 pipeline 无需重启
                if cur_f or cur_t:
                    P.set_route(cur_f, cur_t, log=log)   # reload 后要还原当前路线
                data = P.query_transfer(date=date, via=via, log=log)
            except Exception as e:
                traceback.print_exc()
                log(f"中转查询失败：{e}")
                return self._json({"ok": False,
                                   "error": f"{type(e).__name__}: {e}"}, 500)
            for lg in data["legs"]:
                log(f"  {lg['from']} → {lg['to']}：{len(lg['trains'])} 趟")
            with _TLOCK:
                TCACHE[ck] = data
            return self._json({"ok": True, "cached": False, "data": data})

        if u.path in ("/", "/index.html"):
            self.path = "/" + quote(HTML_NAME)
        return super().do_GET()


def main(argv=None):
    port = 8765
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port 需要跟一个数字")
            return 1

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 62)
    print("  12306 运行图本地服务")
    print(f"  页面      http://127.0.0.1:{port}/")
    print(f"  当前数据  http://127.0.0.1:{port}/api/data")
    print(f"  刷新接口  http://127.0.0.1:{port}/api/refresh")
    print("  Ctrl+C 停止")
    print("=" * 62)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
