"""
server — FastAPI + WebSocket，实时把目标树状态与 Agent 执行过程推给前端。

启动：  uv run uvicorn server:app --reload --port 8000
然后浏览器打开 http://127.0.0.1:8000
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from agent_runner import run_agent
from goal_tree import GoalTree
from run_cache import RunCache
from ui_bridge import UIBridge

app = FastAPI()
ROOT = Path(__file__).parent
STATIC = ROOT / "static"


class Bus:
    """极简发布订阅：每个 WS 连接一个队列。"""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue] = set()
        self.last_snapshot: dict | None = None
        self.last_canvas: dict | None = None
        self.cache: RunCache | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "tree":
            self.last_snapshot = event
        elif etype == "ui_canvas":
            self.last_canvas = event
        for q in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(event)
        # 持久化到本次运行缓存
        if self.cache is not None:
            try:
                if etype == "tree":
                    self.cache.save_json("goal_tree.json", event.get("snapshot"))
                    self.cache.append_jsonl(
                        "events.jsonl",
                        {"type": "tree", "kind": event.get("kind"), "detail": event.get("detail"),
                         "focused_id": event.get("focused_id")},
                    )
                elif etype in ("message", "log", "ui_render", "ui_canvas", "ui_close", "status"):
                    slim = {k: v for k, v in event.items() if k != "html"}  # 避免把整段 HTML 灌进日志
                    self.cache.append_jsonl("events.jsonl", slim)
            except Exception:  # noqa: BLE001 缓存失败不影响主流程
                pass


bus = Bus()
bridge = UIBridge(publish=bus.publish)


class Runner:
    """保证同一时间只有一个 Agent 在跑。"""

    def __init__(self) -> None:
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, requirement: str) -> bool:
        if self.running:
            return False
        bus.last_snapshot = None
        bus.last_canvas = None
        run_id = datetime.now().strftime("run_%Y%m%d_%H%M%S")
        cache = RunCache("cache", run_id)
        cache.save_json("requirement.json", {"run_id": run_id, "requirement": requirement})
        bus.cache = cache
        tree = GoalTree(on_event=bus.publish)
        bus.publish({"type": "status", "state": "running", "requirement": requirement, "run_id": run_id})

        def log(text: str) -> None:
            bus.publish({"type": "log", "text": text})

        async def _go() -> None:
            try:
                await run_agent(requirement, tree, bridge, cache, log)
            except Exception:  # noqa: BLE001 已在 run_agent 内记录日志
                pass
            finally:
                with contextlib.suppress(Exception):
                    cache.save_json("run_full.json", tree.export())
                bus.publish({"type": "status", "state": "idle"})

        self.task = asyncio.create_task(_go())
        return True


runner = Runner()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.post("/debug/render")
async def debug_render(body: dict) -> dict:
    """仅用于浏览器端到端验证：直接触发一次 ui_render 并阻塞等待前端回传。"""
    payload = await bridge.show(
        html=body.get("html"), url=body.get("url"), title=body.get("title", "DEBUG"),
        await_result=True, timeout=30,
    )
    return {"payload": payload}


@app.post("/debug/canvas")
async def debug_canvas(body: dict) -> dict:
    """仅用于验证常驻画布：非阻塞推送一次画布更新。"""
    bridge.canvas(html=body.get("html"), url=body.get("url"), title=body.get("title", "DEBUG"))
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    q = bus.subscribe()
    # 新连接先补发当前快照与运行状态
    if bus.last_snapshot:
        await websocket.send_json(bus.last_snapshot)
    if bus.last_canvas:
        await websocket.send_json(bus.last_canvas)
    await websocket.send_json({"type": "status", "state": "running" if runner.running else "idle"})

    async def pump() -> None:
        while True:
            event = await q.get()
            await websocket.send_json(event)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            if action == "run":
                req = (data.get("requirement") or "").strip()
                if not req:
                    await websocket.send_json({"type": "log", "text": "⚠ 需求为空。"})
                elif not runner.start(req):
                    await websocket.send_json({"type": "log", "text": "⚠ 已有任务在运行，请稍候。"})
            elif action == "ui_result":
                # 前端把 iframe 内用户提交的结构化数据回传 → 唤醒阻塞的 ui_render 工具
                bridge.resolve(data.get("request_id"), data.get("payload"))
    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        bus.unsubscribe(q)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/req-eval", StaticFiles(directory=ROOT / "req-eval"), name="req-eval")
