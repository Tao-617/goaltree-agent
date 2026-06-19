"""
UIBridge — 让 Agent 在前端 iframe 中渲染交互界面，并阻塞等待用户回传结果。

流程：
  1. Agent 调用 ui_render 工具 → bridge.show() 发布 {type:"ui_render", request_id, html|url}
  2. 前端在 iframe 里渲染该界面（html→srcdoc，url→src）
  3. 用户在 iframe 内提交 → iframe 通过 postMessage 把结构化数据发给父窗口
  4. 前端把数据经 WebSocket 回传 {action:"ui_result", request_id, payload}
  5. 服务端 bridge.resolve(request_id, payload) 唤醒第 1 步阻塞的 Future
  6. ui_render 工具把 payload 作为工具结果返回给 Agent（人在环闭环）
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional


class UIBridge:
    def __init__(self, publish: Callable[[dict], None]):
        self.publish = publish
        self.pending: dict[str, asyncio.Future] = {}
        self.counter = 0

    async def show(
        self,
        *,
        html: Optional[str] = None,
        url: Optional[str] = None,
        title: str = "",
        height: int = 660,
        await_result: bool = True,
        timeout: float = 900.0,
    ) -> Optional[Any]:
        self.counter += 1
        rid = f"ui-{self.counter}"
        self.publish(
            {
                "type": "ui_render",
                "request_id": rid,
                "html": html,
                "url": url,
                "title": title or "交互界面",
                "height": height,
                "await_result": await_result,
            }
        )
        if not await_result:
            return None
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[rid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return {"__timeout__": True, "request_id": rid}
        finally:
            self.pending.pop(rid, None)
            # 通知前端可以收起该界面
            self.publish({"type": "ui_close", "request_id": rid})

    def resolve(self, request_id: str, payload: Any) -> None:
        fut = self.pending.get(request_id)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def canvas(self, *, html: Optional[str] = None, url: Optional[str] = None, title: str = "") -> None:
        """非阻塞：把内容推到前端常驻的「Agent 画布」面板，前端会自动刷新。可反复调用以"编辑"。"""
        self.publish(
            {"type": "ui_canvas", "html": html, "url": url, "title": title or "Agent 画布"}
        )
