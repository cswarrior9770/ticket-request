# 12306 车次运行图原型（郑州 → 常州）
<img width="2560" height="1304" alt="image" src="https://github.com/user-attachments/assets/ef26b568-0301-4b92-abbe-f62b70e50a4b" />
<img width="2560" height="1304" alt="image" src="https://github.com/user-attachments/assets/7d8e6d31-afbc-4f6d-a227-4c72de4d7a47" />

一个**零第三方依赖**的本地工具：把 12306 某条线路的列车时刻，画成一张可视化的「运行图」，
支持按车型筛选、余票/席别、以及「经某站中转」的两程联程查询。

> 当前内置线路为 **郑州 → 常州**，覆盖三条通道：**京沪通道 / 合杭通道 / 连镇通道**。
> 线路、站点、日期都集中在 `scripts/pipeline.py` 里，改几行常量即可适配其它区间。

---

## ✨ 功能特性

- **运行图可视化**：每趟车一条横线，横轴是时间、纵轴是车站；G/D/Z/T/K 与纯数字车次各有线型。
- **车型筛选（多选）**：正常模式下可任意勾选车型（G / D / Z / T / K …），线型只分三类
  - 高铁 **G** 实线
  - 动车 **D** / 城际 **C** 虚线
  - 普速 **Z / T / K / 纯数字** 点线
- **席别筛选**：默认关注「二等座 + 卧铺」，可改；起价按所选席别中有票的最低价计算。
- **余票 / 有票高亮**：可只看做有票的车次，或把有票的车次高亮出来。
- **中转联程查询**：点左侧站名即选中该站，刷新后**实时抓取**「郑州 → 该站」与「该站 → 常州」两程，
  就地叠加绘制，且原线路隐藏；退出即恢复。
  - 两程的**车型筛选互相独立**（第 1 程 / 第 2 程各一行按钮）。
  - 中转站若涉及多个实际车站（如 **徐州** 与 **徐州东**），图上用**不同形状的点**区分，并带行内图例。
  - 车次明细表在联程模式下**按两程分段展示**，每段各自排序、各自计趟数。
- **搜索车次**：支持完整车次 / 纯数字 / 前缀，即时筛选运行图与明细表。

---

## 🚀 快速开始

### 方式一：双击启动（推荐）

直接双击根目录的 **`启动.bat`**。它会自动探测本机的 Python（3.10+），
起好本地服务并打开浏览器。关闭那个黑色窗口即停止服务。

### 方式二：命令行

```bash
# 默认 http://127.0.0.1:8765/
python scripts/start.py

# 指定端口
python scripts/start.py --port 9000

# 只起服务、不自动开浏览器
python scripts/start.py --no-browser

# 改默认乘车日期
python scripts/start.py --date 2026-10-01
```

打开浏览器访问 <http://127.0.0.1:8765/> 即可。

> 也可以直接用浏览器打开 `运行图原型.html`（`file://` 路径），但「刷新数据 / 中转查询」
> 需要本地服务在跑，所以建议走上面的启动方式。

---

## 📁 目录结构

```
ticket-request/
├── 启动.bat              一键启动脚本（ASCII，双击即用）
├── 运行图原型.html        单文件前端（运行图 + 明细表）
├── data/                 缓存与数据集（JSON）
│   ├── viz_data.json       前端数据集（由 pipeline 生成）
│   ├── live_snapshot.json  余票快照
│   ├── price_raw.json      票价原始返回
│   ├── route_zz_cz.json    线路原始返回
│   ├── stations.json       站点表
│   └── stops.json          经停站明细
└── scripts/
    ├── start.py          启动器：探测 Python → 起服务 → 开浏览器
    ├── server.py         本地 HTTP 服务 + 数据刷新 / 中转 API
    ├── pipeline.py       取数流水线（票价 → 经停 → 余票 → 构建数据集 → 注入 HTML）
    ├── mcp_client.py      通用 MCP stdio 客户端（纯标准库，可复用）
    ├── mcp_source.py      MCP 返回 → pipeline 认识的旧结构
    ├── probe_mcp.py       MCP 返回结构探查工具
    ├── audit_data.py      数据完整性审计
    ├── analyze_routes.py  线路分析
    ├── axis_plan.py       运行图纵轴（站点）规划
    └── fetch_12306_demo.py 12306 接口直连示例
```

---

## 🔌 数据来源与刷新

数据源默认 **`auto`**：先尝试 `mcp-server-12306`（`uvx` 拉起的 stdio 子进程），
失败自动回退到内置 `urllib` 直连 12306 接口。

- 环境变量 `TRAIN_SOURCE=mcp|urllib|auto` 可强制指定；
- API 参数 `?source=` 亦可临时覆盖。
- 页面里的「刷新数据」按钮会重新抓取并写回 `data/viz_data.json`。

刷新 / 状态 / 中转接口：

| 接口 | 说明 |
|---|---|
| `GET /api/data` | 返回当前数据集 |
| `GET /api/refresh?date=YYYY-MM-DD` | 后台启动刷新（返回是否已启动） |
| `GET /api/status` | 查询刷新进度与日志 |
| `GET /api/transfer?via=徐州` | 查询经该站中转的两段车次（同步返回，带缓存） |

---

## 🤖 开发与第三方开源软件

### 本项目的产出方式

本项目由 **DeepSeek V3.1** 以 *vibe coding*（自然语言驱动、AI 辅助迭代）方式辅助生成与打磨：
前端运行图、取数流水线、中转联程逻辑均由对话式迭代完成，人工负责架构决策与最终校验。

### 第三方开源软件

本项目在runtime以独立子进程方式调用以下开源组件：

| 组件 | 用途 | 许可证 | 与本仓库的关系 |
|---|---|---|---|
| [`mcp-server-12306`](https://github.com/drfccv/12306-mcp-server)（drfccv） | 12306 余票 / 经停 / 中转查询（可选数据源） | MIT（含「禁止商用」附加条款） | 经 `uvx` 以**独立子进程**拉起 |
| [`uv` / `uvx`](https://github.com/astral-sh/uv)（Astral） | 可选的 Python 包运行器 | MIT | 运行 MCP 服务 |

数据来源为 **12306 官方公开接口**（属数据而非软件）。

---

## ⚠️ 已知限制 / 说明

- 线路、站点、默认日期均为**示例配置**，集中在 `scripts/pipeline.py`（`AXES` / `FROM_CODE` / `DEFAULT_DATE` 等），
  要换区间需改这几处常量并重新抓取。
- 实时余票来自 12306 公开接口，高频调用会被限流；中转查询对两程分别抓取，耗时随车次数量增加。
- 第三方开源软件与许可证见上方「开发与第三方开源软件」一节；本仓库以 **MIT License** 发布（详见根目录 [`LICENSE`](./LICENSE)）。

---

## 📄 许可证

本仓库以 **MIT License** 发布（详见根目录 [`LICENSE`](./LICENSE) 文件）。

