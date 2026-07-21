"""选股结果 / 个股分析报告的文本渲染。"""

from __future__ import annotations

import pandas as pd


def _fmt(v, suffix: str = "", nd: int = 2) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    try:
        return f"{float(v):.{nd}f}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def selection_table(ranked: pd.DataFrame) -> str:
    """把最终选股结果渲染成对齐的文本表格。"""
    if ranked.empty:
        return "（无符合条件的标的）"
    header = (f"{'排名':<4}{'代码':<8}{'名称':<10}{'综合分':>7}"
              f"{'估值':>6}{'动量':>6}{'质量':>6}{'成长':>6}{'技术':>6}"
              f"{'现价':>9}{'PE':>8}{'PB':>7}{'市值(亿)':>10}")
    lines = [header, "-" * len(header)]
    for i, r in ranked.iterrows():
        lines.append(
            f"{i + 1:<4}{str(r.get('code','')):<8}{str(r.get('name','')):<10}"
            f"{_fmt(r.get('score')):>7}"
            f"{_fmt(r.get('f_value'),nd=0):>6}{_fmt(r.get('f_momentum'),nd=0):>6}"
            f"{_fmt(r.get('f_quality'),nd=0):>6}{_fmt(r.get('f_growth'),nd=0):>6}"
            f"{_fmt(r.get('f_technical'),nd=0):>6}"
            f"{_fmt(r.get('price')):>9}{_fmt(r.get('pe_ttm')):>8}"
            f"{_fmt(r.get('pb')):>7}{_fmt(r.get('mktcap'),nd=1):>10}"
        )
    return "\n".join(lines)


def stock_detail(r: pd.Series) -> str:
    """单只个股的分项解读。"""
    L = []
    L.append(f"【{r.get('name','')} {r.get('code','')}】 综合得分 {_fmt(r.get('score'))}")
    L.append(f"  现价 {_fmt(r.get('price'))}  涨跌 {_fmt(r.get('pct'),'%')}  "
             f"换手 {_fmt(r.get('turnover'),'%')}  市值 {_fmt(r.get('mktcap'),'亿',1)}")
    L.append(f"  估值 PE {_fmt(r.get('pe_ttm'))} / PB {_fmt(r.get('pb'))}  "
             f"→ 估值分 {_fmt(r.get('f_value'),nd=0)}")
    L.append(f"  动量 20日 {_fmt(r.get('ret20'),'%')} / 60日 {_fmt(r.get('ret60'),'%')}  "
             f"→ 动量分 {_fmt(r.get('f_momentum'),nd=0)}")
    if pd.notna(r.get("roe")) or pd.notna(r.get("rev_yoy")):
        L.append(f"  质量 ROE {_fmt(r.get('roe'),'%')} / 毛利 {_fmt(r.get('gross_margin'),'%')} "
                 f"/ 负债 {_fmt(r.get('debt_ratio'),'%')}  → 质量分 {_fmt(r.get('f_quality'),nd=0)}")
        L.append(f"  成长 营收同比 {_fmt(r.get('rev_yoy'),'%')} / 净利同比 "
                 f"{_fmt(r.get('profit_yoy'),'%')}  → 成长分 {_fmt(r.get('f_growth'),nd=0)}")
    L.append(f"  技术面综合 → 技术分 {_fmt(r.get('f_technical'),nd=0)}")
    return "\n".join(L)


def full_report(ranked: pd.DataFrame, cfg=None) -> str:
    from datetime import datetime
    parts = ["=" * 96,
             f"A 股多因子选股结果    生成时间 {datetime.now():%Y-%m-%d %H:%M}",
             "=" * 96, "", selection_table(ranked), "",
             "—— 分项解读 " + "—" * 40, ""]
    for _, r in ranked.iterrows():
        parts.append(stock_detail(r))
        parts.append("")
    parts.append("免责声明：本结果由量化模型基于公开数据自动生成，仅供研究参考，"
                 "不构成任何投资建议。投资有风险，入市需谨慎。")
    return "\n".join(parts)
