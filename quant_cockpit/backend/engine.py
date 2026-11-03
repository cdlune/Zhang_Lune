"""交易引擎：把六个环节串成一条流水线。

  ① 五源情报采集   00:00-09:15   intel.py
  ② 两级选票漏斗   09:15         预筛池冻结（量化规则） → LLM 深度分析（因子雷达 + 置信度）
  ③ 交易意向发布   盘前 + 滚动    LLM 输出结构化意向；代码铁律前置过滤 + 后置校验
  ④ 区间执行器     09:30-14:55   价格落入意向区间才触发下单
  ⑤ T+3 强制复评   滚动          持满 3 个交易日 → LLM 二选一：持有 / 卖出
  ⑥ 预算与风控台账 全天          LLM 日预算、熔断、仓位上限、对账
"""
from __future__ import annotations

import math
from datetime import datetime

from . import intel as intel_mod
from .broker import BrokerBase, PaperBroker
from .clock import Clock, is_trading, trade_days_between
from .config import RiskConfig, Settings
from .db import DB
from .llm import FUNNEL_SYSTEM_PROMPT, LLMClient, build_user_prompt
from .market import AkShareMarket, Breadth, MarketProvider, Quote, SimMarket, board, exchange
from .risk import AccountState, RiskEngine

FACTORS = ["事件冲击", "信息新颖", "资金流向", "技术动能", "安全边际", "合规风控", "模型置信"]
ACTIVE = ("已发布", "执行中")


def clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


class Engine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.db = DB(settings.db_path)
        self.clock = Clock(simulated=settings.mode == "sim", speed=settings.sim_speed)
        self.market: MarketProvider = AkShareMarket() if settings.market_source == "akshare" else SimMarket()
        self.broker: BrokerBase = PaperBroker(settings.initial_cash)
        self.cfg = RiskConfig.from_dict(self.db.get_kv("risk_cfg", {})) if self.db.get_kv("risk_cfg") else RiskConfig()
        self.risk = RiskEngine(self.cfg)
        self.llm = LLMClient(settings, self.db, self.clock)
        if self.db.get_kv("cash") is None:
            self.db.set_kv("cash", settings.initial_cash)
        self.day: str | None = None
        self.flags: dict[str, bool] = {}
        self.last_infer: datetime | None = None
        self.recon = {"status": "未对账", "diffs": []}
        self.market.step(self.clock)
        if settings.llm_provider != "mock" and self.llm.provider == "mock":
            self.event("系统", f"LLM provider={settings.llm_provider} 但未设置 QC_LLM_API_KEY，已使用规则引擎", level="warn")

    # ================================================================ 工具
    def event(self, category: str, title: str, detail: str = "", level: str = "info") -> None:
        self.db.execute(
            "INSERT INTO events (ts, category, level, title, detail) VALUES (?,?,?,?,?)",
            (self.clock.now().isoformat(timespec="seconds"), category, level, title, detail),
        )

    @property
    def cash(self) -> float:
        return float(self.db.get_kv("cash", 0))

    def positions(self) -> list[dict]:
        return self.db.query("SELECT * FROM positions WHERE qty>0 ORDER BY symbol")

    def account(self) -> AccountState:
        qs = self.market.quotes()
        pv: dict[str, float] = {}
        for p in self.positions():
            q = qs.get(p["symbol"])
            pv[p["symbol"]] = p["qty"] * (q.price if q else p["cost"])
        pending = self.db.query(
            "SELECT symbol, price*(qty-filled_qty) amt FROM orders WHERE trade_date=? AND side='buy' AND status='已报'",
            (self.clock.trade_date,),
        )
        pending_amt = 0.0
        for o in pending:
            pv[o["symbol"]] = pv.get(o["symbol"], 0) + o["amt"]
            pending_amt += o["amt"]
        held = sum(p["qty"] * (qs[p["symbol"]].price if p["symbol"] in qs else p["cost"]) for p in self.positions())
        td = self.clock.trade_date
        bought = self.db.one(
            "SELECT COALESCE(SUM(price*qty),0) v FROM orders WHERE trade_date=? AND side='buy' AND status!='拒单'", (td,))["v"]
        n_orders = self.db.one("SELECT COUNT(*) n FROM orders WHERE trade_date=? AND status!='拒单'", (td,))["n"]
        n_cancel = self.db.one("SELECT COUNT(*) n FROM orders WHERE trade_date=? AND status='已撤'", (td,))["n"]
        return AccountState(
            equity=self.cash + held, cash=self.cash - pending_amt, position_values=pv, bought_today=bought,
            orders_today=n_orders, cancels_today=n_cancel, seats_used=len(pv),
        )

    # ================================================================ 主循环
    def tick(self, real_seconds: float) -> None:
        if self.risk.paused:
            return
        self.clock.advance(real_seconds)
        try:
            self.market.step(self.clock)
        except Exception as e:
            self.event("系统", "行情刷新失败", str(e), "warn")
            return
        if self.clock.trade_date != self.day:
            self._new_day()
        t, hhmm = self.clock.now().time(), self.clock.hhmm
        f = self.flags
        if hhmm >= "09:00" and not f.get("intel"):
            self.collect_intel()
        if hhmm >= "09:15" and not f.get("funnel"):
            self.run_funnel()
        if hhmm >= "09:25" and not f.get("preopen") and f.get("funnel"):
            f["preopen"] = True
            self.run_inference(reason="盘前")
        if is_trading(t):
            if self.last_infer is None or (self.clock.now() - self.last_infer).total_seconds() >= self.s.inference_interval_min * 60:
                if hhmm < self.cfg.intent_expire:
                    self.run_inference(reason="滚动")
            self.stop_loss_monitor()
            self.execute_intents()
            self.apply_fills(self.broker.on_tick(self.market.quotes()))
            if not f.get("recon_" + hhmm[:4]):  # 每 10 分钟对账一次
                f["recon_" + hhmm[:4]] = True
                self.reconcile()
        if hhmm >= self.cfg.intent_expire and not f.get("expired"):
            f["expired"] = True
            self.expire_intents()
        if hhmm >= "15:00" and not f.get("closed"):
            f["closed"] = True
            self.close_day()

    def _new_day(self) -> None:
        if self.day is not None:
            self.broker.settle_day()
        self.day = self.clock.trade_date
        self.flags = {}
        self.last_infer = None
        self.risk.halted_reason = ""
        self.db.execute("UPDATE positions SET sellable=qty")
        self.db.execute("UPDATE orders SET status='已撤', note='收盘失效' WHERE status='已报'")
        self.db.execute("UPDATE intents SET status='已过期' WHERE status IN ('已发布','执行中')")
        self.db.execute("INSERT OR IGNORE INTO equity_snap (trade_date, equity) VALUES (?, ?)",
                        (self.day, self._equity_at_preclose()))
        self.event("系统状态", f"新交易日 {self.day}", f"引擎运行模式: {self.s.mode} · 柜台: {self.broker.name}")

    def _equity_at_preclose(self) -> float:
        qs = self.market.quotes()
        return self.cash + sum(p["qty"] * (qs[p["symbol"]].pre_close if p["symbol"] in qs else p["cost"])
                               for p in self.positions())

    # ================================================================ ① 情报
    def collect_intel(self) -> None:
        self.flags["intel"] = True
        uni = self.market.universe()
        try:
            items = intel_mod.collect_akshare(self.day, uni) if self.s.market_source == "akshare" \
                else intel_mod.collect_sim(self.day, uni)
        except Exception as e:
            items = []
            self.event("情报", "情报采集失败", str(e), "warn")
        self.db.execute("DELETE FROM intel WHERE trade_date=?", (self.day,))
        for it in items:
            self.db.execute(
                "INSERT INTO intel (trade_date, ts, source, symbol, title, sentiment, novelty) VALUES (?,?,?,?,?,?,?)",
                (self.day, self.clock.now().isoformat(timespec="seconds"), it.source, it.symbol, it.title, it.sentiment, it.novelty))
        by_src = {}
        for it in items:
            by_src[it.source] = by_src.get(it.source, 0) + 1
        self.event("情报", f"五源情报采集完成 {len(items)} 条", " · ".join(f"{k}{v}" for k, v in by_src.items()))

    # ================================================================ ② 两级漏斗
    def factor_scores(self, q: Quote, intel_rows: list[dict], conf: float | None = None) -> dict[str, float]:
        sent = [r["sentiment"] for r in intel_rows]
        flow = [r["sentiment"] for r in intel_rows if r["source"] in ("资金流", "龙虎榜")]
        neg_notice = any(r["source"] == "公告" and r["sentiment"] < 0 for r in intel_rows)
        f = {
            "事件冲击": clamp(50 + 50 * (sum(sent) / len(sent) if sent else 0) * min(len(sent), 2) / 1.5),
            "信息新颖": clamp(100 * max([r["novelty"] for r in intel_rows], default=0.3)),
            "资金流向": clamp(50 + 40 * (sum(flow) / len(flow) if flow else 0) + (q.vwap_dev * 3)),
            "技术动能": clamp(50 + q.chg_pct * 7),
            "安全边际": clamp(80 - abs(q.vwap_dev) * 12 - max(q.chg_pct - 3.5, 0) * 10 - max(-q.chg_pct - 3, 0) * 8),
            "合规风控": 50.0 if neg_notice else 85.0,
            "模型置信": 50.0 if conf is None else conf,
        }
        return {k: round(v) for k, v in f.items()}

    def run_funnel(self) -> None:
        self.flags["funnel"] = True
        qs = self.market.quotes()
        if isinstance(self.market, AkShareMarket):
            self.market.ensure_industry(list(qs)[:80])
        rows = self.db.query("SELECT * FROM intel WHERE trade_date=?", (self.day,))
        by_sym: dict[str, list] = {}
        for r in rows:
            by_sym.setdefault(r["symbol"], []).append(r)
        # 一级：量化预筛（有情报、非 ST、合规分正常），按综合分取前 15 冻结
        scored = []
        for s, q in qs.items():
            if "ST" in q.name:
                continue
            f = self.factor_scores(q, by_sym.get(s, []))
            pre = 0.35 * f["事件冲击"] + 0.25 * f["信息新颖"] + 0.2 * f["资金流向"] + 0.2 * f["合规风控"]
            scored.append((pre, s, q, f))
        scored.sort(key=lambda x: x[0], reverse=True)
        stage1 = scored[:15]
        self.db.execute("DELETE FROM pool WHERE trade_date=?", (self.day,))
        # 二级：LLM 深度分析，给出置信度与一句话逻辑
        ctx = [{"symbol": s, "name": q.name, "industry": q.industry, "factors": f,
                "intel": [r["title"] for r in by_sym.get(s, [])]} for _, s, q, f in stage1]

        def mock():
            out = []
            for c in ctx:
                f = c["factors"]
                conf = round(0.45 * f["事件冲击"] + 0.25 * f["信息新颖"] + 0.3 * f["资金流向"])
                out.append({"symbol": c["symbol"], "confidence": conf,
                            "thesis": (c["intel"][0] if c["intel"] else "无增量情报") + "，关注盘中承接"})
            return {"analysis": out}

        prompt = "预筛候选池（JSON）：\n" + _dumps(ctx) + "\n请输出 analysis JSON。"
        res, provider = self.llm.chat_json("funnel", prompt, mock, system=FUNNEL_SYSTEM_PROMPT)
        conf = {a["symbol"]: (float(a.get("confidence", 50)), a.get("thesis", "")) for a in res.get("analysis", [])}
        ranked = sorted(stage1, key=lambda x: conf.get(x[1], (0, ""))[0], reverse=True)
        for k, (pre, s, q, f) in enumerate(ranked):
            c, thesis = conf.get(s, (50.0, ""))
            f = self.factor_scores(q, by_sym.get(s, []), c)
            stage = 2 if k < 8 else 1
            self.db.execute("INSERT INTO pool VALUES (?,?,?,?,?,?,?)",
                            (self.day, s, q.name, stage, round(c, 1), _dumps(f), thesis))
        self.event("选股", "预筛池已冻结", f"一级 {len(stage1)} 只 → 二级 {min(8, len(stage1))} 只 · LLM: {provider}")

    # ================================================================ ③ 推演 + 意向
    def guard_candidates(self, br: Breadth) -> tuple[list[dict], list[dict], dict]:
        """代码铁律：先于 LLM 执行。返回 (合格候选, 被拒候选, 全局守卫状态)。"""
        c, qs = self.cfg, self.market.quotes()
        rank, total = self.market.industry_rank()
        top_n = max(1, math.ceil(total * c.mainline_top_ratio))
        now = self.clock.now()
        elapsed = max(0.1, min(1.0, ((now.hour * 60 + now.minute) - 570) / 240))
        min_turn = c.min_turnover * elapsed
        guard = {"panic": br.up_ratio < c.panic_up_ratio, "up_ratio": round(br.up_ratio, 4),
                 "mainline": [i for i, r in sorted(rank.items(), key=lambda x: x[1])[:top_n]]}
        ok, rej = [], []
        pool = self.db.query("SELECT * FROM pool WHERE trade_date=? ORDER BY stage DESC, score DESC", (self.day,))
        for p in pool:
            q = qs.get(p["symbol"])
            if not q:
                continue
            reasons = []
            if guard["panic"]:
                reasons.append("大盘极端弱势防守铁律")
            if q.chg_pct > c.golden_dip_max_chg:
                reasons.append(f"涨幅{q.chg_pct:+.2f}%超出+{c.golden_dip_max_chg}%黄金低吸上限")
            if q.chg_pct < c.golden_dip_min_chg:
                reasons.append(f"跌幅{q.chg_pct:.2f}%，不抄底接飞刀")
            if abs(q.vwap_dev) > c.max_vwap_deviation:
                reasons.append(f"偏离VWAP {q.vwap_dev:+.2f}%过远")
            if q.price * 100 > c.order_amount_limit:
                reasons.append(f"股价{q.price:.0f}元一手超单笔预算上限")
            r = rank.get(q.industry, total)
            if r > top_n:
                reasons.append(f"所属{q.industry}排名{r}/{total}非主线")
            if q.turnover < min_turn:
                reasons.append(f"成交额{q.turnover / 1e4:.0f}万不足门槛")
            item = {"symbol": q.symbol, "name": q.name, "industry": q.industry, "industry_rank": f"{r}/{total}",
                    "price": q.price, "chg_pct": q.chg_pct, "vwap": round(q.vwap, 3), "vwap_dev": round(q.vwap_dev, 2),
                    "turnover_wan": round(q.turnover / 1e4), "llm_score": p["score"], "thesis": p["note"],
                    "stage": p["stage"]}
            (rej if reasons else ok).append({**item, "reasons": reasons} if reasons else item)
        return ok, rej, guard

    def holdings_ctx(self) -> list[dict]:
        qs, out = self.market.quotes(), []
        for p in self.positions():
            q = qs.get(p["symbol"])
            price = q.price if q else p["cost"]
            days = trade_days_between(p["open_date"], self.clock.trade_date)
            out.append({"symbol": p["symbol"], "name": p["name"], "qty": p["qty"], "sellable": p["sellable"],
                        "cost": round(p["cost"], 3), "price": price, "pnl_pct": round((price / p["cost"] - 1) * 100, 2),
                        "chg_pct": q.chg_pct if q else 0, "vwap": round(q.vwap, 3) if q else price,
                        "days_held": days, "must_review": days >= self.cfg.hold_review_days})
        return out

    def run_inference(self, reason: str = "滚动") -> None:
        self.last_infer = self.clock.now()
        br = self.market.breadth()
        ok, rej, guard = self.guard_candidates(br)
        holds = self.holdings_ctx()
        acc = self.account()
        rank, total = self.market.industry_rank()
        top_inds = [i for i, _ in sorted(rank.items(), key=lambda x: x[1])[:3]]
        ctx = {
            "time": self.clock.now().strftime("%Y-%m-%d %H:%M"), "round_reason": reason,
            "breadth": br.to_dict(), "top_industries": top_inds, "guard": guard,
            "eligible_candidates": ok, "rejected_candidates": rej, "holdings": holds,
            "budget": {"cash": round(acc.cash, 2), "order_amount_limit": self.cfg.order_amount_limit,
                       "daily_buy_left": round(self.cfg.daily_buy_limit - acc.bought_today, 2),
                       "seats_left": self.cfg.max_seats - acc.seats_used},
        }
        res, provider = self.llm.chat_json("inference", build_user_prompt(ctx), lambda: mock_inference(ctx, self.cfg))
        intents = self.validate_intents(res.get("intents", []), ok, holds, guard, acc)
        n = self.db.one("SELECT COUNT(*) n FROM rounds WHERE trade_date=?", (self.day,))["n"] + 1
        macro = res.get("macro", {})
        rid = self.db.execute(
            "INSERT INTO rounds (trade_date, round_no, ts, macro, position_bias, sentiment, rejections, holdings_review,"
            " intents_count, guard, raw, provider) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.day, n, self.clock.now().strftime("%H:%M:%S"), macro.get("summary", ""),
             int(macro.get("position_bias", 0)), macro.get("sentiment", br.sentiment), res.get("rejections", ""),
             res.get("holdings_review", ""), len(intents), _dumps(guard), _dumps(res), provider))
        self.publish_intents(rid, intents)
        lvl = "warn" if guard["panic"] else "info"
        self.event("推演", f"第 {n} 轮推演（{reason}）· {len(intents)} 笔意向",
                   f"情绪 {macro.get('sentiment', br.sentiment)} · 上涨比例 {br.up_ratio:.1%} · {provider}"
                   + (f" · {res['_error']}" if res.get("_error") else ""), lvl)

    def validate_intents(self, raw: list, ok: list, holds: list, guard: dict, acc: AccountState) -> list[dict]:
        """后置校验：LLM 说了不算，越界的意向直接丢弃或修正。"""
        okmap, hmap, qs, c = {o["symbol"]: o for o in ok}, {h["symbol"]: h for h in holds}, self.market.quotes(), self.cfg
        out, seen = [], set()
        for it in raw if isinstance(raw, list) else []:
            sym, act = str(it.get("symbol", ""))[:6], it.get("action")
            q = qs.get(sym)
            if not q or sym in seen:
                continue
            if act == "buy":
                if guard["panic"] or sym not in okmap:
                    self.event("风控", f"拦截越界买入意向 {sym}", "不在合格候选池或触发防守铁律", "warn")
                    continue
                hi = min(float(it.get("price_high") or q.price), q.price * 1.01, q.limit_up - 0.01)
                lo = min(float(it.get("price_low") or q.vwap * 0.99), hi)
                qty = int(min(int(it.get("target_qty") or 0), c.order_amount_limit // hi, c.max_order_qty) // 100 * 100)
                if qty < 100:
                    continue
            elif act == "sell":
                if sym not in hmap or hmap[sym]["sellable"] <= 0:
                    continue
                lo = max(float(it.get("price_low") or q.price * 0.99), q.limit_down)
                hi = float(it.get("price_high") or q.limit_up)
                qty = min(int(it.get("target_qty") or hmap[sym]["sellable"]), hmap[sym]["sellable"])
            elif act == "hold":
                if sym not in hmap:
                    continue
                lo = hi = None
                qty = 0
            else:
                continue
            seen.add(sym)
            out.append({"symbol": sym, "name": q.name, "action": act, "price_low": lo and round(lo, 2),
                        "price_high": hi and round(hi, 2), "target_qty": qty,
                        "amount": round(qty * (hi or 0), 2) if act == "buy" else None,
                        "confidence": float(it.get("confidence") or 0.5), "reason": str(it.get("reason", ""))[:500]})
        return out

    def publish_intents(self, round_id: int, intents: list[dict]) -> None:
        now = self.clock.now().isoformat(timespec="seconds")
        for it in intents:
            # 同一标的旧意向被新一轮替代（已下单的不动）
            self.db.execute("UPDATE intents SET status='已替代', updated_ts=? WHERE trade_date=? AND symbol=? AND status='已发布'",
                            (now, self.day, it["symbol"]))
            tier = "A" if it["confidence"] >= 0.8 else "B" if it["confidence"] >= 0.6 else "C"
            self.db.execute(
                "INSERT INTO intents (trade_date, round_id, symbol, name, action, status, price_low, price_high, target_qty,"
                " amount, tier, confidence, expire, reason, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.day, round_id, it["symbol"], it["name"], it["action"], "已发布", it["price_low"], it["price_high"],
                 it["target_qty"], it["amount"], tier if it["action"] == "buy" else None, it["confidence"],
                 self.cfg.intent_expire + ":00", it["reason"], now, now))

    # ================================================================ ④ 区间执行器
    def execute_intents(self) -> None:
        hhmm = self.clock.hhmm
        qs = self.market.quotes()
        for it in self.db.query("SELECT * FROM intents WHERE trade_date=? AND status='已发布' AND action IN ('buy','sell')",
                                (self.day,)):
            q = qs.get(it["symbol"])
            if not q or hhmm >= it["expire"][:5]:
                continue
            if it["action"] == "buy":
                if not (it["price_low"] <= q.price <= it["price_high"]):
                    continue  # 价格不在区间内，继续等待
                price = round(min(it["price_high"], q.price * 1.002), 2)
            else:
                if q.price < it["price_low"]:
                    continue
                price = round(max(q.price * 0.995, q.limit_down), 2)
            self.submit_order(it, q, it["action"], price, it["target_qty"])

    def submit_order(self, it: dict | None, q: Quote, side: str, price: float, qty: int, note: str = "") -> bool:
        acc = self.account()
        errs = self.risk.check(side, q, price, qty, acc, self.clock.hhmm)
        now = self.clock.now().isoformat(timespec="seconds")
        if errs:
            if it:
                self.db.execute("UPDATE intents SET status='风控拒绝', updated_ts=? WHERE id=?", (now, it["id"]))
            self.event("风控", f"拒绝{('买入' if side == 'buy' else '卖出')} {q.name}({q.symbol})", "；".join(errs), "warn")
            return False
        bid, err = self.broker.place(q.symbol, side, price, qty)
        status = "拒单" if err else "已报"
        self.db.execute(
            "INSERT INTO orders (trade_date, intent_id, symbol, name, side, price, qty, status, broker_id, ts, updated_ts, note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.day, it and it["id"], q.symbol, q.name, side, price, qty, status, bid, now, now, err or note))
        if it:
            self.db.execute("UPDATE intents SET status=?, updated_ts=? WHERE id=?",
                            ("柜台拒单" if err else "执行中", now, it["id"]))
        self.event("委托", f"{'买入' if side == 'buy' else '卖出'} {q.name} {qty} 股 @ {price:.2f}",
                   err or f"柜台编号 {bid} {note}", "warn" if err else "info")
        return not err

    def apply_fills(self, fills) -> None:
        for f in fills:
            o = self.db.one("SELECT * FROM orders WHERE broker_id=?", (f.broker_id,))
            if not o:
                continue
            now = self.clock.now().isoformat(timespec="seconds")
            self.db.execute("UPDATE orders SET filled_qty=?, avg_price=?, status='已成', updated_ts=? WHERE id=?",
                            (f.qty, f.price, now, o["id"]))
            self.db.execute("INSERT INTO fills (trade_date, order_id, symbol, name, side, price, qty, fee, ts)"
                            " VALUES (?,?,?,?,?,?,?,?,?)", (self.day, o["id"], f.symbol, o["name"], f.side, f.price, f.qty, f.fee, now))
            if o["intent_id"]:
                self.db.execute("UPDATE intents SET status='已成交', updated_ts=? WHERE id=?", (now, o["intent_id"]))
            amt = f.price * f.qty
            p = self.db.one("SELECT * FROM positions WHERE symbol=?", (f.symbol,))
            if f.side == "buy":
                self.db.set_kv("cash", self.cash - amt - f.fee)
                if p and p["qty"] > 0:
                    cost = (p["cost"] * p["qty"] + amt + f.fee) / (p["qty"] + f.qty)
                    self.db.execute("UPDATE positions SET qty=qty+?, cost=?, updated_ts=? WHERE symbol=?",
                                    (f.qty, cost, now, f.symbol))
                else:
                    self.db.execute("INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
                                    (f.symbol, o["name"], f.qty, 0, (amt + f.fee) / f.qty, self.day, self.s.strategy_id, now))
            else:
                self.db.set_kv("cash", self.cash + amt - f.fee)
                left = p["qty"] - f.qty
                if left <= 0:
                    self.db.execute("INSERT INTO closed_positions (symbol, name, open_date, close_date, qty, cost, close_price, pnl)"
                                    " VALUES (?,?,?,?,?,?,?,?)", (f.symbol, p["name"], p["open_date"], self.day, f.qty,
                                                                  p["cost"], f.price, (f.price - p["cost"]) * f.qty - f.fee))
                    self.db.execute("DELETE FROM positions WHERE symbol=?", (f.symbol,))
                else:
                    self.db.execute("UPDATE positions SET qty=?, sellable=sellable-?, updated_ts=? WHERE symbol=?",
                                    (left, f.qty, now, f.symbol))
            self.event("成交", f"{'买入' if f.side == 'buy' else '卖出'}成交 {o['name']} {f.qty} 股 @ {f.price:.2f}",
                       f"费用 {f.fee:.2f}")

    # ================================================================ ⑤ 止损 / T+3 / 收盘
    def stop_loss_monitor(self) -> None:
        for h in self.holdings_ctx():
            if h["pnl_pct"] <= self.cfg.stop_loss_pct and h["sellable"] > 0:
                busy = self.db.one("SELECT 1 FROM orders WHERE symbol=? AND side='sell' AND status='已报'", (h["symbol"],))
                if busy:
                    continue
                q = self.market.quote(h["symbol"])
                self.event("风控", f"触发硬止损 {h['name']} {h['pnl_pct']:.2f}%", "不经 LLM，直接卖出", "warn")
                self.submit_order(None, q, "sell", round(max(q.price * 0.99, q.limit_down), 2), h["sellable"], "硬止损")

    def expire_intents(self) -> None:
        now = self.clock.now().isoformat(timespec="seconds")
        self.db.execute("UPDATE intents SET status='已过期', updated_ts=? WHERE trade_date=? AND status='已发布'"
                        " AND action!='hold'", (now, self.day))
        for o in self.db.query("SELECT * FROM orders WHERE trade_date=? AND status='已报'", (self.day,)):
            if self.broker.cancel(o["broker_id"]):
                self.db.execute("UPDATE orders SET status='已撤', updated_ts=?, note='意向到期撤单' WHERE id=?", (now, o["id"]))
        self.event("系统状态", "意向到期", "未触发的意向已过期，未成交委托已撤")

    def close_day(self) -> None:
        self.reconcile()
        acc = self.account()
        snap = self.db.one("SELECT equity FROM equity_snap WHERE trade_date=?", (self.day,))
        pnl = acc.equity - (snap["equity"] if snap else acc.equity)
        self.event("系统状态", f"收盘 · 当日盈亏 {pnl:+.2f}", f"权益 {acc.equity:.2f} · LLM 花费 {self.llm.spent_today():.3f} 元")

    def reconcile(self) -> None:
        local = {p["symbol"]: p["qty"] for p in self.positions()}
        counter = {s: p.qty for s, p in self.broker.positions().items() if p.qty}
        diffs = [f"{s}: 本地 {local.get(s, 0)} / 柜台 {counter.get(s, 0)}"
                 for s in set(local) | set(counter) if local.get(s, 0) != counter.get(s, 0)]
        cash_diff = round(self.cash - self.broker.asset()["cash"], 2)
        if abs(cash_diff) > 1:
            diffs.append(f"现金差异 {cash_diff}")
        self.recon = {"status": "对账一致" if not diffs else "对账差异", "diffs": diffs, "ts": self.clock.hhmm}
        if diffs:
            self.risk.halted_reason = "对账差异，暂停新开仓"
            self.event("对账", "对账发现差异，已熔断", "；".join(diffs), "error")

    # ================================================================ 人工干预
    def liquidate_all(self) -> int:
        n = 0
        for o in self.db.query("SELECT * FROM orders WHERE status='已报'"):
            if self.broker.cancel(o["broker_id"]):
                self.db.execute("UPDATE orders SET status='已撤', note='一键清仓撤单' WHERE id=?", (o["id"],))
        for h in self.holdings_ctx():
            q = self.market.quote(h["symbol"])
            if q and h["sellable"] > 0:
                n += self.submit_order(None, q, "sell", round(max(q.price * 0.98, q.limit_down), 2), h["sellable"], "一键清仓")
        self.event("人工干预", "一键清仓", f"提交 {n} 笔卖单", "warn")
        return n

    def set_risk_cfg(self, d: dict) -> None:
        self.cfg = RiskConfig.from_dict({**self.cfg.to_dict(), **d})
        self.risk.cfg = self.cfg
        self.db.set_kv("risk_cfg", self.cfg.to_dict())
        self.event("审计", f"发布风控版本 {self.cfg.version}", _dumps(d))

    # ================================================================ 视图
    def pipeline(self) -> list[dict]:
        f, hhmm = self.flags, self.clock.hhmm
        rounds = self.db.one("SELECT COUNT(*) n FROM rounds WHERE trade_date=?", (self.day,))["n"]
        n_orders = self.db.one("SELECT COUNT(*) n FROM orders WHERE trade_date=?", (self.day,))["n"]
        review = sum(1 for h in self.holdings_ctx() if h["must_review"])

        def st(done, running=False):
            return "已完成" if done else "运行中" if running else "待命"

        return [
            {"key": "intel", "title": "五源情报采集", "desc": "事件面情报登记与采集", "time": "00:00-09:15",
             "status": st(f.get("intel"))},
            {"key": "funnel", "title": "两级选票漏斗", "desc": "预筛池冻结 → LLM 深度分析", "time": "09:15",
             "status": st(f.get("funnel"))},
            {"key": "intent", "title": "交易意向发布", "desc": f"已推演 {rounds} 轮", "time": "盘前+滚动",
             "status": st(f.get("closed"), rounds > 0)},
            {"key": "exec", "title": "区间执行器", "desc": f"今日委托 {n_orders} 笔", "time": "09:30-14:55",
             "status": st(hhmm >= "14:55", "09:30" <= hhmm < "14:55")},
            {"key": "review", "title": "T+3 强制复评", "desc": f"待复评 {review} 只", "time": "T+3 滚动",
             "status": "运行中" if review else "待命"},
            {"key": "risk", "title": "预算与风控台账", "desc": f"LLM 剩余 ¥{self.llm.budget_left():.2f}", "time": "全天",
             "status": "熔断" if self.risk.halted_reason else "运行中"},
        ]

    def state(self) -> dict:
        qs, td = self.market.quotes(), self.clock.trade_date
        acc = self.account()
        snap = self.db.one("SELECT equity FROM equity_snap WHERE trade_date=?", (td,))
        base = snap["equity"] if snap else acc.equity
        held_val = sum(p["qty"] * (qs[p["symbol"]].price if p["symbol"] in qs else p["cost"]) for p in self.positions())
        positions = []
        for p in self.positions():
            q = qs.get(p["symbol"])
            price = q.price if q else p["cost"]
            positions.append({**p, "price": price, "chg_pct": q.chg_pct if q else 0, "board": board(p["symbol"]),
                              "value": round(p["qty"] * price, 2),
                              "day_pnl": round(p["qty"] * (price - (q.pre_close if q and p["open_date"] != td else p["cost"])), 2),
                              "float_pnl": round(p["qty"] * (price - p["cost"]), 2),
                              "float_pct": round((price / p["cost"] - 1) * 100, 2),
                              "days_held": trade_days_between(p["open_date"], td)})
        br = self.market.breadth()
        rounds = self.db.query("SELECT * FROM rounds WHERE trade_date=? ORDER BY id DESC LIMIT 30", (td,))
        pool = self.db.query("SELECT * FROM pool WHERE trade_date=? ORDER BY stage DESC, score DESC", (td,))
        for p in pool:
            p["factors"] = _loads(p["factors"])
            q = qs.get(p["symbol"])
            if q:
                p["factors"] = self.factor_scores(q, self.db.query(
                    "SELECT * FROM intel WHERE trade_date=? AND symbol=?", (td, p["symbol"])), p["score"])
                p["price"], p["chg_pct"], p["industry"] = q.price, q.chg_pct, q.industry
        intents = self.db.query("SELECT * FROM intents WHERE trade_date=? AND status IN ('已发布','执行中','已成交','风控拒绝')"
                                " ORDER BY id DESC LIMIT 30", (td,))
        for it in intents:
            it["exchange"] = exchange(it["symbol"])
        return {
            "clock": {"now": self.clock.now().strftime("%Y-%m-%d %H:%M:%S"), "date": td, "phase": self.clock.phase,
                      "simulated": self.clock.simulated, "speed": self.clock.speed},
            "mode": self.s.mode, "broker": self.broker.name, "strategy": self.s.strategy_id, "account": self.s.account_name,
            "llm": {"provider": self.llm.provider, "model": self.s.llm_model, "spent": round(self.llm.spent_today(), 4),
                    "budget": self.s.llm_daily_budget_cny, "interval": self.s.inference_interval_min},
            "risk_state": {"paused": self.risk.paused, "read_only": self.risk.read_only, "halted": self.risk.halted_reason},
            "risk_cfg": self.cfg.to_dict(),
            "asset": {"equity": round(acc.equity, 2), "cash": round(self.cash, 2), "available": round(acc.cash, 2),
                      "position_value": round(held_val, 2), "day_pnl": round(acc.equity - base, 2),
                      "day_pnl_pct": round((acc.equity / base - 1) * 100, 2) if base else 0, "base": round(base, 2),
                      "bought_today": round(acc.bought_today, 2), "orders_today": acc.orders_today},
            "counter": self.broker.asset(), "recon": self.recon,
            "breadth": br.to_dict(), "pipeline": self.pipeline(), "positions": positions,
            "closed": self.db.query("SELECT * FROM closed_positions ORDER BY id DESC LIMIT 20"),
            "orders": self.db.query("SELECT * FROM orders WHERE trade_date=? ORDER BY id DESC", (td,)),
            "fills": self.db.query("SELECT * FROM fills WHERE trade_date=? ORDER BY id DESC", (td,)),
            "intents": intents, "rounds": rounds, "pool": pool,
            "events": self.db.query("SELECT * FROM events ORDER BY id DESC LIMIT 60"),
            "intel": self.db.query("SELECT * FROM intel WHERE trade_date=? ORDER BY ABS(sentiment) DESC LIMIT 15", (td,)),
        }


# ==================================================================== Mock LLM（规则引擎）
def mock_inference(ctx: dict, cfg: RiskConfig) -> dict:
    """没有 API Key 时的确定性"LLM"：输出与真实 LLM 相同的 JSON 结构，保证系统可离线演示。"""
    br, guard = ctx["breadth"], ctx["guard"]
    r = br["up_ratio"]
    inds = "、".join(ctx["top_industries"])
    sent = br["sentiment"]
    bias = -1 if r < 0.4 else 1 if r > 0.6 else 0
    mood = {"panic_bearish": "呈现极端泥沙俱下恐慌格局", "weak_bearish": "整体偏弱，赚钱效应差",
            "neutral_divergent": "震荡分化，结构性行情明显", "mild_bullish": "普涨偏暖", "strong_bullish": "普涨偏强格局"}[sent]
    macro = (f"全市场上涨{br['up']}家占比{r:.1%}（{br['up']}/{br['total']}），主板上涨比例{br['main_up_ratio']:.1%}，"
             f"涨停{br['limit_up']}家/跌停{br['limit_down']}家，情绪{sent}，{mood}。{inds}等板块领涨。")
    if guard["panic"]:
        macro += "触发大盘极端弱势防守铁律，必须空仓避险。"
    intents, rej_txt = [], []
    if guard["panic"]:
        rej_txt.append(f"大盘呈现极端泥沙俱下恐慌行情（上涨比例 {r:.1%} < {cfg.panic_up_ratio:.0%}），触发【大盘极端弱势防守铁律】："
                       "严禁任何逆势开仓或抄底行为，强制空仓避险以保全本金安全。")
    else:
        for c in ctx["rejected_candidates"][:6]:
            rej_txt.append(f"{c['name']}({c['symbol']}){'、'.join(c['reasons'])}")
        budget = ctx["budget"]
        slots = max(0, min(budget["seats_left"], 2 if bias >= 0 else 1))
        for c in sorted(ctx["eligible_candidates"], key=lambda x: x["llm_score"], reverse=True):
            if slots <= 0 or c["llm_score"] < 55:
                if c["llm_score"] < 55:
                    rej_txt.append(f"{c['name']}({c['symbol']})综合置信{c['llm_score']:.0f}不足")
                continue
            hi = round(min(c["price"] * 1.003, c["vwap"] * 1.008), 2)
            lo = round(c["vwap"] * 0.985, 2)
            qty = int(min(cfg.order_amount_limit, budget["daily_buy_left"], budget["cash"]) // hi // 100 * 100)
            if qty < 100:
                continue
            intents.append({"symbol": c["symbol"], "action": "buy", "price_low": lo, "price_high": hi, "target_qty": qty,
                            "confidence": round(min(0.95, c["llm_score"] / 100 + 0.1), 2),
                            "reason": f"{c['industry']}主线({c['industry_rank']})，涨幅{c['chg_pct']:+.2f}%处黄金低吸区，"
                                      f"偏离VWAP {c['vwap_dev']:+.2f}%；{c['thesis']}"})
            slots -= 1
        if not intents:
            rej_txt.append("候选池无完全契合黄金低吸区间且兼具主线动能的标的，宁缺毋滥，坚决不买。")
    review = []
    for h in ctx["holdings"]:
        if h["must_review"]:
            act = "sell" if h["pnl_pct"] < 2 or guard["panic"] else "hold"
            why = f"持满T+{h['days_held']}强制复评，浮盈{h['pnl_pct']:+.2f}%，" + ("动能不足，卖出了结" if act == "sell" else "趋势延续，继续持有")
        else:
            act = "sell" if guard["panic"] and h["pnl_pct"] < 0 and h["sellable"] else "hold"
            why = (f"T+1当日买入不可卖，" if h["sellable"] == 0 else "") + \
                  f"持仓{h['name']}浮动{h['pnl_pct']:+.2f}%，现价{h['price']}" + \
                  ("处于VWAP上方，缩量震荡走势稳健" if h["price"] >= h["vwap"] else "略低于VWAP，观察承接")
        intents.append({"symbol": h["symbol"], "action": act, "target_qty": h["sellable"] if act == "sell" else 0,
                        "confidence": 0.75 if act == "hold" else 0.8, "reason": why})
        review.append(f"{h['symbol']}{'继续持有' if act == 'hold' else '卖出'}")
    hr = ("持仓" + "；".join(review) + "。") if review else "本轮 review_set 为空，无持仓需要处理。"
    return {"macro": {"summary": macro, "sentiment": sent, "position_bias": bias},
            "rejections": "；".join(rej_txt) or "无", "holdings_review": hr, "intents": intents}


def _dumps(x) -> str:
    import json
    return json.dumps(x, ensure_ascii=False, default=str)


def _loads(x):
    import json
    return json.loads(x) if x else {}
