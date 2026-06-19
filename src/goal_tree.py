"""
GoalTree — 目标树核心数据结构与控制流（带"评价门控 + 重试/重规划 + 历史"机制）。

核心机制：
  - 每个叶子节点都有 requirements(硬性需求) + metrics(评价指标) + pass_threshold(及格线)。
  - 执行后必须 goal_eval 自评打分；只有评估 passed 才允许 goal_close（门控）。
  - 未达标 → goal_retry(摘要本次尝试并归档历史后重做) 或 goal_plan(replace) 重规划。
  - 每个节点累积 attempts 历史，供可视化展示。

不变量：
  I1 单焦点  I2 焦点是叶子  I3 消息归属焦点叶子  I4 close 即自动转焦点
  I5 叶子节点未通过评估不可关闭（父节点级联关闭不需评估，由子节点评估背书）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

STATUSES = {"pending", "active", "done", "blocked", "abandoned"}
OPEN_STATUSES = {"pending", "active", "blocked"}

MAX_PASS_THRESHOLD = 0.85  # 及格线上限：防止 agent 自设过高(0.9/0.95)导致几乎不可能通过


def _clamp_threshold(v: float) -> float:
    return max(0.5, min(MAX_PASS_THRESHOLD, float(v)))


@dataclass
class GoalNode:
    id: str
    parent_id: Optional[str]
    title: str
    description: str = ""
    acceptance: Optional[str] = None
    status: str = "pending"
    children: list[str] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    summary: Optional[str] = None
    artifacts: dict[str, Any] = field(default_factory=dict)
    # —— 评价门控相关 ——
    requirements: list[str] = field(default_factory=list)          # 硬性需求
    metrics: list[dict[str, Any]] = field(default_factory=list)    # 评价指标
    pass_threshold: float = 0.8                                    # 及格线
    current_eval: Optional[dict[str, Any]] = None                  # 最近一次评估
    attempts: list[dict[str, Any]] = field(default_factory=list)   # 历史尝试
    attempt_no: int = 1                                            # 当前是第几次尝试
    # —— 真实用户输入门控 ——
    requires_user_input: bool = False                             # 是否必须有真实用户提交才能关闭
    user_submitted: bool = False                                  # 是否已收到过真实用户提交
    user_inputs: Optional[dict[str, Any]] = None                  # 用户实际提交的数据
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class GoalTree:
    def __init__(self, on_event: Optional[Callable[[dict], None]] = None):
        self.nodes: dict[str, GoalNode] = {}
        self.root_id: Optional[str] = None
        self.focused_id: Optional[str] = None
        self.on_event = on_event

    # ---------------------------------------------------------------- events
    def _emit(self, kind: str, detail: str = "") -> None:
        if self.on_event:
            self.on_event(
                {
                    "type": "tree",
                    "kind": kind,
                    "detail": detail,
                    "focused_id": self.focused_id,
                    "root_id": self.root_id,
                    "snapshot": self.snapshot(),
                }
            )

    def emit_message(self, node_id: Optional[str], role: str, kind: str, text: str) -> None:
        nid = node_id or self.focused_id
        if nid and nid in self.nodes:
            self.nodes[nid].transcript.append(
                {"role": role, "kind": kind, "text": text, "ts": time.time()}
            )
            self.nodes[nid].updated_at = time.time()
        if self.on_event:
            self.on_event(
                {"type": "message", "node_id": nid, "role": role, "kind": kind, "text": text}
            )

    # ---------------------------------------------------------------- writes
    def create_root(self, title: str, description: str = "", acceptance: Optional[str] = None) -> GoalNode:
        if self.nodes:
            raise ValueError("根节点已存在")
        n = GoalNode("1", None, title, description, acceptance, status="active")
        self.nodes["1"] = n
        self.root_id = "1"
        self.focused_id = "1"
        self._emit("init", f"建立根目标：{title}")
        return n

    def add_children(self, parent_id: str, subgoals: list[dict], replace: bool = False) -> list[GoalNode]:
        parent = self._require(parent_id)
        if replace:
            # 重规划：把现有未完成子节点作废，并把父节点本次尝试归档为 "replanned"
            for cid in list(parent.children):
                if self.nodes[cid].status in OPEN_STATUSES:
                    self.nodes[cid].status = "abandoned"
            self._archive_attempt(parent, outcome="replanned", summary="重新规划该目标的子结构")
        base = len(parent.children)
        created: list[GoalNode] = []
        for i, sg in enumerate(subgoals, start=base + 1):
            cid = f"{parent_id}.{i}"
            n = GoalNode(
                cid,
                parent_id,
                sg.get("title", f"子目标{i}"),
                sg.get("description", ""),
                sg.get("acceptance"),
                requirements=list(sg.get("requirements", []) or []),
                metrics=list(sg.get("metrics", []) or []),
                pass_threshold=_clamp_threshold(sg.get("pass_threshold", 0.8) or 0.8),
            )
            self.nodes[cid] = n
            parent.children.append(cid)
            created.append(n)
        if parent.status == "active":
            parent.status = "pending"
        parent.updated_at = time.time()
        self._emit("plan", f"在 {parent_id} 下规划 {len(created)} 个子目标")
        return created

    def focus(self, node_id: str) -> GoalNode:
        target = self._require(node_id)
        if self.focused_id and self.focused_id in self.nodes:
            cur = self.nodes[self.focused_id]
            if cur.status == "active":
                cur.status = "pending"
        self.focused_id = node_id
        target.status = "active"
        target.updated_at = time.time()
        self._emit("focus", f"聚焦到 {node_id}：{target.title}")
        return target

    def update(self, node_id: str, fields: dict) -> GoalNode:
        n = self._require(node_id)
        for k in ("title", "description", "acceptance"):
            if fields.get(k) is not None:
                setattr(n, k, fields[k])
        if isinstance(fields.get("artifacts"), dict):
            n.artifacts.update(fields["artifacts"])
        if fields.get("status") in (STATUSES - {"done"}):
            n.status = fields["status"]
        n.updated_at = time.time()
        self._emit("update", f"修改 {node_id}")
        return n

    def set_criteria(
        self,
        node_id: str,
        requirements: Optional[list] = None,
        metrics: Optional[list] = None,
        pass_threshold: Optional[float] = None,
        requires_user_input: Optional[bool] = None,
    ) -> GoalNode:
        n = self._require(node_id)
        if requirements is not None:
            n.requirements = list(requirements)
        if metrics is not None:
            n.metrics = list(metrics)
        if pass_threshold is not None:
            n.pass_threshold = _clamp_threshold(pass_threshold)
        if requires_user_input is not None:
            n.requires_user_input = bool(requires_user_input)
        n.updated_at = time.time()
        extra = "（需真实用户输入）" if n.requires_user_input else ""
        self._emit("criteria", f"为 {node_id} 设定 {len(n.metrics)} 项评价指标，及格线 {n.pass_threshold}{extra}")
        return n

    def record_user_input(self, node_id: str, payload: Any) -> GoalNode:
        """登记一次真实的用户提交（由 ui_render 成功 round-trip 后调用）。"""
        n = self._require(node_id)
        n.user_submitted = True
        n.user_inputs = payload
        if isinstance(payload, dict):
            n.artifacts["user_inputs"] = payload
        n.updated_at = time.time()
        self._emit("user_input", f"{node_id} 收到真实用户提交")
        return n

    def record_eval(
        self, node_id: str, scores: list[dict], overall: float, notes: str = "",
        judged_by: str = "",
    ) -> dict:
        """记录一次（独立评审模型给出的）评估。scores: [{key,name,score,verdict,evidence}]。"""
        n = self._require(node_id)
        metric_layer = {m.get("key") or m.get("name"): m.get("layer", "") for m in n.metrics}
        passed = overall >= n.pass_threshold
        hard_fail = []
        for s in scores:
            key = s.get("key") or s.get("name")
            if s.get("verdict") == "fail":
                if metric_layer.get(key) == "规则":  # 规则层任一 fail => 硬不通过
                    passed = False
                    hard_fail.append(key)
        failed = [s.get("key") or s.get("name") for s in scores if s.get("verdict") == "fail"]
        n.current_eval = {
            "scores": scores,
            "overall": round(float(overall), 3),
            "threshold": n.pass_threshold,
            "passed": passed,
            "failed": failed,
            "hard_fail": hard_fail,
            "notes": notes,
            "judged_by": judged_by,
            "attempt_no": n.attempt_no,
            "ts": time.time(),
        }
        n.updated_at = time.time()
        self._emit(
            "eval",
            f"评估 {node_id}：overall={overall:.2f} 阈值={n.pass_threshold} → {'通过' if passed else '未通过'}",
        )
        return {"passed": passed, "overall": overall, "threshold": n.pass_threshold, "failed": failed, "notes": notes}

    def _archive_attempt(self, n: GoalNode, outcome: str, summary: str = "", reason: str = "") -> None:
        ev = n.current_eval or {}
        produced = "\n".join(e["text"] for e in n.transcript if e.get("kind") == "text")[:2000]
        n.attempts.append(
            {
                "n": n.attempt_no,
                "summary": summary or ev.get("notes", ""),
                "produced": produced,         # 保留本次尝试的产出文字，retry 清空 transcript 后仍可追溯
                "overall": ev.get("overall"),
                "threshold": ev.get("threshold", n.pass_threshold),
                "passed": ev.get("passed", outcome == "passed"),
                "failed": ev.get("failed", []),
                "outcome": outcome,           # passed | failed | replanned
                "reason": reason,
                "ts": time.time(),
            }
        )
        n.attempt_no += 1
        n.current_eval = None

    def retry(self, node_id: str, summary: str = "", reason: str = "") -> GoalNode:
        """摘要并归档本次尝试到历史，清空工作记录，准备重做（焦点保持在该节点）。"""
        n = self._require(node_id)
        self._archive_attempt(n, outcome="failed", summary=summary, reason=reason)
        n.transcript = []  # 摘要后清空，重新执行（上下文压缩）
        n.status = "active"
        n.updated_at = time.time()
        self._emit("retry", f"归档 {node_id} 第 {n.attempt_no - 1} 次尝试并重做")
        return n

    def can_close(self, node_id: str) -> tuple[bool, str]:
        n = self._require(node_id)
        if n.children:  # 内部节点：由子节点背书
            unfinished = [c for c in n.children if self.nodes[c].status in OPEN_STATUSES]
            return (not unfinished, f"存在未完成子节点：{unfinished}" if unfinished else "")
        # 硬门控：声明需要真实用户输入的节点，必须真的收到过用户提交，不能用 agent 自行假设的数据关闭
        if n.requires_user_input and not n.user_submitted:
            return False, (
                "本节点要求真实用户输入：必须先用 ui_render(await_result=true) 让用户在问卷里确认/提交，"
                "不能用你自己假设/预填的数据直接关闭。请渲染问卷（可带预填默认值）并等待用户提交。"
            )
        ev = n.current_eval
        if not n.metrics:
            return True, ""  # 未设指标的叶子（如纯采集类）放行
        if not ev:
            return False, "尚未评估：请先用 goal_eval 对本节点产出打分。"
        if not ev.get("passed"):
            return False, (
                f"评估未达标（overall={ev.get('overall')} < 阈值{ev.get('threshold')}，"
                f"未通过指标={ev.get('failed')}）。请 goal_retry 重做或 goal_plan(replace) 重规划。"
            )
        return True, ""

    def close(self, node_id: str, summary: str, artifacts: Optional[dict] = None, force: bool = False) -> GoalNode:
        n = self._require(node_id)
        if not force:
            ok, reason = self.can_close(node_id)
            if not ok:
                raise ValueError(reason)
        # 归档通过的这次尝试到历史（让时间线含最终成功项）
        if n.current_eval and not n.children:
            self._archive_attempt(n, outcome="passed", summary=summary)
        n.status = "done"
        n.summary = summary
        if artifacts:
            n.artifacts.update(artifacts)
        n.updated_at = time.time()
        self._emit("close", f"关闭 {node_id}：{summary[:40]}")
        return n

    # ---------------------------------------------------------- focus advance
    def first_actionable_leaf(self, node_id: str) -> Optional[GoalNode]:
        n = self.nodes.get(node_id)
        if n is None or n.status in ("done", "abandoned"):
            return None
        kids = [c for c in n.children if self.nodes[c].status not in ("done", "abandoned")]
        if not kids:
            return n
        return self.first_actionable_leaf(kids[0])

    def advance_focus(self, just_closed_id: str) -> Optional[str]:
        cur = self.nodes[just_closed_id]
        while cur.parent_id is not None:
            parent = self.nodes[cur.parent_id]
            sibs = parent.children
            idx = sibs.index(cur.id)
            for sid in sibs[idx + 1:]:
                leaf = self.first_actionable_leaf(sid)
                if leaf is not None:
                    return leaf.id
            agg = self._aggregate_summary(parent.id)
            parent.status = "done"
            parent.summary = agg
            parent.updated_at = time.time()
            self._emit("close", f"级联关闭 {parent.id}")
            cur = parent
        return None

    # ---------------------------------------------------------------- summary
    def _aggregate_summary(self, node_id: str) -> str:
        kids = self.nodes[node_id].children
        parts = [self.nodes[c].summary or self.nodes[c].title for c in kids if self.nodes[c].status == "done"]
        return "；".join(p for p in parts if p)

    def summarize(self, node_id: str, mode: str = "leaf") -> str:
        n = self._require(node_id)
        if mode == "subtree":
            n.summary = self._aggregate_summary(node_id)
        else:
            texts = [e["text"] for e in n.transcript if e.get("kind") == "text"]
            n.summary = (texts[-1][:300] if texts else n.title)
        n.updated_at = time.time()
        return n.summary

    # ----------------------------------------------------------------- render
    def is_complete(self) -> bool:
        return self.root_id is not None and self.nodes[self.root_id].status == "done"

    def path_to(self, node_id: str) -> list[GoalNode]:
        path: list[GoalNode] = []
        cur = self.nodes.get(node_id)
        while cur is not None:
            path.append(cur)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(path))

    def render_focus_context(self) -> str:
        if self.focused_id is None:
            return "（目标树尚未初始化，请先调用 goal_init。）"
        node = self.nodes[self.focused_id]
        lines = ["=== 当前目标树焦点上下文 ==="]
        path = self.path_to(self.focused_id)
        lines.append("焦点路径：")
        for d, n in enumerate(path):
            mark = "👉" if n.id == self.focused_id else "  "
            lines.append(f"{'  ' * d}{mark} [{n.id}] {n.title}  ({n.status})")
        lines.append("")
        lines.append(f"★ 当前焦点节点 [{node.id}]：{node.title}（第 {node.attempt_no} 次尝试）")
        if node.description:
            lines.append(f"  说明：{node.description}")
        if node.requires_user_input:
            state = "✅ 已收到用户提交" if node.user_submitted else "⛔ 尚未收到用户提交（必须先 ui_render 让用户提交，否则无法关闭本节点）"
            lines.append(f"  【需真实用户输入】{state}")
        # 完成标准 + 评价指标（门控）
        if node.requirements:
            lines.append("  硬性需求(requirements)：")
            for r in node.requirements:
                lines.append(f"    - {r}")
        if node.metrics:
            lines.append(f"  评价指标(metrics)，及格线 overall≥{node.pass_threshold}：")
            for m in node.metrics:
                lines.append(
                    f"    · [{m.get('layer', '?')}] {m.get('name', m.get('key', '?'))} —— "
                    f"判定:{m.get('judge_method', '?')}；通过标准:{m.get('pass_criteria', '?')}"
                )
        else:
            lines.append("  ⚠ 本节点尚未设定评价指标：请先 goal_set_criteria 补全 requirements/metrics/pass_threshold。")
        if node.current_eval:
            ev = node.current_eval
            lines.append(f"  最近评估：overall={ev['overall']} → {'✅通过' if ev['passed'] else '❌未通过'}"
                         + (f"，未过指标={ev['failed']}" if ev["failed"] else ""))
        if node.attempts:
            hist = "，".join(f"#{a['n']}:{a['outcome']}({a.get('overall')})" for a in node.attempts)
            lines.append(f"  历史尝试：{hist}")
            if node.attempt_no >= 4:
                lines.append("  ⚠ 该节点已多次失败，建议用 goal_plan(replace=true) 重规划，而非继续重试。")
        # 兄弟摘要
        if node.parent_id:
            parent = self.nodes[node.parent_id]
            done_sibs = [self.nodes[s] for s in parent.children if self.nodes[s].status == "done"]
            todo_sibs = [self.nodes[s] for s in parent.children
                         if self.nodes[s].status in OPEN_STATUSES and s != node.id]
            if done_sibs:
                lines.append("  已完成的兄弟（仅摘要）：")
                for s in done_sibs:
                    lines.append(f"    ✔ [{s.id}] {s.title} → {s.summary or '(无摘要)'}")
            if todo_sibs:
                lines.append("  后续待办兄弟：" + "，".join(f"[{s.id}]{s.title}" for s in todo_sibs))
        lines.append("===========================")
        return "\n".join(lines)

    # ------------------------------------------------------------- serialize
    def snapshot(self) -> dict:
        return {
            "root_id": self.root_id,
            "focused_id": self.focused_id,
            "nodes": {
                k: {
                    "id": v.id,
                    "parent_id": v.parent_id,
                    "title": v.title,
                    "description": v.description,
                    "status": v.status,
                    "children": list(v.children),
                    "summary": v.summary,
                    "artifacts": v.artifacts,
                    "requirements": v.requirements,
                    "metrics": v.metrics,
                    "pass_threshold": v.pass_threshold,
                    "current_eval": v.current_eval,
                    "attempts": v.attempts,
                    "attempt_no": v.attempt_no,
                    "requires_user_input": v.requires_user_input,
                    "user_submitted": v.user_submitted,
                    "msg_count": len(v.transcript),
                    "last_msg": (v.transcript[-1]["text"][:120] if v.transcript else None),
                }
                for k, v in self.nodes.items()
            },
        }

    def export(self) -> dict:
        """完整导出（含各节点 transcript / 历史 / 评审），用于运行结束归档。"""
        return {
            "root_id": self.root_id,
            "focused_id": self.focused_id,
            "complete": self.is_complete(),
            "nodes": {
                k: {
                    "id": v.id, "parent_id": v.parent_id, "title": v.title,
                    "description": v.description, "acceptance": v.acceptance, "status": v.status,
                    "children": list(v.children), "summary": v.summary, "artifacts": v.artifacts,
                    "requirements": v.requirements, "metrics": v.metrics,
                    "pass_threshold": v.pass_threshold, "current_eval": v.current_eval,
                    "attempts": v.attempts, "attempt_no": v.attempt_no,
                    "requires_user_input": v.requires_user_input, "user_submitted": v.user_submitted,
                    "user_inputs": v.user_inputs, "transcript": v.transcript,
                }
                for k, v in self.nodes.items()
            },
        }

    def _require(self, node_id: str) -> GoalNode:
        if node_id not in self.nodes:
            raise ValueError(f"节点不存在：{node_id}")
        return self.nodes[node_id]
