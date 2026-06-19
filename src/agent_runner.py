"""
agent_runner — 用 Claude Agent SDK 驱动目标树 Agent。

复用 claude_code_oauth.py 的 OAuth 模式（空串顶掉 API key + setting_sources=[]），
让 SDK 子进程走 claude CLI 的 OAuth 登录态（Max 订阅额度）。

7 个 goal 工具注册为进程内 MCP server，闭包同一个 GoalTree 实例。
消息绑定（I3）在 receive_response 消费循环里完成。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 python-dotenv 可选
    pass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from claude_code_oauth import create_claude_code_oauth_llm_call
from evaluator import evaluate_node
from goal_tree import GoalTree
from run_cache import RunCache
from ui_bridge import UIBridge

_OAUTH_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": "", "ANTHROPIC_AUTH_TOKEN": ""}

# 独立评审模型（与执行 agent 相互独立的另一个大模型调用）。可在 .env 里用 JUDGE_MODEL 配置。
AGENT_MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-5")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "claude-opus-4-1")

SYSTEM_PROMPT = """你是一个基于「目标树」工作的规划-执行 Agent。你必须严格遵守以下工作方式：

1. 接到需求后，第一步永远是调用 goal_init 建立根目标（只调用一次）。
2. 对当前焦点节点二选一：
   (a) 若它足够小、可一步做完 → 走下面【节点完成的标准流程】把它做完并通过评估后 goal_close；
   (b) 若它仍然偏大 → 调用 goal_plan 拆成有序子目标（系统会自动把你带到第一个子目标）。
3. 你永远只在「当前焦点叶子」上工作，不要跳着做。focus 的切换由系统在 plan/close/retry 后自动完成。
4. goal_close 的 summary 必须自包含（写清结论/产出），能结构化的产出放进 artifacts。
5. 鼓励多层规划：当一个子目标内部还包含多个可独立推进的部分时，主动再 goal_plan 向下细分（例如：行程 → 逐天 → 每天的时段），形成 2~3 层的清晰结构，让每个叶子足够具体、单一、可一步完成。但一般不要超过 3 层（根节点为第 1 层）；不要为拆而拆，叶子约对应 1~3 步可完成的工作。
6. 每次行动前，系统会在上下文里提醒你「当前在哪个节点、它的评价指标、历史尝试」。请始终围绕该节点工作。
7. 当 goal_close 返回「全部目标已完成」时，输出最终交付物的完整内容，然后停止。

【节点完成的标准流程（本系统的核心：评价门控 + 重试/重规划 + 历史）】
每个叶子节点都必须满足「需求」且「评价分数达标」才能关闭，否则要重做或重规划：
  A. 设标准：先用 goal_set_criteria 给该节点补全 requirements(硬性需求) + metrics(评价指标) + pass_threshold(及格线，如0.8)。
     - metrics 每条含 key/name/layer(规则|常识|体验)/judge_method/pass_criteria/weight。
     - 规则层=确定性硬判定（预算/必去/忌口/无障碍/时间冲突等）；常识层=空间时间合理性等(LLM+rubric)；体验层=主题契合/吸引力(LLM+引用证据)。
  B. 执行：完成该节点的工作（用文字产出结果）。
  C. 评估（关键：你不能自己打分）：评分一律由一个【独立评审大模型】自动完成，它与你相互独立、只看指标与产出。
     - 你【绝不要】自己给分数，也没有传分数的入口。
     - 每次 goal_close 都会【自动触发】一次独立评审来打分并决定能否关闭；你也可以在关闭前先用 goal_eval 主动触发一次评审预览。
  D. 门控：只有独立评审判定通过，goal_close 才会成功。未通过时（系统会返回评审意见与未过指标）：
     - 小问题可修 → goal_retry(写本次尝试摘要与失败原因)，系统会把这次尝试归档进历史并清空工作记录，你在同一节点重做；
     - 方法/拆分有问题，或同一节点已失败≥3次 → goal_plan(replace=true) 重新规划该节点的子结构。
  E. 历史：每次 retry/replan 都会把上一次尝试归档；可视化会展示每个节点的尝试历史与独立评审的评分轨迹。

【短途旅行规划：用技能驱动「评价指标」与「需求采集」】
本项目有一个 req-eval 技能（位于 req-eval/ 目录），它是一套「需求→评价指标」映射库：
  1. 任务开始时用 Read 读取 `req-eval/SKILL.md` 了解流程，并读取 `req-eval/mappings.json`（评价指标库，含三层指标）。
  2. 采集需求（铁律，禁止跳过用户）：
     - 对「收集需求」节点，必须先 goal_set_criteria 设 requires_user_input=true（系统会硬门控：没有真实用户提交就不允许 goal_close）。
     - 必须调用 ui_render(await_result=true) 把问卷渲染到前端，等待用户在界面上**真实提交**。绝不允许"我已经从需求里知道了，直接预填一份需求表跳过用户"——那样 goal_close 会被系统拒绝。
     - 允许（且推荐）把用户已说过的信息作为问卷的**预填默认值**，让用户确认/微调后再提交，提升体验；但提交这一步必须由用户完成。
     - 预填做法：用 html 参数生成一份带默认值的问卷 HTML（表单提交时执行 window.parent.postMessage({type:'trip-req-eval:submit', payload:{...}}, '*')）；或直接用 url="/req-eval/questionnaire.html"。
     - 用户真实提交后，ui_render 会返回《旅行需求表》JSON——以这份为准（不是你预设的那份），它会被登记为该节点的真实用户输入。
  3. 设指标：对每个规划节点（如逐天行程、横向校验），按本次《需求表》的字段去 mappings.json 里匹配相关指标，
     用 goal_set_criteria 填入该节点的 metrics（这就是"补全完成需求/评价指标"的来源）。
  4. 之后严格走上面的 A~E 标准流程：执行→goal_eval→达标才 goal_close，不达标就 retry/replan。

【运行缓存】你有一个本次运行专属的缓存目录 cache/<run_id>/，用 cache_save(name, content) 写过程文件与结果、cache_read(name) 取回、cache_list() 查看。
- 建议：每个关键节点完成时，把它的产出存一份（如 day1.md、budget.json）；最终把完整行程单存为 result.md。
- 系统也会自动把目标树快照(goal_tree.json)与事件流(events.jsonl)写进该目录，无需你管。

【在前端渲染交互界面】你可以调用 ui_render 工具，在用户的前端界面里嵌入一个 iframe 交互界面：
- 传 html=「一整段自包含的 HTML」→ 以 iframe srcdoc 渲染（适合你自己生成的界面，如自定义问卷）；
- 或传 url=「同源相对路径」→ 以 iframe src 渲染（适合加载项目里已有的 HTML 文件）；
- await_result=true 时，工具会【阻塞】，直到用户在界面上提交，并把用户提交的结构化数据(JSON)作为工具结果返回给你。
- 若用 html 自定义界面，请在表单提交时执行：window.parent.postMessage({type:'trip-req-eval:submit', payload:{...}}, '*')，payload 即结构化结果。

【常驻「Agent 画布」实时预览】前端有一个常驻面板，你可以用 ui_canvas 工具往里渲染/更新可视化内容（非阻塞、立即返回、自动刷新）：
- 与 ui_render 的区别：ui_render 是弹出式、阻塞、用于收集用户输入；ui_canvas 是常驻面板、不阻塞、不收集输入，专门用于"实时给用户看进度"。
- 建议用法：每完成一个关键节点（如某天行程确定、评估通过、最终行程单成形）就调用 ui_canvas 渲染最新成果，让用户实时看到产物逐步成形。可反复调用刷新。
- 例如把"逐步成形的行程单 + 当前各节点完成度"渲染成一个好看的 HTML 页面推到画布。

【短途旅行规划：先用问卷采集需求】项目里有一个采集旅行需求的技能：
1. 先用 Read 读取 `req-eval/SKILL.md` 了解流程（其第 5 节说明把 `req-eval/questionnaire.html` 作为 iframe 嵌入采集需求）。
2. 在「收集需求」这个目标节点上，调用 ui_render(url="/req-eval/questionnaire.html", title="旅行需求问卷", await_result=true) 把问卷渲染到前端；
   用户填写并点「生成需求表」后，你会收到《旅行需求表》JSON。
3. 把这份需求表写进该节点的 artifacts，作为后续逐天规划的依据；其余子目标据此展开。
（如果你更想自定义问卷，也可以自行生成 HTML 用 html 参数渲染，但首选复用技能里的 questionnaire.html。）

注意：本环境没有联网/外部数据。凡是依赖时效性的事实（价格、营业时间、班次等）都基于你的常识推理，并把不确定项显式登记为「假设，需出行前核实」。"""

ALLOWED_TOOLS = [
    "mcp__goal__goal_init",
    "mcp__goal__goal_plan",
    "mcp__goal__goal_set_criteria",
    "mcp__goal__goal_focus",
    "mcp__goal__goal_update",
    "mcp__goal__goal_eval",
    "mcp__goal__goal_retry",
    "mcp__goal__goal_close",
    "mcp__goal__goal_summarize",
    "mcp__goal__goal_tree",
    "mcp__ui__ui_render",
    "mcp__ui__ui_canvas",
    "mcp__cache__cache_save",
    "mcp__cache__cache_read",
    "mcp__cache__cache_list",
    "Read",
]


def build_goal_server(tree: GoalTree, judge_llm: Callable, log: Callable[[str], None]):
    @tool(
        "goal_init",
        "初始化根目标节点；整个任务只调用一次。返回当前焦点上下文。",
        {"title": str, "description": str, "acceptance": str},
    )
    async def goal_init(args: dict) -> dict:
        tree.create_root(args["title"], args.get("description", ""), args.get("acceptance"))
        return _ctx(tree)

    @tool(
        "goal_plan",
        "在当前(或指定)节点下规划一组有序子目标，并自动聚焦到第一个子目标。subgoals 为对象数组，每个可含 title/description/acceptance/requirements/metrics/pass_threshold。replace=true 时先作废现有未完成子节点并把本次尝试归档为重规划（用于该目标失败后重做结构）。",
        {"subgoals": list, "parent_id": str, "replace": bool},
    )
    async def goal_plan(args: dict) -> dict:
        parent = args.get("parent_id") or tree.focused_id
        tree.add_children(parent, args["subgoals"], replace=bool(args.get("replace", False)))
        leaf = tree.first_actionable_leaf(parent)
        if leaf:
            tree.focus(leaf.id)
        return _ctx(tree)

    @tool(
        "goal_set_criteria",
        "为当前(或指定)节点补全完成标准：requirements(硬性需求字符串数组) + metrics(评价指标数组，每条含 key/name/layer(规则|常识|体验)/judge_method/pass_criteria/weight) + pass_threshold(0~1 及格线) + requires_user_input(布尔，true 表示该节点必须真的收到用户通过 ui_render 的提交才能关闭，用于需求采集类节点)。执行该节点前应先设定。",
        {"node_id": str, "requirements": list, "metrics": list, "pass_threshold": float, "requires_user_input": bool},
    )
    async def goal_set_criteria(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        tree.set_criteria(
            nid, args.get("requirements"), args.get("metrics"),
            args.get("pass_threshold"), args.get("requires_user_input"),
        )
        return _ctx(tree)

    @tool(
        "goal_eval",
        "触发【独立评审模型】对当前(或指定)节点的产出自动打分（你不要、也无法自己打分——评分一律由独立模型给出）。返回评审结果与是否通过门控。仅用于在 close 前预览；goal_close 也会自动评审。",
        {"node_id": str},
    )
    async def goal_eval(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        res = await evaluate_node(tree, nid, judge_llm, JUDGE_MODEL, log)
        verdict = "✅ 独立评审通过，可以 goal_close。" if res["passed"] else (
            f"❌ 独立评审未通过（overall={res['overall']} < 阈值{res['threshold']}，未过指标={res['failed']}）。"
            f"评审意见：{res.get('notes', '')}。请据此 goal_retry 重做，或 goal_plan(replace=true) 重规划。"
        )
        return {"content": [{"type": "text", "text": f"独立评审结果：{verdict}"}]}

    @tool(
        "goal_retry",
        "本节点未达标时调用：把本次尝试摘要并归档进历史，清空工作记录，准备在同一节点重做（焦点不变）。summary=本次尝试做了什么，reason=失败原因/下次改进点。",
        {"node_id": str, "summary": str, "reason": str},
    )
    async def goal_retry(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        tree.retry(nid, args.get("summary", ""), args.get("reason", ""))
        return _ctx(tree)

    @tool("goal_focus", "聚焦到指定节点（通常由系统自动调用，仅在需要跳回时手动用）。", {"node_id": str})
    async def goal_focus(args: dict) -> dict:
        leaf = tree.first_actionable_leaf(args["node_id"])
        if leaf:
            tree.focus(leaf.id)
        return _ctx(tree)

    @tool(
        "goal_update",
        "修改节点的 title/description/acceptance/status/artifacts。status 不能改成 done（请用 goal_close）。",
        {"node_id": str, "title": str, "description": str, "acceptance": str, "status": str, "artifacts": dict},
    )
    async def goal_update(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        tree.update(nid, args)
        return _ctx(tree)

    @tool(
        "goal_close",
        "关闭当前(或指定)节点：写 summary(必填) 与可选 artifacts，并自动聚焦到下一个待办叶子。",
        {"node_id": str, "summary": str, "artifacts": dict},
    )
    async def goal_close(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        node = tree.nodes.get(nid)
        # 每次关闭都自动触发一次独立评审（叶子且已设指标时）。requires_user_input 缺失会在 close 内拦截。
        if node is not None and not node.children and node.metrics:
            if node.requires_user_input and not node.user_submitted:
                return {"content": [{"type": "text", "text":
                    "⛔ 无法关闭：本节点要求真实用户输入，请先用 ui_render(await_result=true) 让用户提交，再关闭。"}]}
            res = await evaluate_node(tree, nid, judge_llm, JUDGE_MODEL, log)
            if not res["passed"]:
                return {"content": [{"type": "text", "text":
                    f"⛔ 无法关闭：独立评审未通过（overall={res['overall']} < 阈值{res['threshold']}，"
                    f"未过指标={res['failed']}）。评审意见：{res.get('notes', '')}。\n"
                    "请 goal_retry 重做本节点，或 goal_plan(replace=true) 重规划其子结构。"}]}
        try:
            tree.close(nid, args.get("summary", ""), args.get("artifacts"))
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"⛔ 无法关闭（门控）：{e}"}]}
        nxt = tree.advance_focus(nid)
        if nxt is None:
            return {
                "content": [
                    {"type": "text", "text": "✅ 全部目标已完成。请现在输出最终交付物的完整内容。"}
                ]
            }
        tree.focus(nxt)
        return _ctx(tree)

    @tool("goal_summarize", "为节点生成/刷新摘要：mode=leaf 压缩本节点，mode=subtree 聚合子树。", {"node_id": str, "mode": str})
    async def goal_summarize(args: dict) -> dict:
        nid = args.get("node_id") or tree.focused_id
        text = tree.summarize(nid, args.get("mode", "leaf"))
        return {"content": [{"type": "text", "text": f"[{nid}] 摘要：{text}"}]}

    @tool("goal_tree", "查看当前目标树全貌（焦点高亮，已完成节点只显示标题+摘要）。", {})
    async def goal_tree(args: dict) -> dict:
        return {"content": [{"type": "text", "text": tree.render_focus_context()}]}

    return create_sdk_mcp_server(
        name="goal",
        version="1.0.0",
        tools=[
            goal_init, goal_plan, goal_set_criteria, goal_focus, goal_update,
            goal_eval, goal_retry, goal_close, goal_summarize, goal_tree,
        ],
    )


def build_ui_server(bridge: UIBridge, tree: GoalTree):
    @tool(
        "ui_render",
        "在用户前端界面里嵌入一个 iframe 交互界面。传 html(整段自包含 HTML, 以 srcdoc 渲染) 或 url(同源相对路径, 以 src 渲染); await_result=true 时阻塞等待用户真实提交并返回其结构化数据(JSON)。成功提交会被登记为当前节点的「真实用户输入」。",
        {"html": str, "url": str, "title": str, "height": int, "await_result": bool},
    )
    async def ui_render(args: dict) -> dict:
        target = tree.focused_id  # 提交将登记到此刻的焦点节点
        payload = await bridge.show(
            html=args.get("html") or None,
            url=args.get("url") or None,
            title=args.get("title", ""),
            height=int(args.get("height") or 660),
            await_result=bool(args.get("await_result", True)),
        )
        if payload is None:
            return {"content": [{"type": "text", "text": "✅ 界面已在前端渲染（未等待用户提交）。"}]}
        if isinstance(payload, dict) and payload.get("__timeout__"):
            return {"content": [{"type": "text", "text": "⏳ 用户未在限定时间内提交，可稍后重试或改用文本采集。"}]}
        # 登记真实用户提交 → 满足该节点的 requires_user_input 硬门控
        if target:
            tree.record_user_input(target, payload)
        return {
            "content": [
                {"type": "text", "text": "用户已真实提交，结构化结果如下（JSON）。这才是采集到的需求，请以此为准：\n"
                                          + json.dumps(payload, ensure_ascii=False, indent=2)}
            ]
        }

    @tool(
        "ui_canvas",
        "在前端常驻的「Agent 画布」面板里渲染/更新一块可视化内容（非阻塞，立即返回，前端自动刷新）。传 html(整段自包含 HTML) 或 url(同源相对路径)。可反复调用以持续编辑/刷新，用于实时预览正在构建的产物（如逐步成形的行程单、当前目标进度图）。不会等待用户、也不收集输入——需要用户提交时请改用 ui_render。",
        {"html": str, "url": str, "title": str},
    )
    async def ui_canvas(args: dict) -> dict:
        bridge.canvas(html=args.get("html") or None, url=args.get("url") or None, title=args.get("title", ""))
        return {"content": [{"type": "text", "text": "✅ 已更新「Agent 画布」（前端已自动刷新）。"}]}

    return create_sdk_mcp_server(name="ui", version="1.0.0", tools=[ui_render, ui_canvas])


def build_cache_server(cache: RunCache):
    @tool(
        "cache_save",
        "把过程文件或结果写入本次运行的缓存目录(cache/<run_id>/)。name=相对文件名(如 day1.md / result.md / req.json)，content=文本内容。返回保存的相对路径。",
        {"name": str, "content": str},
    )
    async def cache_save(args: dict) -> dict:
        try:
            rel = cache.save(args["name"], args.get("content", ""))
            return {"content": [{"type": "text", "text": f"✅ 已保存到缓存：{rel}"}]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"❌ 保存失败：{e}"}]}

    @tool("cache_read", "读取本次运行缓存目录里的某个文件。", {"name": str})
    async def cache_read(args: dict) -> dict:
        text = cache.read(args.get("name", ""))
        if text is None:
            return {"content": [{"type": "text", "text": "（文件不存在）"}]}
        return {"content": [{"type": "text", "text": text}]}

    @tool("cache_list", "列出本次运行缓存目录里的所有文件。", {})
    async def cache_list(args: dict) -> dict:
        files = cache.list_files()
        return {"content": [{"type": "text", "text": "缓存文件：\n" + ("\n".join(files) or "（空）")}]}

    return create_sdk_mcp_server(name="cache", version="1.0.0", tools=[cache_save, cache_read, cache_list])


def _ctx(tree: GoalTree) -> dict:
    return {"content": [{"type": "text", "text": tree.render_focus_context()}]}


def _build_options(
    tree: GoalTree, bridge: UIBridge, cache: RunCache, judge_llm: Callable,
    log: Callable[[str], None], stderr_sink: Callable[[str], None],
) -> ClaudeAgentOptions:
    async def inject_focus_context(input_data, tool_use_id, context):
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": tree.render_focus_context(),
            }
        }

    return ClaudeAgentOptions(
        model=AGENT_MODEL,
        # 让 agent 的 Read('req-eval/...') 等相对路径在 src/ 下解析（req-eval 随代码一起归档到 src/）
        cwd=str(Path(__file__).parent),
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={
            "goal": build_goal_server(tree, judge_llm, log),
            "ui": build_ui_server(bridge, tree),
            "cache": build_cache_server(cache),
        },
        allowed_tools=ALLOWED_TOOLS,
        hooks={"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[inject_focus_context])]},
        env=_OAUTH_ENV,
        setting_sources=[],
        permission_mode="bypassPermissions",
        max_turns=120,
        stderr=stderr_sink,
    )


async def run_agent(
    requirement: str,
    tree: GoalTree,
    bridge: UIBridge,
    cache: RunCache,
    log: Callable[[str], None],
    max_continuations: int = 8,
) -> None:
    """运行 Agent 直至目标树完成或达到续跑上限。所有进展通过 tree.on_event 广播。"""
    stderr_lines: list[str] = []

    def _stderr(line: str) -> None:
        if line:
            stderr_lines.append(line)

    judge_llm = create_claude_code_oauth_llm_call(JUDGE_MODEL)
    options = _build_options(tree, bridge, cache, judge_llm, log, _stderr)
    log(f"启动 Agent（OAuth/claude CLI）… 执行模型 {options.model}；独立评审模型 {JUDGE_MODEL}；缓存目录 cache/{cache.run_id}")

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(requirement)
            await _consume(client, tree, log)

            cont = 0
            while not tree.is_complete() and cont < max_continuations:
                cont += 1
                log(f"目标树尚未完成，续跑第 {cont} 轮…")
                await client.query("请聚焦到当前节点并继续推进，直到全部目标完成。")
                await _consume(client, tree, log)

        if tree.is_complete():
            log("🎉 目标树全部完成。")
        else:
            log("⏹ Agent 结束（目标树未完全闭合，可重试或人工接管）。")
    except Exception as e:  # noqa: BLE001
        tail = "\n".join(stderr_lines[-15:])
        log(f"❌ 运行出错：{type(e).__name__}: {e}\n--- CLI stderr ---\n{tail}")
        raise


async def _consume(client: ClaudeSDKClient, tree: GoalTree, log: Callable[[str], None]) -> None:
    """消费一轮流式响应；I3：把每条 assistant 消息绑定到当时的焦点叶子。"""
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ThinkingBlock):
                    continue
                if isinstance(block, TextBlock):
                    if block.text.strip():
                        tree.emit_message(tree.focused_id, "assistant", "text", block.text)
                elif isinstance(block, ToolUseBlock):
                    name = block.name.replace("mcp__goal__", "")
                    tree.emit_message(
                        tree.focused_id, "assistant", "tool_use", f"🔧 {name}({_short(block.input)})"
                    )
        elif isinstance(msg, ResultMessage):
            u = msg.usage or {}
            log(
                f"· 本轮结束 turns={msg.num_turns} "
                f"in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)}"
            )
        elif isinstance(msg, SystemMessage):
            pass


def _short(obj: Any, limit: int = 160) -> str:
    s = str(obj)
    return s if len(s) <= limit else s[:limit] + "…"
