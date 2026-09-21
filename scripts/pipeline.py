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
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

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

# ---------------- 车站表：「站名 → 城市」不再手写 ----------------
# 12306 官方 station_name.js 的每行其实有 11 个字段：
#     @简拼|站名|三字码|拼音|简拼|编号|区域码|城市|||
# 旧版正则只取了前两个字段，所以「站名 → 城市」只能靠手写字典（CITY_FIX），
# 一换路线（上海→杭州）就失效。现在直接解析官方「城市」字段，3389 站全覆盖：
#     武进 / 戚墅堰 / 金坛 → 常州      大港南 / 丹阳北 → 镇江
#     桐乡 / 海宁西 / 嘉兴南 → 嘉兴      上海松江 / 南翔北 → 上海
STATION_JS = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
STATION_TABLE = os.path.join(DATA, "stations_city.json")

_CODE_OF = {}         # 站名 -> 电报码
_CITY_OF = {}         # 站名 -> 官方城市
_CITY_MEMBERS = {}    # 官方城市 -> [该城市的全部站名]
_BY2 = {}             # 站名前两字 -> 同前缀候选站名（按长度降序，供 norm() 第 3 条规则用）

FROM_ST = {"郑州", "郑州东", "郑州航空港"}
TO_ST = {"常州", "常州北", "金坛"}
DEFAULT_PAIR = ("郑州", "常州")


def city_of(name):
    """站名 -> 官方城市（查不到就返回站名本身）"""
    if not _CITY_OF:
        fetch_stations()
    return _CITY_OF.get(name) or name


def _prefix_candidates(name):
    """有可能成为 `name` 前缀的官方站名（只查前两字相同的，避免全表 3389 次扫描）"""
    if not _CITY_OF:
        fetch_stations()
    return _BY2.get(name[:2]) or ()


def city_members(city):
    """官方「同一座城市」的全部车站（含同名站）"""
    if not _CITY_OF:
        fetch_stations()
    return set(_CITY_MEMBERS.get(city) or ()) | {city}


def same_city(name, stations=None):
    """该城市的「同城核心站」：城市名本身 + 官方同城里以城市名开头的站。

    用来做 `build()` 里区段端点的**兜底**匹配，所以要克制 ——
    官方城市字段是**地级市**口径，郑州名下还挂着 巩义 / 巩义南 / 新郑机场，
    把它们都当端点候选会切错区段。真正需要「同城多站」覆盖的场景
    （上海南 / 上海松江 / 上海虹桥）都是同名前缀，够用。
    """
    if not _CITY_OF:
        fetch_stations()
    out = {name}
    out |= {s for s in _CITY_MEMBERS.get(name) or () if s != name and s.startswith(name)}
    for s in (stations or ()):
        if s != name and s.startswith(name):
            out.add(s)
    return out


def set_route(fr=None, to=None, log=print):
    """切换始发 / 终到站，参数可以是车站名或城市名（郑州东 / 郑州）。

    一律**按城市**解析电报码：12306 传城市码时会把同城各站（郑州 / 郑州东 /
    郑州航空港）的车次一并返回，覆盖面比单站更全；轴上的行名、中转端点
    也统一用城市名，同城多站按时刻左右分开画点。

    归并规则只认「站名以官方城市名开头」这一种（上海虹桥 → 上海、郑州东 → 郑州），
    绝不把 义乌 之类解析成它的地级市（金华）—— 用户点名要哪个站就查哪个站。
    """
    global FROM_NAME, TO_NAME, FROM_CODE, TO_CODE, FROM_ST, TO_ST
    st = fetch_stations()

    def resolve(x, dflt):
        x = (x or "").strip() or dflt
        city = x if x not in st else x
        c = city_of(x)
        if c != x and x.startswith(c):      # 郑州东 → 郑州（同城市名开头才归并）
            city = c
        code = st.get(city)
        if not code:
            raise ValueError(f"未知车站或城市：{x}")
        return city, code

    fcity, fcode = resolve(fr, DEFAULT_PAIR[0])
    tcity, tcode = resolve(to, DEFAULT_PAIR[1])
    if fcity == tcity:
        raise ValueError(f"始发站与终到站不能是同一座城市（{fcity}）")
    FROM_NAME, FROM_CODE = fcity, fcode
    TO_NAME, TO_CODE = tcity, tcode
    FROM_ST = same_city(fcity, st)
    TO_ST = same_city(tcity, st)
    log(f"路线：{FROM_NAME}({FROM_CODE}) → {TO_NAME}({TO_CODE})")
    return FROM_NAME, TO_NAME


def _cache_paths():
    """主数据集缓存路径。

    默认路线（郑州→常州）沿用 data/ 根目录下的原有缓存，
    免得升级后还要重新抓一遍；其余路线各自放在 data/routes/<起>_<终>/。
    经停站缓存（stops.json）是「按车次」的，与路线无关，所以全局共用。
    """
    if (FROM_NAME, TO_NAME) == DEFAULT_PAIR:
        return (os.path.join(DATA, "price_raw.json"),
                os.path.join(DATA, "live_snapshot.json"))
    d = os.path.join(DATA, "routes", f"{FROM_NAME}_{TO_NAME}")
    os.makedirs(d, exist_ok=True)
    return (os.path.join(d, "price_raw.json"),
            os.path.join(d, "live_snapshot.json"))


class RateLimited(Exception):
    """被 12306 风控拦截（返回 HTML 而非 JSON）"""


def norm(n, frm=None, to=None):
    """站名 → 城市行名（全部来自官方数据，不再有手写字典）

    三条规则，依次判定：
      1. **路线端点必并**：官方城市 == 本路线起点 / 终点城市的站，一律归到端点城市
         （武进 / 戚墅堰 / 金坛 → 常州、上海松江 → 上海）。这一条不能省 ——
         漏了它们会自成一「行」，被密度筛掉后停靠点画不出来（G7155 武进→上海松江
         就是这么把起点丢掉的）。
      2. **同名城市前缀**：站名以官方城市名开头 → 归到该城市
         （郑州东→郑州、上海虹桥→上海、嘉兴南→嘉兴、黄山北→黄山）。
      3. **同城站名前缀**：官方站名里存在「与本站同城、且是本站名前缀」的站 →
         归到那个站（兰考南→兰考、民权北→民权、砀山南→砀山、丹阳北→丹阳）。
         这一条保住了「县级站自成一行」的现有观感：兰考南 归 兰考 而不是 开封。
      4. 其余自成一行（大港南、桐乡、黟县东、巩义…）。
    """
    if frm and (n == frm or city_of(n) == frm):
        return frm
    if to and (n == to or city_of(n) == to):
        return to
    c = city_of(n)
    if c != n and n.startswith(c):
        return c
    for s in _prefix_candidates(n):
        if s != n and n.startswith(s) and _CITY_OF.get(s) == c:
            return s
    return n


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

def fetch_stations(interval=2.0, force=False):
    """站点字典 {站名: 电报码}，同时把「站名 → 城市」一并建好（本地缓存）。

    缓存 `data/stations_city.json` 存官方全字段；旧的 `data/stations.json`（只有站名+码）
    同时重新生成，保证历史脚本（audit_data 等）继续可用。
    """
    global _CODE_OF, _CITY_OF, _CITY_MEMBERS, _BY2
    if _CODE_OF and not force:
        return _CODE_OF
    raw = {}
    if os.path.exists(STATION_TABLE) and not force:
        with open(STATION_TABLE, encoding="utf-8") as f:
            raw = json.load(f)
    if not raw:
        txt = http_text(STATION_JS, interval=interval)
        for rec in txt.split("@"):
            f = rec.split("|")
            if len(f) < 8:                # 官方文件是 11 段；老格式只有 6 段
                continue
            name, code, city = f[1].strip(), f[2].strip(), f[7].strip()
            if not name or not code:
                continue
            raw[name] = [code, city, f[6].strip()]
        if len(raw) < 1000:
            raise RuntimeError(f"车站表解析异常，只得到 {len(raw)} 条")
        os.makedirs(DATA, exist_ok=True)
        with open(STATION_TABLE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(DATA, "stations.json"), "w", encoding="utf-8") as f:
            json.dump({n: v[0] for n, v in raw.items()}, f, ensure_ascii=False)
    _CODE_OF = {n: v[0] for n, v in raw.items()}
    _CITY_OF = {n: (v[1] or n) for n, v in raw.items()}
    _CITY_MEMBERS = {}
    for n, c in _CITY_OF.items():
        _CITY_MEMBERS.setdefault(c, []).append(n)
    _BY2 = {}
    for n in _CITY_OF:
        _BY2.setdefault(n[:2], []).append(n)
    for v in _BY2.values():
        v.sort(key=len, reverse=True)
    return _CODE_OF


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
        json.dump(j, open(_cache_paths()[0], "w", encoding="utf-8"),
                  ensure_ascii=False)
    return j


DEFAULT_CONC = 6          # 经停抓取并发（1 = 串行）


def _stops_url(t, date):
    return ("https://kyfw.12306.cn/otn/czxx/queryByTrainNo"
            f"?train_no={t['train_no']}&from_station_telecode={t['fc']}"
            f"&to_station_telecode={t['tc']}&depart_date={date}")


def _stops_rows(j):
    rows = (j.get("data") or {}).get("data") or []
    return [{"n": r.get("station_name"), "arr": r.get("arrive_time"),
             "dep": r.get("start_time"), "stop": r.get("stopover_time")}
            for r in rows]


def fetch_stop_batch(todo, date, conc=DEFAULT_CONC, interval=0.0, log=print):
    """并发抓一批车次的经停站，返回 (got, missing)。

    `todo` 每项需带 key / code / train_no / fc / tc / fn / tn / dep / arr / dur。

    ⚠️ 一律**直连** `czxx/queryByTrainNo`：`train_no`（12306 内部编号）直接取自
    票价载荷的 `queryLeftNewDTO.train_no`，一趟一个请求。而 MCP 的
    `get-train-route-stations` 得先发一次 leftTicket 把「车次号」解析成内部编号
    （日志里那句「检测到车次号 K1158，正在转换为列车编号…」），请求数翻倍、
    单趟 1.2 秒 vs 直连 0.25 秒。

    ⚠️ 并发是这里唯一的速度来源：旧实现串行 + 每趟强制 sleep 3.5 秒，
    73 趟要 4 分 15 秒，而其中绝大多数是纯等待。实测 6 并发 16 趟共 1.6 秒、零限流。
    被限流时立刻置位 stop 让其余线程收工（不硬冲），串行模式下保留 interval 节流。
    """
    got, missing = {}, []
    todo = list(todo)
    if not todo:
        return got, missing
    conc = max(1, int(conc or 1))
    stop = threading.Event()
    lock = threading.Lock()
    last = [0.0]

    def work(t):
        if stop.is_set():
            return
        try:
            if conc == 1 and interval:
                with lock:
                    throttle(interval)
            j = http_json(_stops_url(t, date), interval=0.0)
            rows = _stops_rows(j)
            if not rows:
                with lock:
                    missing.append(t["key"])
                log(f"  {t['code']:<7} 返回 0 站，跳过")
                return
            rec = {"code": t["code"], "train_no": t["train_no"],
                   "from": t.get("frm"), "to": t.get("to"), "dep": t.get("dep"),
                   "arr": t.get("arr"), "dur": t.get("dur"), "stops": rows}
            with lock:
                got[t["key"]] = rec
            log(f"  {t['code']:<7} {len(rows):>3} 站")
        except RateLimited:
            stop.set()
            with lock:
                missing.append(t["key"])
            log(f"  {t['code']} 被限流，停止本轮抓取")
        except Exception as e:
            with lock:
                missing.append(t["key"])
            log(f"  {t['code']} 失败：{type(e).__name__}: {str(e)[:60]}")

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(work, todo))
    return got, missing


def _trains_from_price(price, st):
    """票价载荷 → 抓经停用的车次项（电报码缺失时按站名反查）"""
    out = []
    for it in price.get("data") or []:
        q = it.get("queryLeftNewDTO") or {}
        code, dep = q.get("station_train_code"), q.get("start_time")
        if not code or not dep:
            continue
        out.append({"key": f"{code}|{dep}", "code": code,
                    "train_no": q.get("train_no"),
                    "fc": q.get("from_station_telecode")
                          or st.get(q.get("from_station_name")),
                    "tc": q.get("to_station_telecode")
                          or st.get(q.get("to_station_name")),
                    "frm": q.get("from_station_name"),
                    "to": q.get("to_station_name"),
                    "dep": dep, "arr": q.get("arrive_time"),
                    "dur": q.get("lishi")})
    return out


def fetch_stops(date=DEFAULT_DATE, interval=3.5, price=None, log=print,
                conc=DEFAULT_CONC, cache_path=None):
    """车次经停站，增量抓取（已有缓存则跳过）"""
    price = price or json.load(open(_cache_paths()[0], encoding="utf-8"))
    out = cache_path or os.path.join(DATA, "stops.json")
    done = json.load(open(out, encoding="utf-8")) if os.path.exists(out) else {}
    st = fetch_stations()

    trains = _trains_from_price(price, st)
    usable = [t for t in trains if t["train_no"] and t["fc"] and t["tc"]]
    todo = [t for t in usable if not done.get(t["key"], {}).get("stops")]
    log(f"经停站：共 {len(trains)} 趟，待抓 {len(todo)} 趟"
        f"（直连 · 并发 {conc}）")
    got, _miss = fetch_stop_batch(todo, date, conc=conc, interval=interval,
                                  log=log)
    if got:
        done.update(got)
        os.makedirs(DATA, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False, indent=1)
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
        json.dump(payload, open(_cache_paths()[1], "w", encoding="utf-8"),
                  ensure_ascii=False)
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


ALIAS_PATH = os.path.join(DATA, "channel_aliases.json")
DEFAULT_FALLBACK_CHANNEL = "主通道"


def load_aliases():
    try:
        with open(ALIAS_PATH, encoding="utf-8") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def save_aliases(a):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(ALIAS_PATH, "w", encoding="utf-8") as f:
            json.dump(a, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def route_fallback():
    """只有一条通道时用的兜底名（自定义起终点时不再硬套「京沪通道」）"""
    return "主通道"


def _channel_key(members, tot):
    """通道的「特征站」：本通道覆盖率最高、全局覆盖率最低的那个中间城市。

    郑州→常州 实测：京沪 → 蚌埠、合杭 → 合肥、连镇 → 宿迁。
    只作别名表的定位键（自动命名也用它），不影响分组。
    """
    cc = Counter()
    for r in members:
        cc.update(set(r["mid"]))
    cand = [s for s in cc if cc[s] >= 2]
    if not cand:
        return ""
    cand.sort(key=lambda s: (-(cc[s] / max(1, tot.get(s, 0))), -cc[s], s))
    return cand[0]


def split_channels(recs, threshold=0.5, min_frac=0.10, min_abs=3,
                   min_merge_overlap=None):
    """把车次按「中间城市集合」的相似度聚成若干条通道（返回分组后的下标列表）。

    一条干线两侧常有好几条走廊（郑州→常州 就同时有 京沪线 / 郑阜+商合杭 /
    郑徐+连镇 三条），它们的车站轴互不相同，画在同一根轴上会互相穿插，
    所以必须先分组、每组一根轴。旧实现用「经过合肥 / 经过淮安扬州」这类**手写
    关键词**判定，换条路线（上海→杭州）就全落进「主通道」里了。

    做法：
      1. 每趟车的中间城市集合两两算 Jaccard，≥ threshold 的连边 → 连通分量
      2. 太小的分量（< max(min_abs, min_frac×总趟数)）若与某条大通道的重叠度
         ≥ min_merge_overlap，就并进重叠最多的那条 —— 像「经黄口的 K154」这种
         **同走廊多停一站**的车，重叠度很高，本就不该自立一条通道。
      3. 重叠度达不到、且自己**有**中间站的小分量，说明它的走向与谁都不同
         （典型：上海南→湖州→杭州→合肥→南京→常州 这种绕行圈），
         不能再硬塞进主干道，否则主干道的车站轴会被两套走向交替污染、
         线画成人字交叉。这些「异形簇」统一收进一条独立通道。

    `min_merge_overlap` 默认取 threshold，让判据自洽：两个对象（单车或整簇）
    只有 Jaccard ≥ threshold 才算同一条通道，连边与并入用同一把尺子。

    ⚠️ 旧版第 2 步是**无条件**并入「重叠度最大」的大分量，重叠度为 0 也照并，
       于是 上海→常州 的 G8387（重叠度 0.00）、G8981（0.13）被塞进苏州通道，
       把该通道的轴污染成「上海·苏州·无锡·湖州·杭州·合肥·南京·常州」两套走向
       交替排列 —— 就是用户看到的「明显不和谐」。

    在已有缓存路线上实测（threshold=0.5），划出来的结果与人眼判断一致：
      郑州→常州 47 趟 → 京沪 32 / 合杭 12 / 连镇 3
      郑州→南京 79 趟 → 徐州 55 / 合肥 24
      郑州→上海 99 趟 → 南京 75 / 淮安 24
      常州→上海 113 趟 → 苏州 92 / 张家港 21
      南京→上海 18 趟 → 1 条
      上海→常州 287 趟 → 苏州 257 / 江阴 28 / 绕行 2（修复后）
    """
    if min_merge_overlap is None:
        min_merge_overlap = threshold
    n = len(recs)
    if n == 0:
        return []
    sets = [set(r["mid"]) for r in recs]
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for a in range(n):
        for b in range(a + 1, n):
            A, B = sets[a], sets[b]
            if not A or not B:
                continue
            if len(A & B) / len(A | B) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    groups = sorted(groups.values(), key=len, reverse=True)

    floor = max(min_abs, int(n * min_frac))
    big = [g for g in groups if len(g) >= floor]
    small = [g for g in groups if len(g) < floor]
    if not big:                       # 全部都很小（车次极少）→ 合成一条
        return [list(range(n))]
    orphans, solo = [], []            # 零星异形车 / 走向独特但车次够多的簇
    for s in small:
        S = set()
        for i in s:
            S |= sets[i]
        best, bv = None, -1.0
        for g in big:
            G = set()
            for i in g:
                G |= sets[i]
            v = len(S & G) / max(1, len(S | G))
            if v > bv:
                best, bv = g, v
        # ① 自己没有任何中间站（起终直达）→ 无从刻画走向，归到最大的那条通道；
        # ② 与某条大通道重叠度达标 → 是同一走廊，并进去；
        # ③ 走向独特但**车次够多**（≥ min_abs）→ 自己就是一条走廊，别硬塞进主干道，
        #    也别和别的异形车混在一起（曾把「南沿江 21 趟」和 2 趟绕行圈混成一桶，
        #    那条通道的轴又变成 25 站、两种走向交替 —— 等于白修）；
        # ④ 剩下的零零星星（1~2 趟）→ 收进一条「其他」通道，免得满屏单趟选项卡。
        if not S or bv >= min_merge_overlap:
            best.extend(s)
        elif len(s) >= min_abs:
            solo.append(s)
        else:
            orphans.append((bv, s))
    # ⚠️ 顺序要紧：主干道按趟数排在前，独立的异形走廊随后，最后才是零星桶。
    # `name_channels` 的 names 与 groups 按下标一一对应，且靠「特征站」认名字，
    # 所以这里分门别类地追加，不会串位。
    big.sort(key=len, reverse=True)
    for s in sorted(solo, key=len, reverse=True):
        big.append(s)
    if orphans:
        # 按与主干道的重叠度排序，让同一条绕行走向的车挨在一起，
        # 便于 build_axes 给它们排出一根还算顺的轴
        merged = []
        for _, s in sorted(orphans, key=lambda t: -t[0]):
            merged.extend(s)
        big.append(merged)
    return big


def name_channels(recs, groups, route_key="", log=print):
    """给每组通道起名，并维护可编辑的别名表 `data/channel_aliases.json`。

    文件格式（`names` 可以手改，`keys` 是每次构建重算的「特征站」，仅供对照）：
        {
          "郑州→常州": {"names": ["京沪通道","合杭通道","连镇通道"],
                       "keys":  ["蚌埠","合肥","宿迁"]}
        }
    自动名 = 特征站 + 「通道」；只有一条通道时用 `主通道`。

    特征站为空（该组没有任何城市出现两次，典型是刚被拆出来的「异形簇」只有一两趟车）
    而**又不是唯一那条通道**时，叫 `其他通道` —— 叫「主通道」会和真正的主干道撞名。
    """
    tot = Counter()
    for r in recs:
        tot.update(set(r["mid"]))
    multi = len(groups) > 1
    keys, autos = [], []
    for g in groups:
        k = _channel_key([recs[i] for i in g], tot)
        keys.append(k)
        if k:
            autos.append(k + "通道")
        else:
            autos.append("其他通道" if multi else route_fallback())

    alias = load_aliases()
    ent = alias.get(route_key)
    names = None
    if isinstance(ent, dict):
        names = ent.get("names")
    elif isinstance(ent, list):        # 兼容手写的等价简写
        names = ent
    if not names or len(names) != len(groups):
        # 通道数变了，**人工维护的名字要尽量接回来**（否则 京沪通道 / 苏州通道
        # 会被自动名顶掉，出现过「宁陵县通道」这种名字）。
        # 认名字靠**特征站**：老 entries 里的 keys ↔ names 是一一对应的，
        # 新通道的特征站若命中某个老 key，就沿用那个名字 —— 这条对「通道数变多」
        # （拆出异形簇）和「通道数变少」（数据量变化让小簇被并回主干道）都成立，
        # 比「按位置对」稳：变少时位置会整体前移，名字会串位。
        old_names = names if isinstance(names, (list, tuple)) else None
        old_keys = ent.get("keys") if isinstance(ent, dict) else None
        by_key = {}
        if old_names and old_keys:
            for i, k in enumerate(old_keys):
                if k and i < len(old_names):
                    by_key.setdefault(k, old_names[i])
        picked, used = [], set()
        for i, k in enumerate(keys):
            nm = by_key.get(k)
            if nm and nm not in used:
                picked.append(nm)
                used.add(nm)
            else:
                picked.append(None)
        # 特征站认不出来的，再按**位置**把剩下的老名字补齐：
        # 特征站会随车次集合变化（徐州 → 蚌埠 / 南京 → 镇江），光靠它会把人工维护的
        # 名字全丢掉（出现过「宁陵县通道」）；而「第 i 条大通道还是第 i 条」
        # 在绝大多数情况下成立 —— 大通道的相对顺序只由趟数决定，很稳。
        rest = iter([n for n in (old_names or []) if n not in used])
        for i, nm in enumerate(picked):
            if nm is None:
                picked[i] = next(rest, autos[i])
        names = picked
        if route_key:
            alias[route_key] = {"names": names, "keys": keys}
            save_aliases(alias)
    else:
        if not isinstance(ent, dict) or ent.get("keys") != keys:
            alias[route_key] = {"names": names, "keys": keys}
            save_aliases(alias)
    log(f"通道划分：{len(groups)} 条 → " +
        " / ".join(f"{nm}({len(g)})" for nm, g in zip(names, groups)))
    return names


def _dedupe(names):
    """合并连续重复站，并丢掉「回头站」。

    同城双站归并到城市后会出现假回头路，例如 G3191 的真实经停是
    郑州 → 新郑机场 → **郑州航空港**，归并成 郑州 → 新郑机场 → 郑州，
    站点图上就成了一个环，`build_axes` 的拓扑排序直接失效、整条轴顺序乱掉。
    保留**首次**出现的位置（而不是最后一次）：真实里程顺序里 郑州 在新郑机场之前。
    ⚠️ 只影响轴与通道判定；图上画点仍用原始 seg，同城多站照旧各画各的。
    """
    out, seen = [], set()
    for n in names:
        if n in seen:
            continue
        seen.add(n)
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
    # ⚠️ 通道的先后顺序**按趟数从多到少**排，不能直接用 by_route 的插入序：
    #   插入序取决于「哪条通道的车次先出现在 recs 里」，随机性很大 ——
    #   曾经因为「绕行通道」恰好占了 recs[0]，2 趟车的通道被排到第一个选项卡、
    #   成为默认视图。按趟数排 → 主干道永远在最前，异形簇垫底。
    for route, seqs in sorted(by_route.items(), key=lambda kv: -len(kv[1])):
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

        if len(topo) != len(nodes):
            # 兜底：补边或同城归并仍可能引入环（如 郑州 ⇄ 新郑机场）。
            # 用「平均相对位置」排序 —— 每趟车里该站处在行程的百分之几处，
            # 取平均。比按首次出现次序排稳得多（首次出现序会被第一条车次带偏）。
            pos, hit = defaultdict(float), Counter()
            for s in seqs:
                d = len(s) - 1
                if d <= 0:
                    continue
                for i, x in enumerate(s):
                    pos[x] += i / d
                    hit[x] += 1
            axes[route] = sorted(nodes, key=lambda x:
                                 (pos[x] / max(1, hit[x]), -cnt.get(x, 0), x))
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


def build(date=DEFAULT_DATE, price=None, stops=None, live=None, log=print):
    """合并三类数据 -> 前端数据集"""
    price = price or json.load(open(_cache_paths()[0], encoding="utf-8"))
    stops = stops or json.load(open(os.path.join(DATA, "stops.json"),
                                    encoding="utf-8"))
    lp = _cache_paths()[1]
    live = live if live is not None else (
        json.load(open(lp, encoding="utf-8")) if os.path.exists(lp) else {})
    # ⚠️ 日期守卫：快照只在「快照日期 == 本次乘车日期」时才可用。
    # 否则查 10-05 时会拿 10-01 的余票去填（车次相同、余票完全不同），
    # 那种「看起来有数据其实是别日」的错误比显示「余票未知」危险得多。
    if live.get("date") and live["date"] != date:
        live = {}
    live_map = live.get("trains", {})

    # ---- 0) 把票价载荷按**物理车次**归并 ----
    # 按城市查票时，12306 会为「同城各站 × 同城各站」的每一种组合各返回一条：
    # G7744 就返回 4 条（上海松江/上海虹桥 → 武进/金坛），train_no 完全相同，
    # 只有 from/to 与到发时刻不同。
    # ⚠️ 旧写法用 `车次|开车时刻` 当字典键，同一时刻的两条会被后一条**悄悄覆盖**：
    #    同一趟车在表里出现两次（上海松江→金坛 / 上海虹桥→武进），
    #    而真正能选的组合（上海松江→武进）反而丢了 —— 始发/终到站筛选于是选不出来。
    # train_no 才是物理车次编号；缺失时退回车次号，宁可少归并也别丢数据。
    groups = {}
    for it in price["data"]:
        q = it["queryLeftNewDTO"]
        if not q.get("station_train_code"):
            continue
        groups.setdefault(q.get("train_no") or q["station_train_code"], []).append(q)

    def _mins(hhmm):
        """'02:06' → 126（分钟）。解析不了给 -1，排到最后。"""
        try:
            h, m = str(hhmm).split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return -1

    # ---- 1) 解析每趟车的区段 / 完整站序（每个物理车次只出一条）----
    segs = []
    for tid, legs in groups.items():
        # 归并成一条时取**跨度最大**的那条腿：历时最长 → 覆盖的站最多，
        # 起终站、到发时刻、票价/余票都用它（代表「这趟车在本区间能坐的最远一段」）。
        widest = max(legs, key=lambda q: (_mins(q.get("lishi")),
                                          q.get("lishi") or "", q.get("start_time") or ""))
        # 经停缓存是按 `车次|开车时刻` 存的；跨度最大那条腿的时刻若没抓到，
        # 就用同车其它腿的键去取（经停是全车次的，哪条腿取出来都一样）
        v = stops.get(widest["station_train_code"] + "|" + widest["start_time"])
        if not v:
            v = next((stops[k] for k in
                      (q2["station_train_code"] + "|" + q2["start_time"] for q2 in legs)
                      if k in stops), None)
        if not v:
            continue
        raw = [s["n"] for s in v["stops"]]
        # 区段端点**优先认查询载荷里点名的那个站**，认不到再退回「同城集合里的
        # 第一个」。只用后者会出错：查「郑州西→上海虹桥」时 G3298 的经停里
        # 郑州东排在郑州西前面，取点会从郑州东开始，起终站就与查询不符。
        fn = widest.get("from_station_name")
        tn = widest.get("to_station_name")
        i = next((k for k, s in enumerate(raw) if fn and s == fn), None)
        if i is None:
            i = next((k for k, s in enumerate(raw) if s in FROM_ST), None)
        j = None
        if i is not None:
            j = next((k for k, s in enumerate(raw) if tn and s == tn and k > i), None)
            if j is None:
                j = next((k for k, s in enumerate(raw) if s in TO_ST and k > i), None)
        if i is None or j is None:
            continue
        seg = v["stops"][i:j + 1]
        # 这趟车在本区间内**所有**可选的始发站 / 终到站（同城各站），
        # 给前端的「始发站 / 终到站」筛选用 —— 见上面 G7744 的例子。
        fs = _dedupe([q2["from_station_name"] for q2 in legs
                      if q2.get("from_station_name")])
        ts = _dedupe([q2["to_station_name"] for q2 in legs
                      if q2.get("to_station_name")])
        segs.append((widest["station_train_code"] + "|" + widest["start_time"],
                     v, widest, seg, fs, ts))

    def _norm(name):
        return norm(name, frm=FROM_NAME, to=TO_NAME)

    recs = []
    for key, v, q, seg, fs, ts in segs:
        seq = _dedupe([_norm(s["n"]) for s in seg])
        recs.append({"key": key, "v": v, "q": q, "seg": seg, "seq": seq,
                     "mid": seq[1:-1], "fs": fs, "ts": ts})

    # ---- 2) 通道划分（聚类，不再靠手写关键词）----
    groups = split_channels(recs)
    names = name_channels(recs, groups, route_key=f"{FROM_NAME} → {TO_NAME}",
                          log=log)
    for nm, g in zip(names, groups):
        for i in g:
            recs[i]["route"] = nm

    # ---- 3) 自动生成完整车站轴（含全部真实经停站） ----
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
            city = _norm(s["n"])
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
                       # ⚠️ 起终站与到发时刻必须取自**本次查询的 price 载荷**，
                       # 或本次算出的区段端点 —— 不能再用经停缓存 v 里的值。
                       # stops.json 是按「车次|开车时刻」共享的增量缓存，某趟车第一次
                       # 是在哪个区间被抓的，就永久记着那个区间的起终站；换区间后
                       # （郑州→常州 改查 郑州→上海）会张冠李戴：99 趟里 72 趟的
                       # 终到站显示成上一次的 常州/南京，而车其实开到了上海。
                       "dep": q.get("start_time") or v["dep"],
                       "arr": q.get("arrive_time") or v["arr"],
                       "dur": q.get("lishi") or v["dur"],
                       "from": seg[0]["n"], "to": seg[-1]["n"],
                       # fs / ts = 这趟车在本区间内**所有**可选的始发 / 终到站
                       # （同城各站，来自 12306 按城市查票返回的全部组合）。
                       # 前端「始发站 / 终到站」筛选按这两个集合判，所以
                       # 「上海松江 + 武进」这种不是归并后起终站的组合也能筛出来。
                       "fs": r["fs"], "ts": r["ts"],
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

    # ---- 5) 每通道一句话描述（取停靠车次最多的几个中间站）----
    desc = {}
    for rt, sts in axes.items():
        f = freqs.get(rt, {})
        mid = sorted(sts[1:-1], key=lambda s: (-(f.get(s, 0)),))
        desc[rt] = ("经 " + " · ".join(mid[:4])) if mid else ""

    data = {
        "meta": {
            "travel_date": date,
            "price_captured": time.strftime("%Y-%m-%d %H:%M"),
            "live_captured": live.get("captured_at", "—"),
            "route": f"{FROM_NAME} → {TO_NAME}",
            "from_name": FROM_NAME, "to_name": TO_NAME,
            "from_code": FROM_CODE, "to_code": TO_CODE,
        },
        "desc": desc,
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


def _seg_triples(stops, frm, to, fcity=None, tcity=None):
    """经停列表 + 区段两端 -> 前端用的 [[实际站名, 城市行, 时刻], ...]

    端点优先按站名精确匹配；匹配不到就退化成整段（不丢车）。
    城市行用 norm()，与主数据集同一套归一化规则。

    ⚠️ 首末站的「城市行」要**强制**成该程的起终点城市：武进 / 戚墅堰 都是常州的车站，
    但 norm() 认不出来（不在 CITY_FIX、也不以「常州」开头），会被判成「武进」行 ——
    这一行不在轴上，点直接被丢掉，线就只能从半路开始画（实测 G7155 / G2421：
    武进→上海松江，起点丢在武进，线从张家港才开始）。
    """
    raw = [s.get("n") for s in stops]
    i = next((k for k, s in enumerate(raw) if frm and s == frm), 0)
    j = next((k for k, s in enumerate(raw) if to and s == to and k > i), len(raw) - 1)
    seg = stops[i:j + 1]
    out = []
    for k, s in enumerate(seg):
        t = _pick_time(s.get("arr"), s.get("dep"), k == 0)
        if not t:
            continue
        nm = s.get("n")
        city = norm(nm, frm=fcity, to=tcity)
        if k == 0 and fcity and nm == frm:
            city = fcity
        elif k == len(seg) - 1 and tcity and nm == to:
            city = tcity
        out.append([nm, city, t])
    return out


def leg_stops(items, date=DEFAULT_DATE, interval=0.4, source=None, log=print,
              conc=DEFAULT_CONC):
    """补全中转两程车次的经停站（和直达**同一套查询原理**）。

    items = [{"key","code","dep","from","to","tn","fc","tc","fcity","tcity"}, ...]
      tn/fc/tc = 内部列车编号与两端电报码，来自该程的票价载荷（`_leg_trains` 带出来的）
      fcity/tcity = 该程两端所属城市，用来强制首末站的城市行

    直达用 `fetch_stops` 直连 `czxx/queryByTrainNo`，**一趟一次请求**；
    而走 MCP 时它得先把「车次号」解析成内部编号（多一次 leftTicket 请求），
    实测 1.2 秒/趟 vs 直连 0.23 秒/趟 —— 所以这里一律直连。
    并发数 `conc` 与直达共用前端「高级设置」里的那一档。
    结果并入**共享**的 stops.json（按车次存、与路线无关），
    所以同一个枢纽第二次进来几乎瞬时完成。

    返回 {"stops": {key: [[站名, 城市行, 时刻], ...]}, "missing":[key...]}
    """
    out_path = os.path.join(DATA, "stops.json")
    done = {}
    if os.path.exists(out_path):
        with open(out_path, encoding="utf-8") as f:
            done = json.load(f)

    def _names(v):
        return [s.get("n") for s in (v.get("stops") or [])]

    todo, bad = [], []
    for it in items:
        nm = _names(done.get(it.get("key")) or {})
        if not (it.get("tn") and it.get("fc") and it.get("tc")):
            bad.append(it)
        elif not nm:
            todo.append(it)
        elif it.get("to") and it["to"] not in nm:
            # ⚠️ 该车次已有经停，但范围不够：同一趟车「同起点不同终点」的变体
            # （金坛→上海虹桥 / 金坛→上海松江）共用 code|dep 一条缓存，
            # 先抓的那个可能把范围截短。缺站就用本变体的终点重抓一次。
            todo.append(it)
    got, missing = {}, []
    for it in bad:
        log(f"  {it.get('code')} 缺列车编号/电报码，跳过")
        missing.append(it.get("key"))
    log(f"经停补全：共 {len(items)} 趟，待抓 {len(todo)} 趟"
        f"（直连 · 并发 {conc}）")

    # 去重：同一趟车的多个变体（同起点不同终点）共用一次抓取
    uniq, seen = [], set()
    for it in todo:
        if it["key"] in seen:
            continue
        seen.add(it["key"])
        uniq.append({"key": it["key"], "code": it.get("code"),
                     "train_no": it.get("tn"),          # 注意：中转载荷里 tn = 内部编号
                     "fc": it.get("fc"), "tc": it.get("tc"),
                     "frm": it.get("from"), "to": it.get("to"),
                     "dep": it.get("dep"), "arr": it.get("arr"),
                     "dur": it.get("dur")})
    fresh, miss = fetch_stop_batch(uniq, date, conc=conc, interval=interval,
                                   log=log)
    dirty = False
    for k, rec in fresh.items():
        old_n = len(_names(done.get(k) or {}))
        # 只在「范围不比原来窄」时才回写：重抓本是为了补全，
        # 万一这次返回更短，别把已经够用的那条换掉
        if len(rec["stops"]) >= old_n:
            done[k] = rec
            dirty = True
        else:
            log(f"  {rec['code']} 本次只返回 {len(rec['stops'])} 站"
                f" < 已有 {old_n} 站，保留原记录")
    missing.extend(miss)
    for it in uniq:
        if it["key"] not in done:
            missing.append(it["key"])
    if dirty:
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log(f"  经停缓存写入失败：{e}")

    for it in items:
        v = done.get(it.get("key")) or {}
        if v.get("stops"):
            # ⚠️ 返回键用 vkey（含 from/to）：同一趟车会有「同起点、不同终点」的多个
            # 变体（G7155 金坛→上海虹桥 / 金坛→上海松江），它们 code|dep 相同，
            # 只用 code|dep 当返回键会互相覆盖、终点串号。
            # 抓取仍按 code|dep 去重（一份经停服务所有变体），切段各切各的。
            got[it.get("vkey") or it.get("key")] = _seg_triples(
                v["stops"], it.get("from"), it.get("to"),
                it.get("fcity"), it.get("tcity"))
        elif it.get("key") not in missing:
            missing.append(it.get("key"))
    log(f"经停补全完成：新抓 {len(fresh)} 趟，本次可返回 {len(got)} 趟，"
        f"仍缺 {len(missing)} 趟")
    return {"stops": got, "missing": missing}


# ---------------- 中转查询（点击站名时用） ----------------

def _leg_trains(price_payload, live_payload, fcity=None, tcity=None):
    """(票价旧结构, 余票快照) -> 统一车次列表

    统一结构 = [{"code","type","from","to","dep","arr","dur","seats"}]，
    seats 与 build() 产出的完全一致，所以前端可以走同一套渲染。
    另带 tn/fc/tc（直连查经停要用的内部编号与电报码）和 fcity/tcity
    （该程两端所属城市，前端画端点行、后端切区段都要用）。
    """
    lmap = (live_payload or {}).get("trains", {}) or {}
    try:
        names = fetch_stations()
    except Exception:
        names = {}
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
        fn = q.get("from_station_name")
        tn = q.get("to_station_name")
        out.append({"code": code, "type": code[0],
                    "from": fn, "to": tn,
                    "dep": dep, "arr": q.get("arrive_time"),
                    "dur": q.get("lishi"), "seats": seats,
                    "tn": q.get("train_no"),
                    "fc": q.get("from_station_telecode") or names.get(fn),
                    "tc": q.get("to_station_telecode") or names.get(tn),
                    "fcity": fcity, "tcity": tcity})
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
                    res.append({"from": a, "to": b, "trains": _leg_trains(p, lv, a, b)})
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
        res.append({"from": a, "to": b, "trains": _leg_trains(p, lv, a, b)})
    return {"date": date, "via": via, "source_used": "urllib",
            "captured_at": stamp,
            "legs": res, "warnings": warn}


# ---------------- 编排 ----------------

SOURCE_ENV = "TRAIN_SOURCE"          # mcp | urllib | auto（默认 auto）


def _fetch_via_mcp(date, want_live, interval, log, conc=DEFAULT_CONC):
    """用 mcp-server-12306 取票价 / 余票，经停站一律走直连（见 fetch_stop_batch）。

    返回 (price, stops, live)。任一步失败都抛异常，由调用方决定是否回退。
    """
    import mcp_client
    import mcp_source
    price_cache, live_cache = _cache_paths()
    with mcp_client.open_client(log=lambda m: log("  " + m)) as cli:
        price = mcp_source.load_price(cli, FROM_NAME, TO_NAME, date, log=log,
                                      save_path=price_cache)
        live = (mcp_source.load_live(cli, FROM_NAME, TO_NAME, date, log=log,
                                     save_path=live_cache)
                if want_live else None)
    # ⚠️ 经停**不走 MCP**：MCP 的 get-train-route-stations 每次要先发一次 leftTicket
    # 把「车次号」解析成内部编号（请求数翻倍、单趟 1.2 秒），而票价载荷里本来就有
    # `train_no`，直连一趟一次请求、0.25 秒。见 fetch_stop_batch 的说明。
    stops = fetch_stops(date=date, interval=interval, price=price, log=log,
                        conc=conc)
    return price, stops, live


def refresh(date=DEFAULT_DATE, want_live=True, interval=3.5, log=print,
            source=None, from_name=None, to_name=None, conc=DEFAULT_CONC):
    """完整刷新：取数 -> 构建 -> 注入

    取数有两套实现：
      mcp    —— 走 mcp-server-12306（stdio 子进程），自带重试与会话保持
      urllib —— 内置直连（原始实现），作为回退
    默认 auto：先试 mcp，失败则回退 urllib 并在报告里标明实际用的是哪套。

    from_name / to_name 可指定本次的始发 / 终到站（车站名或城市名）；
    不传则沿用进程内的当前路线，首次运行时为默认的 郑州 → 常州。
    """
    if from_name or to_name:
        set_route(from_name, to_name, log=log)
    src = (source or os.environ.get(SOURCE_ENV) or "auto").strip().lower()
    report = {"date": date, "steps": [], "ok": True,
              "source": src, "source_used": None,
              "route": f"{FROM_NAME} → {TO_NAME}",
              "from_name": FROM_NAME, "to_name": TO_NAME}

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
        log(f"数据源：MCP（mcp-server-12306）＋ 直连经停")
        try:
            price, stops, live = _fetch_via_mcp(date, want_live, interval, log, conc)
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

        log("② 经停站（增量 · 直连）")
        stops = step("经停站", fetch_stops, date=date, interval=interval,
                     price=price, log=log, conc=conc)
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
    data = step("构建", build, date=date, price=price, stops=stops, live=live,
                log=log)
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

    def arg(flag, dflt=None):
        return argv[argv.index(flag) + 1] if flag in argv else dflt

    _, rep = refresh(date=date, source=arg("--source"),
                     from_name=arg("--from"), to_name=arg("--to"))
    print(json.dumps(rep, ensure_ascii=False, indent=2))
