"""DeepSeek 调用封装层（技术选型 §1）。

统一提供三项能力：
1. 超时 + 重试：网络抖动/服务端错误自动重试，避免单次失败打断对话；
2. token 记账：区分 输入/输出/思考/缓存命中，支撑 N1 消耗统计与优化对比；
3. 思考开关：机械任务关思考（实测延迟 3.4s→1.9s、token 202→44），
   决策任务开思考保质量。

说明：最终答复含结构化 <decision> JSON 块，需完整接收后解析，故不做逐字流式；
前端的"过程感"由 Agent 的 trace 事件流（SSE）提供。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from openai import OpenAI

from app import config


class LLMError(RuntimeError):
    """重试耗尽后的最终失败。"""


@dataclass
class TokenUsage:
    """单次调用的 token 账目（区分思考与缓存，用于实验报告统计）。"""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    latency_seconds: float = 0.0

    def add(self, other: "TokenUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cached_tokens += other.cached_tokens
        self.latency_seconds += other.latency_seconds

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
            "latency_seconds": round(self.latency_seconds, 2),
        }


@dataclass
class LLMResponse:
    message: object          # openai ChatCompletionMessage
    usage: TokenUsage
    finish_reason: str


def _extract_usage(usage, latency: float) -> TokenUsage:
    if usage is None:
        return TokenUsage(latency_seconds=latency)
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", 0) if details else 0
    cached = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
    return TokenUsage(
        prompt_tokens=usage.prompt_tokens or 0,
        completion_tokens=usage.completion_tokens or 0,
        reasoning_tokens=reasoning or 0,
        cached_tokens=cached,
        latency_seconds=latency,
    )


class DeepSeekClient:
    """最小封装：只暴露 chat / stream_chat 两个入口。"""

    def __init__(self, model: str | None = None):
        if not config.DEEPSEEK_API_KEY:
            raise LLMError("缺少 DEEPSEEK_API_KEY：请在 .env 中配置")
        self.model = model or config.DEEPSEEK_MODEL
        self._client = OpenAI(
            api_key=config.DEEPSEEK_API_KEY,
            base_url=config.DEEPSEEK_BASE_URL,
            timeout=config.LLM_TIMEOUT_SECONDS,
        )

    def _common_kwargs(self, thinking: bool) -> dict:
        kwargs: dict = {"model": self.model}
        if not thinking:
            # 已实测验证：关闭思考可显著降低机械任务的延迟与 token
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return kwargs

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             thinking: bool = True, max_tokens: int = 1500) -> LLMResponse:
        """非流式调用（用于工具调用轮），带重试。"""
        kwargs = self._common_kwargs(thinking)
        if tools:
            kwargs["tools"] = tools
        last_err: Exception | None = None
        for attempt in range(config.LLM_MAX_RETRIES + 1):
            start = time.time()
            try:
                r = self._client.chat.completions.create(
                    messages=messages, max_tokens=max_tokens, **kwargs)
                return LLMResponse(
                    message=r.choices[0].message,
                    usage=_extract_usage(r.usage, time.time() - start),
                    finish_reason=r.choices[0].finish_reason,
                )
            except Exception as e:  # noqa: BLE001 网络/服务端错误统一重试
                last_err = e
                if attempt < config.LLM_MAX_RETRIES:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"调用 {self.model} 失败（已重试 {config.LLM_MAX_RETRIES} 次）: {last_err}")
