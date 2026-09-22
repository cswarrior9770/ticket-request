# -*- coding: utf-8 -*-
"""mcp-server-12306 → 本项目中间结构 的适配层

设计取舍
--------
本模块把 MCP 工具的返回**转成 pipeline.build() 已经认识的旧结构**
（`{"data":[{"queryLeftNewDTO":{...}}]}` / `{key:{...stops}}` / `{key:{席别:值}}`），
这样 build() 一行都不用改，切换数据源不会引入回归风险。

代价是这里要模拟旧接口的两处「怪东西」（都有注释标明）：
  1. 票价用「角」为单位的整数字符串（旧接口是 03010 = 301.0 元）
  2. 余票字段名用旧接口的缩写（edz/ydz/tdz …）
将来若做大重构，可把 build() 改成吃规范化结构，再把本层去掉。

实测：MCP 与旧直连两套数据源在这条线路上**车次完全一致**
（47/47，车次+发车时刻相同，到达时刻与历时零差异），可直接替换。
"""
import json
import os
import time

# ---- query-ticket-price 的 prices 中文席别名 → 内部席别键 ----
SEAT_PRICE = {
    "商务座": "swz", "特等座": "tz", "优选一等座": "zy", "一等座": "zy",
    "二等座": "ze", "高级软卧": "gr", "软卧": "rw", "硬卧": "yw",
    "软座": "rz", "硬座": "yz", "无座": "wz",
}
# ---- query-tickets 的 seats 英文键 → 旧余票字段缩写（见 pipeline.SEAT_MAP）----
SEAT_AVAIL = {
    "business": "swz", "special_class": "tdz", "first_class": "ydz",
    "second_class": "edz", "advanced_soft_sleeper": "gr",
    "soft_sleeper": "rw", "hard_sleeper": "yw", "soft_seat": "rz",
    "hard_seat": "yz", "no_seat": "wz", "standing": "wz",
}
# ---- train_class_name → 车型字母（仅用于日志核对，build() 用 train_code 首字母）----
CLASS_TO_TYPE = {"高速": "G", "动车": "D", "城际": "C",
                 "直特": "Z", "特快": "T", "快速": "K"}

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data")


def _enc_price(p):
    """'315.0' → '3150'（旧接口以角为单位的整数字符串）"""
    if p in (None, "", "--"):
        return None
    try:
        return str(int(round(float(p) * 10)))
    except (TypeError, ValueError):
        return None


def _throttle(last, interval):
    gap = time.time() - last[0]
    if gap < interval:
        time.sleep(interval - gap)
    last[0] = time.time()


# ---------------- 票价 + 席别编组 ----------------

def load_price(client, from_station, to_station, date, log=print, save=True,
               save_path=None):
    """⚠️ 已停用：这条路取到的是**公布票价**，不是实付的执行票价。

    MCP 的 `query-ticket-price` 打的是 `leftTicketPrice/queryAllPublicPrice`
    （接口名里就是 PublicPrice），不打折；而 12306 实际售卖的是
    `leftTicketPrice/query` 的**执行票价**（浮动折扣后）。两者在高铁动车上
    普遍差 5~40%（实测 G1808 二等座 公布 ¥502 / 执行 ¥398）。

    所以票价一律走 `pipeline.fetch_price()` 直连。本函数保留仅供对照/测试，
    **不要再接到正式取数路径上**（pipeline 已不再调用它）。
    """
    log("MCP query-ticket-price …（注意：这是公布票价，正式路径不使用）")
    r = client.call_json("query-ticket-price", {
        "from_station": from_station, "to_station": to_station,
        "train_date": date}, timeout=90)
    if not r or not r.get("data"):
        raise RuntimeError(f"query-ticket-price 返回空：{str(r)[:200]}")
    out = []
    for it in r["data"]:
        q = {
            "station_train_code": it.get("train_code"),
            "train_no": it.get("train_no"),
            "from_station_name": it.get("from_station"),
            "to_station_name": it.get("to_station"),
            "start_time": it.get("start_time"),
            "arrive_time": it.get("arrive_time"),
            "lishi": it.get("duration"),
            "train_class_name": it.get("train_class_name"),
        }
        for cn, p in (it.get("prices") or {}).items():
            k = SEAT_PRICE.get(cn)
            if k:
                q[k + "_price"] = _enc_price(p)
        out.append({"queryLeftNewDTO": q})
    log(f"MCP 票价：{len(out)} 趟")
    payload = {"source": "mcp", "from_station": from_station,
               "to_station": to_station, "train_date": date, "data": out}
    # ⚠️ save=False 用于「中转查询」等旁路取数：绝不能覆盖主数据集缓存
    #    save_path 由调用方按「当前路线」给出（自定义起终点时各自一份缓存）
    if save:
        p = save_path or os.path.join(DATA, "price_raw.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    return payload


# ---------------- 经停站（增量缓存） ----------------

def load_stops(client, price, date, interval=3.5, log=print,
               cache_path=None, force=False):
    """⚠️ 已弃用（2026-09-20）：经停抓取现在一律直连，见 `pipeline.fetch_stop_batch`。

    保留只为兼容旧调用。走 MCP 的 `get-train-route-stations` 会先把「车次号」
    解析成内部列车编号（那次 leftTicket 请求），而票价载荷里本来就有 `train_no`，
    等于白白多一倍请求、单趟 1.2 秒 vs 直连 0.25 秒。
    """
    out_path = cache_path or os.path.join(DATA, "stops.json")
    done = {}
    if os.path.exists(out_path) and not force:
        with open(out_path, encoding="utf-8") as f:
            done = json.load(f)

    trains = [it["queryLeftNewDTO"] for it in price["data"]]
    todo = [q for q in trains
            if force or not done.get(f"{q['station_train_code']}|{q['start_time']}",
                                     {}).get("stops")]
    log(f"MCP 经停站：共 {len(trains)} 趟，待抓 {len(todo)} 趟")
    last = [0.0]
    for q in todo:
        code = q["station_train_code"]
        key = f"{code}|{q['start_time']}"
        _throttle(last, interval)
        try:
            r = client.call_json("get-train-route-stations", {
                "train_no": code,
                "from_station": q.get("from_station_name"),
                "to_station": q.get("to_station_name"),
                "train_date": date}, timeout=90)
            rows = (r or {}).get("stations") or []
            if not rows:
                log(f"  {code:<7} 返回 0 站，跳过")
                continue
            done[key] = {
                "code": code, "train_no": code,
                "from": q.get("from_station_name"), "to": q.get("to_station_name"),
                "dep": q.get("start_time"), "arr": q.get("arrive_time"),
                "dur": q.get("lishi"),
                "stops": [{"n": s.get("station_name"),
                           "arr": s.get("arrive_time"),
                           "dep": s.get("start_time"),
                           "stop": s.get("stopover_time")} for s in rows],
            }
            log(f"  {code:<7} {len(rows):>3} 站")
            os.makedirs(DATA, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(done, f, ensure_ascii=False, indent=1)
        except Exception as e:
            log(f"  {code} 失败：{type(e).__name__}: {str(e)[:90]}")
    return done


# ---------------- 实时余票 ----------------

def load_live(client, from_station, to_station, date, log=print, save=True,
              save_path=None):
    log("MCP query-tickets …")
    r = client.call_json("query-tickets", {
        "from_station": from_station, "to_station": to_station,
        "train_date": date}, timeout=90)
    if not r or not r.get("trains"):
        raise RuntimeError(f"query-tickets 返回空：{str(r)[:200]}")
    snap = {}
    for t in r["trains"]:
        code = t.get("train_no")          # 注意：这里的 train_no 是「车次号」
        dep = t.get("start_time")
        if not code or not dep:
            continue
        seats = {}
        for en, v in (t.get("seats") or {}).items():
            k = SEAT_AVAIL.get(en)
            if k and v not in (None, ""):
                seats[k] = v
        snap[f"{code}|{dep}"] = seats
    payload = {"source": "mcp", "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "date": date, "trains": snap}
    # ⚠️ save=False 用于「中转查询」等旁路取数：
    #    否则会把别的区间的余票写进主快照，整张图的数据就串了
    #    save_path 由调用方按「当前路线」给出（自定义起终点时各自一份缓存）
    if save:
        p = save_path or os.path.join(DATA, "live_snapshot.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    log(f"MCP 余票：{len(snap)} 趟")
    return payload
