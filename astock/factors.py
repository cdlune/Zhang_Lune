"""因子打分 —— 横截面百分位排名 + 技术面规则打分，合成综合得分。

打分口径：所有子因子统一归一到 0~100（越高越好），再按权重加权求和。
横截面排名保证不同量纲因子可比，且对极端值稳健（用秩而非原值）。
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from . import indicators


def rank_score(series: pd.Series, ascending: bool = True) -> pd.Series:
    """把一列因子值转成 0~100 的百分位得分。

    ascending=True 表示“值越小越好”（如 PE/PB/负债率），得分随秩递减。
    NaN 记为中性分 50，避免缺失值主导排名。
    """
    s = pd.to_numeric(series, errors="coerce")
    valid = s.notna()
    out = pd.Series(50.0, index=s.index)
    if valid.sum() >= 2:
        # ascending=True 表示“值越小越好”：用 rank(ascending=False) 让最小值拿到最高秩→最高分
        pct = s[valid].rank(ascending=not ascending, pct=True)
        out[valid] = pct * 100.0
    elif valid.sum() == 1:
        out[valid] = 100.0
    return out.round(2)


def value_score(df: pd.DataFrame) -> pd.Series:
    """估值因子：PE、PB 越低越好（剔除非正 PE 的影响，非正记中性）。"""
    pe = df.get("pe_ttm", pd.Series(np.nan, index=df.index)).copy()
    pe[pe <= 0] = np.nan  # 亏损股 PE 无意义，不给低估奖励
    pb = df.get("pb", pd.Series(np.nan, index=df.index)).copy()
    pb[pb <= 0] = np.nan
    return (0.55 * rank_score(pe, ascending=True)
            + 0.45 * rank_score(pb, ascending=True)).round(2)


def momentum_score(df: pd.DataFrame) -> pd.Series:
    """动量因子：中期涨幅越高越好。优先用日线算的 ret20/ret60，缺失回落到快照 60 日涨幅。"""
    r20 = df.get("ret20")
    r60 = df.get("ret60")
    if r20 is None:
        r20 = pd.Series(np.nan, index=df.index)
    if r60 is None:
        r60 = df.get("pct_60d", pd.Series(np.nan, index=df.index))
    return (0.5 * rank_score(r20, ascending=False)
            + 0.5 * rank_score(r60, ascending=False)).round(2)


def quality_score(df: pd.DataFrame) -> pd.Series:
    """质量因子：ROE、毛利率越高越好，资产负债率越低越好。"""
    roe = rank_score(df.get("roe", pd.Series(np.nan, index=df.index)), ascending=False)
    gm = rank_score(df.get("gross_margin", pd.Series(np.nan, index=df.index)), ascending=False)
    debt = rank_score(df.get("debt_ratio", pd.Series(np.nan, index=df.index)), ascending=True)
    return (0.45 * roe + 0.30 * gm + 0.25 * debt).round(2)


def growth_score(df: pd.DataFrame) -> pd.Series:
    """成长因子：营收、净利润同比增速越高越好（对极端负增长稳健，用秩）。"""
    rev = rank_score(df.get("rev_yoy", pd.Series(np.nan, index=df.index)), ascending=False)
    profit = rank_score(df.get("profit_yoy", pd.Series(np.nan, index=df.index)), ascending=False)
    return (0.45 * rev + 0.55 * profit).round(2)


def technical_score_from_kline(kdf: pd.DataFrame) -> Optional[float]:
    """由单只个股（已 enrich 的）日线算 0~100 技术面得分。数据不足返回 None。"""
    if kdf is None or len(kdf) < 20:
        return None
    last = kdf.iloc[-1]
    score = 50.0  # 中性起点

    close = last["close"]
    # 1) 均线多头排列 / 站上均线
    if close > last["ma20"]:
        score += 8
    if last["ma5"] > last["ma20"] > last["ma60"]:
        score += 10
    elif last["ma5"] < last["ma20"] < last["ma60"]:
        score -= 10

    # 2) MACD：DIF 在零轴上方且金叉/红柱走强
    if last["dif"] > last["dea"]:
        score += 6
    if last["dif"] > 0:
        score += 4
    if len(kdf) >= 2 and last["macd_hist"] > kdf.iloc[-2]["macd_hist"]:
        score += 4  # 红柱放大 / 绿柱缩短

    # 3) RSI：健康区间加分，超买减分，超卖轻微加分（潜在反弹）
    rsi = last["rsi14"]
    if 45 <= rsi <= 68:
        score += 8
    elif rsi > 80:
        score -= 10
    elif rsi < 25:
        score += 3

    # 4) 量能：近 5 日均量温和放大（1.0~2.5 倍）为健康
    if last["vol_ma20"] and last["vol_ma20"] > 0:
        vr = last["vol_ma5"] / last["vol_ma20"]
        if 1.0 <= vr <= 2.5:
            score += 6
        elif vr > 4.0:
            score -= 6  # 过度放量，警惕高位出货

    return float(np.clip(score, 0, 100))


def composite(df: pd.DataFrame, weights: Dict[str, float]) -> pd.DataFrame:
    """给候选 DataFrame 计算各因子分与加权综合分，返回带打分列的新 DataFrame。

    df 需含用于打分的原始列；缺失的因子列会被记为中性分 50 并仍参与加权。
    technical 分若已由 K 线预先算好（列 technical），则直接使用。
    """
    out = df.copy()
    out["f_value"] = value_score(out)
    out["f_momentum"] = momentum_score(out)
    out["f_quality"] = quality_score(out)
    out["f_growth"] = growth_score(out)
    if "technical" in out.columns:
        out["f_technical"] = pd.to_numeric(out["technical"], errors="coerce").fillna(50.0)
    else:
        out["f_technical"] = 50.0

    factor_col = {
        "value": "f_value", "momentum": "f_momentum", "quality": "f_quality",
        "growth": "f_growth", "technical": "f_technical",
    }
    total = 0.0
    score = pd.Series(0.0, index=out.index)
    for name, w in weights.items():
        col = factor_col.get(name)
        if col and col in out.columns:
            score += w * out[col]
            total += w
    out["score"] = (score / total).round(2) if total else score
    return out.sort_values("score", ascending=False).reset_index(drop=True)
