"""行情层：统一的 MarketProvider 接口 + 模拟行情 + AkShare 真实行情。

决策层只依赖 Quote / Breadth 这两个结构，切换数据源不影响上层逻辑。
"""
from __future__ import annotations

import hashlib
import math
import random
import time as _time
from dataclasses import asdict, dataclass

from .clock import Clock, is_trading


@dataclass
class Quote:
    symbol: str
    name: str
    industry: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    chg_pct: float
    vwap: float
    turnover: float  # 成交额（元）
    volume: float  # 成交量（股）

    @property
    def vwap_dev(self) -> float:
        return (self.price / self.vwap - 1) * 100 if self.vwap else 0.0

    @property
    def limit_up(self) -> float:
        return round(self.pre_close * (1 + limit_ratio(self.symbol)), 2)

    @property
    def limit_down(self) -> float:
        return round(self.pre_close * (1 - limit_ratio(self.symbol)), 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["vwap_dev"] = round(self.vwap_dev, 2)
        return d


@dataclass
class Breadth:
    total: int
    up: int
    down: int
    main_up_ratio: float
    limit_up: int
    limit_down: int

    @property
    def up_ratio(self) -> float:
        return self.up / self.total if self.total else 0.0

    @property
    def sentiment(self) -> str:
        r = self.up_ratio
        if r < 0.25:
            return "panic_bearish"
        if r < 0.40:
            return "weak_bearish"
        if r < 0.60:
            return "neutral_divergent"
        if r < 0.75:
            return "mild_bullish"
        return "strong_bullish"

    @property
    def is_extreme_panic(self) -> bool:
        return self.up_ratio < 0.25 or self.limit_down > self.limit_up

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(up_ratio=round(self.up_ratio, 4), sentiment=self.sentiment, is_extreme_panic=self.is_extreme_panic)
        return d


def limit_ratio(symbol: str) -> float:
    if symbol.startswith(("300", "301", "688")):
        return 0.20
    if symbol.startswith(("8", "4")):
        return 0.30
    return 0.10


def board(symbol: str) -> str:
    if symbol.startswith(("300", "301")):
        return "创业板"
    if symbol.startswith("688"):
        return "科创板"
    return "主板"


def exchange(symbol: str) -> str:
    return "沪市" if symbol.startswith("6") else "深市"


class MarketProvider:
    def step(self, clock: Clock) -> None: ...
    def quotes(self) -> dict[str, Quote]: ...
    def quote(self, symbol: str) -> Quote | None:
        return self.quotes().get(symbol)
    def breadth(self) -> Breadth: ...
    def industry_rank(self) -> tuple[dict[str, int], int]: ...
    def universe(self) -> list[tuple[str, str, str]]: ...


# ---------------------------------------------------------------- 模拟行情
UNIVERSE = [
    ("600519", "贵州茅台", "白酒", 1450), ("000858", "五粮液", "白酒", 128), ("000568", "泸州老窖", "白酒", 132),
    ("000776", "广发证券", "证券", 18.6), ("600999", "招商证券", "证券", 19.2), ("601696", "中银证券", "证券", 12.4),
    ("600030", "中信证券", "证券", 27.5), ("601009", "南京银行", "银行", 10.9), ("600036", "招商银行", "银行", 42.1),
    ("002074", "国轩高科", "电池", 28.3), ("300750", "宁德时代", "电池", 268), ("002600", "领益智造", "消费电子", 11.5),
    ("002475", "立讯精密", "消费电子", 41.2), ("002585", "双星新材", "塑料制品", 6.8), ("002249", "大洋电机", "电机", 7.9),
    ("600848", "上海临港", "园区开发", 10.4), ("300251", "光线传媒", "影视音像", 14.6), ("002739", "万达电影", "影视音像", 12.3),
    ("300413", "芒果超媒", "影视音像", 24.8), ("002027", "分众传媒", "广告营销", 7.2), ("300058", "蓝色光标", "广告营销", 8.9),
    ("600547", "山东黄金", "黄金", 27.6), ("601899", "紫金矿业", "黄金", 18.4), ("600489", "中金黄金", "黄金", 14.2),
    ("601088", "中国神华", "煤炭", 39.5), ("601225", "陕西煤业", "煤炭", 23.4), ("000063", "中兴通讯", "通信设备", 33.6),
    ("600498", "烽火通信", "通信设备", 21.5), ("688981", "中芯国际", "半导体", 89.0), ("603501", "韦尔股份", "半导体", 108),
    ("002371", "北方华创", "半导体", 415), ("000725", "京东方A", "元器件", 4.4), ("002138", "顺络电子", "元器件", 30.1),
    ("300059", "东方财富", "互联网服务", 23.8), ("002024", "苏宁易购", "互联网服务", 2.1), ("600588", "用友网络", "软件开发", 13.6),
    ("002230", "科大讯飞", "软件开发", 49.2), ("601318", "中国平安", "保险", 55.1), ("600276", "恒瑞医药", "化学制药", 52.4),
    ("000333", "美的集团", "家电", 76.8),
]

REGIMES = {  # 日内漂移、波动、名称
    "strong_bull": (0.012, 0.010),
    "neutral": (0.000, 0.009),
    "weak": (-0.008, 0.010),
    "panic": (-0.025, 0.012),
}


def _seed(*parts) -> int:
    return int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()[:8], 16)


class SimMarket(MarketProvider):
    """可复现的模拟行情：市场因子 + 行业因子 + 个股因子的随机游走，含涨跌停约束。"""

    def __init__(self, regime: str | None = None):
        self.fixed_regime = regime
        self.date: str | None = None
        self.close_prices = {s: p for s, _, _, p in UNIVERSE}
        self._q: dict[str, Quote] = {}
        self.industries = sorted({i for _, _, i, _ in UNIVERSE})
        self.minute = 0

    def universe(self):
        return [(s, n, i) for s, n, i, _ in UNIVERSE]

    def _new_day(self, d: str) -> None:
        if self._q:  # 昨收结转
            self.close_prices = {s: q.price for s, q in self._q.items()}
        self.date = d
        rng = random.Random(_seed("regime", d))
        self.regime = self.fixed_regime or rng.choices(list(REGIMES), weights=[3, 4, 2, 1])[0]
        self.rng = random.Random(_seed("path", d))
        drift, vol = REGIMES[self.regime]
        self.m_drift, self.m_vol = drift / 240, vol / math.sqrt(240)
        self.m = self.rng.gauss(drift * 0.2, vol * 0.3)  # 开盘跳空
        self.ind = {i: self.rng.gauss(0, 0.008) for i in self.industries}
        self.ind_drift = {i: self.rng.gauss(0, 0.012) / 240 for i in self.industries}
        self.idio = {s: self.rng.gauss(0, 0.006) for s, *_ in UNIVERSE}
        self.minute = 0
        self._last_min = 9 * 60 + 29
        self._q = {}
        for s, n, ind, _ in UNIVERSE:
            pc = self.close_prices[s]
            op = self._clip(s, pc, pc * math.exp(self.m + self.ind[ind] + self.idio[s]))
            vol0 = self.rng.uniform(2e5, 2e6)
            self._q[s] = Quote(s, n, ind, op, pc, op, op, op, round((op / pc - 1) * 100, 2), op, op * vol0, vol0)

    @staticmethod
    def _clip(s: str, pc: float, p: float) -> float:
        r = limit_ratio(s)
        return round(min(max(p, pc * (1 - r)), pc * (1 + r)), 2)

    def step(self, clock: Clock) -> None:
        if clock.trade_date != self.date:
            self._new_day(clock.trade_date)
        if not is_trading(clock.now().time()):
            return
        # 每个模拟分钟推进一次
        t = clock.now()
        minute = (t.hour * 60 + t.minute)
        diff = minute - self._last_min
        steps = 1 if diff > 30 else max(diff, 0)  # 午休跨越只走一步
        self._last_min = minute
        for _ in range(steps):
            self.m += self.rng.gauss(self.m_drift, self.m_vol)
            for i in self.industries:
                self.ind[i] += self.rng.gauss(self.ind_drift[i], 0.0012)
            for s, n, ind, _ in UNIVERSE:
                self.idio[s] += self.rng.gauss(0, 0.0015)
                q = self._q[s]
                p = self._clip(s, q.pre_close, q.pre_close * math.exp(self.m + self.ind[ind] + self.idio[s]))
                v = self.rng.uniform(5e3, 8e4) * (1 + abs(p / q.price - 1) * 200)
                q.turnover += p * v
                q.volume += v
                q.price, q.high, q.low = p, max(q.high, p), min(q.low, p)
                q.vwap = q.turnover / q.volume
                q.chg_pct = round((p / q.pre_close - 1) * 100, 2)

    def quotes(self):
        return self._q

    def breadth(self) -> Breadth:
        # 用市场因子推算全市场 5500 只股票的涨跌分布（不必真模拟 5500 只）
        total = 5500
        z = self.m / 0.018
        up_ratio = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        up = int(total * min(max(up_ratio, 0.03), 0.97))
        main_up_ratio = min(max(up_ratio + 0.03, 0), 1)
        lu = int(30 + 90 * up_ratio ** 3)
        ld = int(5 + 120 * (1 - up_ratio) ** 4)
        return Breadth(total, up, total - up, round(main_up_ratio, 4), lu, ld)

    def industry_rank(self):
        avg = {}
        for i in self.industries:
            chgs = [q.chg_pct for q in self._q.values() if q.industry == i]
            avg[i] = sum(chgs) / len(chgs)
        ordered = sorted(avg, key=avg.get, reverse=True)
        return {i: k + 1 for k, i in enumerate(ordered)}, len(ordered)


# ---------------------------------------------------------------- AkShare 真实行情
class AkShareMarket(MarketProvider):
    """东方财富数据（通过 AkShare）。全市场快照 5500+ 只，用于宽度统计；候选池只取前 N 只。

    注意：免费接口有频率限制，盘中建议 ≥ 30 秒刷新一次。
    """

    def __init__(self, refresh_seconds: int = 30, pool_size: int = 300):
        import akshare as ak  # noqa: F401  延迟导入

        self.refresh_seconds = refresh_seconds
        self.pool_size = pool_size
        self._last = 0.0
        self._q: dict[str, Quote] = {}
        self._breadth = Breadth(1, 0, 1, 0, 0, 0)
        self._ind_rank: dict[str, int] = {}
        self._ind_total = 1
        self._industry_of: dict[str, str] = {}

    def _industry(self, ak, symbol: str) -> str:
        if symbol not in self._industry_of:
            try:
                df = ak.stock_individual_info_em(symbol=symbol)
                self._industry_of[symbol] = str(df.set_index("item").loc["行业", "value"])
            except Exception:
                self._industry_of[symbol] = "未知"
        return self._industry_of[symbol]

    def step(self, clock: Clock) -> None:
        if _time.time() - self._last < self.refresh_seconds:
            return
        self._last = _time.time()
        import akshare as ak

        df = ak.stock_zh_a_spot_em()
        df = df[df["最新价"].notna() & (df["最新价"] > 0)]
        chg = df["涨跌幅"]
        main = df[~df["代码"].str.startswith(("300", "301", "688", "8", "4"))]
        self._breadth = Breadth(
            total=len(df), up=int((chg > 0).sum()), down=int((chg < 0).sum()),
            main_up_ratio=round(float((main["涨跌幅"] > 0).mean()), 4),
            limit_up=int((chg >= 9.8).sum()), limit_down=int((chg <= -9.8).sum()),
        )
        boards = ak.stock_board_industry_name_em().sort_values("涨跌幅", ascending=False)
        self._ind_rank = {n: k + 1 for k, n in enumerate(boards["板块名称"])}
        self._ind_total = len(boards)
        # 候选池：成交额前 N 的非 ST 股票
        pool = df[~df["名称"].str.contains("ST")].sort_values("成交额", ascending=False).head(self.pool_size)
        q = {}
        for _, r in pool.iterrows():
            s = r["代码"]
            vol = float(r["成交量"]) * 100
            vwap = float(r["成交额"]) / vol if vol else float(r["最新价"])
            q[s] = Quote(s, r["名称"], self._industry_of.get(s, "未知"), float(r["最新价"]), float(r["昨收"]),
                         float(r["今开"]), float(r["最高"]), float(r["最低"]), float(r["涨跌幅"]),
                         vwap, float(r["成交额"]), vol)
        self._q = q

    def ensure_industry(self, symbols: list[str]) -> None:
        import akshare as ak

        for s in symbols:
            if s in self._q:
                self._q[s].industry = self._industry(ak, s)

    def quotes(self):
        return self._q

    def breadth(self):
        return self._breadth

    def industry_rank(self):
        return self._ind_rank, self._ind_total

    def universe(self):
        return [(s, q.name, q.industry) for s, q in self._q.items()]
