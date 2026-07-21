#!/usr/bin/env python3
"""A 股多因子分析 / 选股 命令行入口。

用法示例：
    python main.py select                      # 全市场多因子选股，输出前 15
    python main.py select --final 20 --no-fund # 关闭财务抓取（更快），输出前 20
    python main.py select --small-cap          # 偏中小盘参数
    python main.py analyze 600519 000001        # 分析指定个股
    python main.py select --csv out.csv        # 结果同时导出 CSV

注意：需在可访问东方财富行情接口的网络环境运行（部分云沙箱会拦截）。
依赖：requests, pandas, numpy（见 requirements.txt）。
"""

from __future__ import annotations

import argparse
import sys

import pandas as pd

from astock import report
from astock.config import DEFAULT_CONFIG, Config
from astock import datasource as ds
from astock import factors, indicators, screener


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _build_config(args) -> Config:
    cfg = DEFAULT_CONFIG.copy_with(
        final_n=args.final,
        top_n_candidates=args.candidates,
        use_fundamentals=not args.no_fund,
    )
    if args.small_cap:
        cfg = cfg.copy_with(min_mktcap_yi=20.0, max_mktcap_yi=200.0, max_pe=60.0)
    if args.exclude_star:
        cfg = cfg.copy_with(exclude_star=True)
    return cfg


def cmd_select(args) -> int:
    cfg = _build_config(args)
    try:
        ranked = screener.select(cfg, log=_log)
    except ds.DataSourceError as exc:
        _log(f"[错误] 数据抓取失败：{exc}")
        _log("       请在可访问东方财富接口的网络环境重试（本沙箱可能被网络策略拦截）。")
        return 2
    print(report.full_report(ranked, cfg))
    if args.csv and not ranked.empty:
        ranked.to_csv(args.csv, index=False, encoding="utf-8-sig")
        _log(f"已导出 CSV：{args.csv}")
    return 0


def cmd_analyze(args) -> int:
    rows = []
    for code in args.codes:
        code = str(code).zfill(6)
        try:
            board = ds.spot_board()
        except ds.DataSourceError as exc:
            _log(f"[错误] 快照抓取失败：{exc}")
            return 2
        snap = board[board["code"] == code]
        if snap.empty:
            _log(f"[跳过] 未找到 {code}")
            continue
        row = snap.iloc[0].to_dict()
        kdf = ds.daily_kline(code, days=DEFAULT_CONFIG.kline_days)
        if kdf is not None and len(kdf) >= 20:
            kdf = indicators.enrich(kdf)
            last = kdf.iloc[-1]
            row["ret20"], row["ret60"] = last["ret20"], last["ret60"]
            row["technical"] = factors.technical_score_from_kline(kdf)
        fin = ds.main_indicators(code)
        if fin:
            row.update(fin)
        rows.append(row)

    if not rows:
        _log("无可分析标的")
        return 1
    df = pd.DataFrame(rows)
    has_fund = any(c in df.columns for c in ("roe", "rev_yoy"))
    weights = DEFAULT_CONFIG.redistributed_weights(has_fund)
    scored = factors.composite(df, weights)
    print(report.selection_table(scored))
    print()
    for _, r in scored.iterrows():
        print(report.stock_detail(r))
        print()
    return 0


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
    ps.set_defaults(func=cmd_select)

    pa = sub.add_parser("analyze", help="分析指定个股")
    pa.add_argument("codes", nargs="+", help="股票代码，如 600519 000001")
    pa.set_defaults(func=cmd_analyze)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
