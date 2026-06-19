"""
Claude Code OAuth Provider

通过 claude-agent-sdk 复用 `claude` CLI 的 OAuth 登录态调用 Claude（Max 订阅额度）。

实现方式：使用 `ClaudeSDKClient`（双向 session）+ AsyncIterable[dict] 形式发送
用户消息。这种模式同时满足：
  1. 协议正确（client 内部管 stdin 生命周期，不会卡死）
  2. 支持多模态（content blocks 可带 image 节点）

Auth：依赖 `~/.claude/.credentials.json` 的 OAuth token；如父进程有
  ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL，会从子进程 env 中剥离，让 CLI
  回落到 OAuth。父进程 os.environ 不变。

输出契约（与现有 llm_call 一致）：
    {"content": str, "usage": {"input_tokens": int, "output_tokens": int}}
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _convert_messages(
    messages: List[Dict[str, Any]],
) -> Tuple[Optional[str], List[Dict[str, Any]], bool]:
    """
    把 OpenAI 风格 messages 拆为 (system_prompt, anthropic_content_blocks, has_image)。

    - role=system 拼接为 system_prompt
    - role=user/assistant 的 content 转为 Anthropic content blocks (text/image)
    - OpenAI {"type":"image_url","image_url":{"url":...}} 转为
      Anthropic {"type":"image","source":{"type":"url","url":...}}
    - has_image：是否包含图片块，用于决定走 string 还是 AsyncIterable 模式
    """
    system_parts: List[str] = []
    blocks: List[Dict[str, Any]] = []
    has_image = False

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            if isinstance(content, str):
                system_parts.append(content)
            continue

        if isinstance(content, str):
            blocks.append({"type": "text", "text": content})
            continue

        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    blocks.append({"type": "text", "text": str(block)})
                    continue
                btype = block.get("type")
                if btype == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})
                elif btype == "image_url":
                    url = (block.get("image_url") or {}).get("url", "")
                    if url:
                        blocks.append(
                            {"type": "image", "source": {"type": "url", "url": url}}
                        )
                        has_image = True
                elif btype == "image":
                    blocks.append(block)
                    has_image = True

    system_prompt = "\n\n".join(system_parts).strip() or None
    return system_prompt, blocks, has_image


def _blocks_to_string(blocks: List[Dict[str, Any]]) -> str:
    """把 content blocks 拍平成字符串（图片降级为 [图片URL: ...] 占位）— string 模式用"""
    parts: List[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "image":
            src = block.get("source") or {}
            url = src.get("url") or src.get("data", "")[:60]
            parts.append(f"[图片URL: {url}]")
    return "\n\n".join(p for p in parts if p).strip()


def create_claude_code_oauth_llm_call(model: str = "claude-sonnet-4-5"):
    """
    工厂：返回兼容 pipeline llm_call 契约的异步函数（基于 ClaudeSDKClient）。

    返回函数签名：
        async (messages, model=..., temperature=..., max_tokens=...,
               response_schema=None, tools=None, **kwargs) -> dict
    其中 temperature / max_tokens / response_schema / tools 静默忽略
    （SDK 不透传这些参数，CLI 用自己的默认值）。
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ClaudeSDKError,
        RateLimitEvent,
        ResultMessage,
        TextBlock,
    )

    # 让 SDK 子进程看不到 API key 相关变量，回落到 OAuth。
    # SDK 内部把 options.env 当作"覆盖层"叠在父进程 os.environ 之上，
    # 所以从 dict 里"移除"这些 key 没用 — 必须显式以空串覆盖父值。
    # 父进程 os.environ 不变（其他 LLM provider 继续可用 API key）。
    _override_env: Dict[str, str] = {
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "",
        "ANTHROPIC_AUTH_TOKEN": "",
    }
    if "ANTHROPIC_API_KEY" in os.environ or "ANTHROPIC_BASE_URL" in os.environ:
        logger.info(
            "[claude_code_oauth] Overriding ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL "
            "with empty values in SDK subprocess env so CLI falls back to OAuth."
        )

    default_model = model

    async def llm_call(
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        actual_model = (model or default_model).split("/")[-1]

        system_prompt, content_blocks, has_image = _convert_messages(messages)
        if not content_blocks:
            content_blocks = [{"type": "text", "text": " "}]

        stderr_lines: List[str] = []

        def _capture_stderr(line: str) -> None:
            if line:
                stderr_lines.append(line)

        options = ClaudeAgentOptions(
            model=actual_model,
            system_prompt=system_prompt,
            allowed_tools=[],
            max_turns=1,
            env=_override_env,
            stderr=_capture_stderr,
            # 关键：屏蔽 CLI 加载用户级 ~/.claude/ 配置（output_style/skills/plugins 等）
            # 否则这些会被注入 system prompt，浪费 token + 影响输出格式
            setting_sources=[],
        )

        text_parts: List[str] = []
        usage: Dict[str, Any] = {}
        is_error = False
        api_error_status: Optional[int] = None
        result_subtype: Optional[str] = None
        result_errors: List[str] = []
        rate_limit_signal: Optional[str] = None

        def _emit(line: str) -> None:
            try:
                print(f"[claude] {line}", flush=True)
            except UnicodeEncodeError:
                # Fallback for cp1252 encoding issues
                print(f"[claude] {line.encode('utf-8', errors='ignore').decode('utf-8')}", flush=True)

        try:
            async with ClaudeSDKClient(options=options) as client:
                if has_image:
                    # 多模态：用 AsyncIterable[dict] 模式发送 Anthropic content blocks
                    async def _input_stream():
                        yield {
                            "type": "user",
                            "message": {"role": "user", "content": content_blocks},
                            "parent_tool_use_id": None,
                            "session_id": "default",
                        }
                    await client.query(_input_stream())
                else:
                    # 纯文本：走 SDK string 模式（已验证稳定路径）
                    await client.query(_blocks_to_string(content_blocks))

                async for msg in client.receive_response():
                    msg_type = type(msg).__name__

                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if hasattr(block, "thinking"):
                                # thinking 内容太多，跳过
                                continue
                            elif isinstance(block, TextBlock):
                                _emit(f"[text] {block.text}")
                                text_parts.append(block.text)
                            elif hasattr(block, "name") and hasattr(block, "input"):
                                _emit(f"[tool_use] {block.name}({block.input})")
                            else:
                                _emit(f"[{type(block).__name__}] {block!r}")
                        if msg.usage and not usage:
                            usage = dict(msg.usage)
                    elif isinstance(msg, ResultMessage):
                        if msg.usage:
                            usage = dict(msg.usage)
                        _emit(
                            f"[result] subtype={msg.subtype} "
                            f"is_error={msg.is_error} turns={msg.num_turns} "
                            f"duration={msg.duration_ms}ms "
                            f"in={msg.usage.get('input_tokens', 0) if msg.usage else 0} "
                            f"out={msg.usage.get('output_tokens', 0) if msg.usage else 0}"
                        )
                        if msg.is_error:
                            is_error = True
                            api_error_status = msg.api_error_status
                            result_subtype = msg.subtype
                            result_errors = list(msg.errors or [])
                    elif isinstance(msg, RateLimitEvent):
                        # RateLimitEvent 是 SDK 定期播报 quota 状态，不等于被限流。
                        # 只有 rate_limit_info.status != 'allowed' 才算真限流。
                        info = getattr(msg, "rate_limit_info", None)
                        info_status = getattr(info, "status", None) if info else None
                        _emit(f"[rate_limit] status={info_status!r} type={getattr(info, 'rate_limit_type', None)!r}")
                        if info_status and info_status != "allowed":
                            rate_limit_signal = f"status={info_status!r}"
                    else:
                        # SystemMessage 简化为关键字段；其他未知类型 fallback
                        if msg_type == "SystemMessage":
                            data = getattr(msg, "data", {}) or {}
                            subtype = getattr(msg, "subtype", "?")
                            if subtype == "init":
                                _emit(
                                    f"[init] model={data.get('model')!r} "
                                    f"apiKeySource={data.get('apiKeySource')!r} "
                                    f"session={data.get('session_id', '')[:8]}"
                                )
                            else:
                                _emit(f"[system] subtype={subtype}")
                        else:
                            _emit(f"[{msg_type}] {msg!r}")
        except ClaudeSDKError as e:
            stderr_tail = "\n".join(stderr_lines[-20:])
            raise RuntimeError(
                f"claude_agent_sdk error: {type(e).__name__}: {e}\n"
                f"--- CLI stderr (last 20 lines) ---\n{stderr_tail}"
            ) from e

        if rate_limit_signal or api_error_status == 429:
            raise RuntimeError(
                "Claude Code OAuth rate-limited (429). "
                "Max subscription quota may be exhausted in current 5-hour window. "
                "Run `claude /status` to check remaining."
            )

        if is_error:
            stderr_tail = "\n".join(stderr_lines[-20:])
            errors_str = "; ".join(result_errors) or "(empty errors[])"
            raise RuntimeError(
                f"claude_agent_sdk is_error=True "
                f"subtype={result_subtype!r} status={api_error_status} "
                f"errors={errors_str}\n"
                f"--- CLI stderr (last 20 lines) ---\n{stderr_tail}"
            )

        content = "".join(text_parts)

        normalized_usage = {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        }
        for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
            if k in usage:
                normalized_usage[k] = int(usage[k] or 0)

        return {"content": content, "usage": normalized_usage}

    return llm_call
