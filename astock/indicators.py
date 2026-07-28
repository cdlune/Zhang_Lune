"""技术指标计算 —— 纯函数，输入日线 DataFrame（含 close/high/low/volume），可离线测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=1).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist)。hist 为 MACD 柱（2*(dif-dea)）。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(100)  # 全程无下跌时 RSI=100


def kdj(df: pd.DataFrame, n: int = 9, k_smooth: int = 3, d_smooth: int = 3):
    """返回 (k, d, j)。基于经典 9,3,3 参数。"""
    low_n = df["low"].rolling(n, min_periods=1).min()
    high_n = df["high"].rolling(n, min_periods=1).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(alpha=1 / k_smooth, adjust=False).mean()
    d = k.ewm(alpha=1 / d_smooth, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def boll(close: pd.Series, n: int = 20, k: float = 2.0):
    """返回 (mid, upper, lower)。"""
    mid = sma(close, n)
    std = close.rolling(n, min_periods=1).std(ddof=0)
    return mid, mid + k * std, mid - k * std


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """在日线 DataFrame 上追加常用技术指标列，返回新 DataFrame。"""
    out = df.copy()
    close = out["close"]
    out["ma5"] = sma(close, 5)
    out["ma10"] = sma(close, 10)
    out["ma20"] = sma(close, 20)
    out["ma60"] = sma(close, 60)
    out["vol_ma5"] = sma(out["volume"], 5)
    out["vol_ma20"] = sma(out["volume"], 20)
    out["dif"], out["dea"], out["macd_hist"] = macd(close)
    out["rsi14"] = rsi(close, 14)
    out["k"], out["d"], out["j"] = kdj(out)
    out["boll_mid"], out["boll_up"], out["boll_low"] = boll(close)
    # 动量：5/10 日（短线）、20/60 日（中期）收益率（%）
    out["ret5"] = close.pct_change(5) * 100
    out["ret10"] = close.pct_change(10) * 100
    out["ret20"] = close.pct_change(20) * 100
    out["ret60"] = close.pct_change(60) * 100
    return out
