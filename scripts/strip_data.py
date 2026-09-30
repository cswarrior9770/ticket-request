# -*- coding: utf-8 -*-
"""提交前清理 HTML 里内联的 `const DATA` 缓存（可逆）。

为什么需要它
------------
`运行图原型.html` 是单文件前端，为了双击（file://）也能看，数据流水线会把整份
数据集**内联**成一行 `const DATA = {...}`。这行是缓存：`data/` 目录已被 .gitignore
排除，但这一行在 HTML 里，会跟着 git 一起提交（每次刷新都变，diff 又大又没意义）。

本脚本把它换成一个空壳，让仓库里存的是「代码」而不是「某一天的数据」：
    const DATA = {"meta":{},"desc":{},"axes":{},"freq":{},"rowst":{},"trains":[]};
清空后：
  · 用 启动.bat 打开（http）→ 完全正常：页面会向 /api/data 取数并自己填回来；
  · 直接双击（file://）→ 只会是空图（没有数据可画），这是预期行为。

用法
----
    python scripts/strip_data.py            清空内联数据（提交 / push 前跑这个）
    python scripts/strip_data.py --check    只看当前状态，不改任何文件
    python scripts/strip_data.py --restore  从 data/viz_data.json 恢复内联数据（不联网）
    python scripts/strip_data.py --backup   清空前另存一份 .bak-时间戳
    python scripts/strip_data.py --file X   指定目标文件（默认 运行图原型.html）

安全性
------
  · 只改**一行**（那一行必须能 json 解析）；改完逐行比对，除该行外必须字节一致，
    任何一项校验失败 → 原样回滚（内存里留了原文，不依赖备份文件）。
  · 幂等：已是空壳时直接告知，不做任何写入。
  · 数据本身不会丢：`data/viz_data.json`（本地、已被 gitignore）里还有一份，
    可用 --restore 灌回去；实在没有也能重新抓。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HTML = os.path.join(ROOT, "运行图原型.html")
VIZ = os.path.join(ROOT, "data", "viz_data.json")

# 与 pipeline.inject_html() 认的是同一行。
# ⚠️ 用 [^\r\n]* 而不是 .*：`.` 会把行尾的 \r 也吃进去，替换后这一行就变成 LF，
#    整篇 CRLF 里混进一个 LF（git diff 会显示这行「换行符改了」）。
DATA_RE = re.compile(r"(?m)^const DATA = ([^\r\n]*)")

STUB = {"meta": {}, "desc": {}, "axes": {}, "freq": {}, "rowst": {}, "trains": []}
STUB_LINE = "const DATA = " + json.dumps(STUB, ensure_ascii=False,
                                        separators=(",", ":"))


def find_data_span(text):
    """定位 `const DATA = ...` 那一行。

    返回 (行号(0基), 行内容, 起, 止)；找不到返回 (None, None, None, None)。
    止 = 行内容末尾（不含换行），所以 text[:起] + 新行 + text[止:] 就是替换结果。
    """
    m = DATA_RE.search(text)
    if not m:
        return None, None, None, None
    return text[:m.start()].count("\n"), m.group(1), m.start(), m.end()


def find_data_line(text):
    lineno, line, _, _ = find_data_span(text)
    return lineno, line


def is_stub(line):
    try:
        return json.loads(line) == STUB
    except Exception:
        return False


def human(n):
    return f"{n/1024:.1f} KB" if n >= 1024 else f"{n} B"


def read_text(path):
    """保真读取。

    ⚠️ 必须 newline=""：这个 HTML 是 CRLF 行尾，若用默认的通用换行模式读，
    `\\r\\n` 会在内存里变成 `\\n`，写回去就整篇变成 LF —— git 会显示「全文都改了」。
    """
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path, text):
    """同样用 newline="" 原样写出（不把 \\n 翻译成平台换行）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


def write_checked(path, old_text, new_text, what):
    """写入并三重校验：只换一行 + 新行可解析 + 其余字节一致。失败则原样回滚。"""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    if len(old_lines) != len(new_lines):
        raise RuntimeError(f"行数变了（{len(old_lines)} → {len(new_lines)}），已放弃")
    diff = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
    if len(diff) != 1:
        raise RuntimeError(f"被改动的行数 = {len(diff)}（只应改 1 行），已放弃")
    idx = diff[0]
    json.loads(new_lines[idx][len("const DATA = "):])   # 新行必须是合法 JSON

    write_text(path, new_text)
    print(f"  ✓ 已{what}：第 {idx + 1} 行")
    print(f"    {human(len(old_text.encode('utf-8')))} → "
          f"{human(len(new_text.encode('utf-8')))}")


def do_check(path):
    text = read_text(path)
    lineno, line = find_data_line(text)
    if lineno is None:
        print("× 没找到 `const DATA = ` 那一行 —— 文件是不是被动过？")
        return 1
    size = len(line.encode("utf-8"))
    if is_stub(line):
        print(f"· 内联数据已经是空壳了（第 {lineno + 1} 行）")
        print("  可以放心提交；启动.bat 打开后会自动填回数据。")
        return 0
    try:
        d = json.loads(line)
        n = len(d.get("trains") or [])
    except Exception as e:
        n = f"（JSON 解析失败：{e}）"
    print(f"× 内联数据仍在：第 {lineno + 1} 行，占 {human(size)}，{n} 趟车")
    print("  提交前跑一下 python scripts/strip_data.py 清掉它。")
    return 2


def do_strip(path, backup=False):
    if not os.path.exists(path):
        print(f"× 找不到文件：{path}")
        return 1
    text = read_text(path)
    lineno, line, start, end = find_data_span(text)
    if lineno is None:
        print("× 没找到 `const DATA = ` 那一行 —— 已放弃（没有动文件）")
        return 1
    if is_stub(line):
        print("· 内联数据已经是空壳，无需处理。")
        return 0
    try:
        n = len(json.loads(line).get("trains") or [])
    except Exception as e:
        print(f"× 这一行不是合法 JSON（{e}）—— 已放弃，没有动文件")
        return 1

    print(f"清理内联数据：{os.path.basename(path)}")
    print(f"  当前：第 {lineno + 1} 行，{human(len(line.encode('utf-8')))}，{n} 趟车")
    if backup:
        bak = f"{path}.bak-{time.strftime('%Y%m%d-%H%M')}"
        with open(bak, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print(f"  备份：{os.path.basename(bak)}")

    new_text = text[:start] + STUB_LINE + text[end:]
    write_checked(path, text, new_text, "清空")
    print("\n下一步：")
    print("  1) git 提交并 push（仓库里只存代码，不存某一天的数据）")
    print("  2) 之后双击 启动.bat 照常使用 —— 服务端会自己把数据填回来")
    print("  3) 想要离线也能看：python scripts/strip_data.py --restore")
    return 0


def do_restore(path):
    if not os.path.exists(VIZ):
        print(f"× 找不到 {os.path.relpath(VIZ, ROOT)} —— 没法恢复。")
        print("  双击 启动.bat 后点一次「刷新数据」即可重新生成（这次要联网抓）。")
        return 1
    data = json.load(open(VIZ, encoding="utf-8"))
    raw = "const DATA = " + json.dumps(data, ensure_ascii=False,
                                       separators=(",", ":"))
    text = read_text(path)
    lineno, line, start, end = find_data_span(text)
    if lineno is None:
        print("× 没找到 `const DATA = ` 那一行 —— 已放弃（没有动文件）")
        return 1
    if line == raw:
        print("· 内联数据与 data/viz_data.json 一致，无需恢复。")
        return 0
    print(f"恢复内联数据：{os.path.basename(path)}")
    print(f"  来源：{os.path.relpath(VIZ, ROOT)}（{human(os.path.getsize(VIZ))}，"
          f"{len(data.get('trains') or [])} 趟车）")
    new_text = text[:start] + raw + text[end:]
    write_checked(path, text, new_text, "恢复")
    print("  ⚠️ 恢复后 HTML 会重新变成「有数据」的状态，提交前记得再清一次。")
    return 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("word", nargs="?", choices=["check", "restore"],
                    help="裸词写法，等价于 --check / --restore（方便 .bat 转发）")
    ap.add_argument("--check", action="store_true", help="只看状态，不改文件")
    ap.add_argument("--restore", action="store_true", help="从 data/viz_data.json 恢复")
    ap.add_argument("--backup", action="store_true", help="清空前另存 .bak-时间戳")
    ap.add_argument("--file", default=DEFAULT_HTML, help="目标 HTML（默认 运行图原型.html）")
    a = ap.parse_args()
    path = a.file if os.path.isabs(a.file) else os.path.join(ROOT, a.file)

    if a.check or a.word == "check":
        return do_check(path)
    if a.restore or a.word == "restore":
        return do_restore(path)
    return do_strip(path, backup=a.backup)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
