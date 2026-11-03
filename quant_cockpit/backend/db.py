"""SQLite 本地台账：意向、委托、成交、持仓、事件、推演轮次、情报、LLM 账单。"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, category TEXT, level TEXT, title TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS intel (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, ts TEXT, source TEXT, symbol TEXT,
  title TEXT, sentiment REAL, novelty REAL);
CREATE TABLE IF NOT EXISTS pool (
  trade_date TEXT, symbol TEXT, name TEXT, stage INTEGER, score REAL, factors TEXT, note TEXT,
  PRIMARY KEY (trade_date, symbol));
CREATE TABLE IF NOT EXISTS rounds (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, round_no INTEGER, ts TEXT,
  macro TEXT, position_bias INTEGER, sentiment TEXT, rejections TEXT, holdings_review TEXT,
  intents_count INTEGER, guard TEXT, raw TEXT, provider TEXT);
CREATE TABLE IF NOT EXISTS intents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, round_id INTEGER, symbol TEXT, name TEXT,
  action TEXT, status TEXT, price_low REAL, price_high REAL, target_qty INTEGER, amount REAL,
  tier TEXT, confidence REAL, expire TEXT, reason TEXT, created_ts TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, intent_id INTEGER, symbol TEXT, name TEXT,
  side TEXT, price REAL, qty INTEGER, filled_qty INTEGER DEFAULT 0, avg_price REAL DEFAULT 0,
  status TEXT, broker_id TEXT, ts TEXT, updated_ts TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, order_id INTEGER, symbol TEXT, name TEXT,
  side TEXT, price REAL, qty INTEGER, fee REAL, ts TEXT);
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, name TEXT, qty INTEGER, sellable INTEGER, cost REAL,
  open_date TEXT, strategy TEXT, updated_ts TEXT);
CREATE TABLE IF NOT EXISTS closed_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, name TEXT, open_date TEXT, close_date TEXT,
  qty INTEGER, cost REAL, close_price REAL, pnl REAL);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, ts TEXT, purpose TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS equity_snap (trade_date TEXT PRIMARY KEY, equity REAL);
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def get_kv(self, k: str, default: Any = None) -> Any:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def set_kv(self, k: str, v: Any) -> None:
        self.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (k, json.dumps(v, ensure_ascii=False)))

    def reset(self) -> None:
        with self.lock:
            tables = [r["name"] for r in self.query("SELECT name FROM sqlite_master WHERE type='table'")]
            for t in tables:
                if t != "sqlite_sequence":
                    self.conn.execute(f"DELETE FROM {t}")
            self.conn.commit()
