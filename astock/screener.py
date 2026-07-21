"""两阶段选股流水线。

Stage 1 —— 初筛：一次请求拿到全市场快照，按价格/市值/换手/PE/PB/ST 等硬条件过滤，
            并用估值+动量做初步打分，取前 top_n_candidates 只进入深度打分。
Stage 2 —— 深度：对候选逐只抓日线（算技术分）与财务主要指标（算质量/成长分），
            合成综合分并排序，输出 final_n 只。
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import pandas as pd

from . import datasource as ds
from . import factors, indicators
from .config import Config, DEFAULT_CONFIG


def _is_st(name: str) -> bool:
    name = str(name)
    return ("ST" in name.upper()) or ("退" in name)


def apply_universe_filter(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Stage 1 硬条件过滤，返回符合条件的股票池。"""
    m = pd.Series(True, index=df.index)
    code = df["code"].astype(str)

    if cfg.exclude_st:
        m &= ~df["name"].map(_is_st)
    if cfg.exclude_star:
        m &= ~code.str.startswith(("688", "300"))
    if cfg.exclude_bj:
        m &= ~code.str.startswith(("4", "8", "92"))

    if "price" in df:
        m &= df["price"].between(cfg.min_price, cfg.max_price)
    if "mktcap" in df:
        m &= df["mktcap"].between(cfg.min_mktcap_yi, cfg.max_mktcap_yi)
    if "turnover" in df:
        m &= df["turnover"].between(cfg.min_turnover, cfg.max_turnover)
    if "pe_ttm" in df:
        pe = df["pe_ttm"]
        m &= (pe > cfg.min_pe) & (pe <= cfg.max_pe)
    if "pb" in df:
        m &= (df["pb"] > 0) & (df["pb"] <= cfg.max_pb)

    return df[m].reset_index(drop=True)


def _prescreen_rank(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """用估值+动量给股票池初步打分（仅用快照字段，不发额外请求）。"""
    pre_weights = {"value": 0.6, "momentum": 0.4}
    scored = factors.composite(df, pre_weights)
    return scored.head(cfg.top_n_candidates).reset_index(drop=True)


def _deep_enrich(candidates: pd.DataFrame, cfg: Config,
                 log: Callable[[str], None], source) -> pd.DataFrame:
    """Stage 2：逐只抓日线与财务指标，补齐技术/质量/成长因子所需列。"""
    tech_scores = []
    fin_rows = []
    got_fin = 0
    for i, row in candidates.iterrows():
        code = row["code"]
        # 技术面
        kdf = source.daily_kline(code, days=cfg.kline_days)
        if kdf is not None and len(kdf) >= 20:
            kdf = indicators.enrich(kdf)
            tech_scores.append(factors.technical_score_from_kline(kdf))
            last = kdf.iloc[-1]
            candidates.loc[i, "ret20"] = last["ret20"]
            candidates.loc[i, "ret60"] = last["ret60"]
        else:
            tech_scores.append(None)
        # 基本面
        fin = source.main_indicators(code) if cfg.use_fundamentals else None
        if fin:
            got_fin += 1
        fin_rows.append(fin or {})
        if (i + 1) % 10 == 0:
            log(f"  深度打分进度 {i + 1}/{len(candidates)} …")
        time.sleep(cfg.request_pause)

    candidates["technical"] = [s if s is not None else 50.0 for s in tech_scores]
    fin_df = pd.DataFrame(fin_rows)
    for col in fin_df.columns:
        candidates[col] = fin_df[col].values
    log(f"  技术面成功 {sum(s is not None for s in tech_scores)}/{len(candidates)}，"
        f"基本面成功 {got_fin}/{len(candidates)}")
    candidates.attrs["has_fundamentals"] = got_fin > 0
    return candidates


def select(cfg: Config = DEFAULT_CONFIG,
           log: Optional[Callable[[str], None]] = None,
           board: Optional[pd.DataFrame] = None,
           source=ds) -> pd.DataFrame:
    """执行完整选股流程，返回最终排名 DataFrame（含各因子分与综合分）。

    source 为数据源对象（默认 eastmoney datasource 模块，也可传 BiyingSource）。
    board 可传入预先抓好的全市场快照（便于测试/复用）；为空则实时抓取。
    """
    log = log or (lambda _msg: None)

    log("Stage 1：抓取全市场快照 …")
    if board is None:
        board = source.spot_board()
    log(f"  全市场 {len(board)} 只")

    pool = apply_universe_filter(board, cfg)
    log(f"  硬条件过滤后剩 {len(pool)} 只")
    if pool.empty:
        return pool

    candidates = _prescreen_rank(pool, cfg)
    log(f"  初筛取前 {len(candidates)} 只进入深度打分")

    log("Stage 2：深度打分（抓日线 + 财务指标）…")
    candidates = _deep_enrich(candidates, cfg, log, source)

    weights = cfg.redistributed_weights(candidates.attrs.get("has_fundamentals", False))
    log(f"  采用权重：{ {k: round(v, 3) for k, v in weights.items()} }")

    ranked = factors.composite(candidates, weights)
    return ranked.head(cfg.final_n).reset_index(drop=True)
