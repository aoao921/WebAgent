"""Model adapters for the canonical agent protocol."""

from __future__ import annotations

import json
from typing import Any, Protocol

from openai import AsyncOpenAI


from .response_adapter import decode_tool_call, normalize_response

from .protocol import (
    AgentMessage,
    ModelRequest,
    ModelResponse,
    ModelProtocolIssue,
    ModelUsage,
    ToolCall,
)


class AgentModelClient(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class OpenAICompatibleAgentModelClient:
    """Translate the internal protocol to an OpenAI-compatible tool API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        default_model: str,
        client: Any | None = None,
    ) -> None:
        self.default_model = default_model
        endpoint = str(base_url or "").replace("/chat/completions", "").rstrip("/")
        # The loop owns its retry budget. SDK retries would multiply it invisibly.
        self.client = client or AsyncOpenAI(api_key=api_key, base_url=endpoint, max_retries=0)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        messages = [self._message_to_provider(item) for item in request.messages]
        kwargs: dict[str, Any] = {
            "model": request.model or self.default_model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "description": item.description,
                        "parameters": item.input_schema or {
                            "type": "object",
                            "properties": {},
                        },
                    },
                }
                for item in request.tools
            ]
            kwargs["tool_choice"] = "auto"
        if request.thinking is not None:
            kwargs["extra_body"] = {
                "thinking": {"type": request.thinking},
            }
        if request.response_format is not None:
            kwargs["response_format"] = dict(request.response_format)

        response = await self.client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        choice = choices[0] if choices else None
        provider_message = getattr(choice, "message", None)
        tool_calls: list[ToolCall] = []
        for raw in getattr(provider_message, "tool_calls", None) or []:
            tool_calls.append(decode_tool_call(
                str(raw.id), str(raw.function.name), str(raw.function.arguments or ""),
            ))

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0
        )
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = int(
            getattr(usage, "prompt_cache_hit_tokens", 0)
            or getattr(prompt_details, "cached_tokens", 0)
            or 0
        )
        cache_miss_tokens = int(
            getattr(usage, "prompt_cache_miss_tokens", 0)
            or max(0, prompt_tokens - cached_tokens)
        )
        return ModelResponse(
            message=AgentMessage(
                role="assistant",
                content=self._content_text(getattr(provider_message, "content", None)),
                reasoning_content=self._reasoning_content(provider_message),
                tool_calls=tool_calls,
            ),
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            usage=ModelUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=total_tokens,
                cache_hit_input_tokens=cached_tokens,
                cache_miss_input_tokens=cache_miss_tokens,
            ),
            provider_metadata={"id": str(getattr(response, "id", "") or "")},
            protocol_error=(
                ModelProtocolIssue(code="missing_response_message", message="provider returned no assistant message")
                if provider_message is None else None
            ),
        )

    @staticmethod
    def _content_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif getattr(item, "type", None) == "text":
                    parts.append(str(getattr(item, "text", "") or ""))
            return "\n".join(parts)
        return str(value)

    @staticmethod
    def _reasoning_content(message: Any) -> str | None:
        value = getattr(message, "reasoning_content", None)
        if value is None:
            extra = getattr(message, "model_extra", None)
            if not isinstance(extra, dict):
                extra = getattr(message, "__pydantic_extra__", None)
            if isinstance(extra, dict):
                value = extra.get("reasoning_content")
        if value is None and hasattr(message, "model_dump"):
            dumped = message.model_dump()
            if isinstance(dumped, dict):
                value = dumped.get("reasoning_content")
        if value is None:
            return None
        return str(value)

    @staticmethod
    def _message_to_provider(message: AgentMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        # Thinking-mode compatible providers require every returned assistant
        # reasoning trace to be replayed verbatim in the next transcript.  A
        # final assistant response can carry reasoning even without a tool
        # call (for example before a structured-output repair), so limiting
        # this field to tool-call messages causes a provider-side 400.
        if message.role == "assistant" and (
            message.reasoning_content is not None or message.tool_calls
        ):
            # Runtime-owned bootstrap/login tool calls are intentionally
            # synthetic and therefore have no provider reasoning trace.
            # Thinking-mode OpenAI-compatible endpoints still require the
            # field on every replayed assistant tool-call message, so emit an
            # empty placeholder for those synthetic transcript entries.
            payload["reasoning_content"] = message.reasoning_content or ""
        if message.role == "assistant" and message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments if call.protocol_error else json.dumps(
                            call.arguments, ensure_ascii=False, separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool":
            payload["tool_call_id"] = message.tool_call_id
            if message.name:
                payload["name"] = message.name
        return payload


async def complete_adapted(
    client: AgentModelClient, request: ModelRequest, *, expect_json: bool, call_prefix: str,
) -> ModelResponse:
    """Single response boundary for live providers and scripted/offline clients."""
    response = await client.complete(request)
    return normalize_response(response, request.tools, expect_json=expect_json, call_prefix=call_prefix)
