#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXPMA 回踩选股器 —— 筛选"上涨后回调至 EMA1 / EMA2 附近"的 A 股个股。

筛选逻辑（可用命令行参数覆盖）：
  1. 排除 ST / *ST / 退市、科创板(688/689)、北交所(43/83/87/92/8 开头)
  2. 在最近 lookback 个交易日中定位最高价那天（阶段高点）
  3. 高点到最新交易日之间的交易日数 <= max_pullback（默认 10）
  4. 高点之前 rise_window 天内定位起涨低点，阶段涨幅 >= min_rise
  5. 上涨段内至少出现 min_limit_ups 次涨停（主板 9.8%、创业板 19.8%）
  6. 两组并列判定，各自校验对应均线在回调区间（阶段高点到最新交易日）内从未被收盘价跌破：
     EMA2 组：收盘价在当日 EMA2 的 0 ~ +ema2_tol 区间（负偏离不算）且回调期间未跌破 EMA2；
     EMA1 组：收盘价落在当日 EMA1 ±ema1_tol 内且回调期间未跌破 EMA1。
     只看收盘价，不看盘中最低价，用"命中组"字段标注
  7. 按命中强度、涨停次数、涨幅、均线趋势、回调量能等指标打分排序

数据源：BaoStock（证券宝，默认，依赖见 requirements.txt；本地 vendor/ 可选），网页接口（新浪/东财/腾讯）作为备用。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_CFG = {
    "ema_short": 12,          # EXPMA 短期线 EMA1（东方财富默认）
    "ema_long": 50,           # EXPMA 长期线 EMA2（东方财富默认）
    "ema2_tol": 0.02,         # EMA2 组只接受 0 ~ +2% 偏离（负偏离不算，硬条件，并列组一）
    "ema1_tol": 0.01,         # 最新交易日收盘价距 EMA1 容差 ±1%（硬条件，并列组二）
    "max_pullback": 10,       # 高点到最新的最大回调交易日数
    "min_rise": 20.0,         # 阶段最小涨幅 %
    "rise_window": 60,        # 起涨点向前搜索窗口（交易日）
    "min_limit_ups": 1,       # 上涨段最少涨停次数
    "lookback": 150,          # 分析窗口（交易日）
    "min_bars": 120,          # 最少 K 线根数（保证 EMA50 收敛）
    "cache_hours": 20,        # K 线缓存有效小时数
    "max_mktcap": 3000000.0,  # 最大总市值（万元），300 亿 = 3,000,000 万元
    "max_price": 50.0,        # 最大当前股价（元）
}

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


# ---------------------------------------------------------------------------
# 网络与数据抓取
# ---------------------------------------------------------------------------
# 全局限速：行情接口对高频请求有风控（腾讯 WAF 501 / 东财 RemoteDisconnected），
# 通过令牌间隔 + 指数退避降低触发概率。可通过 --workers 控制并发。
import threading
_last_request = [0.0]
_rate_lock = threading.Lock()
MIN_INTERVAL = 0.12  # 两次请求最小间隔（秒）


def http_get(url: str, headers: dict | None = None, timeout: int = 20,
             retries: int = 3) -> str:
    last = None
    for attempt in range(retries + 1):
        with _rate_lock:
            wait = MIN_INTERVAL - (time.time() - _last_request[0])
            if wait > 0:
                time.sleep(wait)
            _last_request[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001 - 网络重试
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (2 ** attempt))  # 指数退避 1.5s/3s/6s
    raise last  # type: ignore[misc]


def _cache_path(name: str) -> str:
    cache_dir = os.path.join(BASE_DIR, "output", ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, name)


def _fetch_list_eastmoney() -> list[dict]:
    scopes = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
    stocks: list[dict] = []
    page = 1
    total = 0
    while True:
        url = (
            "https://push2.eastmoney.com/api/qt/clist/get"
            f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={scopes}"
            "&fields=f2,f3,f12,f13,f14,f20,f21"
        )
        payload = json.loads(http_get(url, retries=3)).get("data") or {}
        diff = payload.get("diff") or []
        if not diff:
            break
        total = int(payload.get("total", 0))
        for row in diff:
            stocks.append({
                "code": str(row.get("f12", "")),
                "market": int(row.get("f13", 0)),
                "name": str(row.get("f14", "")).strip(),
                "price": row.get("f2"),
                "change": row.get("f3"),
                "mktcap": row.get("f20"),
            })
        if len(stocks) >= total:
            break
        page += 1
        if page > 80:
            break
    return stocks


def _fetch_list_sina() -> list[dict]:
    """新浪 A 股列表备份源（node=hs_a 覆盖沪深京全部 A 股）。"""
    stocks: list[dict] = []
    for page in range(1, 70):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node=hs_a")
        rows = None
        for attempt in range(4):  # 逐页重试，网络抖动时不要整体放弃
            try:
                req = urllib.request.Request(url, headers={**UA, "Referer": "https://finance.sina.com.cn/"})
                text = urllib.request.urlopen(req, timeout=25).read().decode("gbk", "ignore")
                rows = json.loads(text)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5 * (attempt + 1))
        if not rows:
            continue  # 该页彻底失败则跳过，继续后续页
        for row in rows:
            symbol = str(row.get("symbol", ""))       # sh600000 / sz000001 / bj920000
            code = str(row.get("code", ""))
            market = 1 if symbol.startswith("sh") else 0
            stocks.append({
                "code": code, "market": market,
                "name": str(row.get("name", "")).strip(),
                "price": row.get("trade"), "change": row.get("changepercent"),
                "mktcap": None,
            })
        if len(rows) < 80:
            break
    return stocks


def fetch_stock_list() -> list[dict]:
    """拉取全部 A 股列表：优先东财，失败回退新浪；结果本地缓存当天有效。"""
    cache = _cache_path("stock_list.json")
    if os.path.exists(cache):
        try:
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache))
            if age < timedelta(hours=12):
                with open(cache, "r", encoding="utf-8") as fh:
                    cached = json.load(fh)
                if len(cached) > 3000:
                    return cached
        except Exception:  # noqa: BLE001
            pass

    stocks: list[dict] = []
    try:
        stocks = _fetch_list_sina()
    except Exception as exc:  # noqa: BLE001
        print(f"      新浪列表不可用({type(exc).__name__})，切换东财备份源 ...", flush=True)
    if len(stocks) < 3000:
        try:
            stocks = _fetch_list_eastmoney()
        except Exception as exc:  # noqa: BLE001
            print(f"      东财列表亦不可用({type(exc).__name__})", flush=True)
    if stocks:
        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(stocks, fh, ensure_ascii=False)
        except OSError:
            pass
    return stocks


def excluded(code: str, name: str) -> bool:
    upper = name.upper().replace(" ", "")
    if "ST" in upper or "退" in name or "PT" in upper:
        return True
    if code.startswith(("688", "689")):                 # 科创板
        return True
    if code.startswith(("43", "83", "87", "92")) or code.startswith("8"):  # 北交所
        return True
    if not (code.startswith(("0", "3", "6"))):
        return True
    return False


def _parse_kline_lines(lines: list[str]) -> list[dict]:
    bars = []
    for line in lines:
        p = line.split(",")
        if len(p) < 11:
            continue
        def num(x):
            try:
                return float(x)
            except (ValueError, TypeError):
                return 0.0
        bars.append({
            "date": p[0], "open": num(p[1]), "close": num(p[2]),
            "high": num(p[3]), "low": num(p[4]), "volume": num(p[5]),
            "amount": num(p[6]), "pct": num(p[8]), "turn": num(p[10]),
        })
    return bars


def fetch_kline(market: int, code: str, limit: int, cfg: dict) -> list[dict] | None:
    """抓取前复权日 K；先查本地缓存，失败时回退腾讯数据源。"""
    cache_dir = os.path.join(BASE_DIR, "output", ".cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{code}.json")
    if os.path.exists(cache_file):
        try:
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_file))
            if age < timedelta(hours=cfg["cache_hours"]):
                with open(cache_file, "r", encoding="utf-8") as fh:
                    return _parse_kline_lines(json.load(fh))
        except Exception:  # noqa: BLE001 - 缓存损坏则重新抓取
            pass

    lines: list[str] = []
    secid = f"{market}.{code}"
    try:
        url = (
            "https://63.push2his.eastmoney.com/api/qt/stock/kline/get"
            f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            f"&klt=101&fqt=1&beg=0&end=20500101&lmt={limit}"
        )
        data = json.loads(http_get(url)).get("data") or {}
        lines = data.get("klines") or []
    except Exception:  # noqa: BLE001
        lines = []

    if not lines:
        try:  # 腾讯备用源（前复权）
            prefix = "sh" if market == 1 else "sz"
            url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
                   f"?param={prefix}{code},day,,,{limit},qfq")
            node = json.loads(http_get(url)).get("data", {}).get(prefix + code, {})
            rows = node.get("qfqday") or node.get("day") or []
            prev = None
            for row in rows:
                if len(row) < 6:
                    continue
                close = float(row[2])
                pct = 0.0 if prev in (None, 0) else (close / prev - 1) * 100
                prev = close
                lines.append(f"{row[0]},{row[1]},{row[2]},{row[3]},{row[4]},"
                             f"{row[5]},0,0,{pct:.2f},0,0")
        except Exception:  # noqa: BLE001
            return None

    if not lines:
        return None
    try:
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump(lines, fh, ensure_ascii=False)
    except OSError:
        pass
    return _parse_kline_lines(lines)


# ---------------------------------------------------------------------------
# 指标与形态识别
# ---------------------------------------------------------------------------
def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(value * k + out[-1] * (1 - k))
    return out


def limit_threshold(code: str) -> float:
    return 19.8 if code.startswith(("300", "301")) else 9.8


def analyze(stock: dict, bars: list[dict], cfg: dict) -> dict | None:
    n = len(bars)
    if n < cfg["min_bars"]:
        return None

    close = [b["close"] for b in bars]
    high = [b["high"] for b in bars]
    low = [b["low"] for b in bars]
    volume = [b["volume"] for b in bars]
    pct = [b["pct"] for b in bars]
    e1 = ema(close, cfg["ema_short"])
    e2 = ema(close, cfg["ema_long"])

    # 1) 阶段高点：分析窗口内最高价
    window_start = max(0, n - cfg["lookback"])
    hi_window = high[window_start:]
    peak = window_start + max(range(len(hi_window)), key=lambda i: hi_window[i])
    pullback_days = (n - 1) - peak
    if pullback_days < 1:
        return None
    # 略超期的股票不直接丢弃，放入备选池（用户描述为"最好不要超过 10 天"）
    pullback_over = pullback_days > cfg["max_pullback"]
    if pullback_over and pullback_days > cfg["max_pullback"] + 2:
        return None

    # 2) 起涨低点：高点向前 rise_window 根内的最低价
    search_start = max(0, peak - cfg["rise_window"])
    start = min(range(search_start, peak + 1), key=lambda i: low[i])
    if start >= peak or low[start] <= 0:
        return None
    rise_pct = (high[peak] - low[start]) / low[start] * 100
    if rise_pct < cfg["min_rise"]:
        return None

    # 3) 上涨段涨停次数（硬条件）
    th = limit_threshold(stock["code"])
    limit_days = [i for i in range(start, peak + 1) if pct[i] >= th]
    if len(limit_days) < cfg["min_limit_ups"]:
        return None

    # 4) 均线贴合判定（两组并列），只看收盘价，不看盘中最低价。
    #    每组各自要求：回调区间（阶段高点当天至最新交易日）内收盘价从未跌破对应均线。
    #    EMA2 组只接受最新收盘价高于（或等于）EMA2 的 0 ~ +ema2_tol 偏离，负偏离不算；
    #    EMA1 组接受 ±ema1_tol 偏离（因回调不破约束，负偏离实际会被排除，
    #    有效区间为 0 ~ +ema1_tol），另加 0.02 元价格精度缓冲。
    #    均线按两位小数比较，与行情软件显示一致，避免浮点尾差误判。
    last_close = close[-1]
    e2_last, e1_last = e2[-1], e1[-1]
    dc2 = last_close - e2_last if e2_last > 0 else float("inf")
    dc1 = last_close - e1_last if e1_last > 0 else float("inf")
    e2_upper = round(e2_last * (1 + cfg["ema2_tol"]), 2) if e2_last > 0 else -1
    e2_positive = e2_last > 0 and round(e2_last, 2) <= last_close <= e2_upper
    held_e2 = all(close[i] >= round(e2[i], 2) for i in range(peak, n))
    held_e1 = all(close[i] >= round(e1[i], 2) for i in range(peak, n))
    hit2 = e2_positive and held_e2
    hit1 = (e1_last > 0 and abs(dc1) <= e1_last * cfg["ema1_tol"] + 0.02
            and held_e1)
    if not (hit1 or hit2):       # 两组都不满足，直接排除
        return None
    if hit1 and hit2:
        group = "EMA1+EMA2"
    elif hit1:
        group = "EMA1"
    else:
        group = "EMA2"

    is_hit = not pullback_over   # 回调略超 10 日者仍保留为备选
    touch = n - 1
    touch_kind = "收盘价"
    # 回踩偏离以命中的均线为准；双命中时优先展示策略核心 EMA2
    ref_dev = dc1 / e1_last * 100 if (hit1 and not hit2) else dc2 / e2_last * 100

    # 5) 评分（仅用于排序，过滤条件仍需全部满足）
    score = 0.0
    score += min(len(limit_days), 5) * 8
    score += min(rise_pct / 10.0, 12)
    if e1[-1] > e2[-1]:
        score += 8
    if e2[-1] > e2[max(0, n - 6)]:
        score += 6
    if hit1:                     # 贴近 EMA1 / EMA2 各 +10，双命中 +20
        score += 10
    if hit2:
        score += 10
    if pullback_over:
        score -= 12

    up_vol = sum(volume[start:peak + 1]) / max(1, peak - start + 1)
    pb_vol = sum(volume[peak + 1:n]) / max(1, pullback_days)
    vol_shrink = (pb_vol / up_vol) if up_vol > 0 else 1.0
    if vol_shrink < 0.8:
        score += 8

    return {
        "代码": stock["code"],
        "名称": stock["name"],
        "最新价": round(close[-1], 2),
        "总市值(亿)": (round(stock["mktcap"] / 10000, 1)
                       if stock.get("mktcap") else None),
        "最新涨幅%": round(pct[-1], 2),
        "状态": "命中" if is_hit else "备选(回调超10日)",
        "命中组": group,
        "回踩日": bars[touch]["date"],
        "回踩方式": touch_kind,
        "回踩日EMA1": round(e1[touch], 2),
        "回踩日EMA2": round(e2[touch], 2),
        "回踩偏离%": round(ref_dev, 2),
        "当前偏离EMA1%": round(dc1 / e1_last * 100, 2) if e1_last > 0 else None,
        "当前偏离EMA2%": round(dc2 / e2_last * 100, 2) if e2_last > 0 else None,
        "起涨日": bars[start]["date"],
        "高点日": bars[peak]["date"],
        "阶段涨幅%": round(rise_pct, 2),
        "涨停次数": len(limit_days),
        "回调交易日": pullback_days,
        "EMA1": round(e1[-1], 2),
        "EMA2": round(e2[-1], 2),
        "回调量比": round(vol_shrink, 2),
        "评分": round(score, 1),
        "_peak_price": round(high[peak], 2),
        "_start_price": round(low[start], 2),
    }


# ---------------------------------------------------------------------------
# 结果保存与展示
# ---------------------------------------------------------------------------
def _write_xlsx(path: str, results: list[dict], fields: list[str]) -> bool:
    """写 Excel/WPS 友好版：代码列强制为文本，避免前导 0 被表格软件吃掉。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("      未安装 openpyxl，跳过 xlsx 输出（pip install openpyxl）", flush=True)
        return False

    wb = Workbook()
    ws = wb.active
    ws.title = "EXPMA筛选结果"
    ws.append(fields)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    for row in results:
        ws.append([row.get(f) for f in fields])

    if "代码" in fields and results:
        col = fields.index("代码") + 1
        for r in range(2, ws.max_row + 1):
            cell = ws.cell(row=r, column=col)
            cell.value = str(cell.value or "").zfill(6)
            cell.number_format = "@"          # 文本格式，保留 002187 这类前导 0

    for i, field in enumerate(fields, 1):
        sample = [len(str(r.get(field, ""))) for r in results[:200]]
        width = max([len(field) * 2] + sample) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(max(width, 10), 30)
    ws.freeze_panes = "A2"
    wb.save(path)
    return True


def save_results(results: list[dict], cfg: dict, out_dir: str,
                 scope: str = "") -> tuple[str, str]:
    results.sort(key=lambda x: (x["状态"] != "命中", -x["评分"]))
    print(f"[2/3] 扫描完成：命中 {sum(1 for r in results if r['状态']=='命中')} 只，"
          f"备选 {sum(1 for r in results if r['状态']!='命中')} 只", flush=True)

    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = os.path.join(out_dir, f"expma_screen_{stamp}.csv")
    json_path = os.path.join(out_dir, f"expma_screen_{stamp}.json")
    xlsx_path = os.path.join(out_dir, f"expma_screen_{stamp}.xlsx")
    fields = [k for k in results[0].keys() if not k.startswith("_")] if results else []

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"generated_at": datetime.now().isoformat(timespec="seconds"),
                   "config": cfg, "scope": scope, "count": len(results),
                   "results": results}, fh, ensure_ascii=False, indent=2)
    xlsx_ok = _write_xlsx(xlsx_path, results, fields) if results else False

    saved = f"      {csv_path}\n      {json_path}"
    if xlsx_ok:
        saved += f"\n      {xlsx_path}（Excel/WPS 用这个，代码保留前导 0）"
    print(f"[3/3] 结果已保存：\n{saved}", flush=True)
    if results:
        show = ["代码", "名称", "最新价", "总市值(亿)", "状态", "命中组", "回踩日",
                "回踩偏离%", "当前偏离EMA1%", "当前偏离EMA2%", "阶段涨幅%",
                "涨停次数", "回调交易日", "评分"]
        cols = [c for c in show if c in fields]
        widths = {c: max(len(c), *(len(str(r[c])) for r in results[:50])) for c in cols}
        print("\n" + " | ".join(c.ljust(widths[c]) for c in cols))
        print("-" * sum(widths[c] + 3 for c in cols))
        for row in results[:50]:
            print(" | ".join(str(row[c]).ljust(widths[c]) for c in cols))
        if len(results) > 50:
            print(f"... 其余 {len(results) - 50} 只见结果文件")
    return csv_path, json_path


def _fresh_by_trade_day(cache_file: str, trade_day: str | None,
                        bars: list[dict] | None = None) -> bool:
    """按最新交易日判断 K 线缓存是否新鲜（替代固定小时数计时）。

    满足任一条件即视为新鲜：
      1. 缓存最后一根 K 线的日期 == 最新交易日；
      2. 缓存文件的修改日期严格晚于最新交易日 —— 停牌股数据虽旧，但已在新
         交易日后重新抓过，重拉也拿不到新数据，避免每次运行重复请求。
         注意不能用 >=：新交易日当天的缓存可能是盘中抓的，数据尚未更新。
    trade_day 为空时返回 False，由调用方决定回退策略。
    """
    if not trade_day:
        return False
    try:
        if bars is None:
            with open(cache_file, "r", encoding="utf-8") as fh:
                bars = json.load(fh)
        if not bars:
            return False
        last = bars[-1].get("date") if isinstance(bars[-1], dict) else None
        if last == trade_day:
            return True
        cached_on = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
        return cached_on > datetime.strptime(trade_day, "%Y-%m-%d").date()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


# ---------------------------------------------------------------------------
# BaoStock 主流程（推荐：稳定、免费、服务端协议，不受网页 WAF 影响）
# ---------------------------------------------------------------------------
def run_baostock(cfg: dict, limit: int, out_dir: str,
                 refresh: bool = False) -> list[dict]:
    """串行 + 缓存 + 每 300 只重连，资源占用低，可随时中断续跑。"""
    import baostock_source as bsrc

    print("[1/3] BaoStock 登录并拉取 A 股列表 ...", flush=True)
    if not bsrc.login():
        print("      BaoStock 登录失败，请检查网络", flush=True)
        return []
    stocks = bsrc.fetch_all_stocks()
    trade_day = bsrc._cached_trade_day   # 本轮最新交易日，用于判断缓存是否过期
    print(f"      最新交易日：{trade_day}", flush=True)
    # 用新浪列表补充总市值与当前价（一次拉取，按交易日判断缓存）
    print("      补充市值与股价数据 ...", flush=True)
    stocks = bsrc.enrich_market_cap(stocks, refresh=refresh, trade_day=trade_day)
    try:  # 缓存完整列表供离线模式使用
        with open(_cache_path("bs_stock_list.json"), "w", encoding="utf-8") as fh:
            json.dump(stocks, fh, ensure_ascii=False)
    except OSError:
        pass
    # 前置过滤：ST、市值、股价
    max_cap, max_px = cfg["max_mktcap"], cfg["max_price"]
    filtered = []
    for s in stocks:
        if s.get("is_st"):
            continue
        cap, px = s.get("mktcap"), s.get("price")
        if cap is not None and cap > max_cap:   # 市值超限直接排除
            continue
        if px is not None and px > max_px:      # 股价超限直接排除
            continue
        filtered.append(s)
    stocks = filtered
    if limit > 0:
        stocks = stocks[:limit]
    print(f"      过滤 ST/板块/市值>{max_cap/10000:.0f}亿/股价>{max_px}元后待扫描 {len(stocks)} 只",
          flush=True)

    results: list[dict] = []
    errors = 0
    cache_hits = 0
    for i, stock in enumerate(stocks, 1):
        code = stock["code"]
        cache_file = _cache_path(f"bs_{code}.json")
        bars = None
        if not refresh and os.path.exists(cache_file):
            try:
                # 按最新交易日判断，而非固定小时数：出现新交易日即自动重拉
                if _fresh_by_trade_day(cache_file, trade_day):
                    with open(cache_file, "r", encoding="utf-8") as fh:
                        bars = json.load(fh)
                    cache_hits += 1
            except Exception:  # noqa: BLE001
                bars = None
        if not bars:
            bars = bsrc.fetch_kline(code, stock["market"], cfg["lookback"] + 30,
                                    trade_day=bsrc._cached_trade_day)
            if bars:
                try:
                    with open(cache_file, "w", encoding="utf-8") as fh:
                        json.dump(bars, fh, ensure_ascii=False)
                except OSError:
                    pass
        if not bars:
            errors += 1
        else:
            try:
                item = analyze(stock, bars, cfg)
                if item:
                    results.append(item)
            except Exception:  # noqa: BLE001
                errors += 1

        if i % 300 == 0:
            print(f"      已处理 {i}/{len(stocks)}（缓存 {cache_hits}，异常 {errors}）...",
                  flush=True)
            bsrc.logout()      # 定期重连，避免长连接超时
            bsrc.login()
        if i % 1000 == 0:      # 每千只落盘一次，中断也能保留结果
            save_results(list(results), cfg, out_dir, scope=f"BaoStock 前 {i} 只")
    bsrc.logout()
    print(f"      缓存命中 {cache_hits} 只，数据异常 {errors} 只", flush=True)
    save_results(results, cfg, out_dir, scope="BaoStock 全市场")
    return results


# ---------------------------------------------------------------------------
# 离线筛选：合并 BaoStock 缓存与网页缓存，不发任何网络请求
# ---------------------------------------------------------------------------
def run_offline(cfg: dict, limit: int, out_dir: str) -> list[dict]:
    # 股票列表：优先 BaoStock 缓存（板块更干净），回退新浪缓存
    stocks: list[dict] = []
    bs_list_cache = _cache_path("bs_stock_list.json")
    if os.path.exists(bs_list_cache):
        try:
            with open(bs_list_cache, "r", encoding="utf-8") as fh:
                stocks = json.load(fh)
        except Exception:  # noqa: BLE001
            pass
    if not stocks:
        cache = _cache_path("stock_list.json")
        if not os.path.exists(cache):
            print("离线模式缺少股票列表缓存，请先联网运行一次", flush=True)
            return []
        with open(cache, "r", encoding="utf-8") as fh:
            stocks = json.load(fh)
    max_cap, max_px = cfg["max_mktcap"], cfg["max_price"]
    filtered = []
    for s in stocks:
        if s.get("is_st") or excluded(s["code"], s.get("name", "")):
            continue
        cap, px = s.get("mktcap"), s.get("price")
        if cap is not None and cap > max_cap:
            continue
        if px is not None and px > max_px:
            continue
        filtered.append(s)
    stocks = filtered
    if limit > 0:
        stocks = stocks[:limit]

    results: list[dict] = []
    scanned = 0
    for stock in stocks:
        code = stock["code"]
        bars = None
        bs_cache = _cache_path(f"bs_{code}.json")     # BaoStock dict 列表
        web_cache = _cache_path(f"{code}.json")      # 网页 klines 字符串列表
        if os.path.exists(bs_cache):
            try:
                with open(bs_cache, "r", encoding="utf-8") as fh:
                    bars = json.load(fh)
            except Exception:  # noqa: BLE001
                pass
        elif os.path.exists(web_cache):
            try:
                with open(web_cache, "r", encoding="utf-8") as fh:
                    bars = _parse_kline_lines(json.load(fh))
            except Exception:  # noqa: BLE001
                pass
        if not bars:
            continue
        scanned += 1
        try:
            item = analyze(stock, bars, cfg)
            if item:
                results.append(item)
        except Exception:  # noqa: BLE001
            pass
    print(f"离线模式：扫描 {scanned} 只缓存股票", flush=True)
    save_results(results, cfg, out_dir, scope=f"离线缓存 {scanned} 只")
    return results


# ---------------------------------------------------------------------------
# 网页接口主流程（备用：BaoStock 不可用时使用）
# ---------------------------------------------------------------------------
def run(cfg: dict, workers: int, limit: int, out_dir: str,
        offline: bool = False) -> list[dict]:
    print("[1/3] 拉取 A 股股票列表 ...", flush=True)
    if offline:
        cache = _cache_path("stock_list.json")
        if not os.path.exists(cache):
            print("      离线模式缺少股票列表缓存，请先联网运行一次", flush=True)
            return []
        with open(cache, "r", encoding="utf-8") as fh:
            stocks = json.load(fh)
        print("      离线模式：使用本地缓存股票列表", flush=True)
    else:
        stocks = fetch_stock_list()
    print(f"      共 {len(stocks)} 只，开始过滤板块与 ST ...", flush=True)
    stocks = [s for s in stocks if not excluded(s["code"], s["name"])]
    if limit > 0:
        stocks = stocks[:limit]
    print(f"      待扫描 {len(stocks)} 只", flush=True)

    results: list[dict] = []
    errors = 0
    done = 0
    with ThreadPoolExecutor(max_workers=1 if offline else workers) as pool:
        futures = {}
        for s in stocks:
            if offline:
                cache_file = _cache_path(f"{s['code']}.json")
                if not os.path.exists(cache_file):
                    continue
                try:
                    with open(cache_file, "r", encoding="utf-8") as fh:
                        futures[pool.submit(lambda lines=fh.read(): lines)] = s
                except OSError:
                    continue
            else:
                futures[pool.submit(fetch_kline, s["market"], s["code"],
                                    cfg["lookback"] + 30, cfg)] = s
        total = len(futures)
        print(f"      有数据 {total} 只，开始筛选 ...", flush=True)
        for future in as_completed(futures):
            stock = futures[future]
            done += 1
            if done % 500 == 0:
                print(f"      已处理 {done}/{total} ...", flush=True)
            try:
                raw = future.result()
                bars = _parse_kline_lines(json.loads(raw)) if offline else raw
                if not bars:
                    errors += 1
                    continue
                item = analyze(stock, bars, cfg)
                if item:
                    results.append(item)
            except Exception:  # noqa: BLE001 - 单只失败不影响整体
                errors += 1

    print(f"      网页源扫描完成，数据异常 {errors} 只", flush=True)
    save_results(results, cfg, out_dir, scope="网页接口")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="EXPMA(12,50) 回踩 EMA2 选股器")
    parser.add_argument("--ema2-tol", type=float, default=DEFAULT_CFG["ema2_tol"],
                        help="EMA2 回踩容差，只接受 0 ~ +容差的正偏离，默认 0.02（+2%%）")
    parser.add_argument("--ema1-tol", type=float, default=DEFAULT_CFG["ema1_tol"],
                        help="EMA1 回踩容差，默认 0.01（±1%%）")
    parser.add_argument("--max-pullback", type=int, default=DEFAULT_CFG["max_pullback"],
                        help="高点后最大回调交易日数，默认 10")
    parser.add_argument("--min-rise", type=float, default=DEFAULT_CFG["min_rise"],
                        help="阶段最小涨幅%%，默认 20")
    parser.add_argument("--min-limit-ups", type=int, default=DEFAULT_CFG["min_limit_ups"],
                        help="上涨段最少涨停次数，默认 1")
    parser.add_argument("--limit", type=int, default=0, help="只扫描前 N 只（调试用）")
    parser.add_argument("--workers", type=int, default=16, help="并发线程数，默认 16")
    parser.add_argument("--out", default=os.path.join(BASE_DIR, "output"),
                        help="结果输出目录")
    parser.add_argument("--no-cache", action="store_true", help="忽略 K 线缓存")
    parser.add_argument("--offline", action="store_true",
                        help="离线模式：只用本地已缓存数据筛选，不发起任何网络请求")
    parser.add_argument("--source", choices=["baostock", "web"], default="baostock",
                        help="数据源：baostock（默认，稳定）/ web（网页接口，易被风控）")
    parser.add_argument("--max-mktcap", type=float, default=DEFAULT_CFG["max_mktcap"],
                        help="最大总市值（万元），默认 5000000（500 亿）")
    parser.add_argument("--max-price", type=float, default=DEFAULT_CFG["max_price"],
                        help="最大当前股价（元），默认 100")
    args = parser.parse_args()

    cfg = dict(DEFAULT_CFG)
    cfg.update({
        "ema2_tol": args.ema2_tol,
        "ema1_tol": args.ema1_tol,
        "max_pullback": args.max_pullback,
        "min_rise": args.min_rise,
        "min_limit_ups": args.min_limit_ups,
        "max_mktcap": args.max_mktcap,
        "max_price": args.max_price,
        "cache_hours": 0 if args.no_cache else DEFAULT_CFG["cache_hours"],
    })
    try:
        if args.offline:
            run_offline(cfg, args.limit, args.out)
        elif args.source == "baostock":
            run_baostock(cfg, args.limit, args.out, refresh=args.no_cache)
        else:
            run(cfg, args.workers, args.limit, args.out, offline=False)
    except KeyboardInterrupt:
        print("\n已中断（已处理的数据均已缓存，可直接重跑续传）", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
