"""LLM 连通性自检：python -m backend.check_llm（在 quant_cockpit 目录下）

1. 打印当前 LLM 配置（密钥打码）
2. 发一个最小 JSON 请求，确认网络 / 密钥 / 余额
3. 在临时数据库里跑模拟盘到 10:00，用真实 LLM 完成「漏斗分析 + 一轮盘中推演」，
   打印 LLM 原始输出与代码校验后的意向。任一环节降级为规则引擎则以非 0 退出。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import httpx

from .config import settings


def main() -> int:
    s = settings
    key = s.llm_api_key
    print(f"provider={s.llm_provider} base_url={s.llm_base_url} model={s.llm_model} "
          f"key={'未设置' if not key else key[:5] + '…' + key[-4:]}")
    if s.llm_provider == "mock" or not key:
        print("✗ 请先设置 QC_LLM_PROVIDER=openai 与 QC_LLM_API_KEY（DeepSeek 用 OpenAI 兼容协议）")
        return 2

    # ---- 1. 最小请求
    from .llm import LLMClient

    class _NoDB:  # 自检不记账
        def one(self, *a):
            return {"c": 0}

        def execute(self, *a):
            return 0

    from .clock import Clock

    cli = LLMClient(s, _NoDB(), Clock(simulated=True))
    try:
        text, pt, ct = cli._openai('输出 JSON：{"ok": true}', "你只输出 JSON。") if s.llm_provider != "anthropic" \
            else cli._anthropic('输出 JSON：{"ok": true}', "你只输出 JSON。")
        print(f"✓ 连通：{text.strip()[:80]}  (tokens {pt}+{ct})")
    except httpx.HTTPStatusError as e:
        print(f"✗ HTTP {e.response.status_code}: {e.response.text[:300]}")
        print("  401=密钥错误 402=余额不足 429=限流")
        return 1
    except Exception as e:
        print(f"✗ 网络错误：{e!r}")
        return 1

    # ---- 2. 真实推演
    from .engine import Engine

    s.db_path = os.path.join(tempfile.mkdtemp(), "check.db")
    e = Engine(s)
    print("… 运行模拟盘至 10:00（期间会调用 LLM 做漏斗分析和盘前推演，可能需要 1-2 分钟）")
    while e.clock.hhmm < "10:00":
        e.tick(1)
    rounds = e.db.query("SELECT * FROM rounds ORDER BY id")
    pool_ev = e.db.one("SELECT detail FROM events WHERE title='预筛池已冻结'")
    print(f"\n[漏斗] {pool_ev and pool_ev['detail']}")
    fallback = False
    for r in rounds:
        raw = json.loads(r["raw"])
        print(f"\n[第 {r['round_no']} 轮 {r['ts']} · {r['provider']}]")
        if raw.get("_error"):
            print("  ✗", raw["_error"])
            fallback = True
        print("  宏观：", r["macro"])
        print("  归因：", r["rejections"])
        print("  持仓：", r["holdings_review"])
        print("  LLM 原始意向：", json.dumps(raw.get("intents", []), ensure_ascii=False))
    print("\n[校验后发布的意向]")
    for it in e.db.query("SELECT symbol,name,action,status,price_low,price_high,target_qty,confidence,reason FROM intents"):
        print("  ", it)
    for ev in e.db.query("SELECT title, detail FROM events WHERE category IN ('风控','委托','成交') ORDER BY id"):
        print("  ", ev["title"], "·", ev["detail"])
    print(f"\nLLM 花费（按 QC_LLM_PRICE_1K 估算）：¥{e.llm.spent_today():.4f}")
    fallback = fallback or any(r["provider"].startswith("mock") for r in rounds)
    print("✗ 有轮次降级为规则引擎" if fallback else "✓ 全部轮次由真实 LLM 完成")
    return 1 if fallback else 0


if __name__ == "__main__":
    sys.exit(main())
