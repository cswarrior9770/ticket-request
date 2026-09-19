# -*- coding: utf-8 -*-
"""探查 mcp-server-12306 三个数据工具的真实返回结构

只发 4 次请求：search-stations / query-ticket-price / query-tickets /
get-train-route-stations。目的是拿到字段名，写映射层用。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_client as M  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data")
DATE = "2026-10-01"
FROM, TO = "郑州", "常州"


def brief(obj, depth=0):
    """打印结构摘要：键名 + 类型 + 小样本"""
    pad = "  " * depth
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                n = len(v)
                print(f"{pad}{k}: {type(v).__name__}({n})")
                if depth < 2 and n:
                    brief(v[0] if isinstance(v, list) else v, depth + 1)
            else:
                s = str(v)
                print(f"{pad}{k}: {type(v).__name__} = {s[:70]}")
    elif isinstance(obj, list):
        if obj:
            brief(obj[0], depth)
    else:
        print(f"{pad}{obj!r}")


def main():
    log = lambda m: print(m, file=sys.stderr)          # noqa: E731
    with M.open_client(log=log) as c:
        print("=" * 72)
        print("① search-stations  郑州 / 常州")
        print("=" * 72)
        for q in (FROM, TO):
            r = c.call_json("search-stations", {"query": q, "limit": 4})
            print(f"--- query={q} ---")
            brief(r)
            print()

        print("=" * 72)
        print("② query-ticket-price")
        print("=" * 72)
        r = c.call_json("query-ticket-price",
                        {"from_station": FROM, "to_station": TO,
                         "train_date": DATE})
        brief(r)
        print()
        # 存一份原始样本，后面写映射层时对照
        with open(os.path.join(OUT, "mcp_price_raw.json"), "w",
                  encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print("→ 已存 data/mcp_price_raw.json")
        print()

        print("=" * 72)
        print("③ query-tickets（余票）")
        print("=" * 72)
        r = c.call_json("query-tickets",
                        {"from_station": FROM, "to_station": TO,
                         "train_date": DATE})
        brief(r)
        with open(os.path.join(OUT, "mcp_tickets_raw.json"), "w",
                  encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=1)
        print("→ 已存 data/mcp_tickets_raw.json")
        print()

        print("=" * 72)
        print("④ get-train-route-stations  K558（用三字码与全名都试）")
        print("=" * 72)
        for args in ({"train_no": "K558", "from_station": FROM,
                      "to_station": TO, "train_date": DATE},
                     {"train_no": "K558", "from_station": "ZZF",
                      "to_station": "CZH", "train_date": DATE}):
            try:
                r = c.call_json("get-train-route-stations", args)
                print(f"--- args={args} ---")
                brief(r)
                with open(os.path.join(OUT, "mcp_stops_raw.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, indent=1)
                print("→ 已存 data/mcp_stops_raw.json")
                break
            except Exception as e:
                print(f"失败：{type(e).__name__}: {str(e)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
