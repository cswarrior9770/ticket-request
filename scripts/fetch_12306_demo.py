# -*- coding: utf-8 -*-
"""
12306 接口完整验证 v2：站点字典 -> 余票查询 -> 字段解析 -> 经停站
纯标准库实现，串行低频请求
"""
import json
import re
import ssl
import time
import urllib.request

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_cookie = {"v": ""}


def fetch(url, referer="https://kyfw.12306.cn/otn/leftTicket/init", timeout=20):
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "zh-CN,zh;q=0.9", "Referer": referer,
         "Connection": "keep-alive"}
    if _cookie["v"]:
        h["Cookie"] = _cookie["v"]
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as r:
        sc = r.headers.get("Set-Cookie")
        if sc and not _cookie["v"]:
            _cookie["v"] = sc.split(";")[0]
        return r.status, r.read().decode("utf-8", errors="replace")


# 12306 余票字段索引（依据返回结构实测）
IDX = {
    "secret": 0, "button": 1, "train_no": 2, "train_code": 3,
    "from_code": 4, "to_code": 5, "from_station": 6, "to_station": 7,
    "dep_time": 8, "arr_time": 9, "duration": 10,
    "gr": 21,   # 高级软卧
    "rw": 23,   # 软卧
    "rz": 24,   # 软座
    "yw": 28,   # 硬卧
    "yz": 29,   # 硬座
    "wz": 26,   # 无座
    "edz": 30,  # 二等座
    "ydz": 31,  # 一等座
    "swz": 32,  # 商务座
    "tdz": 33,  # 特等座
    "dw": 20,   # 动卧
}


def main():
    print("=" * 78)
    print(" 12306 接口完整验证")
    print("=" * 78)

    # 1. 站点字典
    print("\n[1] 站点字典")
    st, txt = fetch("https://kyfw.12306.cn/otn/resources/js/framework/station_name.js")
    stations = dict(re.findall(r"@[a-z]+\|([\u4e00-\u9fa5]+)\|([A-Z]+)\|", txt))
    print(f"    HTTP {st}  站点总数 = {len(stations)}")
    for n in ["郑州", "常州", "徐州", "南京", "郑州东", "常州北", "徐州东"]:
        print(f"      {n:<6} -> {stations.get(n, 'N/A')}")

    date = "2026-10-01"
    ZZF, CZH = stations["郑州"], stations["常州"]

    # 2. 余票查询
    print(f"\n[2] 余票查询  郑州({ZZF}) -> 常州({CZH})  {date}")
    time.sleep(2)
    url = ("https://kyfw.12306.cn/otn/leftTicket/query"
           f"?leftTicketDTO.train_date={date}"
           f"&leftTicketDTO.from_station={ZZF}"
           f"&leftTicketDTO.to_station={CZH}&purpose_codes=ADULT")
    st, txt = fetch(url)
    d = json.loads(txt)
    print(f"    HTTP {st}  httpstatus={d.get('httpstatus')}  status={d.get('status')}")
    data = d.get("data") or {}
    result = data.get("result") or []
    smap = data.get("map") or {}
    print(f"    车次总数 = {len(result)}   站点映射 = {len(smap)}")

    # 3. 字段解析
    print(f"\n[3] 字段解析（逐列索引，以第 1 趟车为例）")
    cols = result[0].split("|")
    print(f"    字段总数 = {len(cols)}")
    for i, v in enumerate(cols):
        mark = ""
        for k, ix in IDX.items():
            if ix == i:
                mark = f"  <== {k}"
        if v or mark:
            print(f"      [{i:>2}] {v!r:<28}{mark}")

    # 4. 车次表格
    print(f"\n[4] 郑州 -> 常州 全程车次（全部 {len(result)} 趟）")
    print("    " + "-" * 100)
    print(f"    {'车次':<8}{'出发':<7}{'到达':<7}{'历时':<8}"
          f"{'二等':<7}{'一等':<7}{'商务':<7}{'软卧':<7}{'硬卧':<7}{'硬座':<7}")
    print("    " + "-" * 100)
    trains = []
    for rec in result:
        c = rec.split("|")
        if len(c) < 34:
            continue
        row = {
            "code": c[IDX["train_code"]],
            "train_no": c[IDX["train_no"]],
            "from": smap.get(c[IDX["from_station"]], c[IDX["from_station"]]),
            "to": smap.get(c[IDX["to_station"]], c[IDX["to_station"]]),
            "dep": c[IDX["dep_time"]], "arr": c[IDX["arr_time"]],
            "dur": c[IDX["duration"]],
            "edz": c[IDX["edz"]] or "-", "ydz": c[IDX["ydz"]] or "-",
            "swz": c[IDX["swz"]] or "-", "rw": c[IDX["rw"]] or "-",
            "yw": c[IDX["yw"]] or "-", "yz": c[IDX["yz"]] or "-",
        }
        trains.append(row)
        print(f"    {row['code']:<8}{row['dep']:<7}{row['arr']:<7}{row['dur']:<8}"
              f"{row['edz']:<7}{row['ydz']:<7}{row['swz']:<7}"
              f"{row['rw']:<7}{row['yw']:<7}{row['yz']:<7}")
    print("    " + "-" * 100)

    # 车型统计
    from collections import Counter
    types = Counter(t["code"][0] for t in trains)
    print(f"\n    车型分布: {dict(types)}")
    has_ticket = [t for t in trains
                  if any(t[k] not in ("-", "无", "") for k in ["edz", "ydz", "swz", "rw", "yw", "yz"])]
    print(f"    至少一个席别有票的车次: {len(has_ticket)} / {len(trains)}")

    # 5. 经停站
    print(f"\n[5] 经停站接口测试")
    t0 = trains[0]
    time.sleep(2)
    u = ("https://kyfw.12306.cn/otn/czxx/queryByTrainNo"
         f"?train_no={t0['train_no']}&from_station_telecode={ZZF}"
         f"&to_station_telecode={CZH}&depart_date={date}")
    try:
        st, txt = fetch(u, referer="https://kyfw.12306.cn/otn/leftTicket/init")
        j = json.loads(txt)
        rows = (j.get("data") or {}).get("data") or []
        print(f"    HTTP {st}  车次 {t0['code']} 经停站数 = {len(rows)}")
        print("    " + "-" * 74)
        print(f"    {'序':<4}{'车站':<12}{'到达':<9}{'发车':<9}{'停车':<9}{'里程':<8}")
        print("    " + "-" * 74)
        for r_ in rows:
            print(f"    {r_.get('station_no',''):<4}{r_.get('station_name',''):<12}"
                  f"{r_.get('arrive_time',''):<9}{r_.get('start_time',''):<9}"
                  f"{str(r_.get('stopover_time','')):<9}{str(r_.get('distance','')):<8}")
        print("    " + "-" * 74)
    except Exception as e:
        print(f"    失败: {type(e).__name__}: {str(e)[:120]}")

    print("\n" + "=" * 78)
    print(" 验证完成 —— 接口可用，数据结构已确认")
    print("=" * 78)


if __name__ == "__main__":
    main()
