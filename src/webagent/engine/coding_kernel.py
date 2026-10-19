"""Reusable coding-agent substrate shared by standalone and domain agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from webagent.store import CheckpointStore

from .coding_tools import CodingCapabilityPolicy, CodingToolBackend, build_coding_tool_definitions
from .protocol import ToolDefinition
from .sandbox_executor import SandboxExecutor, build_sandbox_executor
from .tooling import ToolSession
from .workspace import WorkspaceLimits, WorkspaceRuntime, workspace_from_state


@dataclass
class CodingKernel:
    """Composition root for run-scoped coding capabilities.

    Domain agents retain their own authorization and HTTP policies.  They only
    compose this kernel when the job explicitly grants coding capabilities.
    """

    workspace: WorkspaceRuntime
    backend: CodingToolBackend
    policy: CodingCapabilityPolicy

    @classmethod
    def for_run(
        cls,
        *,
        state: dict[str, Any],
        task_id: str,
        run_id: str,
        mode: str = "task",
        workspace_limits: WorkspaceLimits | None = None,
        checkpoint_store: CheckpointStore | None = None,
        allowed_executables: set[str] | frozenset[str] | None = None,
        allow_mutation: bool = True,
        allow_process: bool = True,
        allow_python: bool = True,
        sandbox_mode: str = "native",
        sandbox_image: str = "",
        sandbox_network_policy: str = "none",
        sandbox_executor: SandboxExecutor | None = None,
    ) -> "CodingKernel":
        workspace = workspace_from_state(
            state,
            task_id=task_id,
            run_id=run_id,
            mode=mode,
            checkpoint_store=checkpoint_store,
        )
        if workspace_limits is not None:
            workspace.limits = workspace_limits
        return cls.for_workspace(
            workspace,
            allowed_executables=allowed_executables,
            allow_mutation=allow_mutation,
            allow_process=allow_process,
            allow_python=allow_python,
            sandbox_mode=sandbox_mode,
            sandbox_image=sandbox_image,
            sandbox_network_policy=sandbox_network_policy,
            sandbox_executor=sandbox_executor,
        )

    @classmethod
    def for_workspace(
        cls,
        workspace: WorkspaceRuntime,
        *,
        allowed_executables: set[str] | frozenset[str] | None = None,
        allow_mutation: bool = False,
        allow_process: bool = False,
        allow_python: bool = False,
        sandbox_mode: str = "native",
        sandbox_image: str = "",
        sandbox_network_policy: str = "none",
        sandbox_executor: SandboxExecutor | None = None,
    ) -> "CodingKernel":
        executor = sandbox_executor or build_sandbox_executor(
            workspace.root,
            mode=sandbox_mode,
            image=sandbox_image,
            network_policy=sandbox_network_policy,
        )
        backend = CodingToolBackend(
            workspace,
            allowed_executables=allowed_executables,
            sandbox_executor=executor,
        )
        policy = CodingCapabilityPolicy(
            workspace=workspace,
            allow_mutation=allow_mutation,
            allow_process=allow_process,
            allow_python=allow_python,
        )
        return cls(workspace=workspace, backend=backend, policy=policy)

    def build_session(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        agent_name: str,
        target: str,
        state: dict[str, Any],
        allowed_tools: frozenset[str] | None = None,
        definitions: dict[str, ToolDefinition] | None = None,
        result_projector=None,
        step_id: str | None = None,
    ) -> ToolSession:
        return ToolSession(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            agent_name=agent_name,
            target=target,
            allowed_tools=allowed_tools or frozenset(self.backend.registry),
            state=state,
            executor=self.backend,
            policy=self.policy,
            definitions=definitions or build_coding_tool_definitions(),
            result_projector=result_projector,
            checkpoint_store=self.workspace.checkpoint_store,
            step_id=step_id,
        )

    def snapshot(self) -> dict[str, Any]:
        return self.workspace.snapshot()
