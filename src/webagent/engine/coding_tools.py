"""Generic coding capabilities exposed through the canonical ToolGateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from webagent.specs import ToolSpec
from .process_runtime import ProcessLimits, ProcessRuntime
from .protocol import ToolDefinition, ToolCall
from .sandbox_executor import SandboxExecutor
from .workspace import WorkspaceRuntime


WORKSPACE_LIST_TOOL = "workspace_list"
WORKSPACE_READ_TOOL = "workspace_read"
WORKSPACE_WRITE_TOOL = "workspace_write"
PYTHON_RUN_TOOL = "python_run"

CODING_TOOL_NAMES = frozenset(
    {
        WORKSPACE_LIST_TOOL,
        WORKSPACE_READ_TOOL,
        WORKSPACE_WRITE_TOOL,
        PYTHON_RUN_TOOL,
    }
)


@dataclass
class CodingCapabilityPolicy:
    """Policy for run-scoped coding tools.

    The policy is intentionally capability-based.  It does not inspect or
    rewrite model-authored source code; it limits what resources the runtime
    can touch and whether mutation/process capabilities are granted.
    """

    workspace: WorkspaceRuntime
    allow_mutation: bool = False
    allow_process: bool = False
    allow_python: bool = False
    validate_raw_arguments: bool = True

    async def authorize(self, session, call: ToolCall, definition: ToolDefinition) -> dict[str, Any]:
        del session, definition
        if call.name not in CODING_TOOL_NAMES:
            raise ValueError(f"unsupported coding tool: {call.name}")
        if call.name == WORKSPACE_WRITE_TOOL and not self.allow_mutation:
            raise ValueError("workspace mutation capability is not granted")
        if call.name == PYTHON_RUN_TOOL and not (
            self.allow_process and self.allow_python
        ):
            raise ValueError("python capability is not granted")
        return dict(call.arguments or {})


class CodingToolBackend:
    """Tool backend for a single WorkspaceRuntime."""

    def __init__(
        self,
        workspace: WorkspaceRuntime,
        *,
        process_limits: ProcessLimits | None = None,
        allowed_executables: set[str] | frozenset[str] | None = None,
        allowed_env_keys: set[str] | frozenset[str] | None = None,
        sandbox_executor: SandboxExecutor | None = None,
    ) -> None:
        self.workspace = workspace
        self.process = ProcessRuntime(
            workspace,
            limits=process_limits,
            allowed_executables=allowed_executables,
            allowed_env_keys=allowed_env_keys,
            executor=sandbox_executor,
        )
        # Single source of truth: the runtime ToolSpec schema is derived from
        # the model-facing definitions so both views cannot drift apart.
        definitions = build_coding_tool_definitions()
        self.registry = {
            name: ToolSpec(
                name=name,
                display_name=name.replace("_", " "),
                runtime="agent_coding_workspace",
                category="workspace" if name.startswith("workspace_") else "process",
                risk_level="high" if name in {WORKSPACE_WRITE_TOOL, PYTHON_RUN_TOOL} else "low",
                timeout_seconds=90,
                input_schema=dict(definitions[name].input_schema or {}),
            )
            for name in CODING_TOOL_NAMES
        }

    async def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        data = input_data if isinstance(input_data, dict) else {}
        operation_id = str(data.get("operation_id") or f"{tool_name}-operation")
        if tool_name == WORKSPACE_LIST_TOOL:
            return {"success": True, "items": self.workspace.list(str(data.get("prefix") or ""))}
        if tool_name == WORKSPACE_READ_TOOL:
            return {"success": True, **self.workspace.read(str(data.get("path") or ""))}
        if tool_name == WORKSPACE_WRITE_TOOL:
            return {
                "success": True,
                **self.workspace.write(
                    str(data.get("path") or ""),
                    str(data.get("content") or ""),
                    base_sha256=data.get("base_sha256"),
                    operation_id=operation_id,
                    media_type=str(data.get("media_type") or "text/plain"),
                ),
            }
        if tool_name == PYTHON_RUN_TOOL:
            code = str(data.get("code") or "")
            if not code:
                raise ValueError("python_run requires code")
            argv = [str(data.get("python") or "python"), "-c", code]
            process = await self.process.run(
                argv,
                cwd=str(data.get("cwd") or "."),
                env=data.get("env") if isinstance(data.get("env"), dict) else {},
                stdin=str(data.get("stdin") or ""),
                timeout_seconds=data.get("timeout_seconds"),
                operation_id=operation_id,
            )
            return {
                "success": process.get("status") == "succeeded",
                "process": process,
                "error": process.get("error") or None,
            }
        raise KeyError(tool_name)


def build_coding_tool_definitions() -> dict[str, ToolDefinition]:
    return {
        WORKSPACE_LIST_TOOL: ToolDefinition(
            name=WORKSPACE_LIST_TOOL,
            description="List files and directories inside the current AgentRun workspace.",
            input_schema={"type": "object", "properties": {"prefix": {"type": "string", "maxLength": 240}}, "additionalProperties": False},
        ),
        WORKSPACE_READ_TOOL: ToolDefinition(
            name=WORKSPACE_READ_TOOL,
            description="Read a bounded text file or a base64 binary summary from the current workspace.",
            input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string", "maxLength": 240}}, "additionalProperties": False},
        ),
        WORKSPACE_WRITE_TOOL: ToolDefinition(
            name=WORKSPACE_WRITE_TOOL,
            description="Atomically create or replace a workspace file. Use base_sha256 for edits to an existing file.",
            input_schema={"type": "object", "required": ["path", "content"], "properties": {"path": {"type": "string", "maxLength": 240}, "content": {"type": "string", "maxLength": 2_097_152}, "base_sha256": {"type": "string", "maxLength": 64}, "media_type": {"type": "string", "maxLength": 160}}, "additionalProperties": False},
        ),
        PYTHON_RUN_TOOL: ToolDefinition(
            name=PYTHON_RUN_TOOL,
            description="Run Python code as an argv-only process inside the current workspace.",
            input_schema={"type": "object", "required": ["code"], "properties": {"code": {"type": "string", "minLength": 1, "maxLength": 200_000}, "python": {"type": "string", "maxLength": 240}, "cwd": {"type": "string", "maxLength": 240}, "env": {"type": "object"}, "stdin": {"type": "string", "maxLength": 200_000}, "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 30}}, "additionalProperties": False},
        ),
    }


@dataclass
class CodingToolPackage:
    """ToolPackage view over a single CodingToolBackend."""

    backend: "CodingToolBackend"

    @property
    def name(self) -> str:
        return "coding"

    def specs(self) -> dict[str, ToolSpec]:
        return dict(self.backend.registry)

    def model_definitions(self) -> dict[str, ToolDefinition]:
        return build_coding_tool_definitions()

    async def execute(
        self,
        tool_name: str,
        input_data: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.backend.execute(tool_name, input_data, state)
