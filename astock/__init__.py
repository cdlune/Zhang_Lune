"""astock —— A 股多因子分析与选股工具包。

模块划分：
    config      选股参数、因子权重、股票池过滤阈值
    datasource  行情/财务数据抓取（东方财富公开接口，仅依赖 requests）
    indicators  技术指标计算（MA/EMA/MACD/RSI/KDJ/BOLL 等）
    factors     因子打分与横截面排名
    screener    两阶段选股流水线（初筛 + 深度打分）
    report      个股分析报告生成

设计目标：纯逻辑（指标/因子/打分）与数据抓取解耦，便于离线测试。
"""

from .config import Config, DEFAULT_CONFIG

__all__ = ["Config", "DEFAULT_CONFIG"]
__version__ = "0.1.0"
