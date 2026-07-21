# A 股多因子分析与选股工具（astock）

一套用 Python 编写的 A 股「分析员 + 选股员」工具：拉取全市场行情与财务数据，
计算技术指标、构建多因子打分模型，对全市场股票进行筛选、打分、排名，输出选股清单与个股分项解读。

> 仅依赖 `requests / pandas / numpy`，**不依赖 akshare/tushare**，避免了 akshare 老版本
> `jsonpath` 依赖在部分环境编译失败的问题。数据来自东方财富公开行情接口。

## 特性

- **两阶段选股流水线**：先用一次全市场快照做硬条件初筛与初步打分，再对候选个股抓日线/财务做深度打分，兼顾速度与深度。
- **五维多因子模型**：估值、动量、质量、成长、技术，横截面百分位排名合成综合分（对极端值稳健）。
- **优雅降级**：抓不到财务数据时，自动把质量/成长权重摊到其余因子；抓不到日线时技术分记中性，不造假、不崩溃。
- **完整技术指标库**：MA/EMA、MACD、RSI、KDJ、BOLL、量能、20/60 日动量。
- **个股分析模式**：对指定代码给出分项解读。
- **离线可测**：指标/因子/打分为纯函数，`tests/` 内 7 项单测不依赖网络。

## 安装

```bash
pip install -r requirements.txt
```

## 使用

```bash
# 全市场多因子选股，输出综合分前 15
python main.py select

# 输出前 20，关闭财务抓取（更快）
python main.py select --final 20 --no-fund

# 偏中小盘参数（市值 20~200 亿，PE≤60）
python main.py select --small-cap

# 结果导出 CSV
python main.py select --csv result.csv

# 分析指定个股
python main.py analyze 600519 000001 300750
```

> ⚠️ **网络要求**：需在可访问行情接口的环境运行。部分云沙箱/CI 的网络策略会拦截
> 这些域名（返回 403），此时程序会给出明确提示并退出，请在本地或可访问接口的环境运行。

## 数据源

支持两个可切换的数据源（`--source`）：

| 数据源 | 说明 | licence | 全市场快照 |
| :--- | :--- | :--- | :--- |
| `eastmoney`（默认） | 东方财富公开接口，免费无需凭证 | 不需要 | 一次请求拿到全市场 |
| `biying` | [必盈API](https://www.biyingapi.com/)，需付费 licence | 需要 | 实时按单只查询，逐只请求 |

```bash
# 用必盈数据源选股（licence 建议放环境变量，避免泄露）
export BIYING_LICENCE=你的licence
python main.py select --source biying --max-symbols 800

# 或命令行传入
python main.py analyze 600519 000001 --source biying --licence 你的licence
```

**关于必盈**：其实时接口是「按单只代码」查询，没有一次性全市场快照，因此全市场 `select`
会逐只请求（用 `--max-symbols` 限制数量以免超配额）；`analyze` 指定个股则很轻量、最适合必盈。
已对接的接口：股票列表 `hslt/list`、实时交易 `hsrl/ssjy`、历史日线 `hszbl/fsjy`（前复权）。
若你的套餐文档中 K 线路径/参数不同，只需改 `astock/biyingapi.py` 顶部的
`_KLINE_PATH / _KLINE_LEVEL / _KLINE_FQ` 三个常量。必盈财务指标接口因套餐而异，默认未启用
（质量/成长因子会自动降权），如需可在 `biyingapi.main_indicators` 中按你的字段补齐。

> 🔒 **licence 是私密凭证，不要写入代码库或提交到仓库**，请用环境变量或命令行参数传入。

## 选股方法论

最终综合分为五个子因子的加权和，每个子因子先在候选池内做**横截面百分位排名**归一到 0~100（越高越好）：

| 因子 | 权重 | 口径 | 数据来源 |
| :--- | :---: | :--- | :--- |
| 估值 value | 0.22 | PE(TTM)、PB 越低越好；亏损股 PE 不计入 | 实时快照 |
| 动量 momentum | 0.20 | 20 日、60 日涨幅越高越好 | 日线 / 快照 |
| 质量 quality | 0.23 | ROE、毛利率越高越好，资产负债率越低越好 | 财务主要指标 |
| 成长 growth | 0.20 | 营收、归母净利同比增速越高越好 | 财务主要指标 |
| 技术 technical | 0.15 | 均线多头、MACD、RSI 区间、量能规则打分 | 日线 |

**权重可调**：见 `astock/config.py` 的 `Config.weights`。当某些个股缺失财务数据时，
`redistributed_weights()` 会把质量/成长权重按比例摊到估值/动量/技术上并重新归一。

### 硬条件初筛（可在 `Config` 中调整）

价格区间、总市值区间、换手率区间、PE/PB 上限、剔除 ST/退市、可选剔除科创/创业/北交所。

### 技术面打分规则（0~100，中性 50 起）

- 均线：站上 MA20、MA5>MA20>MA60（多头排列）加分，空头排列减分
- MACD：DIF 在零轴上方、DIF>DEA、红柱走强加分
- RSI：45~68 健康区加分，>80 超买减分，<25 超卖轻微加分
- 量能：近 5 日均量为 20 日均量的 1.0~2.5 倍（温和放量）加分，>4 倍减分

## 项目结构

```
main.py               命令行入口（select / analyze）
astock/
  config.py           选股参数、因子权重、过滤阈值
  datasource.py       东方财富行情/财务数据抓取
  biyingapi.py        必盈API 数据源（drop-in --source biying）
  indicators.py       技术指标计算（纯函数）
  factors.py          因子打分与横截面排名
  screener.py         两阶段选股流水线
  report.py           选股表格与个股解读渲染
tests/test_logic.py   离线单元测试（不联网）
```

## 运行测试

```bash
python tests/test_logic.py        # 或： python -m pytest tests/ -q
```

## 免责声明

本工具由量化模型基于公开数据自动生成结果，**仅供研究与学习参考，不构成任何投资建议**。
市场有风险，据此操作风险自负。
