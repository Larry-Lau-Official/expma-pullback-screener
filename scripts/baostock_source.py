#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BaoStock（证券宝）数据源封装 —— 稳定、免费、免注册的服务端行情协议。

为什么用它：
  - 非网页爬虫，走 BaoStock 自有服务端协议，不受东财/腾讯 WAF 风控影响
  - 免费、免注册，数据覆盖全 A 股历史日线（含前复权、涨跌幅、ST 标记）
  - 单只查询约 0.3 秒，4000 只约 20 分钟可完成全市场

兼容性处理：
  - BaoStock 0.9.4 在 Python 3.13 + 新 pandas 下 rs.next() 会死循环，
    直接读 rs.data（二维列表）绕过。
  - 依赖装在 skill 根目录的 vendor/ 下（离线可用，不污染系统环境）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, "vendor")
if VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)

import baostock as bs  # noqa: E402

_session_active = False
_cached_trade_day: str | None = None   # 最近交易日只算一次，避免每只股票重复查询


def login() -> bool:
    global _session_active
    if _session_active:
        return True
    lg = bs.login()
    ok = lg.error_code == "0"
    if ok:
        _session_active = True
    return ok


def logout() -> None:
    global _session_active
    if _session_active:
        bs.logout()
        _session_active = False


def _bs_code(code: str, market: int | None = None) -> str:
    """把纯数字代码转成 BaoStock 格式 sh.600000 / sz.000001。"""
    if code.startswith(("sh.", "sz.")):
        return code
    prefix = "sh" if (market == 1 or code.startswith(("5", "6", "9"))) else "sz"
    return f"{prefix}.{code}"


# ---------------------------------------------------------------------------
# 股票列表
# ---------------------------------------------------------------------------
def _latest_trade_day(max_lookback: int = 7) -> str:
    """向上回溯找到最近一个有证券列表的交易日（规避盘前/周末）。结果缓存。"""
    global _cached_trade_day
    if _cached_trade_day:
        return _cached_trade_day
    for i in range(max_lookback):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        rs = bs.query_all_stock(day=day)
        # 有数据的日期第一页就有 2000 条；无数据时 data 为空
        if rs.error_code == "0" and rs.data and len(rs.data) >= 100:
            _cached_trade_day = day
            return day
    _cached_trade_day = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    return _cached_trade_day


def fetch_all_stocks(day: str | None = None) -> list[dict]:
    """
    获取指定交易日全部证券，仅保留沪深 A 股个股并附 ST 标记。
    day 缺省取最近有数据的交易日。列表通过 next() 翻页（该接口 next() 正常）。
    """
    if not login():
        return []
    day = day or _latest_trade_day()
    global _cached_trade_day
    _cached_trade_day = day       # 记录交易日，供 K 线查询复用
    rs = bs.query_all_stock(day=day)
    if rs.error_code != "0":
        return []
    # next() 只判断是否还有数据，get_row_data() 才移动游标；二者配合逐行读取
    rows: list[list[str]] = []
    guard = 0
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
        guard += 1
        if guard > 20000:  # 防止异常死循环
            break
    out = []
    for row in rows:  # [code, type, name]
        if len(row) < 3:
            continue
        full_code, sec_type, name = row[0], row[1], row[2]
        code = full_code.split(".")[-1]
        market = 1 if full_code.startswith("sh") else 0
        # 沪市只留 600/601/603/605 主板（排除 sh.000xxx 上证指数、688 科创板）
        # 深市只留 000/001/002/003 主板 + 300/301 创业板（排除 sz.399xxx 指数、北交所）
        valid = (code.startswith(("600", "601", "603", "605")) if market == 1
                 else code.startswith(("000", "001", "002", "003", "300", "301")))
        if not valid:
            continue
        out.append({"code": code, "market": market, "name": name,
                    "is_st": "ST" in name.upper() or "退" in name})
    return out


def enrich_market_cap(stocks: list[dict], refresh: bool = False,
                      trade_day: str | None = None) -> list[dict]:
    """
    用新浪列表接口为股票补充总市值(mktcap, 万元)和当前价(trade)。
    一次分页拉取全市场；缓存按最新交易日判断，出现新交易日即刷新。
    失败时保留原值(None)，不影响主流程。trade_day 为空时回退 12 小时计时。
    """
    cache_file = os.path.join(os.path.dirname(_cache_dir()), "sina_quotes.json")
    quotes: dict[str, dict] = {}
    if not refresh and os.path.exists(cache_file):
        try:
            if trade_day:
                cached_on = datetime.fromtimestamp(os.path.getmtime(cache_file)).date()
                fresh = cached_on >= datetime.strptime(trade_day, "%Y-%m-%d").date()
            else:
                age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_file))
                fresh = age < timedelta(hours=12)
            if fresh:
                with open(cache_file, "r", encoding="utf-8") as fh:
                    quotes = json.load(fh)
        except Exception:  # noqa: BLE001
            quotes = {}

    if not quotes:
        for page in range(1, 75):
            url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                   f"Market_Center.getHQNodeData?page={page}&num=80&sort=symbol&asc=1&node=hs_a")
            rows = None
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Mozilla/5.0",
                                      "Referer": "https://finance.sina.com.cn/"})
                    text = urllib.request.urlopen(req, timeout=20).read().decode("gbk", "ignore")
                    rows = json.loads(text)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.8 * (attempt + 1))
            if not rows:
                continue
            for row in rows:
                code = str(row.get("code", ""))
                try:
                    quotes[code] = {"mktcap": float(row.get("mktcap") or 0),
                                    "price": float(row.get("trade") or 0)}
                except (ValueError, TypeError):
                    continue
            if len(rows) < 80:
                break
        if quotes:
            try:
                with open(cache_file, "w", encoding="utf-8") as fh:
                    json.dump(quotes, fh)
            except OSError:
                pass

    for s in stocks:
        q = quotes.get(s["code"])
        if q:
            s["mktcap"] = q["mktcap"]   # 万元
            s["price"] = q["price"]     # 元
        else:
            s.setdefault("mktcap", None)
            s.setdefault("price", None)
    return stocks


def _cache_dir() -> str:
    """返回 screen 使用的缓存目录（延迟导入避免循环依赖）。"""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "output", ".cache")


def fetch_stock_name(full_code: str) -> str:
    """按代码查名称（批量查询时一般不需要）。"""
    if not login():
        return ""
    rs = bs.query_stock_basic(code=full_code)
    if rs.error_code == "0" and rs.data:
        return rs.data[0][1]
    return ""


# ---------------------------------------------------------------------------
# 日 K 线（前复权）
# ---------------------------------------------------------------------------
def fetch_kline(code: str, market: int, bars: int = 180,
                end_date: str | None = None, trade_day: str | None = None) -> list[dict] | None:
    """
    获取前复权日 K，返回与 screen._parse_kline_lines 兼容的 dict 列表。
    BaoStock 数据 T+1 更新（当日 17:30 后入库），end_date 缺省取昨天。
    """
    if not login():
        return None
    if not end_date:
        # 优先用外部传入的交易日（列表查询时已算出），否则回退到自身缓存
        end_date = trade_day or _latest_trade_day()
    # 180 根交易日约需 260 自然日，留少量余量即可；过小会导致不足 min_bars
    start = (datetime.now() - timedelta(days=int(bars * 1.45) + 20)).strftime("%Y-%m-%d")
    full = _bs_code(code, market)
    fields = "date,open,high,low,close,volume,amount,adjustflag,pctChg"
    rs = bs.query_history_k_data_plus(full, fields, start_date=start,
                                      end_date=end_date, frequency="d", adjustflag="2")
    if rs.error_code != "0" or not rs.data:
        return None
    out = []
    for row in rs.data:
        # fields 顺序: date,open,high,low,close,volume,amount,adjustflag,pctChg
        try:
            out.append({
                "date": row[0],
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]), "amount": float(row[6]),
                "pct": float(row[8]), "turn": 0.0,
            })
        except (ValueError, IndexError):
            continue
    return out or None


if __name__ == "__main__":
    login()
    stocks = fetch_all_stocks()
    print(f"A股股票: {len(stocks)} 只, ST: {sum(1 for s in stocks if s['is_st'])} 只")
    print("样例:", stocks[:3])
    sample = stocks[0] if stocks else None
    bars = fetch_kline(sample["code"], sample["market"], 180) if sample else None
    print("K线样例:", bars[-2:] if bars else None)
    logout()
