"""必盈API (biyingapi.com) 数据源，与 datasource.py 暴露同样的接口，可作为 --source biying 使用。

已确认的接口（见 https://www.biyingapi.com/doc_hs ）：
    股票列表   GET /hslt/list/{licence}                 -> [{dm,mc,jys}, ...]
    实时交易   GET /hsrl/ssjy/{code}/{licence}          -> {t,o,h,l,p,yc,v,cje,hs,pe,sz,lt,zf,zs}
    历史K线    GET /hszbl/fsjy/{code}/{level}/{fq}/{licence}

注意：
1. licence 是私密凭证，**不写入代码库**，通过 --licence 或环境变量 BIYING_LICENCE 传入。
2. 必盈实时接口是「按单只代码」查询，没有一次性全市场快照；因此全市场 select 需要逐只请求，
   调用量大且受 licence 套餐限流/配额约束。analyze 指定个股则很轻量。
3. K线接口的路径前缀(hszbl/fsjy)与参数(level=d, fq=f)已按公开文档填写；若你的套餐文档
   路径不同，只需改本文件顶部的 _KLINE_PATH / _KLINE_LEVEL / _KLINE_FQ 三个常量即可。
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import requests

from .datasource import DataSourceError

_BASE = "https://api.biyingapi.com"
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# —— K线接口的可调常量（如与你的文档不符，改这里即可）——
_KLINE_PATH = "hszbl/fsjy"   # 历史分时交易(含日/周/月线)
_KLINE_LEVEL = "d"           # 时间级别：5/15/30/60/d/w/m/y
_KLINE_FQ = "f"              # 除权：n不复权 / f前复权 / b后复权


def get_licence(explicit: Optional[str] = None) -> str:
    lic = explicit or os.environ.get("BIYING_LICENCE")
    if not lic:
        raise DataSourceError(
            "缺少必盈 licence：请用 --licence 传入，或设置环境变量 BIYING_LICENCE")
    return lic


def _get(path: str, timeout: int = 20, retries: int = 3):
    url = f"{_BASE}/{path.lstrip('/')}"
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            # 必盈异常时可能返回字符串错误信息而非数组/对象
            if isinstance(data, str):
                raise DataSourceError(f"接口返回错误信息：{data}")
            return data
        except DataSourceError:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6 * (attempt + 1))
    raise DataSourceError(f"请求失败 {url}: {last}")


# ---------------- 纯解析函数（离线可测，不发请求）----------------

def parse_list(records: List[dict]) -> pd.DataFrame:
    """解析 hslt/list 返回，标准化列名 code/name/exchange。"""
    df = pd.DataFrame(records or [])
    if df.empty:
        raise DataSourceError("股票列表为空")
    df = df.rename(columns={"dm": "code", "mc": "name", "jys": "exchange"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name", "exchange"]].reset_index(drop=True)


# 实时字段 -> 标准列名（与 eastmoney 数据源保持一致，便于共用打分流水线）
_RT_MAP = {
    "o": "open", "h": "high", "l": "low", "p": "price", "yc": "prev_close",
    "v": "volume", "cje": "amount", "hs": "turnover", "pe": "pe_ttm",
    "sz": "mktcap", "lt": "float_mktcap", "zf": "amplitude", "zs": "zs", "t": "time",
}


def parse_realtime(code: str, obj: dict) -> dict:
    """解析 hsrl/ssjy 单只返回为标准 dict；补算涨跌幅 pct，市值转亿元。"""
    row: Dict[str, object] = {"code": str(code).zfill(6)}
    for src, dst in _RT_MAP.items():
        if src in obj:
            row[dst] = pd.to_numeric(obj[src], errors="coerce") if src != "t" else obj[src]
    # 涨跌幅：必盈实时不直接给，用现价/昨收计算
    p, yc = row.get("price"), row.get("prev_close")
    if pd.notna(p) and pd.notna(yc) and yc:
        row["pct"] = round((float(p) - float(yc)) / float(yc) * 100, 2)
    # 市值元 -> 亿元
    for col in ("mktcap", "float_mktcap"):
        if col in row and pd.notna(row[col]):
            row[col] = float(row[col]) / 1e8
    row["pb"] = np.nan  # 必盈实时接口不含 PB
    return row


def parse_kline(records: List[dict]) -> Optional[pd.DataFrame]:
    """解析历史K线返回为标准日线 DataFrame（date/open/close/high/low/volume/amount...）。

    字段名容错：日期取 d/t，成交额取 cje/e/a。数据不足返回 None。
    """
    if not records:
        return None
    df = pd.DataFrame(records)
    date_col = next((c for c in ("d", "t", "date") if c in df.columns), None)
    amt_col = next((c for c in ("cje", "e", "a", "amount") if c in df.columns), None)
    ren = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    if date_col:
        ren[date_col] = "date"
    if amt_col:
        ren[amt_col] = "amount"
    df = df.rename(columns=ren)
    need = {"open", "high", "low", "close", "volume", "date"}
    if not need.issubset(df.columns):
        return None
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "amount" not in df.columns:
        df["amount"] = np.nan
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date")
    return df.reset_index(drop=True) if len(df) else None


# ---------------- 联网接口 ----------------

def stock_list(licence: Optional[str] = None) -> pd.DataFrame:
    return parse_list(_get(f"hslt/list/{get_licence(licence)}"))


def realtime(code: str, licence: Optional[str] = None) -> Optional[dict]:
    lic = get_licence(licence)
    code = str(code).zfill(6)
    try:
        obj = _get(f"hsrl/ssjy/{code}/{lic}")
    except DataSourceError:
        return None
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    return parse_realtime(code, obj) if obj else None


def daily_kline(code: str, days: int = 120, licence: Optional[str] = None):
    """抓取单只个股日线（前复权），返回标准 DataFrame；失败/无数据返回 None。"""
    lic = get_licence(licence)
    code = str(code).zfill(6)
    path = f"{_KLINE_PATH}/{code}/{_KLINE_LEVEL}/{_KLINE_FQ}/{lic}"
    try:
        records = _get(path)
    except DataSourceError:
        return None
    df = parse_kline(records if isinstance(records, list) else records.get("data"))
    if df is None:
        return None
    return df.tail(int(days)).reset_index(drop=True)


def spot_board(licence: Optional[str] = None, codes: Optional[List[str]] = None,
               max_symbols: Optional[int] = None, pause: float = 0.12,
               log=lambda _m: None) -> pd.DataFrame:
    """构造「全市场快照」——必盈无一次性全市场接口，需逐只请求实时数据。

    codes 为空时取全市场列表；max_symbols 限制请求数量（避免海量调用/超配额）。
    """
    lic = get_licence(licence)
    if codes is None:
        lst = stock_list(lic)
        codes = list(lst["code"])
        name_map = dict(zip(lst["code"], lst["name"]))
    else:
        codes = [str(c).zfill(6) for c in codes]
        name_map = {}
    if max_symbols:
        codes = codes[:int(max_symbols)]
    log(f"  必盈逐只抓取实时数据 {len(codes)} 只（每只一次请求）…")

    rows = []
    for i, code in enumerate(codes):
        rt = realtime(code, lic)
        if rt:
            rt.setdefault("name", name_map.get(code, code))
            rows.append(rt)
        if (i + 1) % 50 == 0:
            log(f"    进度 {i + 1}/{len(codes)}")
        time.sleep(pause)
    if not rows:
        raise DataSourceError("未取到任何实时数据（licence 配额/限流或接口变更）")
    return pd.DataFrame(rows).reset_index(drop=True)


def main_indicators(code: str, licence: Optional[str] = None) -> Optional[Dict[str, float]]:
    """必盈财务指标接口因套餐而异，此处默认不启用，返回 None（质量/成长因子将自动降权）。

    如你的套餐含财务主要指标接口，可在此按其字段实现，返回
    {roe, gross_margin, net_margin, rev_yoy, profit_yoy, debt_ratio, eps} 的子集。
    """
    return None


class BiyingSource:
    """把必盈接口包装成与 eastmoney datasource 模块一致的数据源对象。

    绑定 licence 与全市场抓取上限，供 screener 以统一接口调用：
        spot_board() / daily_kline(code, days) / main_indicators(code)
    """

    def __init__(self, licence: Optional[str] = None,
                 max_symbols: Optional[int] = None,
                 codes: Optional[List[str]] = None,
                 log=lambda _m: None):
        self.licence = get_licence(licence)
        self.max_symbols = max_symbols
        self.codes = codes
        self.log = log

    def spot_board(self) -> pd.DataFrame:
        return spot_board(self.licence, codes=self.codes,
                          max_symbols=self.max_symbols, log=self.log)

    def daily_kline(self, code: str, days: int = 120):
        return daily_kline(code, days=days, licence=self.licence)

    def main_indicators(self, code: str):
        return main_indicators(code, licence=self.licence)
