"""五源情报采集（00:00-09:15）：公告 / 新闻快讯 / 资金流 / 龙虎榜 / 政策研报。

每条情报统一为 IntelItem，带 sentiment(-1~1) 与 novelty(0~1)，供漏斗打分使用。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

from .market import _seed

SOURCES = ["公告", "新闻快讯", "资金流", "龙虎榜", "政策研报"]

_TEMPLATES = {
    "公告": [("{n}发布三季报，净利润同比增长{x}%", 1), ("{n}股东拟减持不超过{y}%股份", -1),
            ("{n}中标{z}亿元重大合同", 1), ("{n}收到监管问询函", -1)],
    "新闻快讯": [("{i}板块获多家机构调研，景气度回升", 1), ("{n}产品价格上调，渠道反馈积极", 1),
              ("{i}行业需求疲软，库存压力加大", -1)],
    "资金流": [("{n}主力净流入{z}亿元", 1), ("{n}北向资金连续{y}日净卖出", -1)],
    "龙虎榜": [("{n}登上龙虎榜，机构专用席位买入", 1), ("{n}游资席位集中卖出", -1)],
    "政策研报": [("券商研报上调{n}目标价", 1), ("{i}行业迎政策利好", 1), ("{i}行业监管趋严", -1)],
}


@dataclass
class IntelItem:
    source: str
    symbol: str
    title: str
    sentiment: float
    novelty: float


def collect_sim(trade_date: str, universe: list[tuple[str, str, str]]) -> list[IntelItem]:
    rng = random.Random(_seed("intel", trade_date))
    items: list[IntelItem] = []
    for sym, name, ind in universe:
        for _ in range(rng.choice([0, 0, 1, 1, 2])):
            src = rng.choice(SOURCES)
            tpl, sign = rng.choice(_TEMPLATES[src])
            title = tpl.format(n=name, i=ind, x=rng.randint(10, 80), y=rng.randint(1, 5), z=rng.randint(1, 30))
            items.append(IntelItem(src, sym, title, round(sign * rng.uniform(0.3, 1.0), 2), round(rng.uniform(0.2, 1.0), 2)))
    return items


def collect_akshare(trade_date: str, universe: list[tuple[str, str, str]]) -> list[IntelItem]:
    """真实情报的最小实现：财联社电报 + 个股资金流排行。任一源失败不影响其他源。"""
    import akshare as ak

    names = {n: s for s, n, _ in universe}
    items: list[IntelItem] = []
    try:
        df = ak.stock_info_global_cls(symbol="全部")
        for _, r in df.head(200).iterrows():
            text = f"{r.get('标题', '')}{r.get('内容', '')}"
            for n, s in names.items():
                if n in text:
                    items.append(IntelItem("新闻快讯", s, text[:80], 0.0, 0.8))  # 情感交给 LLM 判断
    except Exception:
        pass
    try:
        df = ak.stock_individual_fund_flow_rank(indicator="今日")
        for _, r in df.head(100).iterrows():
            s = str(r["代码"])
            if s in {x for x, *_ in universe}:
                amt = float(r["今日主力净流入-净额"]) / 1e8
                items.append(IntelItem("资金流", s, f"{r['名称']}主力净流入{amt:.2f}亿", max(min(amt / 3, 1), -1), 0.5))
    except Exception:
        pass
    return items
