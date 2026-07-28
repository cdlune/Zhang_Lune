#!/usr/bin/env python3
"""A 股多因子分析 / 选股 命令行入口。

数据源可选：
    --source eastmoney  东方财富公开接口（默认，全市场一次抓取，无需 licence）
    --source biying     必盈API（需 --licence 或环境变量 BIYING_LICENCE；实时按单只查询）

选股风格（--style）：
    balanced      均衡多因子（默认）：估值/质量/成长权重较高，偏中长线
    short_term    短线交易：看重强势/放量/活跃度，弱化估值与基本面，
                  动量改用 5/10 日短周期，波动与回撤风险显著更高

用法示例：
    python main.py select                                  # 东财，全市场多因子选股
    python main.py select --style short_term                # 短线选股
    python main.py select --final 20 --no-fund             # 关闭财务抓取（更快）
    python main.py select --source biying --licence XXXX --max-symbols 800
    python main.py analyze 600519 000001                   # 分析指定个股（东财）
    python main.py analyze 600519 --style short_term        # 按短线口径分析个股
    python main.py analyze 600519 --source biying --licence XXXX

依赖：requests, pandas, numpy（见 requirements.txt）。
注意：licence 为私密凭证，请勿写入代码库；建议用环境变量 BIYING_LICENCE 传入。
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from astock import report
from astock.config import DEFAULT_CONFIG, Config
from astock import datasource as ds
from astock import biyingapi
from astock import factors, indicators, screener


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _build_config(args) -> Config:
    base = Config.short_term() if getattr(args, "style", "balanced") == "short_term" else DEFAULT_CONFIG
    cfg = base.copy_with(
        final_n=getattr(args, "final", base.final_n),
        top_n_candidates=getattr(args, "candidates", base.top_n_candidates),
        use_fundamentals=not getattr(args, "no_fund", False),
    )
    if getattr(args, "small_cap", False):
        cfg = cfg.copy_with(min_mktcap_yi=20.0, max_mktcap_yi=200.0, max_pe=60.0)
    if getattr(args, "exclude_star", False):
        cfg = cfg.copy_with(exclude_star=True)
    return cfg


def _build_source(args):
    """按 --source 构造数据源对象。biying 需要 licence。"""
    if args.source == "biying":
        return biyingapi.BiyingSource(
            licence=args.licence, max_symbols=args.max_symbols, log=_log)
    return ds  # eastmoney：模块本身即数据源对象


def _err_hint(source_name: str) -> None:
    if source_name == "biying":
        _log("       请检查 licence 是否有效/未超配额，且网络可访问 api.biyingapi.com。")
    else:
        _log("       请在可访问东方财富接口的网络环境重试（本沙箱可能被网络策略拦截）。")


def cmd_select(args) -> int:
    cfg = _build_config(args)
    try:
        source = _build_source(args)
        ranked = screener.select(cfg, log=_log, source=source)
    except (ds.DataSourceError,) as exc:
        _log(f"[错误] 数据抓取失败：{exc}")
        _err_hint(args.source)
        return 2
    print(report.full_report(ranked, cfg))
    if args.csv and not ranked.empty:
        ranked.to_csv(args.csv, index=False, encoding="utf-8-sig")
        _log(f"已导出 CSV：{args.csv}")
    return 0


def _snapshot_row(args, source, code):
    """取单只个股的快照 dict（东财走一次全市场缓存，必盈走实时接口）。"""
    if args.source == "biying":
        return biyingapi.realtime(code, args.licence)
    board = _snapshot_row._board
    if board is None:
        board = source.spot_board()
        _snapshot_row._board = board
    snap = board[board["code"] == code]
    return snap.iloc[0].to_dict() if not snap.empty else None


_snapshot_row._board = None


def cmd_analyze(args) -> int:
    cfg = _build_config(args)
    short = cfg.style == "short_term"
    try:
        source = _build_source(args)
    except ds.DataSourceError as exc:
        _log(f"[错误] {exc}")
        return 2
    rows = []
    for code in args.codes:
        code = str(code).zfill(6)
        try:
            row = _snapshot_row(args, source, code)
        except ds.DataSourceError as exc:
            _log(f"[错误] 快照抓取失败：{exc}")
            _err_hint(args.source)
            return 2
        if not row:
            _log(f"[跳过] 未找到或无数据：{code}")
            continue
        kdf = source.daily_kline(code, days=cfg.kline_days)
        if kdf is not None and len(kdf) >= 20:
            kdf = indicators.enrich(kdf)
            last = kdf.iloc[-1]
            row["ret5"], row["ret10"] = last["ret5"], last["ret10"]
            row["ret20"], row["ret60"] = last["ret20"], last["ret60"]
            row["technical"] = factors.technical_score_from_kline(kdf, short_term=short)
        fin = source.main_indicators(code)
        if fin:
            row.update(fin)
        rows.append(row)

    if not rows:
        _log("无可分析标的")
        return 1
    df = pd.DataFrame(rows)
    has_fund = any(c in df.columns for c in ("roe", "rev_yoy"))
    weights = cfg.redistributed_weights(has_fund)
    scored = factors.composite(df, weights, short_term=short)
    print(report.selection_table(scored))
    print()
    for _, r in scored.iterrows():
        print(report.stock_detail(r, short_term=short))
        print()
    return 0


def _add_source_args(sp):
    sp.add_argument("--source", choices=["eastmoney", "biying"], default="eastmoney",
                    help="数据源（默认 eastmoney）")
    sp.add_argument("--licence", type=str, default=None,
                    help="必盈 licence（也可用环境变量 BIYING_LICENCE）")
    sp.add_argument("--max-symbols", type=int, default=None,
                    help="必盈全市场抓取上限（逐只请求，避免超配额）")
    sp.add_argument("--style", choices=["balanced", "short_term"], default="balanced",
                    help="选股风格：balanced 均衡（默认）/ short_term 短线")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="A 股多因子分析与选股")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("select", help="全市场多因子选股")
    ps.add_argument("--final", type=int, default=DEFAULT_CONFIG.final_n, help="最终输出数量")
    ps.add_argument("--candidates", type=int, default=DEFAULT_CONFIG.top_n_candidates,
                    help="进入深度打分的候选数量")
    ps.add_argument("--no-fund", action="store_true", help="关闭财务指标抓取（更快）")
    ps.add_argument("--small-cap", action="store_true", help="偏中小盘参数")
    ps.add_argument("--exclude-star", action="store_true", help="剔除科创板/创业板")
    ps.add_argument("--csv", type=str, default=None, help="导出结果 CSV 路径")
    _add_source_args(ps)
    ps.set_defaults(func=cmd_select)

    pa = sub.add_parser("analyze", help="分析指定个股")
    pa.add_argument("codes", nargs="+", help="股票代码，如 600519 000001")
    _add_source_args(pa)
    pa.set_defaults(func=cmd_analyze)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
