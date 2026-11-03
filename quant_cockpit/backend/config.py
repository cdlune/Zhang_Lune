"""全局配置：运行模式、LLM、风控参数（带版本哈希）。

所有参数都可以通过环境变量覆盖，风控参数还可以在运行时通过 API 发布新版本。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("QC_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    # sim = 模拟行情 + 模拟撮合（默认）；paper = 真实行情 + 模拟撮合；live = 真实行情 + 实盘柜台
    mode: str = _env("QC_MODE", "sim")
    market_source: str = _env("QC_MARKET", "sim")  # sim | akshare
    broker: str = _env("QC_BROKER", "paper")  # paper | qmt
    db_path: str = _env("QC_DB", str(DATA_DIR / "cockpit.db"))
    strategy_id: str = _env("QC_STRATEGY", "intel_short_v1")
    account_name: str = _env("QC_ACCOUNT", "DEMO")
    initial_cash: float = float(_env("QC_INITIAL_CASH", "50000"))

    # 调度
    sim_speed: float = float(_env("QC_SIM_SPEED", "60"))  # 模拟时钟倍速：1 真实秒 = 60 模拟秒
    tick_seconds: float = float(_env("QC_TICK", "1"))
    inference_interval_min: int = int(_env("QC_INFER_INTERVAL", "15"))  # 盘中滚动推演节拍

    # LLM：provider = mock | openai | anthropic（openai 兼容协议也覆盖 DeepSeek/Qwen/Kimi 等）
    llm_provider: str = _env("QC_LLM_PROVIDER", "mock")
    llm_base_url: str = _env("QC_LLM_BASE_URL", "https://api.deepseek.com/v1")
    llm_api_key: str = _env("QC_LLM_API_KEY", "")
    llm_model: str = _env("QC_LLM_MODEL", "deepseek-chat")
    llm_daily_budget_cny: float = float(_env("QC_LLM_BUDGET", "20"))
    llm_price_per_1k_tokens: float = float(_env("QC_LLM_PRICE_1K", "0.004"))


@dataclass
class RiskConfig:
    """风控参数。任何修改都会产生新的版本号 cfg-<hash>。"""

    daily_buy_limit: float = 35000.0  # 单日买入上限（元）
    order_amount_limit: float = 10000.0  # 单笔委托上限（元）
    max_total_position: float = 0.95  # 总仓位上限
    max_single_position: float = 0.25  # 单票占比上限
    max_seats: int = 5  # 策略持仓席位
    max_order_qty: int = 2000  # 单笔最大数量（股）
    max_orders_per_day: int = 10  # 单日报单上限
    max_cancels_per_day: int = 10  # 单日撤单上限
    # 选股纪律（代码硬约束，不依赖 LLM 自觉）
    panic_up_ratio: float = 0.25  # 全市场上涨比例低于此值 → 大盘极端弱势防守铁律
    golden_dip_max_chg: float = 3.5  # 黄金低吸区间：涨幅上限 %
    golden_dip_min_chg: float = -3.0  # 涨幅下限 %（防接飞刀）
    max_vwap_deviation: float = 2.0  # 价格偏离 VWAP 上限 %
    mainline_top_ratio: float = 0.30  # 所属行业排名前 30% 才算主线
    min_turnover: float = 5e7  # 最低成交额（元）
    hold_review_days: int = 3  # T+3 强制复评
    stop_loss_pct: float = -6.0  # 硬止损 %
    order_window_start: str = "09:30"
    order_window_end: str = "14:55"
    intent_expire: str = "14:50"
    extra: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return "cfg-" + hashlib.sha1(raw.encode()).hexdigest()[:24]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["version"] = self.version
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in names})


settings = Settings()
