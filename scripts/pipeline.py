# -*- coding: utf-8 -*-
"""
12306 数据流水线（可复用模块）
职责：抓票价/席别 → 抓经停站（增量）→ 抓余票快照 → 构建前端数据集 → 注入 HTML

接口说明（实测结论）：
  leftTicketPrice/query   票价 + 席别编组   字段名干净，未受限流影响
  czxx/queryByTrainNo     车次经停站        未受限流影响
  leftTicket/query        实时余票          字段为 | 分隔数组，高频会被长时间限流
"""
import json
import os
import re
import ssl
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
HTML = os.path.join(BASE, "运行图原型.html")

DEFAULT_DATE = "2026-10-01"
FROM_CODE, TO_CODE = "ZZF", "CZH"          # 郑州 / 常州
FROM_NAME, TO_NAME = "郑州", "常州"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
_last = [0.0]

# leftTicket/query 的字段索引（实测）
IX = {"train_no": 2, "code": 3, "dep": 8, "arr": 9, "dur": 10,
      "gr": 21, "rw": 23, "wz": 26, "yw": 28, "yz": 29,
      "edz": 30, "ydz": 31, "swz": 32, "tdz": 33}

# 票价接口席别字段 -> (显示名, 旧余票字段, 排序权重)
SEAT_MAP = [
    ("swz", "swz", "商务座", 9), ("tz", "tdz", "特等座", 8),
    ("zy", "ydz", "一等座", 7), ("ze", "edz", "二等座", 6),
    ("gr", "gr", "高级软卧", 5), ("rw", "rw", "软卧", 4),
    ("yw", "yw", "硬卧", 3), ("rz", "rz", "软座", 2),
    ("yz", "yz", "硬座", 1), ("wz", "wz", "无座", 0),
]

CITY_FIX = {
    "郑州东": "郑州", "郑州航空港": "郑州", "常州北": "常州", "金坛": "常州",
    "开封北": "开封", "兰考南": "兰考", "民权北": "民权", "徐州东": "徐州",
    "宿州东": "宿州", "蚌埠南": "蚌埠", "滁州北": "滁州", "南京南": "南京",
    "镇江南": "镇江", "丹阳北": "丹阳", "大港南": "镇江", "合肥北城": "合肥",
    "淮南南": "淮南", "阜阳西": "阜阳", "太和东": "太和", "颍上北": "颍上",
    "亳州南": "亳州", "扬州东": "扬州", "淮安东": "淮安", "萧县北": "萧县",
    "永城北": "永城", "砀山南": "砀山", "洛阳龙门": "洛阳", "渭南北": "渭南",
    "西安北": "西安", "无锡东": "无锡", "苏州北": "苏州", "昆山南": "昆山",
    "上海虹桥": "上海", "合肥西": "合肥",
}

AXES = {
    "京沪通道": ["郑州", "开封", "兰考", "民权", "宁陵县", "商丘", "砀山",
               "永城", "徐州", "宿州", "蚌埠", "滁州", "南京", "镇江", "常州"],
    "合杭通道": ["郑州", "商丘", "阜阳", "寿县", "淮南", "合肥", "南京",
               "镇江", "常州"],
    "连镇通道": ["郑州", "商丘", "徐州", "宿迁", "淮安", "扬州", "镇江",
               "丹阳", "常州"],
}
FROM_ST = {"郑州", "郑州东", "郑州航空港", "焦作"}
TO_ST = {"常州", "常州北", "金坛"}


class RateLimited(Exception):
    """被 12306 风控拦截（返回 HTML 而非 JSON）"""


def norm(n):
    return CITY_FIX.get(n, n)


def throttle(interval):
    gap = time.time() - _last[0]
    if gap < interval:
        time.sleep(interval - gap)
    _last[0] = time.time()


def http_text(url, referer="https://kyfw.12306.cn/otn/leftTicket/init",
              interval=0.0, timeout=25):
    throttle(interval)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer, "Connection": "keep-alive"})
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def http_json(url, **kw):
    body = http_text(url, **kw)
    if not body.strip().startswith("{"):
        raise RateLimited(f"返回 {len(body)} 字节 HTML（被限流）")
    return json.loads(body)


# ---------------- 各步骤 ----------------

def fetch_stations(interval=2.0):
    """站点字典（本地缓存）"""
    p = os.path.join(DATA, "stations.json")
    if os.path.exists(p):
        return json.load(open(p, encoding="utf-8"))
    txt = http_text(
        "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js",
        interval=interval)
    st = dict(re.findall(r"@[a-z]+\|([\u4e00-\u9fa5]+)\|([A-Z]+)\|", txt))
    os.makedirs(DATA, exist_ok=True)
    json.dump(st, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return st


def fetch_price(date=DEFAULT_DATE, interval=3.0,
                from_code=None, to_code=None, save=True):
    """票价 + 席别编组（该接口未受限流影响）

    from_code/to_code 可指定任意区间（中转查询用）；save=False 时不覆盖主缓存。
    """
    fc = from_code or FROM_CODE
    tc = to_code or TO_CODE
    u = ("https://kyfw.12306.cn/otn/leftTicketPrice/query"
         f"?leftTicketDTO.train_date={date}"
         f"&leftTicketDTO.from_station={fc}"
         f"&leftTicketDTO.to_station={tc}&purpose_codes=ADULT")
    j = http_json(u, interval=interval)
    items = j.get("data") or []
    if not items:
        raise RuntimeError("票价接口返回空")
    if save:
        os.makedirs(DATA, exist_ok=True)
        json.dump(j, open(os.path.join(DATA, "price_raw.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
    return j


def fetch_stops(date=DEFAULT_DATE, interval=3.5, price=None, log=print):
    """车次经停站，增量抓取（已有缓存则跳过）"""
    price = price or json.load(open(os.path.join(DATA, "price_raw.json"),
                                    encoding="utf-8"))
    out = os.path.join(DATA, "stops.json")
    done = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}

    trains = []
    for it in price["data"]:
        q = it["queryLeftNewDTO"]
        trains.append({"code": q["station_train_code"], "train_no": q["train_no"],
                       "fc": q["from_station_telecode"],
                       "tc": q["to_station_telecode"],
                       "fn": q["from_station_name"], "tn": q["to_station_name"],
                       "dep": q["start_time"], "arr": q["arrive_time"],
                       "dur": q["lishi"]})

    todo = [t for t in trains if not done.get(f"{t['code']}|{t['dep']}", {}).get("stops")]
    log(f"经停站：共 {len(trains)} 趟，待抓 {len(todo)} 趟")
    blocked = 0
    for t in todo:
        key = f"{t['code']}|{t['dep']}"
        u = ("https://kyfw.12306.cn/otn/czxx/queryByTrainNo"
             f"?train_no={t['train_no']}&from_station_telecode={t['fc']}"
             f"&to_station_telecode={t['tc']}&depart_date={date}")
        try:
            j = http_json(u, interval=interval)
            rows = (j.get("data") or {}).get("data") or []
            done[key] = {"code": t["code"], "train_no": t["train_no"],
                         "from": t["fn"], "to": t["tn"], "dep": t["dep"],
                         "arr": t["arr"], "dur": t["dur"],
                         "stops": [{"n": r.get("station_name"),
                                    "arr": r.get("arrive_time"),
                                    "dep": r.get("start_time"),
                                    "stop": r.get("stopover_time")} for r in rows]}
            log(f"  {t['code']:<7} {len(rows):>3} 站")
            json.dump(done, open(out, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            blocked = 0
        except RateLimited:
            blocked += 1
            log(f"  {t['code']} 被限流，停止抓取")
            if blocked >= 1:
                break
        except Exception as e:
            log(f"  {t['code']} 异常 {type(e).__name__}: {str(e)[:60]}")
    return done


def fetch_live(date=DEFAULT_DATE, interval=4.0,
               from_code=None, to_code=None, save=True):
    """实时余票快照。被限流时抛 RateLimited（调用方保留旧快照）。

    from_code/to_code 可指定任意区间（中转查询用）；save=False 时**绝不写主快照**。
    """
    fc = from_code or FROM_CODE
    tc = to_code or TO_CODE
    u = ("https://kyfw.12306.cn/otn/leftTicket/query"
         f"?leftTicketDTO.train_date={date}"
         f"&leftTicketDTO.from_station={fc}"
         f"&leftTicketDTO.to_station={tc}&purpose_codes=ADULT")
    j = http_json(u, interval=interval)
    res = (j.get("data") or {}).get("result") or []
    snap = {}
    for rec in res:
        c = rec.split("|")
        if len(c) < 34:
            continue
        seats = {}
        for k, ix in [("swz", IX["swz"]), ("tdz", IX["tdz"]), ("edz", IX["edz"]),
                      ("ydz", IX["ydz"]), ("gr", IX["gr"]), ("rw", IX["rw"]),
                      ("yw", IX["yw"]), ("yz", IX["yz"]), ("wz", IX["wz"])]:
            v = c[ix]
            if v:
                seats[k] = v
        snap[f"{c[IX['code']]}|{c[IX['dep']]}"] = seats
    payload = {"captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "date": date, "trains": snap}
    if save:
        json.dump(payload, open(os.path.join(DATA, "live_snapshot.json"), "w",
                                encoding="utf-8"), ensure_ascii=False)
    return payload


def _pick_time(arr, dep, is_first):
    order = (dep, arr) if is_first else (arr, dep)
    for v in order:
        if isinstance(v, str) and TIME_RE.match(v.strip()):
            return v.strip()
    return None


def _price_of(v):
    if v in ("--", "", None):
        return None
    try:
        return round(int(v) / 10, 1)
    except (ValueError, TypeError):
        return None


def _route_of(mid):
    m = set(mid)
    huai = {"合肥", "阜阳", "淮南", "水家湖", "颍上", "寿县", "太和", "亳州",
            "安庆", "芜湖", "周口", "扶沟", "临泉", "许昌"}
    yang = {"淮安", "扬州", "宝应", "高邮", "宿迁", "泗阳", "睢宁"}
    if m & yang and "南京" not in m:
        return "连镇通道"
    if m & huai:
        return "合杭通道"
    return "京沪通道"


def _dedupe(names):
    """合并连续重复站（同城双站归一化后会出现 郑州→郑州）"""
    out = []
    for n in names:
        if not out or out[-1] != n:
            out.append(n)
    return out


def _linearize(seqs):
    """把若干条站序合并成一条线性骨架。

    排序键 = 从起点出发的「最长路径深度」。取最长路径（而非最短）
    是为了让支线站点落在分叉点与汇合点之间：
    合肥→安庆→芜湖→南京 会得到 合肥(+0) 安庆(+1) 芜湖(+2) 南京(+3)，
    而直达的 合肥→南京 只是一条捷径，不会把南京提到芜湖之前。
    """
    from collections import Counter, defaultdict

    cnt = Counter()
    for s in seqs:
        for n in s:
            cnt[n] += 1

    nodes, edges = set(), set()
    for s in seqs:
        nodes.update(s)
        for a, b in zip(s, s[1:]):
            if a != b:
                edges.add((a, b))

    succ = defaultdict(set)
    indeg = defaultdict(int)
    for a, b in edges:
        succ[a].add(b)
        indeg[b] += 1

    # Kahn 拓扑排序
    stack = [n for n in nodes if indeg[n] == 0]
    topo = []
    while stack:
        n = stack.pop()
        topo.append(n)
        for m in succ[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                stack.append(m)

    anchor = FROM_NAME
    if len(topo) != len(nodes) or anchor not in nodes:
        order = []                      # 图有环，退化为首次出现次序
        for s in seqs:
            for n in s:
                if n not in order:
                    order.append(n)
        return order, cnt

    dep = {}
    for n in topo:                      # topo 序保证前驱已算完
        dep.setdefault(n, 0)
        for m in succ[n]:
            dep[m] = max(dep.get(m, 0), dep[n] + 1)

    base = dep.get(anchor, 0)
    order = [n for n in nodes if dep[n] >= base]      # 丢掉起点之前的站
    order.sort(key=lambda n: (dep[n], -cnt.get(n, 0), n))
    return order, cnt


def build_axes(recs):
    """**每通道独立**推导完整车站轴 —— 站点全部来自该通道各趟车的实测经停。

    节点集：只取该通道车次真实停靠过的站，所以每个通道的站点集合都不一样。
    排序  ：在该通道的真实前后序上做「最长路径深度」拓扑排序。
    补全  ：某段链若只被别的通道的车次走全（例如 砀山→永城→萧县 只出现在
            连镇通道的 G2614 上），借用全局相邻站对补边，避免局部顺序错乱。
    返回 (axes, freqs)，freqs 为该通道各站的停靠车次数（供前端做密度切换）。
    """
    from collections import Counter, defaultdict

    # 全局相邻站对，仅作为补全提示
    gsucc = defaultdict(set)
    for r in recs:
        s = r["seq"]
        for a, b in zip(s, s[1:]):
            if a != b:
                gsucc[a].add(b)
    master, _ = _linearize([r["seq"] for r in recs])
    mpos = {n: i for i, n in enumerate(master)}

    by_route = defaultdict(list)
    for r in recs:
        by_route[r["route"]].append(r["seq"])

    axes, freqs = {}, {}
    for route, seqs in by_route.items():
        cnt = Counter()
        for s in seqs:
            for n in s:                 # 含首末站，避免起终点显示为 0 趟
                cnt[n] += 1
        nodes = set(cnt)

        edges = set()
        for s in seqs:                  # 本通道的真实相邻站对
            for a, b in zip(s, s[1:]):
                if a != b:
                    edges.add((a, b))
        for a in nodes:                 # 借用全局相邻站对补全（两端都属本通道）
            for b in gsucc.get(a, ()):
                if b in nodes and a != b:
                    edges.add((a, b))

        succ = defaultdict(set)
        indeg = defaultdict(int)
        for a, b in edges:
            succ[a].add(b)
            indeg[b] += 1
        stack = [n for n in nodes if indeg[n] == 0]
        topo = []
        while stack:
            n = stack.pop()
            topo.append(n)
            for m in succ[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    stack.append(m)

        if len(topo) != len(nodes):     # 补边引入环 → 退回全局骨架顺序
            axes[route] = [n for n in master if n in nodes]
            freqs[route] = dict(cnt)
            continue

        dep = {}
        for n in topo:                  # topo 序保证前驱已算完
            dep.setdefault(n, 0)
            for m in succ[n]:
                dep[m] = max(dep.get(m, 0), dep[n] + 1)

        base = dep.get(FROM_NAME, 0)    # 丢掉起点之前的站
        order = [n for n in nodes if dep[n] >= base]
        order.sort(key=lambda n: (dep[n], -cnt.get(n, 0), mpos.get(n, 0), n))
        axes[route] = order
        freqs[route] = dict(cnt)

    return axes, freqs


def build(date=DEFAULT_DATE, price=None, stops=None, live=None):
    """合并三类数据 -> 前端数据集"""
    price = price or json.load(open(os.path.join(DATA, "price_raw.json"),
                                    encoding="utf-8"))
    stops = stops or json.load(open(os.path.join(DATA, "stops.json"),
                                    encoding="utf-8"))
    lp = os.path.join(DATA, "live_snapshot.json")
    live = live if live is not None else (
        json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {})
    # ⚠️ 日期守卫：快照只在「快照日期 == 本次乘车日期」时才可用。
    # 否则查 10-05 时会拿 10-01 的余票去填（车次相同、余票完全不同），
    # 那种「看起来有数据其实是别日」的错误比显示「余票未知」危险得多。
    if live.get("date") and live["date"] != date:
        live = {}
    live_map = live.get("trains", {})

    order = {}
    for it in price["data"]:
        q = it["queryLeftNewDTO"]
        order[q["station_train_code"] + "|" + q["start_time"]] = q

    # ---- 1) 解析每趟车的区段 / 通道 / 完整站序 ----
    recs = []
    for key, v in stops.items():
        q = order.get(key)
        if not q:
            continue
        raw = [s["n"] for s in v["stops"]]
        try:
            i = next(i for i, s in enumerate(raw) if s in FROM_ST)
            j = next(j for j, s in enumerate(raw) if s in TO_ST and j > i)
        except StopIteration:
            continue
        seg = v["stops"][i:j + 1]
        seq = _dedupe([norm(s["n"]) for s in seg])
        recs.append({"key": key, "v": v, "q": q, "seg": seg,
                     "route": _route_of(seq[1:-1]), "seq": seq})

    # ---- 2) 自动生成完整车站轴（含全部真实经停站） ----
    axes, freqs = build_axes(recs)

    # ---- 3) 组装车次 ----
    trains = []
    for r in recs:
        key, v, q, seg = r["key"], r["v"], r["q"], r["seg"]
        lv = live_map.get(key, {})
        seats = {}
        for pkey, okey, name, rank in SEAT_MAP:
            p = _price_of(q.get(pkey + "_price"))
            av = lv.get(okey)
            if p is None and av is None:
                continue
            seats[pkey] = {"n": name, "p": p, "a": av, "r": rank}

        on_axis = set(axes[r["route"]])
        sp = []
        for k, s in enumerate(seg):
            city = norm(s["n"])
            if city not in on_axis:
                continue
            t = _pick_time(s.get("arr"), s.get("dep"), k == 0)
            if not t:
                continue
            # [实际站名, 所属城市行, 时刻]
            # ⚠️ 同城双站**不再合并**：郑州 与 郑州东 是同一行上左右分开的两个点，
            # 合并会把「同一趟车停两次」压成一个点，图上完全看不出来。
            sp.append([s["n"], city, t])

        trains.append({"code": v["code"], "type": v["code"][0],
                       "route": r["route"],
                       "dep": v["dep"], "arr": v["arr"], "dur": v["dur"],
                       "from": v["from"], "to": v["to"],
                       "seats": seats, "stops": sp})

    trains.sort(key=lambda t: t["dep"])

    # ---- 4) 每通道 · 每城市行 → 该行包含的实际站名（按停靠频次降序） ----
    # 供前端画轴标签（如「郑州」行下小字列出 郑州东 / 郑州航空港）
    from collections import Counter as _C
    _rows = {}
    for t in trains:
        rmap = _rows.setdefault(t["route"], {})
        for st, city, _t in t["stops"]:
            rmap.setdefault(city, _C())[st] += 1
    rowst = {rt: {c: [n for n, _ in cnt.most_common()] for c, cnt in rows.items()}
             for rt, rows in _rows.items()}

    data = {
        "meta": {
            "travel_date": date,
            "price_captured": time.strftime("%Y-%m-%d %H:%M"),
            "live_captured": live.get("captured_at", "—"),
            "route": f"{FROM_NAME} → {TO_NAME}",
            "from_code": FROM_CODE, "to_code": TO_CODE,
        },
        "axes": axes, "freq": freqs, "rowst": rowst, "trains": trains,
    }
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "viz_data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    return data


def inject_html(data=None):
    """把数据集写回 HTML 的 const DATA 行（保持 file:// 双击可用）"""
    if data is None:
        data = json.load(open(os.path.join(DATA, "viz_data.json"), encoding="utf-8"))
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = open(HTML, encoding="utf-8").read()
    if "/*__DATA__*/" in html:
        html = html.replace("/*__DATA__*/", raw)
    else:
        html, n = re.subn(r"(?m)^const DATA = .*$", "const DATA = " + raw,
                          html, count=1)
        if n == 0:
            raise RuntimeError("HTML 中未找到 const DATA 行")
    open(HTML, "w", encoding="utf-8").write(html)
    return HTML


# ---------------- 中转查询（点击站名时用） ----------------

def _leg_trains(price_payload, live_payload):
    """(票价旧结构, 余票快照) -> 统一车次列表

    统一结构 = [{"code","type","from","to","dep","arr","dur","seats"}]，
    seats 与 build() 产出的完全一致，所以前端可以走同一套渲染。
    """
    lmap = (live_payload or {}).get("trains", {}) or {}
    out = []
    for it in (price_payload or {}).get("data") or []:
        q = it.get("queryLeftNewDTO") or {}
        code = (q.get("station_train_code") or "").strip()
        dep = (q.get("start_time") or "").strip()
        if not code or not dep:
            continue
        lv = lmap.get(f"{code}|{dep}", {})
        seats = {}
        for pkey, okey, name, rank in SEAT_MAP:
            p = _price_of(q.get(pkey + "_price"))
            av = lv.get(okey)
            if p is None and av is None:
                continue
            seats[pkey] = {"n": name, "p": p, "a": av, "r": rank}
        out.append({"code": code, "type": code[0],
                    "from": q.get("from_station_name"),
                    "to": q.get("to_station_name"),
                    "dep": dep, "arr": q.get("arrive_time"),
                    "dur": q.get("lishi"), "seats": seats})
    out.sort(key=lambda t: t["dep"])
    return out


def query_transfer(date=DEFAULT_DATE, via="徐州", source=None, interval=2.5,
                   log=print):
    """查询经 `via` 中转的两段车次：郑州→via、via→常州。

    只取「票价 + 余票」两样，**不抓经停**，所以很快（两段共 4 次请求量级）。
    返回 {"date","via","source_used","legs":[{"from","to","trains":[...]} ...]}
    """
    src = (source or os.environ.get(SOURCE_ENV) or "auto").strip().lower()
    legs = [(FROM_NAME, via), (via, TO_NAME)]
    warn = []
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")     # 本次取数时间（供前端标注）

    if src in ("mcp", "auto"):
        try:
            import mcp_client
            import mcp_source
            with mcp_client.open_client(log=lambda m: log("  " + m)) as cli:
                res = []
                for a, b in legs:
                    log(f"{a} → {b}：票价")
                    p = mcp_source.load_price(cli, a, b, date, log=log, save=False)
                    lv = None
                    try:
                        lv = mcp_source.load_live(cli, a, b, date, log=log,
                                                  save=False)
                    except Exception as e:
                        warn.append(f"{a}→{b} 余票未取到：{str(e)[:80]}")
                        log(f"  {a}→{b} 余票失败（忽略）：{str(e)[:80]}")
                    res.append({"from": a, "to": b, "trains": _leg_trains(p, lv)})
            return {"date": date, "via": via, "source_used": "mcp",
                    "captured_at": stamp,
                    "legs": res, "warnings": warn}
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            warn.append("mcp: " + msg)
            log(f"MCP 中转查询失败：{msg}")
            if src == "mcp":
                log("已显式指定 source=mcp，仍回退内置直连")

    # ---- 内置直连（回退）----
    log("数据源：内置直连（urllib）")
    code = fetch_stations(interval=interval)
    res = []
    for a, b in legs:
        ca, cb = code.get(a), code.get(b)
        if not ca or not cb:
            miss = [x for x in (a, b) if not code.get(x)]
            raise RuntimeError(f"站点字典里找不到：{miss}")
        log(f"{a} → {b}：票价")
        p = fetch_price(date=date, interval=interval,
                        from_code=ca, to_code=cb, save=False)
        lv = None
        try:
            lv = fetch_live(date=date, interval=interval,
                            from_code=ca, to_code=cb, save=False)
        except RateLimited as e:
            warn.append(f"{a}→{b} 余票未取到：{e}")
            log(f"  {a}→{b} 余票被限流（忽略）")
        res.append({"from": a, "to": b, "trains": _leg_trains(p, lv)})
    return {"date": date, "via": via, "source_used": "urllib",
            "captured_at": stamp,
            "legs": res, "warnings": warn}


# ---------------- 编排 ----------------

SOURCE_ENV = "TRAIN_SOURCE"          # mcp | urllib | auto（默认 auto）


def _fetch_via_mcp(date, want_live, interval, log):
    """用 mcp-server-12306 取三类数据（票价 / 经停 / 余票）

    返回 (price, stops, live)。任一步失败都抛异常，由调用方决定是否回退。
    """
    import mcp_client
    import mcp_source
    with mcp_client.open_client(log=lambda m: log("  " + m)) as cli:
        price = mcp_source.load_price(cli, FROM_NAME, TO_NAME, date, log=log)
        stops = mcp_source.load_stops(cli, price, date, interval=interval, log=log)
        live = (mcp_source.load_live(cli, FROM_NAME, TO_NAME, date, log=log)
                if want_live else None)
    return price, stops, live


def refresh(date=DEFAULT_DATE, want_live=True, interval=3.5, log=print,
            source=None):
    """完整刷新：取数 -> 构建 -> 注入

    取数有两套实现：
      mcp    —— 走 mcp-server-12306（stdio 子进程），自带重试与会话保持
      urllib —— 内置直连（原始实现），作为回退
    默认 auto：先试 mcp，失败则回退 urllib 并在报告里标明实际用的是哪套。
    """
    src = (source or os.environ.get(SOURCE_ENV) or "auto").strip().lower()
    report = {"date": date, "steps": [], "ok": True,
              "source": src, "source_used": None}

    def step(name, fn, **kw):
        try:
            r = fn(**kw)
            report["steps"].append({"name": name, "ok": True})
            return r
        except RateLimited as e:
            report["steps"].append({"name": name, "ok": False,
                                    "err": f"被限流：{e}"})
            log(f"[{name}] 被限流")
            return None
        except Exception as e:
            report["steps"].append({"name": name, "ok": False,
                                    "err": f"{type(e).__name__}: {e}"})
            log(f"[{name}] 失败：{e}")
            return None

    # ---- 取数（票价 / 经停 / 余票）----
    price = stops = live = None
    mcp_ok = False
    if src in ("mcp", "auto"):
        log(f"数据源：MCP（mcp-server-12306）")
        try:
            price, stops, live = _fetch_via_mcp(date, want_live, interval, log)
            mcp_ok = True
            report["source_used"] = "mcp"
            report["steps"].append({"name": "MCP 取数", "ok": True})
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            log(f"MCP 取数失败：{msg}")
            report["steps"].append({"name": "MCP 取数", "ok": False, "err": msg})
            if src == "mcp":
                log("已显式指定 source=mcp，仍回退到内置直连以保证刷新可用")

    if not mcp_ok:
        log("数据源：内置直连（urllib）")
        report["source_used"] = "urllib"
        log("① 票价与席别编组")
        price = step("票价", fetch_price, date=date, interval=interval)
        if price is None:
            report["ok"] = False
            return None, report

        log("② 经停站（增量）")
        stops = step("经停站", fetch_stops, date=date, interval=interval,
                     price=price, log=log)
        if stops is None:
            stops = json.load(open(os.path.join(DATA, "stops.json"),
                                   encoding="utf-8"))
            report["ok"] = False

        if want_live:
            log("③ 实时余票")
            live = step("余票", fetch_live, date=date, interval=interval)
            if live is None:
                report["steps"].append({
                    "name": "余票", "ok": False,
                    "err": "接口被限流，沿用上次快照（余票可能不是最新）"})

    log("④ 构建数据集")
    data = step("构建", build, date=date, price=price, stops=stops, live=live)
    if data is None:
        report["ok"] = False
        return None, report

    log("⑤ 注入 HTML")
    step("注入", inject_html, data=data)

    report["train_count"] = len(data["trains"])
    report["price_captured"] = data["meta"]["price_captured"]
    report["live_captured"] = data["meta"]["live_captured"]
    return data, report


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    date = argv[0] if argv and not argv[0].startswith("-") else DEFAULT_DATE
    src = argv[argv.index("--source") + 1] if "--source" in argv else None
    _, rep = refresh(date=date, source=src)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
