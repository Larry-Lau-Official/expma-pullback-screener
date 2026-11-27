---
name: expma-pullback-screener
version: 1.0.0
display_name: 胡诌EXPMA选股器
display_name_en: EXPMA Pullback Screener
description: 基于 EXPMA(12,50) 指标筛选 A 股个股：经历一轮带涨停的上涨趋势后回调，EMA2 组要求收盘价在 EMA2 上方（0~+2%）且回调期间不跌破 EMA2，EMA1 组要求收盘价在 EMA1 附近（上下 1%）且回调期间不跌破 EMA1，两组并列输出。自动排除科创板、北交所、ST 股。当用户要求选股、筛选股票、找回调到 EXPMA/EMA 支撑位的个股时使用本技能。仅供学习研究与娱乐，不构成投资建议。
description_zh: 基于 EXPMA(12,50) 指标筛选 A 股个股：经历一轮带涨停的上涨趋势后回调，EMA2 组要求收盘价在 EMA2 上方（0~+2%）且回调期间不跌破 EMA2，EMA1 组要求收盘价在 EMA1 附近（上下 1%）且回调期间不跌破 EMA1，两组并列输出。自动排除科创板、北交所、ST 股。当用户要求选股、筛选股票、找回调到 EXPMA/EMA 支撑位的个股时使用本技能。仅供学习研究与娱乐，不构成投资建议。
description_en: Screen A-shares with EXPMA(12,50): find stocks that completed a run-up with at least one limit-up and pulled back — EMA2 group requires the close 0 to +2 percent above EMA2 with no close below EMA2 during the pullback; EMA1 group requires the close within 1 percent of EMA1 with no close below EMA1 during the pullback. Two parallel groups. Excludes STAR Market, Beijing Stock Exchange, and ST stocks. Use when the user asks for EXPMA/EMA-based stock screening or stocks pulling back to EMA support. For learning, research, and entertainment only; not investment advice.
agent_created: true
---

# EXPMA 回踩选股

## 用途

从全 A 股中筛出符合以下形态的个股：先经历一轮上涨趋势（期间至少一次涨停），随后回调。两组并列判定，**各自要求在回调区间（阶段高点到最新交易日）内收盘价从未跌破自己那条均线**：EMA2 组要求最新收盘价在 EMA2（EXPMA 长期线）上方 0~+2% 以内、且回调期间未跌破 EMA2；EMA1 组要求收盘价在 EMA1（短期线）上下 1% 以内、且回调期间未跌破 EMA1。满足任一组即入选，结果用「命中组」字段标注；输出 CSV、JSON 与 Excel 友好版 XLSX，按评分排序。

这与"曾经触及均线后已经涨上去"的票不同——后者行情已走完，不属于本策略目标。

> **免责声明**：本技能仅供学习、研究与娱乐，输出的是技术形态筛选结果，**不构成任何投资建议或买卖依据**。行情数据来自第三方，不保证准确完整；使用者须自行遵守数据提供方条款，禁止分发或泄露缓存数据与结果文件，风险自负。

## 触发条件

用户提出以下任一诉求时立即使用：

- 用 EXPMA / EMA 指标选股、筛股
- 找"上涨后回调到 EMA2 附近"的个股
- 按涨停 + 回调形态筛选 A 股

## 筛选条件（默认值）

| 条件 | 默认值 | 说明 |
|------|--------|------|
| EXPMA 参数 | EMA1=12 日, EMA2=50 日 | 东方财富默认值，已与截图校准一致 |
| 回踩判定（组一 EMA2） | 最新交易日**收盘价**在 EMA2 上方 0 ~ +2% | 负偏离（收盘价低于 EMA2）一律不算；且阶段高点到最新交易日之间，收盘价任何一天都不低于当日 EMA2 |
| 回踩判定（组二 EMA1） | 同一交易日**收盘价**在 EMA1 上方 0 ~ +1% | 与组一并列，满足任一组即入选；两组都满足标注为 `EMA1+EMA2`。名义容差 ±1%，但因回调不能跌破 EMA1，负偏离价实际会被排除，有效区间为 0 ~ +1% |
| 比较精度 | 均线值四舍五入到 0.01 元 | EMA 为浮点值，按两位小数比较，与行情软件显示一致，避免尾差误判 |
| 阶段高点 | 最近 150 个交易日最高价 | 回调天数从高点算起 |
| 回调周期 | ≤ 10 个交易日 | 超过 10 日最多容忍到 12 日，放备选池 |
| 上涨段 | 高点前 60 日内最低点到高点涨幅 ≥ 20% | 保证确实经历一轮上涨 |
| 涨停 | 上涨段 ≥ 1 次涨停 | 主板/创业板按 9.8%/19.8% 判定，硬条件 |
| 总市值 | < 300 亿 | 前置过滤，可用 `--max-mktcap` 调整（单位：万元） |
| 当前股价 | < 50 元 | 前置过滤，可用 `--max-price` 调整 |
| 排除 | 科创板(688/689)、北交所、ST/*ST/退市 | 板块与风险过滤 |

市值与股价在拉 K 线前先行过滤（新浪列表一次拉取全市场，缓存按最新交易日判断），可显著缩小扫描范围并提速。

评分排序依据：涨停次数、阶段涨幅、EMA1 与 EMA2 多头排列、EMA2 向上、贴近 EMA1 / EMA2 各 +10（双命中 +20）、回调缩量（回调均量 < 上涨均量的 0.8）。评分仅用于排序，过滤条件是硬性的。

## 执行方式

在 skill 根目录（本文件所在目录）执行。首次运行或换到新环境（如另一台机器、另一个 Agent 平台）时先用 requirements.txt 安装依赖，无需随包携带平台相关的二进制依赖：

```bash
python -m pip install -r requirements.txt
python scripts/screen.py
```

默认使用 **BaoStock（证券宝）** 数据源，稳定且免费。输出到 `output/expma_screen_<时间戳>.csv`、`.json` 和 `.xlsx`；Excel/WPS 用户请打开 `.xlsx`，其代码列保留 `002187` 这类前导 0。首次运行会拉取并缓存全市场 K 线；此后缓存**按最新交易日判断新鲜度**：只要缓存里的数据日期落后于最新交易日，就自动重新拉取，不再依赖固定小时数计时。后台串行执行、资源占用低，可随时中断后重跑续传。

常用参数：

```bash
python scripts/screen.py --ema2-tol 0.025    # 放宽 EMA2 正偏离上限到 +2.5%
python scripts/screen.py --ema1-tol 0.008    # 收紧 EMA1 回踩容差到 ±0.8%
python scripts/screen.py --max-pullback 8    # 收紧回调周期为 8 日
python scripts/screen.py --min-rise 30       # 阶段涨幅至少 30%
python scripts/screen.py --min-limit-ups 2   # 上涨段至少 2 次涨停
python scripts/screen.py --max-mktcap 2000000  # 市值上限改为 200 亿（万元）
python scripts/screen.py --max-price 30     # 股价上限改为 30 元
python scripts/screen.py --no-cache          # 强制刷新 K 线缓存
python scripts/screen.py --offline           # 离线模式：只用本地已缓存数据筛选
python scripts/screen.py --limit 500         # 只扫前 500 只（调试用）
python scripts/screen.py --source web        # 改用网页接口（不推荐，易被风控）
```

完整参数说明见 `scripts/screen.py` 的 `--help`。

## 数据源与容错

**主源：BaoStock（证券宝）** —— 免费、免注册、服务端协议（非网页爬虫），不受财经网站 WAF 风控影响，数据稳定。公开发布版用 `requirements.txt` 声明依赖（`baostock==0.9.4`，pandas/numpy 由 pip 安装）；本地若存在 `vendor/` 目录，脚本会优先使用它，可离线运行。

- 股票列表：`bs.query_all_stock`，一次性返回全部沪深 A 股，自带 ST 标记；自动排除科创板、北交所、指数与基金
- K 线：`bs.query_history_k_data_plus`，前复权日线（adjustflag=2），含涨跌幅字段
- 更新节奏：BaoStock 数据 T+1，交易日 17:30 后入库当天日线；盘前/盘中运行时最新为上一交易日
- 已规避 BaoStock 0.9.4 在 Python 3.13 + 新 pandas 下 `rs.next()` 死循环的兼容问题（K 线直接读 `rs.data`）
- **性能保护**：串行抓取（单连接协议）、每 300 只重连、每 1000 只落盘一次、按交易日判断缓存。全市场约 4600 只，单只约 0.3 秒，首次约 20-25 分钟，后续同交易日走缓存仅需数秒

**缓存新鲜度规则**（K 线）：不存在固定小时数计时，每次联网运行先用 BaoStock 确认最新交易日，再逐只比对缓存——

| 情况 | 行为 |
|------|------|
| 缓存数据末日 = 最新交易日 | 直接用缓存，不重新下载 |
| 缓存数据末日 < 最新交易日 | 自动重新拉取该股 K 线 |
| 停牌股数据末日落后，但缓存是在最新交易日**之后**抓的 | 视为已检查过，跳过，避免每次重复请求 |

市值/股价缓存（新浪快照）同样按交易日判断；`--no-cache` 可强制忽略所有缓存重拉，`--offline` 则完全不用网络、只看本地已有数据（不考虑新鲜度）。

**备源：网页接口**（`--source web`）—— 新浪/东财列表 + 东财/腾讯 K 线，仅在 BaoStock 不可用时用。这些接口对高频请求有风控（腾讯 WAF 501、东财断连），内置限速与指数退避，但仍可能失败。

**离线模式**（`--offline`）—— 不发任何网络请求，自动合并 BaoStock 缓存（`bs_<代码>.json`）与早期网页缓存（`<代码>.json`），适合随时基于已累积数据出结果。全市场扫描每 1000 只落盘一次，中断后重跑会从缓存续传。

## 结果解读

- **状态=命中**：所有硬条件满足，最新收盘价符合 EMA2 或 EMA1 组口径，是主要关注对象
- **状态=备选(回调超10日)**：收盘价已符合 EMA2 或 EMA1 组口径，但回调周期 11-12 日，略超偏好
- **命中组**：`EMA2` = 仅贴合长期线（价格在 EMA2 上方、回调期间未跌破 EMA2）；`EMA1` = 仅贴近短期线（回调期间未跌破 EMA1）；`EMA1+EMA2` = 双命中，两条均线粘合，评分最高
- **回踩偏离%**：相对命中均线的偏离（双命中时以 EMA2 为准）；因回调不破约束，两组均为 ≥ 0 的值
- **当前偏离EMA1% / 当前偏离EMA2%**：最新收盘价相对两条均线的偏离，越接近 0 越贴合对应组
- **回调量比**：回调期均量 / 上涨期均量，小于 1 表示缩量回调

详细策略原理与调参建议见 `references/strategy.md`。
