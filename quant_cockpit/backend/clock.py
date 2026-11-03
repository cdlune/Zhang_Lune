"""交易时钟：live 模式用北京时间真实时钟；sim 模式用可加速的模拟时钟。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CN = ZoneInfo("Asia/Shanghai")


def hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def phase_of(t: time) -> str:
    if t < hm("09:15"):
        return "盘前准备"
    if t < hm("09:25"):
        return "集合竞价"
    if t < hm("09:30"):
        return "竞价撮合"
    if t < hm("11:30"):
        return "早盘交易"
    if t < hm("13:00"):
        return "午间休市"
    if t < hm("15:00"):
        return "午盘交易"
    return "已闭市"


def is_trading(t: time) -> bool:
    return hm("09:30") <= t < hm("11:30") or hm("13:00") <= t < hm("15:00")


def next_trade_date(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:  # 简化：只跳过周末，节假日可接入交易日历
        d += timedelta(days=1)
    return d


def trade_days_between(a: str, b: str) -> int:
    """a 到 b 之间经过的交易日数（不含 a，含 b）。"""
    da, db_ = date.fromisoformat(a), date.fromisoformat(b)
    n, d = 0, da
    while d < db_:
        d = next_trade_date(d)
        n += 1
    return n


class Clock:
    def __init__(self, simulated: bool, speed: float = 60.0, start: datetime | None = None):
        self.simulated = simulated
        self.speed = speed
        now = datetime.now(CN)
        if start is None:
            d = now.date()
            while d.weekday() >= 5:
                d = next_trade_date(d)
            start = datetime.combine(d, hm("09:00"), CN)
        self._now = start
        self.paused = False

    def now(self) -> datetime:
        return self._now if self.simulated else datetime.now(CN)

    def advance(self, real_seconds: float) -> None:
        if not self.simulated or self.paused:
            return
        nxt = self._now + timedelta(seconds=real_seconds * self.speed)
        # 午休直接快进，节省演示时间
        if hm("11:30") <= nxt.time() < hm("12:59"):
            nxt = datetime.combine(nxt.date(), hm("12:59"), CN)
        self._now = nxt

    def next_day(self) -> None:
        d = next_trade_date(self.now().date())
        self._now = datetime.combine(d, hm("09:00"), CN)

    @property
    def trade_date(self) -> str:
        return self.now().date().isoformat()

    @property
    def hhmm(self) -> str:
        return self.now().strftime("%H:%M")

    @property
    def phase(self) -> str:
        if not self.simulated and self.now().weekday() >= 5:
            return "休市"
        return phase_of(self.now().time())
