"""Canonical, provider-neutral contracts for the self-hosted agent runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    LIMIT_EXCEEDED = "limit_exceeded"


class ObservationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    capability: str = ""
    risk_level: str = "low"


class ModelProtocolIssue(BaseModel):
    code: str
    message: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None
    protocol_error: ModelProtocolIssue | None = None


class ToolObservation(BaseModel):
    call_id: str
    tool_name: str
    status: ObservationStatus
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    operation_id: str | None = None
    started_at: str = Field(default_factory=utc_now)
    completed_at: str = Field(default_factory=utc_now)
    usage: dict[str, int] = Field(default_factory=dict)
    terminate: bool = False

    @property
    def success(self) -> bool:
        return self.status == ObservationStatus.SUCCEEDED


class AgentMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    # OpenAI-compatible reasoning models such as DeepSeek return this field on
    # assistant tool-call messages and require it to be echoed verbatim on the
    # next request.  It is part of the resumable transcript, not display text.
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None


class ModelUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_hit_input_tokens: int = 0
    cache_miss_input_tokens: int = 0

    def add(self, other: "ModelUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        self.cache_hit_input_tokens += other.cache_hit_input_tokens
        self.cache_miss_input_tokens += other.cache_miss_input_tokens


class ModelRequest(BaseModel):
    model: str
    messages: list[AgentMessage]
    tools: list[ToolDefinition] = Field(default_factory=list)
    temperature: float = 0.0
    max_tokens: int = 2_000
    thinking: Literal["enabled", "disabled"] | None = None
    # Optional OpenAI-compatible structured-output mode (e.g. json_object).
    # ``None`` keeps the default free-form completion behavior.
    response_format: dict[str, Any] | None = None


class ModelResponse(BaseModel):
    message: AgentMessage
    finish_reason: str = ""
    usage: ModelUsage = Field(default_factory=ModelUsage)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    raw_message: AgentMessage | None = None
    normalizations: list[str] = Field(default_factory=list)
    protocol_error: ModelProtocolIssue | None = None


class AgentLimits(BaseModel):
    max_turns: int = Field(default=8, ge=1, le=64)
    max_model_calls: int = Field(default=12, ge=1, le=128)
    max_model_retries_per_turn: int = Field(default=2, ge=0, le=8)
    max_tool_calls: int = Field(default=8, ge=0, le=128)
    max_duration_seconds: int = Field(default=300, ge=1, le=3_600)
    max_model_call_seconds: int = Field(default=60, ge=1, le=600)
    max_output_tokens_per_call: int = Field(default=2_000, ge=64, le=32_000)
    # Zero disables cumulative token gating. Per-call output, duration and
    # context-size limits remain independent runtime safeguards.
    max_total_tokens: int = Field(default=0, ge=0)
    max_context_chars: int = Field(default=80_000, ge=4_000, le=2_000_000)
    # Zero allocates half the message budget to protected initial context.
    max_initial_context_chars: int = Field(default=0, ge=0, le=2_000_000)
    max_repair_attempts: int = Field(default=2, ge=0, le=8)


class AgentSpec(BaseModel):
    name: str
    role: str
    objective: str
    model: str
    system_prompt: str
    allowed_tools: frozenset[str] = Field(default_factory=frozenset)
    capabilities: frozenset[str] = Field(default_factory=frozenset)
    limits: AgentLimits = Field(default_factory=AgentLimits)


class AgentJob(BaseModel):
    job_id: str
    task_id: str
    agent_name: str
    target: str
    objective: str
    step_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str | None = None


class AgentEvent(BaseModel):
    type: str
    run_id: str
    agent_name: str
    task_id: str
    at: str = Field(default_factory=utc_now)
    turn: int = 0
    tool_call_id: str | None = None
    tool_name: str | None = None
    status: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class AgentRun(BaseModel):
    run_id: str
    job: AgentJob
    spec_name: str
    status: RunStatus = RunStatus.CREATED
    messages: list[AgentMessage] = Field(default_factory=list)
    context_projections: list[dict[str, Any]] = Field(default_factory=list)
    response_adaptations: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    turn: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    repair_attempts: int = 0
    usage: ModelUsage = Field(default_factory=ModelUsage)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    started_at: str = Field(default_factory=utc_now)
    completed_at: str | None = None
    workspace_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.LIMIT_EXCEEDED,
        }


class AgentRunResult(BaseModel):
    run_id: str
    agent_name: str
    status: RunStatus
    output: dict[str, Any] = Field(default_factory=dict)
    observations: list[ToolObservation] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    turns: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == RunStatus.COMPLETED
