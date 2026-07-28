"""A 股数据抓取层 —— 基于东方财富公开行情接口，仅依赖 requests。

提供三类数据：
    spot_board()      全市场 A 股实时快照（一次请求拿到代码/价格/PE/PB/市值/换手/涨跌幅）
    daily_kline()     单只个股日线历史（供技术指标计算）
    main_indicators() 单只个股财务主要指标（ROE/毛利率/营收净利同比/资产负债率）

注意：部分运行环境的网络策略会拦截东方财富接口（返回 403）。
本文件不做“降级造假”，抓取失败时抛出/返回 None，由上层决定是否跳过对应因子。
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

import pandas as pd
import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

_SPOT_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_MAINTARGET_URL = (
    "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/MainTargetAjax"
)

# clist 字段含义（fltt=2 时返回可读数值，缺失为 "-"）
_SPOT_FIELDS = "f12,f14,f2,f3,f5,f6,f8,f9,f10,f20,f21,f23,f24,f25"
_SPOT_COLMAP = {
    "f12": "code", "f14": "name", "f2": "price", "f3": "pct",
    "f5": "volume", "f6": "amount", "f8": "turnover", "f9": "pe_ttm",
    "f10": "vol_ratio", "f20": "mktcap", "f21": "float_mktcap",
    "f23": "pb", "f24": "pct_60d", "f25": "pct_ytd",
}
_SPOT_NUMERIC = [
    "price", "pct", "volume", "amount", "turnover", "pe_ttm",
    "vol_ratio", "mktcap", "float_mktcap", "pb", "pct_60d", "pct_ytd",
]


class DataSourceError(RuntimeError):
    """数据抓取失败（网络被拦截、接口变更、限流等）。"""


def _get_json(url: str, params: dict, timeout: int = 15, retries: int = 3) -> dict:
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 —— 统一转成 DataSourceError
            last_err = exc
            time.sleep(0.6 * (attempt + 1))
    raise DataSourceError(f"请求失败 {url}: {last_err}")


def to_secid(code: str) -> str:
    """把 6 位股票代码转成东财 secid（1.=沪市，0.=深/北）。"""
    code = str(code).zfill(6)
    return f"1.{code}" if code[0] == "6" else f"0.{code}"


def spot_board() -> pd.DataFrame:
    """抓取全市场 A 股实时快照，返回 DataFrame（一次请求）。"""
    params = {
        "pn": 1, "pz": 6000, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f3",
        # 沪深主板 + 创业板 + 科创板 + 北交所
        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048,m:0 t:81 s:262144",
        "fields": _SPOT_FIELDS,
    }
    data = _get_json(_SPOT_URL, params)
    diff = (data.get("data") or {}).get("diff")
    if not diff:
        raise DataSourceError("实时快照为空，接口可能被拦截或字段变更")
    rows = list(diff.values()) if isinstance(diff, dict) else list(diff)
    df = pd.DataFrame(rows).rename(columns=_SPOT_COLMAP)
    df = df[[c for c in _SPOT_COLMAP.values() if c in df.columns]]
    df["code"] = df["code"].astype(str).str.zfill(6)
    for col in _SPOT_NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # 市值单位由元转亿元，便于阅读与阈值设置
    for col in ("mktcap", "float_mktcap"):
        if col in df.columns:
            df[col] = df[col] / 1e8
    return df.reset_index(drop=True)


def daily_kline(code: str, days: int = 120, adjust: int = 1) -> Optional[pd.DataFrame]:
    """抓取单只个股日线历史。adjust: 0 不复权 / 1 前复权 / 2 后复权。

    返回列：date, open, close, high, low, volume, amount, amplitude, pct, change, turnover
    失败或无数据返回 None（供上层跳过技术因子）。
    """
    params = {
        "secid": to_secid(code),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101, "fqt": adjust, "end": "20500101", "lmt": int(days),
    }
    try:
        data = _get_json(_KLINE_URL, params)
    except DataSourceError:
        return None
    klines = (data.get("data") or {}).get("klines")
    if not klines:
        return None
    cols = ["date", "open", "close", "high", "low",
            "volume", "amount", "amplitude", "pct", "change", "turnover"]
    df = pd.DataFrame([k.split(",") for k in klines], columns=cols)
    for col in cols[1:]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    return df.reset_index(drop=True)


# 财务主要指标字段映射（东财 MainTargetAjax，单位多为 % 或元）
_FIN_KEYS = {
    "ROEJQ": "roe",                 # 净资产收益率(加权) %
    "XSMLL": "gross_margin",        # 销售毛利率 %
    "XSJLL": "net_margin",          # 销售净利率 %
    "TOTALOPERATEREVETZ": "rev_yoy",      # 营业总收入同比增长 %
    "PARENTNETPROFITTZ": "profit_yoy",    # 归母净利润同比增长 %
    "ZCFZL": "debt_ratio",          # 资产负债率 %
    "EPSJB": "eps",                 # 基本每股收益 元
}


def _fin_code(code: str) -> str:
    code = str(code).zfill(6)
    return ("SH" if code[0] == "6" else "SZ") + code


def main_indicators(code: str) -> Optional[Dict[str, float]]:
    """抓取单只个股最新一期财务主要指标，返回 dict；失败返回 None。"""
    params = {"type": 0, "code": _fin_code(code)}
    try:
        data = _get_json(_MAINTARGET_URL, params)
    except DataSourceError:
        return None
    records = data.get("data") if isinstance(data, dict) else data
    if not records:
        return None
    latest = records[0]  # 接口按报告期降序，取最新一期
    out: Dict[str, float] = {}
    for src, dst in _FIN_KEYS.items():
        val = pd.to_numeric(latest.get(src), errors="coerce")
        if pd.notna(val):
            out[dst] = float(val)
    return out or None
