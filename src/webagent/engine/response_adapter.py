"""One model-response boundary for envelope compatibility and protocol errors.

Only explicit envelopes are converted. This module does not invent tool names,
arguments, evidence or domain fields, and never chooses a JSON object from prose.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any

from .protocol import ModelProtocolIssue, ModelResponse, ToolCall, ToolDefinition


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"non-JSON numeric constant: {value}")


def _json(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def decode_tool_call(call_id: str, name: str, raw_arguments: str) -> ToolCall:
    """Keep invalid native calls addressable so the loop can return a rejection."""
    try:
        arguments = _json(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be a JSON object")
    except ValueError as exc:
        return ToolCall(
            id=call_id, name=name, raw_arguments=raw_arguments,
            protocol_error=ModelProtocolIssue(code="invalid_tool_arguments", message=str(exc)),
        )
    return ToolCall(id=call_id, name=name, arguments=arguments, raw_arguments=raw_arguments)


def _final_object(content: str) -> tuple[dict[str, Any], list[str]]:
    text = content.strip()
    changes = []
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)
        changes.append("json_code_fence")
    try:
        value = _json(text)
    except json.JSONDecodeError:
        # Repair only invalid JSON. A valid object may legitimately have a key
        # beginning with '{'; that key must never be mistaken for a delimiter.
        if text.startswith("{{"):
            text = text[1:]
        elif text.startswith('{"{'):
            text = text[:1] + text[3:]
        else:
            raise
        value = _json(text)
        changes.append("duplicate_object_opener")
    if not isinstance(value, dict):
        raise ValueError("terminal output must be one JSON object")
    return value, changes


def _dsml_calls(text: str, call_prefix: str) -> list[ToolCall]:
    envelope = re.fullmatch(r"<｜｜DSML｜｜ calls>\s*(.*?)\s*</｜｜DSML｜｜ calls>", text, re.DOTALL)
    if envelope is None:
        raise ValueError("DSML must be one complete calls envelope, without terminal output or prose")
    invoke = re.compile(r'<｜｜DSML｜｜ invoke name="([^"\s]+)">\s*(.*?)\s*</｜｜DSML｜｜ invoke>', re.DOTALL)
    parameter = re.compile(r'<｜｜DSML｜｜ parameter name="([^"\s]+)" string="(true|false)">(.*?)</｜｜DSML｜｜ parameter>', re.DOTALL)
    body = envelope.group(1)
    calls = []
    offset = 0
    while body[offset:].strip():
        offset += len(body[offset:]) - len(body[offset:].lstrip())
        match = invoke.match(body, offset)
        if match is None:
            raise ValueError("incomplete or unsupported DSML invocation")
        name, arguments_text = match.groups()
        arguments = {}
        param_offset = 0
        while arguments_text[param_offset:].strip():
            param_offset += len(arguments_text[param_offset:]) - len(arguments_text[param_offset:].lstrip())
            argument = parameter.match(arguments_text, param_offset)
            if argument is None:
                raise ValueError("incomplete or unsupported DSML parameter")
            key, is_string, value = argument.groups()
            if key in arguments:
                raise ValueError(f"duplicate DSML parameter: {key}")
            arguments[key] = value if is_string == "true" else _json(value)
            param_offset = argument.end()
        digest = sha256(f"{call_prefix}|{len(calls)}|{match.group(0)}".encode("utf-8")).hexdigest()[:24]
        calls.append(ToolCall(id=f"dsml:{digest}", name=name, arguments=arguments))
        offset = match.end()
    if not calls:
        raise ValueError("empty DSML calls envelope")
    return calls


def _complete_dsml_suffix(text: str) -> str:
    """Return one complete DSML envelope even after provider commentary."""

    opening = "<｜｜DSML｜｜ calls>"
    closing = "</｜｜DSML｜｜ calls>"
    if text.count(opening) != 1 or not text.endswith(closing):
        return text
    start = text.index(opening)
    if start == 0:
        return text
    # Commentary before a complete envelope is deterministic provider noise.
    # Text after the envelope is still ambiguous and remains rejected.
    return text[start:]


def normalize_response(
    response: ModelResponse, tools: list[ToolDefinition], *,
    expect_json: bool, call_prefix: str,
) -> ModelResponse:
    """Return canonical calls/final text or an explicit error, retaining the source."""
    result = response.model_copy(deep=True)
    raw = response.message.model_copy(deep=True)
    message = result.message
    changes = result.normalizations
    try:
        if result.protocol_error:
            raise ValueError(result.protocol_error.message)
        if result.finish_reason in {"length", "content_filter"}:
            result.protocol_error = ModelProtocolIssue(
                code="incomplete_response", message=f"provider stopped with {result.finish_reason}; no output may be executed or finalized",
            )
            raise ValueError(result.protocol_error.message)
        # Native calls are authoritative. Never recover a second set from their
        # accompanying text or reasoning channel.
        if not message.tool_calls:
            text = message.content.strip()
            if not text and message.reasoning_content:
                text = message.reasoning_content.strip()
                changes.append("reasoning_channel_fallback")
            if "<｜｜DSML｜｜" in text and not text.startswith(("{", "[", "```")):
                dsml_text = _complete_dsml_suffix(text)
                if dsml_text is not text:
                    changes.append("dsml_commentary_prefix")
                message.tool_calls = _dsml_calls(dsml_text, call_prefix)
                message.content = ""
                changes.append("dsml_calls")
            elif expect_json:
                value, conversions = _final_object(text)
                changes.extend(conversions)
                if changes:
                    message.content = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            elif "reasoning_channel_fallback" in changes:
                message.content = text
        if result.finish_reason == "tool_calls" and not message.tool_calls:
            raise ValueError("provider declared tool_calls but supplied no calls")
        ids = [call.id for call in message.tool_calls]
        if any(not value for value in ids) or len(ids) != len(set(ids)):
            raise ValueError("tool call IDs must be unique and nonempty within a response")
        names = {tool.name for tool in tools}
        for call in message.tool_calls:
            if call.protocol_error or call.name in names:
                continue
            matches = [name for name in names if name.endswith(f"_{call.name}")]
            if len(matches) == 1:
                changes.append(f"tool_namespace:{call.name}->{matches[0]}")
                call.name = matches[0]
    except ValueError as exc:
        result.protocol_error = result.protocol_error or ModelProtocolIssue(code="invalid_response_envelope", message=str(exc))
        # Do not leave dangling tool calls in the replay transcript. The raw
        # envelope remains in response_adaptations and the model receives repair feedback.
        message.tool_calls = []
    if changes or result.protocol_error or any(call.protocol_error for call in message.tool_calls):
        result.raw_message = raw
    return result
