"""FastAPI 服务：后台循环驱动引擎，前端轮询 /api/state。

启动：uvicorn backend.app:app --port 8000   （在 quant_cockpit 目录下）
"""
from __future__ import annotations

import asyncio
import contextlib
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import ROOT, settings
from .engine import Engine

engine = Engine(settings)
lock = threading.RLock()


async def _loop():
    while True:
        await asyncio.sleep(settings.tick_seconds)
        try:
            with lock:
                await asyncio.to_thread(engine.tick, settings.tick_seconds)
        except Exception as e:  # 引擎异常只记录，不让循环退出
            engine.event("系统", "引擎异常", repr(e), "error")


@contextlib.asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_loop())
    yield
    task.cancel()


app = FastAPI(title="量化交易驾驶舱", lifespan=lifespan)


@app.get("/api/state")
def state():
    with lock:
        return engine.state()


class Control(BaseModel):
    action: str
    value: float | bool | None = None


@app.post("/api/control")
def control(c: Control):
    with lock:
        e = engine
        if c.action == "pause":
            e.risk.paused = not e.risk.paused
            e.event("人工干预", "暂停调度" if e.risk.paused else "恢复调度")
        elif c.action == "readonly":
            e.risk.read_only = not e.risk.read_only
            e.event("人工干预", "只读防误触 " + ("开启" if e.risk.read_only else "关闭"))
        elif c.action == "liquidate":
            return {"ok": True, "orders": e.liquidate_all()}
        elif c.action == "infer":
            e.run_inference(reason="手动")
        elif c.action == "unhalt":
            e.risk.halted_reason = ""
            e.event("人工干预", "解除熔断")
        elif c.action == "next_day":
            if not e.clock.simulated:
                raise HTTPException(400, "仅模拟模式可用")
            if not e.flags.get("closed"):
                e.expire_intents()
                e.close_day()
            e.clock.next_day()
        elif c.action == "speed":
            e.clock.speed = float(c.value or 60)
        elif c.action == "reset":
            e.db.reset()
            engine.__init__(settings)
        else:
            raise HTTPException(400, f"未知操作 {c.action}")
    return {"ok": True}


@app.post("/api/risk")
def update_risk(d: dict):
    with lock:
        engine.set_risk_cfg(d)
        return engine.cfg.to_dict()


FRONT = ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONT), name="static")


@app.get("/")
def index():
    return FileResponse(FRONT / "index.html")
