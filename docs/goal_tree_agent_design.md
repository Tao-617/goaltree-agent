# 基于目标树（Goal Tree）的规划-执行 Agent 设计文档

> 目标：用 **Claude Agent SDK** 实现一个"先规划、再逐步执行"的 Agent。
> 规划与执行的状态以一棵 **目标树** 承载，树的全部操作收敛为一套 **Goal 工具**。
> LLM 的调用方式复用 `claude_code_oauth.py` 的 OAuth + `claude-agent-sdk` 模式。

---

## 1. 设计动机与核心思想

传统 ReAct/单一 TODO-list 风格的 Agent 有两个长程任务上的痛点：

1. **上下文膨胀**：随着任务推进，历史消息线性增长，模型很快"忘记"全局目标，或被无关细节淹没。
2. **结构缺失**：任务的层级关系（大目标 → 子目标 → 具体动作）只存在于模型的"脑子"里，没有可检视、可回溯、可恢复的外部状态。

**目标树**把任务的层级结构显式化为一棵可持久化的树，并约束 Agent 的每一次输出都必须"落"在某个**叶子节点**上。由此得到三个性质：

- **可规划**：先把根任务拆成第一层子目标，再按需对每个子目标递归细化——天然支持"自顶向下、按需展开"。
- **可聚焦**：任一时刻只有一个 **focused leaf（当前焦点叶子）**，模型只需关心"当前这一步"，全局结构由树托管。
- **可压缩**：节点关闭时产出**摘要**，已完成子树的细节可以移出工作上下文，只保留结论。上下文规模与"当前活跃路径"成正比，而非与"全部历史"成正比。

一句话：**目标树 = 外部化的、可压缩的 Agent 工作记忆 + 控制流。**

---

## 2. 核心概念

### 2.1 节点（GoalNode）

```text
GoalNode
├── id            : str          # 路径式 ID，如 "1", "1.2", "1.2.3"（可读 + 可排序）
├── parent_id     : str | None   # 根节点为 None
├── title         : str          # 一句话目标
├── description   : str          # 展开说明 / 约束 / 验收标准
├── status        : Status       # pending | active | done | blocked | abandoned
├── children      : list[str]    # 子节点 id（有序）
├── acceptance    : str | None   # "完成的定义"（DoD），用于自检是否可 close
├── transcript    : list[Entry]  # 绑定到本节点的消息流（见 §5）
├── summary       : str | None   # close 时写入的摘要（见 §3.6）
├── artifacts     : dict         # 本节点产出的结构化结果（如某天的行程对象）
├── created_at    : ts
└── updated_at    : ts
```

`Status` 状态机：

```text
        plan/创建
pending ─────────────► active ──── close ───► done
   │                     │
   │ skip                │ 遇阻
   ▼                     ▼
abandoned             blocked ──(解阻)──► active
```

**关键不变量（Invariants）**：

- **I1 单焦点**：全树任一时刻最多一个 `active` 节点，即 `focused_id`。
- **I2 焦点是叶子**：`focused_id` 指向的节点 **没有 pending/active 子节点**（执行只发生在叶子上）。
- **I3 消息归属**：Agent 产生的每一条 assistant 消息都归属于当时的 `focused_id`（见 §5）。
- **I4 关闭即焦点转移**：`close` 一个节点后，系统**自动** focus 到下一个可执行叶子（见 §4）。

### 2.2 树（GoalTree）

```text
GoalTree
├── root_id       : str | None
├── focused_id    : str | None       # 当前焦点叶子
├── nodes         : dict[str, GoalNode]
└── seq           : int              # ID 分配计数
```

---

## 3. Goal 工具集（Agent 可见的工具 API）

这 7 个工具是 Agent 操作目标树的**唯一**入口。它们以 **Claude Agent SDK 的进程内 MCP 工具**形式注册（`@tool` + `create_sdk_mcp_server`，见 §6）。命名空间下对外暴露为 `mcp__goal__*`。

> 约定：所有"对当前节点操作"的工具，`node_id` 缺省即 `focused_id`，减少模型出错面。

### 3.1 `goal_init` — 初始化根节点

| 项 | 说明 |
|---|---|
| 输入 | `title: str`, `description: str`, `acceptance: str?` |
| 行为 | 创建根节点（id=`"1"`），`status=active`，设为 `focused_id`。整棵树此前必须为空。 |
| 自动副作用 | `focused_id = "1"` |
| 输出 | 根节点 id + 当前树视图 |

每个任务**只调用一次**，是流程的起点。

### 3.2 `goal_plan` — 在某节点下规划子目标

| 项 | 说明 |
|---|---|
| 输入 | `subgoals: [{title, description, acceptance?}]`, `parent_id: str?`（缺省=焦点） |
| 行为 | 在 `parent_id` 下**追加**有序子节点（全部 `pending`）。`parent_id` 因此从叶子变为内部节点。 |
| 自动副作用 | **自动 focus** 到这批新子节点中的第一个（满足 I2：焦点重新落到叶子）。 |
| 输出 | 新建子节点列表 + 当前焦点 |

这是"细化"动作：当模型判断当前焦点节点**太大、需要拆分**时调用。`goal_plan` 可对**任意**节点调用（包括已有部分 closed 子节点的节点），用于**动态补充计划**（re-planning）。

### 3.3 `goal_focus` — 聚焦到某节点（多为自动调用）

| 项 | 说明 |
|---|---|
| 输入 | `node_id: str` |
| 行为 | 将 `node_id`（须为可执行叶子，或会自动下钻到其第一个可执行叶子）置为 `active`，更新 `focused_id`。 |
| 调用方 | **绝大多数情况由系统在 `plan`/`close` 后自动触发**。手动调用仅用于"跳回某个之前的节点修补"。 |
| 输出 | 新焦点节点的完整上下文（含从 root 到该叶的路径、兄弟摘要） |

> "自动调用"的含义：`goal_plan` 和 `goal_close` 内部会调用焦点转移逻辑（§4），模型通常**不需要**自己显式 focus。显式 `goal_focus` 是逃生舱。

### 3.4 `goal_update` — 修改节点

| 项 | 说明 |
|---|---|
| 输入 | `node_id: str?`, 任意可选字段：`title?, description?, acceptance?, status?, artifacts?` |
| 行为 | 局部更新节点字段。可用于：修正目标描述、登记中间产物、把节点标记为 `blocked`/`abandoned`。 |
| 约束 | 不允许通过此工具把节点改成 `done`（关闭必须走 `goal_close`，以触发摘要与焦点转移）。 |
| 输出 | 更新后的节点 |

### 3.5 `goal_close` — 关闭节点（任务结束）

| 项 | 说明 |
|---|---|
| 输入 | `node_id: str?`, `summary: str`, `artifacts: dict?` |
| 前置检查 | 节点应满足其 `acceptance`；若有 `pending/active` 子节点则拒绝关闭（防止漏做）。 |
| 行为 | `status=done`，写入 `summary` 与 `artifacts`。 |
| 自动副作用 | **自动 focus 到下一个可执行叶子**（§4 的 DFS 后继）；若父节点的子节点全部 `done`，则**级联自动关闭父节点**并继续上溯。 |
| 输出 | 被关闭节点 + 新焦点（或"全树完成"信号） |

### 3.6 `goal_summarize` — 节点摘要

| 项 | 说明 |
|---|---|
| 输入 | `node_id: str?`, `mode: "leaf" \| "subtree" = "leaf"` |
| 行为 | 为节点生成/刷新摘要。`leaf` 模式压缩该节点 `transcript` 为结论；`subtree` 模式聚合子节点 summary。 |
| 实现 | 可由模型在工具内自述，或调用一次**单发 LLM**（复用 `claude_code_oauth.py` 的 `llm_call`）做无副作用压缩。 |
| 用途 | ① `close` 时写入结论；② 主动压缩，把已完成子树移出工作上下文（§7）。 |
| 输出 | 摘要文本 |

### 3.7 （辅助）`goal_tree` — 渲染当前树

只读工具，返回当前树的紧凑视图（焦点路径高亮、已完成节点只显示 title+summary）。供模型随时"看地图"。不计入 7 个核心工具，但实践中很有用。

---

## 4. 自动 focus（焦点转移）算法

焦点转移是整套系统的控制流核心。语义上等价于对目标树做 **先序 DFS（pre-order）遍历**，跳过 `done/abandoned` 分支。

```python
def advance_focus(tree, just_closed_id) -> str | None:
    """关闭 just_closed_id 后，返回下一个应聚焦的叶子 id；None 表示全树完成。"""
    cur = tree.nodes[just_closed_id]
    while cur.parent_id is not None:
        parent = tree.nodes[cur.parent_id]
        siblings = parent.children
        idx = siblings.index(cur.id)
        # 1) 在右侧兄弟里找第一个未完成的，下钻到它的第一个可执行叶子
        for sib_id in siblings[idx + 1:]:
            leaf = descend_to_actionable_leaf(tree, tree.nodes[sib_id])
            if leaf is not None:
                return leaf.id
        # 2) 没有可做的兄弟 => 父节点的子目标全部完成 => 级联自动关闭父节点
        auto_close_parent(tree, parent)   # 写聚合摘要（goal_summarize subtree）
        cur = parent                       # 继续向上回溯
    return None  # 回溯到 root 且无后继 => 整棵树完成


def descend_to_actionable_leaf(tree, node) -> GoalNode | None:
    if node.status in ("done", "abandoned"):
        return None
    actionable_children = [c for c in node.children
                           if tree.nodes[c].status not in ("done", "abandoned")]
    if not actionable_children:
        return node                        # 自身即叶子（或所有子节点已完成）
    return descend_to_actionable_leaf(tree, tree.nodes[actionable_children[0]])
```

要点：

- **先序**保证"先做完一个子目标的全部细分，再去做下一个兄弟"，符合人类规划直觉。
- **级联自动关闭父节点**：当一个内部节点的所有子目标完成，它本身的任务即完成——此时自动 `close` 它，摘要由子节点摘要聚合而成（`goal_summarize subtree`）。模型无需手动逐层 close。
- `blocked` 节点的处理策略可配置：默认"跳过、稍后重访"（被跳过的 blocked 节点在一轮 DFS 结束后再尝试），或"硬停等待解阻"。

> 触发自动 focus 的两个时机：
> - **`goal_plan` 之后** → 下钻到第一个新子节点（焦点从内部节点回到叶子）。
> - **`goal_close` 之后** → DFS 后继（可能伴随父节点级联关闭）。

---

## 5. "每条消息对应叶子节点" 的实现机制

这是约束 I3，也是把"对话流"与"树结构"缝合起来的关键。

实现位置不在工具里，而在**主控制循环消费 SDK 流式响应的地方**（与 `claude_code_oauth.py` 中 `async for msg in client.receive_response()` 的循环同构）：

```python
async for msg in client.receive_response():
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                # ★ 绑定：当前这条 assistant 文本归属于此刻的 focused_id
                tree.nodes[tree.focused_id].transcript.append(
                    Entry(role="assistant", kind="text", text=block.text, ts=now)
                )
            elif is_tool_use(block):
                # 工具调用同样登记到当前焦点节点（goal_plan/goal_close 会随后改变 focused_id）
                tree.nodes[tree.focused_id].transcript.append(
                    Entry(role="assistant", kind="tool_use",
                          tool=block.name, input=block.input, ts=now)
                )
```

机制解释：

- 系统始终维护"此刻的 `focused_id`"。模型每吐出一段文本/一次工具调用，**就近**记到当前焦点节点的 `transcript`。
- 当模型调用 `goal_plan` 或 `goal_close`，工具内部把 `focused_id` 切到新节点；**此后**的消息自然绑定到新节点。切换是原子的、由工具结果驱动的。
- 因此每个叶子节点都拥有一段"只属于它"的局部对话，构成该子目标的完整工作记录——这正是 `goal_summarize` 能逐节点压缩的前提。

为了让模型**始终知道**自己当前落在哪个节点，用 SDK 的 **Hook** 在每轮注入焦点上下文（见 §6.3），形成一个闭环：系统注入"你在节点 X" → 模型在 X 上工作 → 消息记到 X → 切换/关闭 → 注入"你现在在 Y"。

---

## 6. 与 Claude Agent SDK 的集成

整体架构：

```text
┌─────────────────────────────────────────────────────────────┐
│  主控制器 (Python)                                            │
│  ┌───────────────┐   持有/闭包      ┌──────────────────────┐  │
│  │  GoalTree 状态 │◄───────────────►│  Goal 工具处理函数     │  │
│  └───────────────┘                  │ (@tool, in-proc MCP) │  │
│         ▲                           └──────────┬───────────┘  │
│         │ §5 绑定消息                          │ 注册          │
│         │                            create_sdk_mcp_server     │
│  ┌──────┴───────────────────────────────────┐ │              │
│  │  ClaudeSDKClient (多轮会话)               │◄┘              │
│  │   - OAuth（剥离 API key，复用 CLI 登录态）│                │
│  │   - mcp_servers={"goal": server}          │                │
│  │   - hooks: 每轮注入焦点上下文             │                │
│  └──────┬────────────────────────────────────┘                │
└─────────┼─────────────────────────────────────────────────────┘
          │ stdio
     ┌────▼─────┐
     │ claude   │  ← OAuth token (~/.claude/.credentials.json)
     │  CLI     │
     └──────────┘
```

### 6.1 复用 `claude_code_oauth.py` 的 OAuth 模式

沿用参考文件的三个关键技巧，让 SDK 子进程走 **Max 订阅 OAuth 额度**而非 API key：

```python
import os
from claude_agent_sdk import (
    ClaudeAgentOptions, ClaudeSDKClient, AssistantMessage, TextBlock, ResultMessage,
    tool, create_sdk_mcp_server, HookMatcher,
)

# (1) 以空串覆盖父进程的 API key 变量，强制 CLI 回落 OAuth（父进程 os.environ 不变）
_OAUTH_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_BASE_URL": "", "ANTHROPIC_AUTH_TOKEN": ""}
```

### 6.2 把 Goal 工具注册为进程内 MCP 工具

工具处理函数对**同一个 `GoalTree` 实例**闭包，从而读写共享状态：

```python
def build_goal_server(tree: "GoalTree"):

    @tool("goal_init", "初始化根目标节点；整个任务只调用一次",
          {"title": str, "description": str, "acceptance": str})
    async def goal_init(args):
        node = tree.create_root(args["title"], args["description"], args.get("acceptance"))
        tree.focus(node.id)
        return {"content": [{"type": "text", "text": tree.render_focus_context()}]}

    @tool("goal_plan", "在当前(或指定)节点下规划有序子目标，并自动聚焦到第一个子目标",
          {"subgoals": list, "parent_id": str})
    async def goal_plan(args):
        parent = args.get("parent_id") or tree.focused_id
        children = tree.add_children(parent, args["subgoals"])
        tree.focus(tree.first_actionable_leaf(parent).id)     # 自动 focus
        return {"content": [{"type": "text", "text": tree.render_focus_context()}]}

    @tool("goal_focus", "聚焦到指定节点（通常由系统自动调用）", {"node_id": str})
    async def goal_focus(args):
        leaf = tree.first_actionable_leaf(args["node_id"])
        tree.focus(leaf.id)
        return {"content": [{"type": "text", "text": tree.render_focus_context()}]}

    @tool("goal_update", "修改节点的 title/description/acceptance/status/artifacts",
          {"node_id": str, "title": str, "description": str,
           "acceptance": str, "status": str, "artifacts": dict})
    async def goal_update(args):
        node = tree.update(args.get("node_id") or tree.focused_id, args)
        return {"content": [{"type": "text", "text": tree.render_node(node.id)}]}

    @tool("goal_close", "关闭节点（写摘要），并自动聚焦到下一个待办叶子",
          {"node_id": str, "summary": str, "artifacts": dict})
    async def goal_close(args):
        nid = args.get("node_id") or tree.focused_id
        tree.close(nid, args["summary"], args.get("artifacts"))    # 内含级联关闭
        nxt = tree.advance_focus(nid)                              # §4
        if nxt is None:
            return {"content": [{"type": "text", "text": "✅ 全部目标已完成。\n" + tree.render_tree()}]}
        tree.focus(nxt)
        return {"content": [{"type": "text", "text": tree.render_focus_context()}]}

    @tool("goal_summarize", "为节点生成/刷新摘要（leaf=压缩本节点; subtree=聚合子树）",
          {"node_id": str, "mode": str})
    async def goal_summarize(args):
        text = await tree.summarize(args.get("node_id") or tree.focused_id,
                                    args.get("mode", "leaf"))
        return {"content": [{"type": "text", "text": text}]}

    @tool("goal_tree", "查看当前目标树（焦点高亮，已完成节点只显示标题+摘要）", {})
    async def goal_tree(args):
        return {"content": [{"type": "text", "text": tree.render_tree()}]}

    return create_sdk_mcp_server(
        name="goal", version="1.0.0",
        tools=[goal_init, goal_plan, goal_focus, goal_update,
               goal_close, goal_summarize, goal_tree],
    )
```

### 6.3 用 Hook 每轮注入焦点上下文（落实 I3 的"模型可见性"）

`UserPromptSubmit` / `PreToolUse` Hook 在模型每次行动前注入一段 system-reminder，确保它清楚"我在哪个节点、约束是什么、已完成了什么"：

```python
async def inject_focus_context(input_data, tool_use_id, context):
    reminder = tree.render_focus_context()   # 焦点路径 + 当前节点 DoD + 兄弟摘要
    return {"hookSpecificOutput": {"additionalContext": reminder}}

hooks = {"UserPromptSubmit": [HookMatcher(matcher=None, hooks=[inject_focus_context])]}
```

> 不同 SDK 版本的 hooks/`additionalContext` 字段名可能略有差异，集成时以本机安装的 `claude-agent-sdk` 版本为准；退一步可把焦点上下文直接拼进每轮发送的用户消息里。

### 6.4 组装 Options 与运行

```python
def build_options(tree):
    return ClaudeAgentOptions(
        model="claude-sonnet-4-5",
        system_prompt=GOAL_AGENT_SYSTEM_PROMPT,      # §6.5
        mcp_servers={"goal": build_goal_server(tree)},
        allowed_tools=[
            "mcp__goal__goal_init", "mcp__goal__goal_plan", "mcp__goal__goal_focus",
            "mcp__goal__goal_update", "mcp__goal__goal_close",
            "mcp__goal__goal_summarize", "mcp__goal__goal_tree",
        ],
        hooks=hooks,
        env=_OAUTH_ENV,            # (1) 走 OAuth
        setting_sources=[],        # (2) 屏蔽用户级 ~/.claude 配置注入，省 token、稳定输出
        max_turns=60,              # 长程任务需要足够多轮；按规模调
        permission_mode="acceptEdits",   # 自动放行 goal_* 工具（它们无破坏性）
    )

async def run(requirement: str):
    tree = GoalTree(llm_call=create_claude_code_oauth_llm_call())   # 复用参考文件做摘要
    async with ClaudeSDKClient(options=build_options(tree)) as client:
        await client.query(requirement)
        async for msg in client.receive_response():
            bind_message_to_focus(tree, msg)     # §5
        # 若 max_turns 用尽但树未完成，可循环 query("继续") 直至 advance_focus 返回 None
    return tree
```

### 6.5 系统提示词（要点）

System prompt 把"工作方式"教给模型，核心规则：

```text
你是一个基于"目标树"工作的规划-执行 Agent。铁律：
1. 接到需求后，第一步永远是 goal_init 建立根目标。
2. 然后对当前焦点节点二选一：
   (a) 若它足够小、可一步做完 → 直接执行（用文字给出结果），完成后 goal_close 并写 summary；
   (b) 若它仍然偏大 → 用 goal_plan 拆成有序子目标（系统会自动把你带到第一个子目标）。
3. 你永远只在"当前焦点叶子"上工作；不要跳着做。focus 切换由系统自动完成。
4. 每个节点关闭时，summary 必须自包含：写清"这个子目标的结论/产出是什么"，
   因为之后的步骤可能只看得到 summary，看不到过程。
5. 拆分要克制：避免过深（建议 ≤4 层）或过细（一个叶子约对应 1-3 轮可完成的工作）。
6. 当 goal_close 提示"全部目标已完成"时，输出最终交付物。
```

---

## 7. 上下文与 Token 管理（摘要为何是一等公民）

目标树最大的工程价值在于**让上下文规模与"当前活跃路径"成正比，而非与"全部历史"成正比**。

`render_focus_context()` 注入给模型的内容**只包含**：

1. **焦点路径**：root → … → 当前叶子（每层只给 title + 一句 description）。
2. **当前叶子**：完整 description + acceptance（DoD）。
3. **左侧已完成兄弟**：只给 `summary`（不是 transcript）。
4. **右侧待办兄弟**：只给 title（让模型知道后面还有什么，但不展开）。

已 `done` 的子树，其 `transcript` 不进入上下文——它们被压缩成了一行 summary。于是：

- 一个 30 节点的大任务，模型在任意时刻的工作上下文可能只有 5–8 个节点的信息量。
- `goal_summarize subtree` 在级联关闭时把"一个分支的所有细节"折叠成"一个结论"，是上下文的**垃圾回收**机制。
- 摘要还是**断点恢复**的基础：把 `GoalTree` 序列化到磁盘，重启后无需重放历史，凭树+摘要即可继续。

---

## 8. 关键实现细节与边界情况

| 问题 | 处理 |
|---|---|
| **ID 方案** | 路径式 `"1.2.3"`：人类可读、可排序、能从 id 直接看出层级与兄弟次序。新增子节点时取 `max(兄弟尾号)+1`。 |
| **防漏做** | `goal_close` 前置检查：存在 `pending/active` 子节点则拒绝，提示先完成或 `abandoned`。 |
| **动态补计划** | 执行中发现遗漏 → 对父节点再 `goal_plan` 追加子目标；DFS 会自然把它们纳入后继。 |
| **防失控递归** | 软深度上限（如 4 层）写进 system prompt + 工具内硬校验；超限时强制要求模型"直接执行而非再拆"。 |
| **blocked 节点** | `goal_update status=blocked`；DFS 默认跳过、本轮末重访；连续 N 轮无解则升级为 `abandoned` 并记原因。 |
| **死循环/反复横跳** | 主控制器统计"无 close 的连续轮数"，超阈值注入提醒或硬停。 |
| **焦点漂移** | 模型若忘记当前节点，靠 §6.3 的 Hook 每轮重注入纠偏；`goal_tree` 工具兜底。 |
| **幂等与并发** | 单 Agent 单线程，工具串行执行，无需锁；树写操作做基本校验（节点存在、状态合法）。 |
| **持久化** | 每次工具调用后把 `GoalTree` dump 成 JSON（节点表 + focused_id + seq），支持崩溃恢复与审计。 |
| **可观测性** | 每个节点的 `transcript` 即逐节点 trace；导出树为 Mermaid/JSON 便于回看模型的规划过程。 |

---

## 9. 应用案例：短途旅行规划（无外部知识调用）

### 9.1 "无外部知识"意味着什么

不接联网/地图/票务 API，Agent **只能依赖模型自身的参数化知识 + 用户给定的约束 + 逻辑推理**。设计上必须正视其后果：

- **不能保证时效**：营业时间、票价、班次、临时关闭等都是模型的"记忆"，可能过时或出错。
- **对策**：把所有不可验证的事实显式登记为**假设（assumptions）**，并在最终交付物里标注"需出行前自行核实"。这本身就是一个该被建模成节点的子目标。

因此本场景下目标树的设计哲学是：**用结构化的推理与自洽性检查，替代外部数据的准确性。** 树的价值在于强制 Agent 把"约束收集 → 逐时段决策 → 冲突检查 → 假设登记 → 汇总成稿"这条链路走完整，而不漏项。

### 9.2 目标树形态（示例：3 天 2 夜城市短途游）

```text
[1] 规划一次 3 天 2 夜的 X 城短途旅行              (root)
├── [1.1] 收集与确认约束           # 同行人数、预算档位、出发/返程时段、
│                                  #   体力偏好、必去清单、忌口/无障碍需求、天气季节
│   └─(close: 产出"约束清单"对象，写入 root.artifacts)
├── [1.2] 设计行程总体结构          # 几个片区、住哪一带、每天主题、动线方向（顺时针/避免折返）
│   └─(close: 产出"骨架"——每天主题 + 落脚区域)
├── [1.3] Day 1 逐时段规划
│   ├── [1.3.1] 上午（含抵达/寄存）
│   ├── [1.3.2] 午餐
│   ├── [1.3.3] 下午
│   ├── [1.3.4] 晚餐
│   └── [1.3.5] 晚间 / 返回住处
│   └─(子节点全 done → 级联自动 close 1.3，subtree 摘要 = Day1 行程)
├── [1.4] Day 2 逐时段规划          # 结构同上
├── [1.5] Day 3 逐时段规划（含返程缓冲）
├── [1.6] 横向校验                  # 跨天去重、动线是否折返、每日时长是否超载、
│                                  #   预算合计是否在档位内、雨天备选
├── [1.7] 假设与免责清单            # 把所有"凭记忆"的营业时间/价格/班次集中登记，标注需核实
└── [1.8] 汇总成稿                  # 把各天 summary + 校验结论 + 假设清单拼成最终行程单
```

### 9.3 执行流程演示

1. 用户："帮我规划去 X 城 3 天 2 夜，2 个人，预算中等，喜欢人文和美食，不爱爬山。"
2. Agent `goal_init` 建根 → 自动 focus 到 `[1.1]`。
3. 在 `[1.1]` **直接执行**：把已知约束整理成清单；缺失项（如确切日期）登记为假设或用合理默认值；`goal_close` 写 summary（约束清单），自动 focus `[1.2]`。
4. `[1.2]` 直接执行：定"Day1 老城人文 / Day2 博物馆+市集 / Day3 近郊+返程"，住老城一带；close → 自动到 `[1.3]`。
5. `[1.3]` 判断"一天太大" → `goal_plan` 拆成 5 个时段子节点 → 自动 focus `[1.3.1]`。
6. 逐个时段 **直接执行**（每个叶子产出"地点+理由+预计时长+步行/交通衔接"），逐个 `goal_close`。
7. `[1.3.*]` 全部 done → 系统**级联自动关闭 `[1.3]`**（subtree 摘要 = Day1 完整行程）→ 自动 focus `[1.4]`。
8. Day2/Day3 同理。
9. `[1.6]` 横向校验：读取各天 summary（注意——此时 Day1 的逐时段细节已被压缩成摘要，上下文很干净），检查折返/超载/预算/雨备，发现问题就对相应天 `goal_plan` 补一个"调整"节点或 `goal_update` 修订。
10. `[1.7]` 把所有凭记忆的事实集中成"出行前需核实"清单。
11. `[1.8]` 汇总：拼出带时间轴的最终行程单 + 预算估算 + 假设清单，`goal_close` root → "全部目标已完成"，输出交付物。

### 9.4 这个场景凸显的目标树优势

- **无超载**：到 Day3 时，模型不必把 Day1/Day2 的全部推理塞在上下文里，只看它们的摘要——避免"规划到后面忘了前面"。
- **可自检**：`[1.6]` 校验节点把"一致性检查"显式建模为一个必经子目标，而不是指望模型顺手做。
- **诚实**：`[1.7]` 假设节点把"无外部知识"的固有风险结构化、可交付，而非藏着掖着。
- **可改**：用户若反馈"Day2 太满"，只需对 `[1.4]` 重新 `goal_plan`/`goal_update`，其余子树的摘要原样复用，改动局部化。

### 9.5 节点产出的结构化 artifacts（便于汇总）

每个时段叶子 close 时，除文字 summary 外，建议写入结构化 `artifacts`，让 `[1.8]` 汇总变成确定性拼装而非再生成：

```json
{
  "day": 1, "slot": "afternoon",
  "place": "老城历史街区步行线",
  "reason": "契合人文偏好；平坦无爬山；与上午落点步行 10 分钟可达",
  "est_duration_h": 2.5,
  "transit_from_prev": "步行约 10 分钟",
  "assumptions": ["多数小店周一可能闭店——需核实"]
}
```

---

## 10. 代码骨架（GoalTree 核心）

```python
import time, json
from dataclasses import dataclass, field, asdict

STATUSES = {"pending", "active", "done", "blocked", "abandoned"}

@dataclass
class GoalNode:
    id: str
    parent_id: str | None
    title: str
    description: str = ""
    acceptance: str | None = None
    status: str = "pending"
    children: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    summary: str | None = None
    artifacts: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

class GoalTree:
    def __init__(self, llm_call=None):
        self.nodes: dict[str, GoalNode] = {}
        self.root_id: str | None = None
        self.focused_id: str | None = None
        self.llm_call = llm_call           # 复用 claude_code_oauth 的单发 LLM 做摘要

    # ---- 写操作 ----
    def create_root(self, title, description, acceptance=None):
        assert not self.nodes, "根节点已存在"
        n = GoalNode("1", None, title, description, acceptance, status="active")
        self.nodes["1"] = n; self.root_id = "1"; return n

    def add_children(self, parent_id, subgoals):
        parent = self.nodes[parent_id]
        base = len(parent.children)
        created = []
        for i, sg in enumerate(subgoals, start=base + 1):
            cid = f"{parent_id}.{i}"
            n = GoalNode(cid, parent_id, sg["title"], sg.get("description", ""),
                         sg.get("acceptance"))
            self.nodes[cid] = n; parent.children.append(cid); created.append(n)
        if parent.status == "active":      # 父变内部节点
            parent.status = "pending"
        return created

    def focus(self, node_id):
        if self.focused_id and self.nodes[self.focused_id].status == "active":
            # 离开但未关闭的节点保持 active？此处约定：聚焦切换时旧焦点回落 pending
            self.nodes[self.focused_id].status = "pending"
        self.focused_id = node_id
        self.nodes[node_id].status = "active"

    def update(self, node_id, fields):
        n = self.nodes[node_id]
        for k in ("title", "description", "acceptance", "artifacts"):
            if k in fields and fields[k] is not None: setattr(n, k, fields[k])
        if fields.get("status") in (STATUSES - {"done"}):
            n.status = fields["status"]
        n.updated_at = time.time(); return n

    def close(self, node_id, summary, artifacts=None):
        n = self.nodes[node_id]
        unfinished = [c for c in n.children
                      if self.nodes[c].status in ("pending", "active", "blocked")]
        assert not unfinished, f"存在未完成子节点：{unfinished}"
        n.status = "done"; n.summary = summary
        if artifacts: n.artifacts.update(artifacts)
        n.updated_at = time.time()

    # ---- 焦点转移（§4） ----
    def first_actionable_leaf(self, node_id):
        n = self.nodes[node_id]
        if n.status in ("done", "abandoned"): return None
        kids = [c for c in n.children
                if self.nodes[c].status not in ("done", "abandoned")]
        return n if not kids else self.first_actionable_leaf(kids[0])

    def advance_focus(self, just_closed_id):
        cur = self.nodes[just_closed_id]
        while cur.parent_id is not None:
            parent = self.nodes[cur.parent_id]
            sibs = parent.children; idx = sibs.index(cur.id)
            for sid in sibs[idx + 1:]:
                leaf = self.first_actionable_leaf(sid)
                if leaf: return leaf.id
            # 兄弟做完 => 级联自动关闭父节点
            agg = self._aggregate_summary(parent.id)
            self.close(parent.id, agg)
            cur = parent
        return None

    # ---- 摘要（§3.6） ----
    def _aggregate_summary(self, node_id):
        kids = self.nodes[node_id].children
        return "；".join(self.nodes[c].summary or self.nodes[c].title for c in kids)

    async def summarize(self, node_id, mode="leaf"):
        n = self.nodes[node_id]
        if mode == "subtree":
            n.summary = self._aggregate_summary(node_id); return n.summary
        if self.llm_call:                  # 用单发 LLM 压缩 transcript
            text = "\n".join(e.get("text", "") for e in n.transcript)
            r = await self.llm_call([
                {"role": "system", "content": "用 2-3 句话总结该子目标的结论与产出。"},
                {"role": "user", "content": text},
            ])
            n.summary = r["content"]
        else:
            n.summary = (n.transcript[-1]["text"][:200] if n.transcript else n.title)
        return n.summary

    # ---- 渲染（§7） / 持久化 ----
    def render_focus_context(self): ...     # 焦点路径 + DoD + 兄弟摘要
    def render_tree(self): ...              # 紧凑全树视图
    def render_node(self, node_id): ...
    def to_json(self):
        return json.dumps({"root_id": self.root_id, "focused_id": self.focused_id,
                           "nodes": {k: asdict(v) for k, v in self.nodes.items()}},
                          ensure_ascii=False, indent=2)
```

---

## 11. 测试与验收

- **单元**：`advance_focus` 在各种树形（满树、单链、含 abandoned/blocked、级联关闭）下的后继正确性；`close` 的前置校验；ID 分配。
- **不变量断言**（每次工具调用后跑）：I1 单焦点、I2 焦点是叶子、I3 消息有归属、I4 close 必转焦点。
- **端到端**：给定旅行需求，断言最终树满足"root done、所有叶子有 summary、artifacts 可拼装出完整行程、存在假设清单节点"。
- **回归**：把一次完整运行的 `GoalTree.to_json()` 存为快照，比对规划结构是否稳定。

---

## 12. 可扩展方向

- **接入外部知识**：把"无外部知识"约束放开后，新增 `search`/`maps` 工具；目标树结构不变，只是 `[1.7]` 假设节点缩小、各时段叶子改为"先查证再决策"。
- **多 Agent**：把整棵子树派给子 Agent 执行（子 Agent 内部也是一棵 goal tree），父 Agent 只消费其 root summary——目标树天然支持这种分形委派。
- **人在环（HITL）**：在关键节点（如预算超档）插入 `blocked + 等待用户确认`，用 SDK 的 `can_use_tool`/权限回调挂起等待。
- **回溯/重规划**：保留树的版本快照，支持"回到某节点、丢弃其后子树、换条路重做"。

---

## 附：与 `claude_code_oauth.py` 的关系小结

| 复用点 | 本系统中的用途 |
|---|---|
| OAuth env 覆盖（空串顶掉 API key） | 让主 Agent 的 `ClaudeSDKClient` 走 Max 订阅额度 |
| `setting_sources=[]` | 屏蔽用户级配置注入，稳定输出、省 token |
| `stderr` 捕获 + 错误规范化 | 主循环排错；429/限流识别 |
| `receive_response()` 消费循环 | §5 把每条 assistant 消息绑定到焦点叶子 |
| `create_claude_code_oauth_llm_call()` 单发 LLM | `goal_summarize` 的无副作用压缩调用 |

参考文件是"一次 LLM 调用"的最小封装；本系统在其 OAuth/SDK 地基之上，把 `max_turns=1` 升级为多轮会话，并用一套 Goal 工具 + Hook 把"规划-执行"的控制流外部化到目标树。
```
