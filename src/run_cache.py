"""
RunCache —— 每次运行隔离的缓存目录：cache/<run_id>/

存放：
  - requirement.json     本次需求
  - goal_tree.json       目标树实时快照（每次 tree 事件覆盖写）
  - events.jsonl         事件流（tree/message/log/eval/ui，追加）
  - run_full.json        运行结束时的完整目标树（含各节点 transcript/历史/评审）
  - 以及 agent 通过 cache_save 自行写入的过程文件与结果（如 day1.md、result.md）
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class RunCache:
    def __init__(self, base: str, run_id: str):
        self.run_id = run_id
        self.dir = (Path(base) / run_id).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)

    def _safe(self, name: str) -> Path:
        name = re.sub(r"[^0-9A-Za-z._\-/一-鿿]", "_", name or "").lstrip("/")
        if not name:
            raise ValueError("文件名为空")
        p = (self.dir / name).resolve()
        if not str(p).startswith(str(self.dir)):
            raise ValueError("非法路径（越界）")
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def save(self, name: str, content: str) -> str:
        p = self._safe(name)
        p.write_text(content if isinstance(content, str) else str(content), encoding="utf-8")
        return str(p.relative_to(self.dir)).replace("\\", "/")

    def read(self, name: str) -> str | None:
        p = self._safe(name)
        return p.read_text(encoding="utf-8") if p.exists() else None

    def list_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.dir)).replace("\\", "/")
            for p in self.dir.rglob("*")
            if p.is_file()
        )

    def save_json(self, name: str, obj: Any) -> str:
        return self.save(name, json.dumps(obj, ensure_ascii=False, indent=2))

    def append_jsonl(self, name: str, obj: Any) -> None:
        p = self._safe(name)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
