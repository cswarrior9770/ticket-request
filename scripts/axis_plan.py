# -*- coding: utf-8 -*-
"""为每条通道计算车站轴：按停靠频次筛选，保证图表清晰"""
import json
import os
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(os.path.join(BASE, "data", "stops.json"), encoding="utf-8"))

FROM_ST = {"郑州", "郑州东", "郑州航空港", "焦作"}
TO_ST = {"常州", "常州北", "金坛"}

# 城市名归一化（去掉方位后缀 / 同城站统一）
CITY_FIX = {
    "郑州东": "郑州", "郑州航空港": "郑州",
    "常州北": "常州", "金坛": "常州",
    "开封北": "开封", "兰考南": "兰考", "民权北": "民权",
    "商丘东": "商丘", "徐州东": "徐州", "宿州东": "宿州",
    "蚌埠南": "蚌埠", "滁州北": "滁州", "南京南": "南京",
    "镇江南": "镇江", "丹阳北": "丹阳", "无锡东": "无锡",
    "苏州北": "苏州", "昆山南": "昆山", "上海虹桥": "上海",
    "洛阳龙门": "洛阳", "渭南北": "渭南", "西安北": "西安",
    "合肥北城": "合肥", "淮南南": "淮南", "阜阳西": "阜阳",
    "太和东": "太和", "颍上北": "颍上", "亳州南": "亳州",
    "扬州东": "扬州", "淮安东": "淮安", "萧县北": "萧县",
    "永城北": "永城", "砀山南": "砀山", "睢宁": "睢宁",
    "大港南": "镇江",
}


def norm(n):
    return CITY_FIX.get(n, n)


rows = []
for k, v in d.items():
    stops = [s["n"] for s in v["stops"]]
    try:
        i = next(i for i, s in enumerate(stops) if s in FROM_ST)
        j = next(j for j, s in enumerate(stops) if s in TO_ST and j > i)
    except StopIteration:
        continue
    seg = stops[i:j + 1]
    rows.append({"code": v["code"], "dep": v["dep"], "arr": v["arr"],
                 "dur": v["dur"], "type": v["code"][0],
                 "seg": [norm(s) for s in seg],
                 "raw": seg})


def route_of(mid):
    m = set(mid)
    huai = {"合肥", "阜阳", "淮南", "水家湖", "颍上", "寿县", "太和", "亳州", "安庆", "芜湖"}
    yang = {"淮安", "扬州", "宝应", "高邮", "宿迁", "泗阳", "睢宁"}
    if m & yang and "南京" not in m:
        return "连镇通道"
    if m & huai:
        return "合杭通道"
    return "京沪通道"


for r in rows:
    r["route"] = route_of(r["seg"][1:-1])

groups = defaultdict(list)
for r in rows:
    groups[r["route"]].append(r)

for name in ["京沪通道", "合杭通道", "连镇通道"]:
    g = groups.get(name, [])
    if not g:
        continue
    cnt = Counter()
    for r in g:
        for s in r["seg"][1:-1]:
            cnt[s] += 1
    print(f"\n{'='*88}")
    print(f"【{name}】{len(g)} 趟车   中间站共 {len(cnt)} 个")
    print(f"{'='*88}")
    print("  停靠频次：")
    for s, c in cnt.most_common():
        bar = "█" * c
        print(f"    {s:<8}{c:>3}  {bar}")
    for th in (2, 3, 5):
        axis = [s for s, c in cnt.items() if c >= th]
        # 保持出现顺序
        order = []
        for r in g:
            for s in r["seg"][1:-1]:
                if s in axis and s not in order:
                    order.append(s)
        print(f"  阈值>={th}: {len(order)+2} 站 -> 郑州 | {' | '.join(order)} | 常州")

print(f"\n\n{'='*88}")
print("全部车型分布（用于筛选器）")
t = Counter(r["type"] for r in rows)
for k in ["G", "D", "Z", "T", "K"]:
    if t.get(k):
        print(f"  {k}: {t[k]}")
