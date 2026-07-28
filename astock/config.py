"""选股配置：股票池过滤阈值、因子权重、深度打分参数。

所有阈值可在实例化 Config 时覆盖，也可在命令行中通过参数调整。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict


@dataclass
class Config:
    # -------- 股票池初筛（stage 1）--------
    min_price: float = 2.0            # 最低股价，剔除低价问题股
    max_price: float = 500.0          # 最高股价
    min_mktcap_yi: float = 30.0       # 最小总市值（亿元），剔除超小盘
    max_mktcap_yi: float = 8000.0     # 最大总市值（亿元）
    min_turnover: float = 0.5         # 最小换手率（%），保证流动性
    max_turnover: float = 30.0        # 最大换手率（%），剔除过度炒作
    min_pe: float = 0.0               # PE 下限（>0 表示剔除亏损股）
    max_pe: float = 120.0             # PE 上限，剔除极端高估
    max_pb: float = 20.0              # PB 上限
    exclude_st: bool = True           # 剔除 ST/*ST/退市
    exclude_star: bool = False        # 是否剔除科创板(688)/创业板(300)
    exclude_bj: bool = True           # 剔除北交所(4/8 开头)

    # -------- 因子权重（stage 2，会按是否有基本面自动归一化）--------
    weights: Dict[str, float] = field(default_factory=lambda: {
        "value": 0.22,       # 估值：PE、PB 越低越好
        "momentum": 0.20,    # 动量：中期涨幅
        "quality": 0.23,     # 质量：ROE、毛利率、负债率（需基本面）
        "growth": 0.20,      # 成长：营收/净利同比增速（需基本面）
        "technical": 0.15,   # 技术：均线/MACD/RSI/量能
    })

    # -------- 深度打分（stage 2）--------
    top_n_candidates: int = 60   # 初筛后进入深度打分的候选数量
    final_n: int = 15            # 最终输出的选股数量
    kline_days: int = 120        # 拉取的日线根数（技术指标用）
    use_fundamentals: bool = True  # 是否抓取财务主要指标（较慢）
    request_pause: float = 0.15  # 每次请求间隔（秒），避免被限流

    # -------- 风格 --------
    style: str = "balanced"      # "balanced"（均衡多因子）或 "short_term"（短线）

    @classmethod
    def short_term(cls) -> "Config":
        """短线交易预设：看重强势/放量/活跃度，弱化估值与基本面。

        与均衡模式的主要差异：
        - 提高最低换手率门槛（要有人气/成交活跃才谈得上短线）
        - 大幅放宽 PE/PB 上限（短线不为估值买单，只为趋势买单）
        - 技术、动量权重大幅上调，估值权重降到很低
        - 动量因子改用更短周期（5/10 日）而非 20/60 日
        """
        return cls(
            min_turnover=3.0, max_turnover=25.0,
            min_mktcap_yi=20.0, max_mktcap_yi=3000.0,
            max_pe=300.0, max_pb=30.0,
            weights={
                "technical": 0.40, "momentum": 0.35,
                "value": 0.05, "quality": 0.10, "growth": 0.10,
            },
            kline_days=90,
            style="short_term",
        )

    def redistributed_weights(self, has_fundamentals: bool) -> Dict[str, float]:
        """当无基本面数据时，把 quality/growth 权重按比例摊到其余因子上。"""
        w = dict(self.weights)
        if not has_fundamentals:
            spare = w.pop("quality", 0.0) + w.pop("growth", 0.0)
            keep = {k: w.get(k, 0.0) for k in ("value", "momentum", "technical")}
            s = sum(keep.values()) or 1.0
            for k in keep:
                keep[k] += spare * keep[k] / s
            return keep
        total = sum(w.values()) or 1.0
        return {k: v / total for k, v in w.items()}

    def copy_with(self, **kwargs) -> "Config":
        return replace(self, **kwargs)


DEFAULT_CONFIG = Config()
