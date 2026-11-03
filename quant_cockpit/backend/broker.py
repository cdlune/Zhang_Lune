"""柜台层：PaperBroker（模拟撮合，遵守 A 股规则）与 QmtBroker（迅投 miniQMT 实盘骨架）。

引擎维护「本地台账」，柜台维护「柜台视图」，两者定期对账。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .market import Quote


def calc_fee(side: str, amount: float) -> float:
    commission = max(amount * 0.00025, 5.0)  # 佣金万 2.5，最低 5 元
    transfer = amount * 0.00001  # 过户费
    stamp = amount * 0.0005 if side == "sell" else 0.0  # 印花税仅卖出
    return round(commission + transfer + stamp, 2)


@dataclass
class BrokerOrder:
    broker_id: str
    symbol: str
    side: str
    price: float
    qty: int
    filled: int = 0
    status: str = "submitted"  # submitted / filled / cancelled / rejected


@dataclass
class Fill:
    broker_id: str
    symbol: str
    side: str
    price: float
    qty: int
    fee: float


@dataclass
class CounterPosition:
    qty: int = 0
    sellable: int = 0
    cost: float = 0.0


class BrokerBase:
    name = "base"

    def place(self, symbol: str, side: str, price: float, qty: int) -> tuple[str, str]:
        """返回 (broker_id, error)。error 非空表示柜台拒单。"""
        raise NotImplementedError

    def cancel(self, broker_id: str) -> bool: ...
    def on_tick(self, quotes: dict[str, Quote]) -> list[Fill]: ...
    def settle_day(self) -> None: ...
    def asset(self) -> dict: ...
    def positions(self) -> dict[str, CounterPosition]: ...


class PaperBroker(BrokerBase):
    name = "paper"

    def __init__(self, cash: float):
        self.cash = cash
        self.frozen = 0.0
        self.pos: dict[str, CounterPosition] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self._ids = itertools.count(1)

    def place(self, symbol, side, price, qty):
        bid = f"P{next(self._ids):06d}"
        if qty <= 0 or (side == "buy" and qty % 100):
            return bid, "买入数量必须为 100 股整数倍"
        if side == "buy":
            need = price * qty + calc_fee("buy", price * qty)
            if need > self.cash - self.frozen:
                return bid, "可用资金不足"
            self.frozen += need
        else:
            p = self.pos.get(symbol)
            if not p or p.sellable < qty:
                return bid, "可卖数量不足（T+1）"
            p.sellable -= qty
        self.orders[bid] = BrokerOrder(bid, symbol, side, price, qty)
        return bid, ""

    def cancel(self, broker_id):
        o = self.orders.get(broker_id)
        if not o or o.status != "submitted":
            return False
        self._release(o)
        o.status = "cancelled"
        return True

    def _release(self, o: BrokerOrder):
        left = o.qty - o.filled
        if o.side == "buy":
            self.frozen -= o.price * left + calc_fee("buy", o.price * left)
        else:
            self.pos[o.symbol].sellable += left

    def on_tick(self, quotes):
        fills = []
        for o in self.orders.values():
            if o.status != "submitted":
                continue
            q = quotes.get(o.symbol)
            if not q:
                continue
            if o.side == "buy" and q.price <= o.price and q.price < q.limit_up:
                px = q.price
            elif o.side == "sell" and q.price >= o.price and q.price > q.limit_down:
                px = q.price
            else:
                continue
            qty = o.qty - o.filled
            amt = px * qty
            fee = calc_fee(o.side, amt)
            if o.side == "buy":
                self.frozen -= o.price * qty + calc_fee("buy", o.price * qty)
                self.cash -= amt + fee
                p = self.pos.setdefault(o.symbol, CounterPosition())
                p.cost = (p.cost * p.qty + amt + fee) / (p.qty + qty)
                p.qty += qty  # 当日买入不可卖
            else:
                self.cash += amt - fee
                p = self.pos[o.symbol]
                p.qty -= qty
                if p.qty == 0:
                    del self.pos[o.symbol]
            o.filled, o.status = o.qty, "filled"
            fills.append(Fill(o.broker_id, o.symbol, o.side, px, qty, fee))
        return fills

    def settle_day(self):
        for bid, o in list(self.orders.items()):
            if o.status == "submitted":  # 当日委托收盘自动失效
                self._release(o)
                o.status = "cancelled"
        for p in self.pos.values():
            p.sellable = p.qty

    def asset(self):
        return {"cash": round(self.cash, 2), "frozen": round(self.frozen, 2)}

    def positions(self):
        return self.pos


class QmtBroker(BrokerBase):
    """迅投 miniQMT（xtquant）实盘适配骨架。

    需要：券商开通 QMT 极简模式 + Windows 上运行 miniQMT 客户端 + pip install xtquant。
    本仓库默认不启用；真实下单前务必先用 PaperBroker 跑满至少一个月。
    """

    name = "qmt"

    def __init__(self, qmt_path: str, account_id: str, session_id: int = 123456):
        from xtquant import xtconstant  # type: ignore
        from xtquant.xttrader import XtQuantTrader  # type: ignore
        from xtquant.xttype import StockAccount  # type: ignore

        self.xtc = xtconstant
        self.trader = XtQuantTrader(qmt_path, session_id)
        self.acc = StockAccount(account_id)
        self.trader.start()
        if self.trader.connect() != 0:
            raise RuntimeError("miniQMT 连接失败")
        self.trader.subscribe(self.acc)

    @staticmethod
    def _code(symbol: str) -> str:
        return f"{symbol}.SH" if symbol.startswith("6") else f"{symbol}.SZ"

    def place(self, symbol, side, price, qty):
        t = self.xtc.STOCK_BUY if side == "buy" else self.xtc.STOCK_SELL
        oid = self.trader.order_stock(self.acc, self._code(symbol), t, qty, self.xtc.FIX_PRICE, price, "intel_short", "")
        return str(oid), "" if oid > 0 else "柜台拒单"

    def cancel(self, broker_id):
        return self.trader.cancel_order_stock(self.acc, int(broker_id)) == 0

    def on_tick(self, quotes):
        # 实盘成交通过 XtQuantTraderCallback.on_stock_trade 回调推送，这里轮询当日成交做增量同步
        fills, seen = [], getattr(self, "_seen", set())
        for t in self.trader.query_stock_trades(self.acc) or []:
            if t.traded_id in seen:
                continue
            seen.add(t.traded_id)
            side = "buy" if t.order_type == self.xtc.STOCK_BUY else "sell"
            amt = t.traded_price * t.traded_volume
            fills.append(Fill(str(t.order_id), t.stock_code[:6], side, t.traded_price, t.traded_volume, calc_fee(side, amt)))
        self._seen = seen
        return fills

    def settle_day(self):
        pass

    def asset(self):
        a = self.trader.query_stock_asset(self.acc)
        return {"cash": a.cash, "frozen": a.frozen_cash, "total": a.total_asset}

    def positions(self):
        return {
            p.stock_code[:6]: CounterPosition(p.volume, p.can_use_volume, p.open_price)
            for p in self.trader.query_stock_positions(self.acc) or []
        }
