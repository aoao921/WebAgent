"""Agent harness boundaries used by the generic loop."""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
from typing import Awaitable, Callable

from .model_client import AgentModelClient
from .protocol import (
    AgentEvent,
    AgentJob,
    AgentMessage,
    AgentRun,
    AgentSpec,
    ToolDefinition,
    ToolObservation,
)
from .context import CONTEXT_READ, project_history
from .context_tool import CONTEXT_DEFINITION, ContextReadBackend
from .tooling import ToolGateway, ToolSession, _validate_json_schema


AgentEventSink = Callable[[AgentEvent], Awaitable[None] | None]


class FinalOutputRejected(ValueError):
    """The model stopped, but its answer is not a valid terminal agent result."""

    def __init__(
        self,
        message: str,
        *,
        counts_as_repair_attempt: bool = True,
    ) -> None:
        super().__init__(message)
        self.counts_as_repair_attempt = counts_as_repair_attempt


class AgentHarness(ABC):
    FINAL_OUTPUT_FORMAT = "text"
    FULL_TOOL_HISTORY = 8
    HISTORY_TOOL_TAIL = 24
    INITIAL_OPTIONAL_PATHS: tuple[tuple[str, ...], ...] = ()
    def __init__(
        self,
        *,
        spec: AgentSpec,
        model_client: AgentModelClient,
        tool_session: ToolSession,
        tool_gateway: ToolGateway | None = None,
        event_sink: AgentEventSink | None = None,
    ) -> None:
        self.spec = spec
        self.model_client = model_client
        self.tool_session = tool_session
        self.tool_gateway = tool_gateway or ToolGateway()
        self.event_sink = event_sink
        self._context_run: AgentRun | None = None

    @abstractmethod
    async def initial_messages(self, job: AgentJob) -> list[AgentMessage]:
        raise NotImplementedError

    async def tool_definitions(self, run: AgentRun) -> list[ToolDefinition]:
        definitions = self.tool_session.available_definitions()
        if not any(item.name == CONTEXT_READ for item in definitions):
            definitions.append(CONTEXT_DEFINITION)
        return definitions

    def model_thinking(self, run: AgentRun) -> str | None:
        """Optionally select a provider thinking mode for the next model call."""
        del run
        return None

    def model_response_format(self, run: AgentRun) -> dict | None:
        """Optionally force a structured-output mode (e.g. json_object)."""
        del run
        return None

    def project_model_message(
        self,
        run: AgentRun,
        message: AgentMessage,
        *,
        index: int,
        total: int,
    ) -> AgentMessage:
        """Project a durable transcript message into its model-facing view.

        Domain harnesses may keep rich observations in checkpoints while
        exposing a bounded working representation to the provider.
        """
        del run, index, total
        return message

    @staticmethod
    def _messages_size(items: list[AgentMessage]) -> int:
        return sum(
            len(
                json.dumps(
                    item.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            for item in items
        )

    @staticmethod
    def _truncate_message(message: AgentMessage, content_chars: int) -> AgentMessage:
        content_chars = max(0, int(content_chars))
        content = str(message.content or "")
        if len(content) > content_chars:
            marker = "\n...[runtime context truncation]"
            keep = max(0, content_chars - len(marker))
            content = content[:keep] + (marker if content_chars >= len(marker) else "")
        reasoning = message.reasoning_content
        if reasoning and len(reasoning) > 2_000:
            reasoning = reasoning[:2_000] + "\n...[reasoning compacted]"
        return message.model_copy(
            update={"content": content, "reasoning_content": reasoning}
        )

    async def model_messages(self, run: AgentRun) -> list[AgentMessage]:
        """Return a bounded transcript without breaking tool-call/result groups."""
        total = len(run.messages)
        messages = [
            self.project_model_message(run, item, index=index, total=total)
            for index, item in enumerate(run.messages)
        ]
        limit = self.spec.limits.max_context_chars
        initial_budget = self.spec.limits.max_initial_context_chars or max(4_000, limit // 2)
        initial_end = next(
            (i for i, item in enumerate(messages) if item.role in {"assistant", "tool"}),
            len(messages),
        )

        if self._messages_size(messages) <= limit and self._messages_size(messages[:initial_end]) <= initial_budget:
            self._context_run = run
            return messages

        bounded, manifest = project_history(
            run.messages,
            messages,
            limit=limit,
            initial_budget=initial_budget,
            optional_paths=self.INITIAL_OPTIONAL_PATHS,
            recent_tail=self.HISTORY_TOOL_TAIL,
        )
        self._context_run = run
        run.context_projections.append(manifest)
        return bounded
    async def execute_tool(
        self,
        run: AgentRun,
        call,
    ) -> ToolObservation:
        if call.name == CONTEXT_READ:
            from .protocol import ObservationStatus

            if self._context_run is not run:
                return ToolObservation(
                    call_id=call.id, tool_name=call.name,
                    status=ObservationStatus.BLOCKED,
                    error="context_read is only available for the active run",
                )
            try:
                _validate_json_schema(call.arguments, CONTEXT_DEFINITION.input_schema)
                output = await ContextReadBackend(run).execute(call.name, call.arguments, {})
                return ToolObservation(
                    call_id=call.id, tool_name=call.name,
                    status=ObservationStatus.SUCCEEDED, output=output,
                )
            except Exception as exc:
                return ToolObservation(
                    call_id=call.id, tool_name=call.name,
                    status=ObservationStatus.BLOCKED, error=str(exc),
                )
        return await self.tool_gateway.execute(self.tool_session, call)

    def continue_tool_batch(self, run: AgentRun) -> bool:
        """Return whether another tool from the same model turn may execute."""
        del run
        return True

    @abstractmethod
    async def finalize(self, run: AgentRun, content: str) -> dict:
        raise NotImplementedError

    async def finalize_interrupted(self, run: AgentRun, reason: str) -> dict | None:
        """Optionally preserve domain facts when a run ends before model finalization."""
        del run, reason
        return None

    async def emit(self, event: AgentEvent) -> None:
        if self.event_sink is None:
            return
        result = self.event_sink(event)
        if hasattr(result, "__await__"):
            await result

    async def checkpoint(self, run: AgentRun) -> None:
        """Hook for a domain harness to persist a resumable run snapshot."""

    def repair_message(self, error: str) -> AgentMessage:
        return AgentMessage(
            role="user",
            content=(
                "Your previous final response was not accepted by the runtime: "
                f"{error}. Continue the task. Use an available tool if evidence is "
                "missing; otherwise return a corrected final JSON object."
            ),
        )
