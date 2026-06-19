# 目标树 Agent（Goal Tree Agent）+ 实时可视化

用 **Claude Agent SDK** 实现的「先规划、再逐步执行」Agent：规划与执行状态由一棵**目标树**承载，
树的全部操作收敛为一套 **goal 工具**。LLM 调用复用 `claude_code_oauth.py` 的 OAuth 模式
（剥离 `ANTHROPIC_API_KEY` → 走 `claude` CLI 的登录态 / Max 订阅额度）。

设计文档见 [`docs/goal_tree_agent_design.md`](./docs/goal_tree_agent_design.md)。

## 目录结构

```
.
├── src/                        # 代码 + 运行时资源
│   ├── server.py               # FastAPI + WebSocket，广播目标树快照与执行事件
│   ├── agent_runner.py         # 注册 goal/ui/cache 工具，OAuth 驱动 ClaudeSDKClient
│   ├── goal_tree.py            # 目标树：规划/聚焦/级联关闭/评审门控/历史/摘要
│   ├── evaluator.py            # 独立评审模型（另一个大模型，自动给节点打分）
│   ├── run_cache.py            # 每次运行隔离的缓存目录
│   ├── ui_bridge.py            # iframe 渲染 + 人机回传（问卷）/ 常驻画布
│   ├── claude_code_oauth.py    # OAuth LLM 封装（被复用）
│   ├── static/index.html       # 单文件前端：目标树 + 画布 + 详情/历史 + 执行流
│   └── req-eval/               # 「需求→评价指标」技能（问卷 + 映射库）
├── docs/                       # 设计文档
├── examples/                   # 样例输出（成都行程）
├── pyproject.toml / uv.lock    # 依赖
└── cache/                      # 运行缓存（自动生成，已 gitignore）
```

## 运行

前置：已安装 `uv`、`claude` CLI，且 `claude` 已登录（`~/.claude/.credentials.json` 存在）。

```bash
uv sync                                              # 安装依赖
uv run uvicorn server:app --app-dir src --port 8000  # 启动
# 浏览器打开 http://127.0.0.1:8000
```

可选环境变量（写在 `.env`）：`AGENT_MODEL`（默认 `claude-sonnet-4-5`）、
`JUDGE_MODEL`（独立评审模型，默认 `claude-opus-4-1`）。

在输入框写需求（或点示例），点「运行」：左栏实时渲染目标树（焦点高亮、状态着色、
评分/历史 chips），中栏是 Agent 实时画布，右栏是节点详情/尝试历史 + 执行流。

## 核心机制

- **goal 工具集**：`goal_init / goal_plan / goal_set_criteria / goal_focus / goal_update /
  goal_eval / goal_retry / goal_close / goal_summarize / goal_tree`。
- **自动 focus**：`goal_plan` 后下钻到第一个子目标；`goal_close` 后按先序 DFS 找下一个待办叶子，
  父节点子目标全部完成时**级联自动关闭**。鼓励多层（一般 ≤3 层）规划。
- **每条消息对应叶子节点**：`receive_response` 循环里把每条 assistant 文本/工具调用就近记到当时的焦点叶子。
- **独立评审门控**：每个叶子节点自带 requirements + metrics + 阈值；**每次关闭都自动触发**一个
  独立大模型（非 agent 自评）按指标打分，达标才允许 `goal_close`，否则 `goal_retry` 重做或
  `goal_plan(replace)` 重规划。每个节点累积尝试历史。
- **真实用户输入硬门控**：标记 `requires_user_input` 的节点，必须经 `ui_render` 收到用户真实提交才能关闭，
  禁止 agent 自行脑补需求。
- **可视化交互**：`ui_render`（弹窗 iframe，阻塞收集用户输入，如问卷）与 `ui_canvas`
  （常驻面板，非阻塞，实时预览、自动刷新）。
- **运行缓存**：每次运行 `cache/<run_id>/`，自动落盘 `goal_tree.json` / `events.jsonl` / `run_full.json`，
  agent 也可用 `cache_save/cache_read/cache_list` 存取过程文件与结果。
- **上下文压缩**：注入给模型的只有焦点路径 + 当前 DoD + 已完成兄弟的摘要，已完成子树细节不进上下文。
- **无外部知识**：依赖时效性的事实（价格/营业时间/班次）作合理推理并登记为「假设，需出行前核实」。
