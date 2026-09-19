# -*- coding: utf-8 -*-
"""分析 47 趟车在「郑州 → 常州」区段的实际经由，自动聚类成若干线路"""
import json
import os
from collections import defaultdict, Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(os.path.join(BASE, "data", "stops.json"), encoding="utf-8"))

FROM_ST = {"郑州", "郑州东", "郑州航空港", "焦作"}
TO_ST = {"常州", "常州北", "金坛"}

rows = []
for k, v in d.items():
    stops = [s["n"] for s in v["stops"]]
    try:
        i = next(i for i, s in enumerate(stops) if s in FROM_ST)
        j = next(j for j, s in enumerate(stops) if s in TO_ST and j > i)
    except StopIteration:
        continue
    seg = stops[i:j + 1]
    mid = seg[1:-1]
    rows.append({
        "code": v["code"], "dep": v["dep"], "arr": v["arr"],
        "dur": v["dur"], "type": v["code"][0],
        "from": seg[0], "to": seg[-1], "mid": mid,
    })

rows.sort(key=lambda r: r["dep"])

print(f"共 {len(rows)} 趟车有完整区段\n")
print("=" * 140)
for r in rows:
    print(f"{r['code']:<7}{r['type']}  {r['dep']}-{r['arr']} ({r['dur']})  "
          f"{r['from']}→{r['to']}")
    print(f"        经由: {' → '.join(r['mid']) if r['mid'] else '(直达无中间站)'}")
print("=" * 140)

# 按「关键枢纽」归类
KEYS = {
    "徐州": ["徐州", "徐州东"],
    "合肥": ["合肥", "合肥西", "水家湖", "淮南南", "寿县", "颍上北", "阜阳西"],
    "南京": ["南京", "南京南"],
}
print("\n\n=== 按经由枢纽归类 ===")
buckets = defaultdict(list)
for r in rows:
    m = set(r["mid"])
    via = []
    for name, alias in KEYS.items():
        if m & set(alias):
            via.append(name)
    key = "+".join(via) if via else "其他"
    buckets[key].append(r)

for k in sorted(buckets, key=lambda x: -len(buckets[x])):
    lst = buckets[k]
    print(f"\n【{k}】{len(lst)} 趟")
    for r in lst:
        print(f"    {r['code']:<7}{r['type']} {r['dep']}-{r['arr']:<7}({r['dur']})  "
              f"经由 {' → '.join(r['mid'][:8])}{'...' if len(r['mid'])>8 else ''}")

print("\n\n=== 中间站数量分布 ===")
c = Counter(len(r["mid"]) for r in rows)
for n in sorted(c):
    print(f"  {n:>2} 个中间站: {c[n]} 趟")
