"""
独立评审（Independent Judge）

评分不由执行任务的 agent 自己做，而是由一个**独立的大模型调用**完成：
该调用没有 agent 的对话上下文，只拿到「评价指标 + 节点产出」，逐项客观打分。
复用 claude_code_oauth.py 的 OAuth llm_call（单发、无状态），可配置评审模型。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

JUDGE_SYSTEM = (
    "你是一个独立、严格、客观的短途旅行规划评审模型。你与执行规划任务的 agent 相互独立，"
    "不偏袒其产出。你的职责：依据给定的【评价指标】，对【待评估产出】逐项打分。"
    "要求：客观苛刻、有据可依；规则层指标按确定性判断（不满足即 fail）；"
    "证据必须引用产出里的具体内容；分数 0~1。"
    "只输出 JSON，不要任何解释性文字或代码块标记。JSON 格式严格如下：\n"
    '{"scores":[{"key":"指标key","name":"指标名","score":0.0,"verdict":"pass|fail",'
    '"evidence":"引用产出中的证据"}],"overall":0.0,"notes":"总体评语与主要扣分点"}'
)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        if text.endswith("```"):
            text = text[:-3]
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1:
        text = text[s : e + 1]
    return json.loads(text)


def _node_output_text(tree, node_id: str, summary: Optional[str] = None) -> str:
    n = tree.nodes[node_id]
    parts: list[str] = []
    if n.description:
        parts.append("目标说明：" + n.description)
    # agent 在 goal_close 时声明的产出/结论（最重要的一手证据）
    declared = summary or n.summary
    if declared:
        parts.append("Agent 声明的产出/结论（summary）：\n" + declared)
    texts = [e["text"] for e in n.transcript if e.get("kind") == "text"]
    if texts:
        parts.append("执行产出（agent 的文字输出）：\n" + "\n".join(texts))
    # 若 transcript 被 retry 清空，则用上次归档的产出兜底
    if not texts and n.attempts:
        last = n.attempts[-1].get("produced")
        if last:
            parts.append("（上次尝试的产出，供参考）：\n" + last)
    artifacts = {k: v for k, v in (n.artifacts or {}).items()}
    canvas = artifacts.pop("_canvas_html", None)
    if artifacts:
        parts.append("结构化产物 artifacts：\n" + json.dumps(artifacts, ensure_ascii=False, indent=2))
    if canvas:
        parts.append("最近一次画布渲染内容（ui_canvas）：\n" + str(canvas)[:4000])
    if n.user_inputs:
        parts.append("用户真实提交：\n" + json.dumps(n.user_inputs, ensure_ascii=False, indent=2))
    return "\n\n".join(parts) or "（本节点暂无产出）"


async def evaluate_node(
    tree,
    node_id: str,
    llm_call: Callable,
    model: str,
    log: Optional[Callable[[str], None]] = None,
    summary: Optional[str] = None,
) -> dict:
    """触发独立评审模型为节点打分，并把结果写回 tree.current_eval。返回门控结果。

    summary：agent 在 goal_close 时声明的产出/结论，作为评审的一手证据一并喂入。
    """
    n = tree.nodes[node_id]
    if not n.metrics:
        return tree.record_eval(node_id, [], 1.0, "无评价指标，跳过评审。", judged_by=f"independent:{model}")

    if log:
        log(f"🧑‍⚖️ 独立评审模型（{model}）正在为 [{node_id}] {n.title} 打分…")

    metrics_desc = json.dumps(n.metrics, ensure_ascii=False, indent=2)
    user = (
        f"目标节点：{n.title}\n"
        f"硬性需求（requirements）：{json.dumps(n.requirements, ensure_ascii=False)}\n\n"
        f"评价指标（metrics，含层级/判定方式/通过标准）：\n{metrics_desc}\n\n"
        f"待评估产出：\n{_node_output_text(tree, node_id, summary)}\n\n"
        f"评分原则：只针对【上述产出是否满足各项指标】打分，不要因为产出文字短/格式不正式而扣分——"
        f"只要内容能满足指标即可通过。请依据每条指标逐项打分，并给出加权 overall（0~1）。只输出规定的 JSON。"
    )

    try:
        resp = await llm_call(
            [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            model=model,
        )
        data = _parse_json(resp.get("content", ""))
        scores = data.get("scores", []) or []
        overall = float(data.get("overall", 0) or 0)
        notes = data.get("notes", "")
    except Exception as e:  # noqa: BLE001 评审失败按"未通过"处理，让 agent 重试
        error_msg = f"{type(e).__name__}: {str(e)}"
        if log:
            log(f"⚠ 独立评审失败：{error_msg}（按未通过处理）")
        return tree.record_eval(
            node_id, [], 0.0, f"Independent review failed: {error_msg}", judged_by=f"independent:{model}"
        )

    res = tree.record_eval(node_id, scores, overall, notes, judged_by=f"independent:{model}")
    if log:
        log(f"🧑‍⚖️ 评审完成 [{node_id}]：overall={res['overall']} → {'通过' if res['passed'] else '未通过'}")
    return res
