"""离线单元测试：覆盖指标、因子打分、权重归一化、股票池过滤与选股流水线（用合成数据）。

运行：  python -m pytest tests/ -q   或   python tests/test_logic.py
不依赖网络。
"""

from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astock import biyingapi, factors, indicators, screener  # noqa: E402
from astock.config import Config  # noqa: E402


def _synth_kline(n=120, trend=0.1, seed=0):
    rng = np.random.default_rng(seed)
    steps = rng.normal(trend, 1.0, n)
    close = 20 + np.cumsum(steps)
    close = np.maximum(close, 1.0)
    high = close + np.abs(rng.normal(0, 0.5, n))
    low = close - np.abs(rng.normal(0, 0.5, n))
    open_ = close - steps / 2
    vol = np.abs(rng.normal(1e6, 2e5, n))
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01", periods=n),
        "open": open_, "close": close, "high": high, "low": low,
        "volume": vol, "amount": vol * close, "amplitude": 0.0,
        "pct": 0.0, "change": 0.0, "turnover": 1.0,
    })


def test_indicators_shapes_and_values():
    df = indicators.enrich(_synth_kline())
    for col in ("ma5", "ma20", "ma60", "dif", "dea", "macd_hist",
                "rsi14", "k", "d", "j", "boll_up", "ret20", "ret60"):
        assert col in df.columns
    # RSI 恒在 0~100
    assert df["rsi14"].between(0, 100).all()
    # 上升趋势中最后一根收盘应高于 MA60
    assert df.iloc[-1]["close"] > df.iloc[-1]["ma60"]


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))       # 单调上涨
    down = pd.Series(np.arange(60, 1, -1, dtype=float))  # 单调下跌
    assert indicators.rsi(up).iloc[-1] > 95
    assert indicators.rsi(down).iloc[-1] < 5


def test_rank_score_direction():
    s = pd.Series([10, 20, 30, 40])
    asc = factors.rank_score(s, ascending=True)    # 值小得分高
    desc = factors.rank_score(s, ascending=False)  # 值大得分高
    assert asc.iloc[0] > asc.iloc[-1]
    assert desc.iloc[0] < desc.iloc[-1]
    # NaN 记中性 50
    s2 = pd.Series([1.0, np.nan, 3.0])
    assert factors.rank_score(s2).iloc[1] == 50.0


def test_technical_score_bounds():
    up = factors.technical_score_from_kline(indicators.enrich(_synth_kline(trend=0.4, seed=1)))
    down = factors.technical_score_from_kline(indicators.enrich(_synth_kline(trend=-0.4, seed=2)))
    assert 0 <= down <= up <= 100
    assert factors.technical_score_from_kline(_synth_kline(n=10)) is None  # 数据不足


def test_weight_redistribution():
    cfg = Config()
    with_f = cfg.redistributed_weights(True)
    without_f = cfg.redistributed_weights(False)
    assert abs(sum(with_f.values()) - 1.0) < 1e-9
    assert abs(sum(without_f.values()) - 1.0) < 1e-9
    assert "quality" not in without_f and "growth" not in without_f
    assert set(without_f) == {"value", "momentum", "technical"}


def _synth_board(n=200, seed=7):
    rng = np.random.default_rng(seed)
    codes = [f"{600000 + i}" for i in range(n // 2)] + [f"{i:06d}" for i in range(n // 2)]
    return pd.DataFrame({
        "code": codes,
        "name": [f"股票{i}" for i in range(n)],
        "price": rng.uniform(3, 100, n),
        "pct": rng.normal(0, 2, n),
        "turnover": rng.uniform(0.2, 15, n),
        "pe_ttm": rng.uniform(-20, 150, n),
        "pb": rng.uniform(0.5, 15, n),
        "mktcap": rng.uniform(10, 5000, n),
        "pct_60d": rng.normal(5, 20, n),
        "ret20": rng.normal(3, 10, n),
        "ret60": rng.normal(5, 20, n),
        "roe": rng.uniform(-5, 30, n),
        "gross_margin": rng.uniform(5, 60, n),
        "debt_ratio": rng.uniform(10, 80, n),
        "rev_yoy": rng.normal(10, 25, n),
        "profit_yoy": rng.normal(8, 40, n),
    })


def test_universe_filter_excludes_bad():
    board = _synth_board()
    board.loc[0, "name"] = "ST垃圾"
    board.loc[1, "pe_ttm"] = -5  # 亏损
    cfg = Config()
    pool = screener.apply_universe_filter(board, cfg)
    assert (~pool["name"].str.contains("ST")).all()
    assert (pool["pe_ttm"] > 0).all()
    assert pool["price"].between(cfg.min_price, cfg.max_price).all()
    assert not pool.empty


def test_composite_ranks_and_offline_select():
    board = _synth_board()
    cfg = Config(top_n_candidates=40, final_n=10)
    # 直接用合成 board 走打分（技术分用中性），验证排序与列完整
    pool = screener.apply_universe_filter(board, cfg)
    weights = cfg.redistributed_weights(True)
    ranked = factors.composite(pool, weights)
    assert list(ranked["score"]) == sorted(ranked["score"], reverse=True)
    assert ranked["score"].between(0, 100).all()
    for col in ("f_value", "f_momentum", "f_quality", "f_growth", "f_technical"):
        assert col in ranked.columns


def test_biying_parse_list():
    df = biyingapi.parse_list([
        {"dm": "000001", "mc": "平安银行", "jys": "sz"},
        {"dm": "600519", "mc": "贵州茅台", "jys": "sh"},
    ])
    assert list(df.columns) == ["code", "name", "exchange"]
    assert df.loc[0, "code"] == "000001"
    assert df.loc[1, "name"] == "贵州茅台"


def test_biying_parse_realtime():
    obj = {"t": "2026-07-21 15:00:00", "o": "10.0", "h": "10.8", "l": "9.9",
           "p": "10.5", "yc": "10.0", "v": "123456", "cje": "1.3e8",
           "hs": "2.5", "pe": "12.3", "sz": "2.0e11", "lt": "1.5e11",
           "zf": "9.0", "zs": "0.3"}
    row = biyingapi.parse_realtime("000001", obj)
    assert row["code"] == "000001"
    assert row["price"] == 10.5 and row["pe_ttm"] == 12.3
    assert abs(row["pct"] - 5.0) < 1e-6          # (10.5-10)/10*100
    assert abs(row["mktcap"] - 2000.0) < 1e-6    # 2.0e11 元 -> 2000 亿
    assert row["pb"] != row["pb"] or pd.isna(row["pb"])  # PB 缺失记 NaN


def test_biying_parse_kline():
    recs = [{"d": f"2026-01-{i:02d}", "o": 10 + i, "h": 11 + i, "l": 9 + i,
             "c": 10.5 + i, "v": 1000 * i, "cje": 1e6 * i} for i in range(1, 26)]
    kdf = biyingapi.parse_kline(recs)
    assert kdf is not None and len(kdf) == 25
    for col in ("date", "open", "high", "low", "close", "volume", "amount"):
        assert col in kdf.columns
    # parse_kline 产物应能直接喂给指标计算
    enriched = indicators.enrich(kdf)
    assert "macd_hist" in enriched.columns
    # 空/缺字段返回 None
    assert biyingapi.parse_kline([]) is None
    assert biyingapi.parse_kline([{"o": 1, "c": 2}]) is None


def test_biying_source_uses_pipeline_offline():
    """用假数据源对象验证 biying 走通 screener（不联网）。"""
    class FakeSource:
        def spot_board(self):
            return _synth_board(120, seed=11)
        def daily_kline(self, code, days=120):
            return indicators.enrich(_synth_kline(80, trend=0.2, seed=hash(code) % 100))
        def main_indicators(self, code):
            return None  # 模拟必盈默认不含财务
    cfg = Config(top_n_candidates=15, final_n=6, request_pause=0.0)
    ranked = screener.select(cfg, source=FakeSource())
    assert len(ranked) == 6
    assert list(ranked["score"]) == sorted(ranked["score"], reverse=True)
    # 无财务 -> 质量/成长权重被摊掉，技术分来自真实K线（非恒 50）
    assert ranked["f_technical"].nunique() > 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS  {fn.__name__}")
    print(f"\n全部 {len(fns)} 项测试通过。")
