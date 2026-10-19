"""Capability sessions, policy enforcement and durable leaf-tool execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from webagent.store import (
    OPERATION_CANCELLED,
    OPERATION_FAILED,
    OPERATION_OUTCOME_UNKNOWN,
    OPERATION_RUNNING,
    OPERATION_SUCCEEDED,
    CheckpointStore,
)
from webagent.store import compact_operation_result

from .protocol import (
    ObservationStatus,
    ToolCall,
    ToolDefinition,
    ToolObservation,
    utc_now,
)


class ToolExecutionBackend(Protocol):
    registry: Any

    async def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]: ...


class ToolPolicy(Protocol):
    async def authorize(
        self,
        session: "ToolSession",
        call: ToolCall,
        definition: ToolDefinition,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ToolPolicyDecision:
    """pi-style pre-execution decision returned by capability policies."""

    arguments: dict[str, Any] | None = None
    blocked: bool = False
    reason: str = ""

    @classmethod
    def allow(cls, arguments: dict[str, Any]) -> "ToolPolicyDecision":
        return cls(arguments=dict(arguments))

    @classmethod
    def block(cls, reason: str) -> "ToolPolicyDecision":
        return cls(blocked=True, reason=str(reason or "tool call blocked"))


@dataclass(frozen=True)
class ToolAfterCallDecision:
    """Post-execution override returned by an optional policy hook.

    ``details``/``content`` affect the model-facing observation only; canonical
    operation results remain unchanged. ``terminate`` closes further tool use
    and reserves the next model turn for terminal synthesis.
    """

    content: str | None = None
    details: dict[str, Any] | None = None
    is_error: bool | None = None
    reason: str = ""
    usage: dict[str, int] | None = None
    terminate: bool = False

    @classmethod
    def result(
        cls,
        *,
        content: str | None = None,
        details: dict[str, Any] | None = None,
        is_error: bool | None = None,
        reason: str = "",
        usage: dict[str, int] | None = None,
        terminate: bool = False,
    ) -> "ToolAfterCallDecision":
        return cls(
            content=content,
            details=details,
            is_error=is_error,
            reason=reason,
            usage=usage,
            terminate=terminate,
        )


class ToolPolicyError(ValueError):
    pass


ToolResultProjector = Callable[
    [str, dict[str, Any], dict[str, Any]], dict[str, Any]
]


def _identity(
    *,
    run_id: str,
    call: ToolCall,
    arguments: dict[str, Any],
) -> tuple[str, str]:
    request_json = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
    # Provider-generated call ids are not stable across a process restart.
    # A bounded AgentRun may not repeat an identical operation, so run + tool +
    # normalized input is the durable idempotency boundary.
    raw = f"{run_id}|{call.name}|{fingerprint}"
    return "agent-op-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32], fingerprint


def _validate_json_schema(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
) -> None:
    """Validate the JSON-schema subset used by the project's ToolSpecs."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ToolPolicyError(f"{path} must be an object")
        required = schema.get("required") or []
        for key in required:
            if key not in value or value[key] is None or value[key] == "":
                raise ToolPolicyError(f"{path}.{key} is required")
        properties = schema.get("properties") or {}
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                _validate_json_schema(item, child_schema, path=f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ToolPolicyError(f"{path}.{key} is not allowed")
    elif expected == "array":
        if not isinstance(value, list):
            raise ToolPolicyError(f"{path} must be an array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_json_schema(item, item_schema, path=f"{path}[{index}]")
    elif expected == "string" and not isinstance(value, str):
        raise ToolPolicyError(f"{path} must be a string")
    elif expected == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        raise ToolPolicyError(f"{path} must be an integer")
    elif expected == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        raise ToolPolicyError(f"{path} must be a number")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ToolPolicyError(f"{path} must be a boolean")

    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise ToolPolicyError(f"{path} must be one of {allowed}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if schema.get("minimum") is not None and value < schema["minimum"]:
            raise ToolPolicyError(f"{path} must be >= {schema['minimum']}")
        if schema.get("maximum") is not None and value > schema["maximum"]:
            raise ToolPolicyError(f"{path} must be <= {schema['maximum']}")
    if isinstance(value, str):
        if schema.get("minLength") is not None and len(value) < schema["minLength"]:
            raise ToolPolicyError(f"{path} is shorter than minLength")
        if schema.get("maxLength") is not None and len(value) > schema["maxLength"]:
            raise ToolPolicyError(f"{path} is longer than maxLength")
    if isinstance(value, list):
        if schema.get("minItems") is not None and len(value) < schema["minItems"]:
            raise ToolPolicyError(f"{path} has fewer than minItems")
        if schema.get("maxItems") is not None and len(value) > schema["maxItems"]:
            raise ToolPolicyError(f"{path} has more than maxItems")


@dataclass
class ToolSession:
    session_id: str
    run_id: str
    task_id: str
    agent_name: str
    target: str
    allowed_tools: frozenset[str]
    state: dict[str, Any]
    executor: ToolExecutionBackend
    policy: ToolPolicy
    definitions: dict[str, ToolDefinition] = field(default_factory=dict)
    result_projector: ToolResultProjector | None = None
    checkpoint_store: CheckpointStore | None = None
    step_id: str | None = None
    raw_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    operation_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def available_definitions(self) -> list[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for tool_name in sorted(self.allowed_tools):
            override = self.definitions.get(tool_name)
            if override is not None:
                definitions.append(override)
                continue
            spec = self.executor.registry.get(tool_name)
            definitions.append(
                ToolDefinition(
                    name=spec.name,
                    description=(
                        f"{spec.display_name}. Category={spec.category}; "
                        f"risk={spec.risk_level}."
                    ),
                    input_schema=dict(spec.input_schema or {}),
                )
            )
        return definitions


class ToolGateway:
    async def execute(
        self,
        session: ToolSession,
        call: ToolCall,
    ) -> ToolObservation:
        started_at = utc_now()
        if call.name not in session.allowed_tools:
            return ToolObservation(
                call_id=call.id,
                tool_name=call.name,
                status=ObservationStatus.BLOCKED,
                error=f"{session.agent_name} is not allowed to use {call.name}",
                started_at=started_at,
            )

        try:
            definition = next(
                item
                for item in session.available_definitions()
                if item.name == call.name
            )
            # Domain policies may normalize arguments before execution.
            # Coding policies opt into raw-envelope validation so their
            # file/process contracts are enforced before
            # normalization.
            if bool(getattr(session.policy, "validate_raw_arguments", False)):
                _validate_json_schema(call.arguments, dict(definition.input_schema or {}))
            before_tool_call = getattr(session.policy, "before_tool_call", None)
            if before_tool_call is not None:
                decision = await before_tool_call(session, call, definition)
                if decision.blocked:
                    return ToolObservation(
                        call_id=call.id,
                        tool_name=call.name,
                        status=ObservationStatus.BLOCKED,
                        error=decision.reason,
                        started_at=started_at,
                    )
                normalized = dict(decision.arguments or {})
            else:
                normalized = await session.policy.authorize(session, call, definition)
            spec = session.executor.registry.get(call.name)
            _validate_json_schema(normalized, dict(spec.input_schema or {}))
        except (KeyError, StopIteration, ToolPolicyError, TypeError, ValueError) as exc:
            return ToolObservation(
                call_id=call.id,
                tool_name=call.name,
                status=ObservationStatus.BLOCKED,
                error=str(exc),
                started_at=started_at,
            )

        operation_id, fingerprint = _identity(
            run_id=session.run_id,
            call=call,
            arguments=normalized,
        )
        side_effect = str(getattr(spec, "risk_level", "low")).lower() not in {
            "none",
            "low",
            "passive",
        }
        replayable = call.name not in {
            "workspace_list",
            "workspace_read",
            "browser_snapshot",
            "browser_wait",
        }
        in_memory_result = session.operation_results.get(operation_id)
        if replayable and isinstance(in_memory_result, dict):
            raw_result = dict(in_memory_result)
            session.raw_results[call.id] = raw_result
            return await self._observation(
                session=session,
                call=call,
                raw_result=raw_result,
                operation_id=operation_id,
                started_at=started_at,
                replayed=True,
            )
        store = session.checkpoint_store
        if store is not None:
            existing = store.prepare_operation(
                operation_id=operation_id,
                task_id=session.task_id,
                step_id=session.step_id,
                tool_name=call.name,
                side_effect=side_effect,
                request_fingerprint=fingerprint,
                parameters=normalized,
            )
            if replayable and existing["status"] == OPERATION_SUCCEEDED and isinstance(
                existing.get("result"), dict
            ):
                raw_result = dict(existing["result"])
                session.raw_results[call.id] = raw_result
                session.operation_results[operation_id] = raw_result
                return await self._observation(
                    session=session,
                    call=call,
                    raw_result=raw_result,
                    operation_id=operation_id,
                    started_at=started_at,
                    replayed=True,
                )
            if existing["status"] in {
                OPERATION_RUNNING,
                OPERATION_OUTCOME_UNKNOWN,
            }:
                return ToolObservation(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ObservationStatus.OUTCOME_UNKNOWN,
                    error=(
                        "A previous execution did not reach a durable terminal "
                        "state; the operation was not replayed."
                    ),
                    operation_id=operation_id,
                    started_at=started_at,
                )
            try:
                store.update_operation(operation_id, OPERATION_RUNNING)
            except KeyError:
                # A task can be explicitly deleted by a user while a queued
                # Agent turn is beginning.  Do not start a tool without its
                # idempotency record, and never let that race crash AgentLoop.
                return self._missing_operation_observation(
                    call=call,
                    operation_id=operation_id,
                    started_at=started_at,
                )

        try:
            # Coding tools need the gateway's durable idempotency identity so
            # their artifact/mutation records can be joined to operation_ledger.
            # This is injected only after model-facing schema validation and
            # only for coding tools and source acquisition that creates workspace artifacts.
            execution_input = dict(normalized)
            if call.name.startswith("workspace_") or call.name in {
                "python_run", "js_asset_crawl",
            }:
                execution_input["operation_id"] = operation_id
            async with asyncio.timeout(max(1, int(spec.timeout_seconds or 180))):
                raw_result = await session.executor.execute(
                    call.name,
                    execution_input,
                    session.state,
                )
        except asyncio.CancelledError:
            if store is not None:
                try:
                    store.update_operation(
                        operation_id,
                        OPERATION_OUTCOME_UNKNOWN if side_effect else OPERATION_CANCELLED,
                        error="agent run cancelled during tool execution",
                    )
                except KeyError:
                    # The caller is already unwinding a cancelled run.  A
                    # concurrent terminal task deletion must not replace that
                    # cancellation with an unrelated ledger exception.
                    pass
            raise
        except TimeoutError:
            if store is not None:
                try:
                    store.update_operation(
                        operation_id,
                        OPERATION_OUTCOME_UNKNOWN if side_effect else OPERATION_FAILED,
                        error="tool execution timed out",
                    )
                except KeyError:
                    return self._missing_operation_observation(
                        call=call,
                        operation_id=operation_id,
                        started_at=started_at,
                        completed_after_execution=True,
                    )
            return ToolObservation(
                call_id=call.id,
                tool_name=call.name,
                status=(
                    ObservationStatus.OUTCOME_UNKNOWN
                    if side_effect
                    else ObservationStatus.TIMED_OUT
                ),
                error="tool execution timed out",
                operation_id=operation_id,
                started_at=started_at,
            )
        except Exception as exc:
            if store is not None:
                try:
                    store.update_operation(
                        operation_id,
                        OPERATION_FAILED,
                        error=str(exc),
                    )
                except KeyError:
                    return self._missing_operation_observation(
                        call=call,
                        operation_id=operation_id,
                        started_at=started_at,
                        completed_after_execution=True,
                    )
            return ToolObservation(
                call_id=call.id,
                tool_name=call.name,
                status=ObservationStatus.FAILED,
                error=str(exc),
                operation_id=operation_id,
                started_at=started_at,
            )

        session.raw_results[call.id] = raw_result
        if replayable and raw_result.get("success"):
            session.operation_results[operation_id] = dict(raw_result)
        if store is not None:
            try:
                store.update_operation(
                    operation_id,
                    OPERATION_OUTCOME_UNKNOWN if raw_result.get("execution_state") == "outcome_unknown"
                    else OPERATION_SUCCEEDED if raw_result.get("success") else OPERATION_FAILED,
                    result=compact_operation_result(raw_result),
                    error=str(raw_result.get("error") or "") or None,
                )
            except KeyError:
                # The operation did run, but its durable record was removed
                # externally.  Its outcome can no longer be safely resumed or
                # replayed, so surface a controlled terminal observation.
                return self._missing_operation_observation(
                    call=call,
                    operation_id=operation_id,
                    started_at=started_at,
                    completed_after_execution=True,
                )
        return await self._observation(
            session=session,
            call=call,
            raw_result=raw_result,
            operation_id=operation_id,
            started_at=started_at,
        )

    @staticmethod
    def _missing_operation_observation(
        *,
        call: ToolCall,
        operation_id: str,
        started_at: str,
        completed_after_execution: bool = False,
    ) -> ToolObservation:
        phase = "after tool execution" if completed_after_execution else "before tool execution"
        return ToolObservation(
            call_id=call.id,
            tool_name=call.name,
            status=ObservationStatus.FAILED,
            error=(
                "operation ledger entry disappeared "
                f"{phase}; the tool result was not accepted"
            ),
            operation_id=operation_id,
            started_at=started_at,
        )

    @staticmethod
    async def _observation(
        *,
        session: ToolSession,
        call: ToolCall,
        raw_result: dict[str, Any],
        operation_id: str,
        started_at: str,
        replayed: bool = False,
    ) -> ToolObservation:
        output = (
            session.result_projector(call.name, call.arguments, raw_result)
            if session.result_projector is not None
            else raw_result
        )
        after_hook = getattr(session.policy, "after_tool_call", None)
        after = ToolAfterCallDecision()
        if after_hook is not None:
            try:
                after = await after_hook(session, call, raw_result)
                if not isinstance(after, ToolAfterCallDecision):
                    raise TypeError(
                        "after_tool_call must return ToolAfterCallDecision"
                    )
            except Exception as exc:
                return ToolObservation(
                    call_id=call.id,
                    tool_name=call.name,
                    status=ObservationStatus.FAILED,
                    error=f"after_tool_call failed: {exc}",
                    operation_id=operation_id,
                    started_at=started_at,
                )
        if after.details is not None:
            output = dict(after.details)
        if after.content is not None:
            output = {**output, "content": after.content}
        if replayed and isinstance(output, dict):
            output = {**output, "replayed": True}
        error = str(raw_result.get("error") or "") or None
        if raw_result.get("execution_state") == "outcome_unknown":
            status = ObservationStatus.OUTCOME_UNKNOWN
        elif raw_result.get("blocked") is True:
            status = ObservationStatus.BLOCKED
        else:
            status = (
                ObservationStatus.SUCCEEDED
                if bool(raw_result.get("success"))
                else ObservationStatus.FAILED
            )
        if after.is_error is True:
            status = ObservationStatus.FAILED
        elif after.is_error is False:
            status = ObservationStatus.SUCCEEDED
        if after.is_error is True:
            error = after.reason or error
        elif after.is_error is False:
            error = None
        return ToolObservation(
            call_id=call.id,
            tool_name=call.name,
            status=status,
            output=output if isinstance(output, dict) else {"value": output},
            error=error,
            operation_id=operation_id,
            started_at=started_at,
            usage={
                key: int(value)
                for key, value in (after.usage or {}).items()
                if type(value) is int
            },
            terminate=bool(after.terminate),
        )
