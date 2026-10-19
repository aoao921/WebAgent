"""Provider-neutral model-tool loop for a single independent AgentRun."""

from __future__ import annotations

import asyncio
import json

from .harness import AgentHarness, FinalOutputRejected
from .model_client import complete_adapted
from .protocol import (
    AgentEvent,
    AgentJob,
    AgentMessage,
    AgentRun,
    AgentRunResult,
    AgentSpec,
    ModelRequest,
    ObservationStatus,
    RunStatus,
    ToolObservation,
    utc_now,
)


class AgentLimitExceeded(RuntimeError):
    pass


TOOL_FACE_CLOSED_MESSAGE = (
    "Target and context tools are no longer exposed for this run. Return the "
    "required terminal result now; do not emit another tool call."
)


class AgentLoop:
    async def execute(
        self,
        *,
        spec: AgentSpec,
        job: AgentJob,
        harness: AgentHarness,
        resume: AgentRun | None = None,
    ) -> AgentRunResult:
        run = resume or AgentRun(
            run_id=job.job_id,
            job=job,
            spec_name=spec.name,
        )
        if run.is_terminal:
            return self._result(run)
        if not run.messages:
            run.messages = await harness.initial_messages(job)

        run.status = RunStatus.RUNNING
        await self._emit(harness, run, "agent_started", data={
            "messages": [message.model_dump(mode="json", exclude={"reasoning_content"})
                         for message in run.messages],
        })
        await harness.checkpoint(run)
        try:
            async with asyncio.timeout(spec.limits.max_duration_seconds):
                await self._drive(run, harness)
        except asyncio.CancelledError as exc:
            paused = any(str(value).strip().lower() == "pause" for value in exc.args)
            run.status = RunStatus.PAUSED if paused else RunStatus.CANCELLED
            run.error = "agent run paused" if paused else "agent run cancelled"
            run.completed_at = None if paused else utc_now()
            await self._emit(
                harness,
                run,
                "agent_paused" if paused else "agent_cancelled",
                status=run.status.value,
            )
            await harness.checkpoint(run)
            raise
        except TimeoutError:
            run.status = RunStatus.TIMED_OUT
            run.error = "agent run exceeded its duration budget"
            run.completed_at = utc_now()
            await self._finalize_interrupted(run, harness, run.error)
            await self._emit(harness, run, "agent_timed_out", status=run.status.value)
            await harness.checkpoint(run)
        except AgentLimitExceeded as exc:
            await self._limit(run, harness, str(exc))
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = str(exc)
            run.completed_at = utc_now()
            await self._finalize_interrupted(run, harness, run.error)
            await self._emit(
                harness,
                run,
                "agent_failed",
                status=run.status.value,
                data={"error": str(exc)},
            )
            await harness.checkpoint(run)
        return self._result(run)

    async def _drive(self, run: AgentRun, harness: AgentHarness) -> None:
        limits = harness.spec.limits
        while not run.is_terminal:
            if run.turn >= limits.max_turns or run.model_calls >= limits.max_model_calls:
                await self._limit(run, harness, "model turn budget exceeded")
                return
            if (
                limits.max_total_tokens > 0
                and run.usage.total_tokens >= limits.max_total_tokens
            ):
                await self._limit(run, harness, "token budget exceeded")
                return

            run.turn += 1
            tools = (
                []
                if any(item.terminate for item in run.observations)
                else await harness.tool_definitions(run)
            )
            tool_face_closed = not tools
            if tool_face_closed and not any(
                message.role == "user"
                and message.content == TOOL_FACE_CLOSED_MESSAGE
                for message in run.messages
            ):
                run.messages.append(
                    AgentMessage(role="user", content=TOOL_FACE_CLOSED_MESSAGE)
                )
                await harness.checkpoint(run)
            await self._emit(
                harness,
                run,
                "model_started",
                data={"available_tools": [item.name for item in tools]},
            )
            response = await self._complete_model(run, harness, tools)
            run.usage.add(response.usage)
            if response.raw_message is not None:
                run.response_adaptations.append({
                    "message_index": len(run.messages),
                    "raw_message": response.raw_message.model_dump(mode="json"),
                    "normalizations": response.normalizations,
                    "protocol_error": response.protocol_error.model_dump() if response.protocol_error else None,
                    "finish_reason": response.finish_reason,
                    "provider_metadata": response.provider_metadata,
                })
            run.messages.append(response.message)
            await self._emit(
                harness,
                run,
                "model_completed",
                data={
                    "finish_reason": response.finish_reason,
                    "tool_calls": [call.name for call in response.message.tool_calls],
                    "usage": response.usage.model_dump(mode="json"),
                    "normalizations": response.normalizations,
                    "protocol_error": response.protocol_error.model_dump() if response.protocol_error else None,
                    "response_adaptation_index": len(run.response_adaptations) - 1 if response.raw_message else None,
                    "message": response.message.model_dump(mode="json", exclude={"reasoning_content"}),
                },
            )
            await harness.checkpoint(run)

            # A provider can account the final answer just beyond the cumulative
            # budget. Preserve a tool-free terminal answer and subject it to the
            # harness' strict finalize contract; only deny further tool work.
            if (
                limits.max_total_tokens > 0
                and run.usage.total_tokens > limits.max_total_tokens
                and response.message.tool_calls
            ):
                await self._limit(run, harness, "token budget exceeded")
                return

            if response.protocol_error:
                await self._reject_output(
                    run, harness, FinalOutputRejected(
                        f"{response.protocol_error.code}: {response.protocol_error.message}"
                    ), stage="model_protocol",
                )
                continue

            if response.message.tool_calls:
                if run.tool_calls + len(response.message.tool_calls) > limits.max_tool_calls:
                    await self._limit(run, harness, "tool call budget exceeded")
                    return
                presented_tools = {item.name for item in tools}
                terminated_call_id = ""
                for call_index, call in enumerate(response.message.tool_calls):
                    requested_tool_name = next(
                        (raw_call.name for raw_call in response.raw_message.tool_calls if raw_call.id == call.id),
                        call.name,
                    ) if response.raw_message else call.name
                    await self._emit(
                        harness,
                        run,
                        "tool_started",
                        tool_call_id=call.id,
                        tool_name=call.name,
                        data={
                            "arguments": call.arguments,
                            "requested_tool_name": requested_tool_name,
                        },
                    )
                    if call.protocol_error:
                        observation = ToolObservation(
                            call_id=call.id, tool_name=call.name,
                            status=ObservationStatus.BLOCKED,
                            error=f"{call.protocol_error.code}: {call.protocol_error.message}",
                        )
                    elif terminated_call_id:
                        observation = ToolObservation(
                            call_id=call.id,
                            tool_name=call.name,
                            status=ObservationStatus.BLOCKED,
                            error=(
                                "after_tool_call terminated further tool use; "
                                "the next model turn must synthesize a final result"
                            ),
                        )
                    elif call_index > 0 and not harness.continue_tool_batch(run):
                        observation = ToolObservation(
                            call_id=call.id,
                            tool_name=call.name,
                            status=ObservationStatus.BLOCKED,
                            error=(
                                "The harness reserved the remaining model turn for "
                                "evidence synthesis after an earlier tool produced "
                                "the terminal or stagnation condition."
                            ),
                        )
                    elif call.name not in presented_tools:
                        observation = ToolObservation(
                            call_id=call.id,
                            tool_name=call.name,
                            status=ObservationStatus.BLOCKED,
                            error=(
                                f"{requested_tool_name} was not exposed in this reasoning "
                                "turn and cannot be executed"
                            ),
                        )
                    else:
                        observation = await harness.execute_tool(run, call)
                    run.tool_calls += 1
                    run.observations.append(observation)
                    run.messages.append(
                        AgentMessage(
                            role="tool",
                            name=call.name,
                            tool_call_id=call.id,
                            content=json.dumps(
                                observation.model_dump(mode="json"),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    )
                    await self._emit(
                        harness,
                        run,
                        "tool_completed",
                        tool_call_id=call.id,
                        tool_name=call.name,
                        status=observation.status.value,
                        data={
                            "operation_id": observation.operation_id,
                            "error": observation.error,
                            "output": observation.output,
                            "usage": observation.usage,
                        },
                    )
                    await harness.checkpoint(run)
                    if observation.terminate:
                        terminated_call_id = call.id
                if tool_face_closed:
                    await self._reject_output(
                        run,
                        harness,
                        FinalOutputRejected(TOOL_FACE_CLOSED_MESSAGE),
                        stage="tool_face_closed",
                    )
                continue

            try:
                run.result = await harness.finalize(run, response.message.content)
            except FinalOutputRejected as exc:
                await self._reject_output(run, harness, exc, stage="domain_output")
                continue

            run.status = RunStatus.COMPLETED
            run.completed_at = utc_now()
            await self._emit(
                harness,
                run,
                "agent_completed",
                status=run.status.value,
                data={
                    "output_keys": sorted(run.result),
                    "success": bool(run.result.get("success", True)),
                },
            )
            await harness.checkpoint(run)

    async def _reject_output(
        self, run: AgentRun, harness: AgentHarness, error: FinalOutputRejected, *, stage: str,
    ) -> None:
        if error.counts_as_repair_attempt:
            run.repair_attempts += 1
        if error.counts_as_repair_attempt and run.repair_attempts > harness.spec.limits.max_repair_attempts:
            run.status = RunStatus.FAILED
            run.error = f"final output rejected: {error}"
            run.completed_at = utc_now()
            await self._finalize_interrupted(run, harness, run.error)
            await self._emit(harness, run, "agent_failed", status=run.status.value,
                             data={"error": run.error, "stage": stage})
        else:
            run.messages.append(harness.repair_message(str(error)))
            await self._emit(harness, run, "final_output_rejected", data={
                "error": str(error), "stage": stage, "attempt": run.repair_attempts,
                "counts_as_repair_attempt": error.counts_as_repair_attempt,
                "message": run.messages[-1].model_dump(mode="json", exclude={"reasoning_content"}),
            })
        await harness.checkpoint(run)

    async def _complete_model(
        self,
        run: AgentRun,
        harness: AgentHarness,
        tools,
    ):
        limits = harness.spec.limits
        attempt = 0
        while True:
            if run.model_calls >= limits.max_model_calls:
                raise AgentLimitExceeded("model call budget exceeded during retry")
            try:
                response = await asyncio.wait_for(
                    complete_adapted(
                        harness.model_client, ModelRequest(
                            model=harness.spec.model,
                            messages=await harness.model_messages(run),
                            tools=tools,
                            max_tokens=limits.max_output_tokens_per_call,
                            thinking=harness.model_thinking(run),
                            response_format=harness.model_response_format(run),
                        ),
                        expect_json=harness.FINAL_OUTPUT_FORMAT == "json_object",
                        call_prefix=f"{run.run_id}:{run.turn}",
                    ),
                    timeout=limits.max_model_call_seconds,
                )
                run.model_calls += 1
                return response
            except asyncio.CancelledError:
                raise
            except TimeoutError as exc:
                run.model_calls += 1
                # Retrying an already stalled provider call makes the UI look
                # frozen.  Let the owning agent decide whether its collected
                # evidence supports a deterministic/degraded handoff.
                raise AgentLimitExceeded(
                    f"model call exceeded {limits.max_model_call_seconds}s timeout"
                ) from exc
            except Exception as exc:
                run.model_calls += 1
                if attempt >= limits.max_model_retries_per_turn:
                    raise
                attempt += 1
                await self._emit(
                    harness,
                    run,
                    "model_retrying",
                    data={"attempt": attempt, "error": str(exc)},
                )
                await harness.checkpoint(run)
                await asyncio.sleep(min(1.0, 0.2 * (2 ** (attempt - 1))))

    async def _limit(
        self,
        run: AgentRun,
        harness: AgentHarness,
        reason: str,
    ) -> None:
        run.status = RunStatus.LIMIT_EXCEEDED
        run.error = reason
        run.completed_at = utc_now()
        await self._finalize_interrupted(run, harness, reason)
        await self._emit(
            harness,
            run,
            "agent_limit_exceeded",
            status=run.status.value,
            data={"error": reason},
        )
        await harness.checkpoint(run)

    @staticmethod
    async def _finalize_interrupted(
        run: AgentRun,
        harness: AgentHarness,
        reason: str,
    ) -> None:
        try:
            result = await harness.finalize_interrupted(run, reason)
        except Exception as exc:
            run.result = {
                **run.result,
                "interrupted_finalize_error": str(exc)[:800],
            }
            return
        if isinstance(result, dict):
            run.result = result

    @staticmethod
    async def _emit(
        harness: AgentHarness,
        run: AgentRun,
        event_type: str,
        *,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        data: dict | None = None,
    ) -> None:
        await harness.emit(
            AgentEvent(
                type=event_type,
                run_id=run.run_id,
                agent_name=harness.spec.name,
                task_id=run.job.task_id,
                turn=run.turn,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=status,
                data=data or {},
            )
        )

    @staticmethod
    def _result(run: AgentRun) -> AgentRunResult:
        return AgentRunResult(
            run_id=run.run_id,
            agent_name=run.spec_name,
            status=run.status,
            output=run.result,
            observations=run.observations,
            usage=run.usage,
            turns=run.turn,
            model_calls=run.model_calls,
            tool_calls=run.tool_calls,
            error=run.error,
        )
