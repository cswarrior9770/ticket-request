# -*- coding: utf-8 -*-
"""
数据完整性审计
1) 三类数据源的车次覆盖是否一致
2) 每趟车在通道轴上有哪些真实经停站被"漏掉"（轴外站点）
3) 站点序列是否异常（重复、倒序、跨日错误）
"""
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pipeline as P

D = P.DATA
stops = json.load(open(os.path.join(D, "stops.json"), encoding="utf-8"))
price = json.load(open(os.path.join(D, "price_raw.json"), encoding="utf-8"))
viz = json.load(open(os.path.join(D, "viz_data.json"), encoding="utf-8"))

# ---------- 1. 车次覆盖 ----------
q = {}
for it in price["data"]:
    dto = it["queryLeftNewDTO"]
    q[dto["station_train_code"] + "|" + dto["start_time"]] = dto

k_price = set(q)
k_stops = set(stops)
k_viz = {t["code"] + "|" + t["dep"] for t in viz["trains"]}

print("=" * 96)
print("一、车次覆盖检查")
print("=" * 96)
print(f"  票价接口        {len(k_price):>3} 趟")
print(f"  经停数据        {len(k_stops):>3} 趟")
print(f"  前端数据集      {len(k_viz):>3} 趟")
print(f"  票价有 / 经停缺 : {sorted(k_price - k_stops) or '无'}")
print(f"  经停有 / 前端缺 : {sorted(k_stops - k_viz) or '无'}")
print(f"  前端有 / 经停缺 : {sorted(k_viz - k_stops) or '无'}")


# ---------- 2. 轴外站点 ----------
def seg_of(key, dto):
    """取出 郑州→常州 区段（未做城市归一化）"""
    v = stops[key]
    raw = [s["n"] for s in v["stops"]]
    try:
        i = next(i for i, s in enumerate(raw) if s in P.FROM_ST)
        j = next(j for j, s in enumerate(raw) if s in P.TO_ST and j > i)
    except StopIteration:
        return None
    return v["stops"][i:j + 1]


print()
print("=" * 96)
print("二、轴外站点（真实经停但未画进运行图的站）")
print("=" * 96)

off_axis = defaultdict(lambda: defaultdict(list))   # route -> station -> [车次]
for key, v in stops.items():
    dto = q.get(key)
    if not dto:
        continue
    seg = seg_of(key, dto)
    if not seg:
        continue
    mid = [P.norm(s["n"]) for s in seg[1:-1]]
    route = P._route_of(mid)
    axis = set(P.AXES[route])
    for s in seg[1:-1]:
        nm = P.norm(s["n"])
        if nm not in axis:
            off_axis[route][nm].append(v["code"])

for route in P.AXES:
    if route not in off_axis:
        continue
    print(f"\n【{route}】轴 {len(P.AXES[route])} 站，轴外站 {len(off_axis[route])} 个：")
    for st, codes in sorted(off_axis[route].items(), key=lambda x: -len(x[1])):
        print(f"    {st:<10} 被 {len(codes):>2} 趟停靠  {sorted(set(codes))}")

# ---------- 3. 序列异常 ----------
print()
print("=" * 96)
print("三、站点序列异常检查")
print("=" * 96)
issues = 0
for key, v in stops.items():
    dto = q.get(key)
    if not dto:
        continue
    seg = seg_of(key, dto)
    if not seg:
        continue
    names = [P.norm(s["n"]) for s in seg]
    # 重复站
    dup = [n for n, c in Counter(names).items() if c > 1]
    if dup:
        issues += 1
        print(f"  ▲ {v['code']:<7} 重复站: {dup}   全序列: {' → '.join(names)}")
print(f"  （重复站共 {issues} 趟）")

# ---------- 4. 前端数据集逐趟核对 ----------
print()
print("=" * 96)
print("四、前端数据集 vs 真实经停 逐趟核对")
print("=" * 96)
loss_total = 0
for t in viz["trains"]:
    key = t["code"] + "|" + t["dep"]
    seg = seg_of(key, q[key])
    if not seg:
        continue
    mid = [P.norm(s["n"]) for s in seg[1:-1]]
    route = P._route_of(mid)
    axis = set(P.AXES[route])
    full = [P.norm(s["n"]) for s in seg]
    full = [x for x in full if x in axis]           # 本应画出的站
    drawn = [s[0] for s in t["stops"]]              # 实际画出
    missing = [x for x in full if x not in drawn]
    if missing or len(full) != len(drawn):
        loss_total += 1
        print(f"  ▲ {t['code']:<7} 应画 {len(full)} 站 / 实画 {len(drawn)} 站   "
              f"缺失={missing}")
print(f"  （存在差异 {loss_total} 趟 / 共 {len(viz['trains'])} 趟）")
