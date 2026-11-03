"""风控引擎：所有委托在发往柜台前必须通过的硬性检查（与 LLM 无关）。"""
from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig
from .market import Quote


@dataclass
class AccountState:
    equity: float
    cash: float
    position_values: dict[str, float]  # symbol -> 市值（含在途买单）
    bought_today: float
    orders_today: int
    cancels_today: int
    seats_used: int


class RiskEngine:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.read_only = False  # 只读防误触：禁止一切新委托
        self.paused = False  # 暂停调度
        self.halted_reason = ""  # 熔断原因

    def check(self, side: str, q: Quote, price: float, qty: int, acc: AccountState, hhmm: str) -> list[str]:
        c, errs = self.cfg, []
        if self.read_only:
            errs.append("只读模式：禁止新委托")
        if self.halted_reason:
            errs.append(f"已熔断：{self.halted_reason}")
        if not (c.order_window_start <= hhmm <= c.order_window_end):
            errs.append(f"不在报单时间窗 {c.order_window_start}-{c.order_window_end}")
        if acc.orders_today >= c.max_orders_per_day:
            errs.append(f"单日报单已达上限 {c.max_orders_per_day} 笔")
        if qty <= 0 or qty > c.max_order_qty:
            errs.append(f"单笔数量 {qty} 超出 (0, {c.max_order_qty}]")
        if not (q.limit_down <= price <= q.limit_up):
            errs.append("委托价超出涨跌停价格笼子")
        if side == "buy":
            amt = price * qty
            if qty % 100:
                errs.append("买入必须为 100 股整数倍")
            if amt > c.order_amount_limit:
                errs.append(f"单笔金额 {amt:.0f} > 上限 {c.order_amount_limit:.0f}")
            if acc.bought_today + amt > c.daily_buy_limit:
                errs.append(f"单日买入将达 {acc.bought_today + amt:.0f} > 上限 {c.daily_buy_limit:.0f}")
            if amt > acc.cash:
                errs.append("可用资金不足")
            total = sum(acc.position_values.values()) + amt
            if acc.equity and total / acc.equity > c.max_total_position:
                errs.append(f"总仓位将达 {total / acc.equity:.0%} > {c.max_total_position:.0%}")
            single = acc.position_values.get(q.symbol, 0) + amt
            if acc.equity and single / acc.equity > c.max_single_position:
                errs.append(f"单票占比将达 {single / acc.equity:.0%} > {c.max_single_position:.0%}")
            if q.symbol not in acc.position_values and acc.seats_used >= c.max_seats:
                errs.append(f"持仓席位已满 {c.max_seats}")
            if price >= q.limit_up:
                errs.append("涨停价不追")
        return errs
