"""核心规则测试：python -m pytest tests -q（在 quant_cockpit 目录下）"""
import os
import tempfile

from backend.broker import PaperBroker, calc_fee
from backend.config import RiskConfig, Settings
from backend.engine import Engine, mock_inference
from backend.market import Quote
from backend.risk import AccountState, RiskEngine


def q(symbol="600000", price=10.0, pre=10.0):
    return Quote(symbol, "测试", "银行", price, pre, pre, price, price, (price / pre - 1) * 100, price, 1e8, 1e7)


def acc(**kw):
    base = dict(equity=50000, cash=50000, position_values={}, bought_today=0, orders_today=0, cancels_today=0, seats_used=0)
    base.update(kw)
    return AccountState(**base)


def test_fee():
    assert calc_fee("buy", 1000) == 5.01  # 最低佣金 5 元
    assert calc_fee("sell", 100000) == 25 + 1 + 50


def test_risk_limits():
    r = RiskEngine(RiskConfig())
    assert r.check("buy", q(), 10, 900, acc(), "10:00") == []
    assert any("单笔金额" in e for e in r.check("buy", q(), 10, 1100, acc(), "10:00"))
    assert any("100 股" in e for e in r.check("buy", q(), 10, 150, acc(), "10:00"))
    assert any("时间窗" in e for e in r.check("buy", q(), 10, 100, acc(), "14:58"))
    assert any("单日买入" in e for e in r.check("buy", q(), 10, 900, acc(bought_today=30000), "10:00"))
    assert any("席位" in e for e in r.check("buy", q(), 10, 100, acc(seats_used=5), "10:00"))
    assert any("涨停" in e for e in r.check("buy", q(price=11.0), 11.0, 100, acc(), "10:00"))
    r.read_only = True
    assert any("只读" in e for e in r.check("buy", q(), 10, 100, acc(), "10:00"))


def test_paper_broker_t1():
    b = PaperBroker(20000)
    bid, err = b.place("600000", "buy", 10.0, 500)
    assert not err
    fills = b.on_tick({"600000": q(price=9.9)})
    assert fills and fills[0].price == 9.9
    _, err = b.place("600000", "sell", 9.9, 500)
    assert "T+1" in err  # 当日买入不可卖
    b.settle_day()
    _, err = b.place("600000", "sell", 9.9, 500)
    assert not err


def test_panic_guard_blocks_buys():
    ctx = {"breadth": {"up_ratio": 0.15, "up": 825, "total": 5500, "main_up_ratio": 0.17, "limit_up": 20,
                       "limit_down": 60, "sentiment": "panic_bearish"},
           "guard": {"panic": True}, "top_industries": ["黄金"], "rejected_candidates": [],
           "eligible_candidates": [{"symbol": "600000", "llm_score": 99, "price": 10, "vwap": 10, "industry": "银行",
                                    "industry_rank": "1/10", "chg_pct": 1, "vwap_dev": 0, "thesis": ""}],
           "holdings": [], "budget": {"seats_left": 5, "daily_buy_left": 35000, "cash": 50000}}
    out = mock_inference(ctx, RiskConfig())
    assert not [i for i in out["intents"] if i["action"] == "buy"]
    assert "防守铁律" in out["rejections"]


def test_engine_full_days():
    s = Settings()
    s.db_path = os.path.join(tempfile.mkdtemp(), "t.db")
    e = Engine(s)
    for _ in range(3):
        for _ in range(400):
            e.tick(1)
        st = e.state()
        assert st["recon"]["status"] == "对账一致"
        assert st["asset"]["bought_today"] <= e.cfg.daily_buy_limit
        assert len(st["positions"]) <= e.cfg.max_seats
        e.clock.next_day()
    # LLM 越界买入意向必须被后置校验拦截
    ok, _, guard = e.guard_candidates(e.market.breadth())
    bad = [{"symbol": "600519", "action": "buy", "price_low": 1, "price_high": 9999, "target_qty": 100}]
    assert e.validate_intents(bad, [], [], guard, e.account()) == []
