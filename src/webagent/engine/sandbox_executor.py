"""Execution backends for model-authored processes.

The agent harness remains the trusted control plane.  Executors only own the
compute boundary used to start and terminate one argv-only process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import signal
import sys
from typing import Any, Mapping, Protocol


class SandboxExecutorError(RuntimeError):
    """The selected execution backend could not start or control a process."""


@dataclass(frozen=True)
class SandboxRuntimeInfo:
    backend: str
    isolated: bool
    host_platform: str
    guest_platform: str
    network_policy: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "isolated": self.isolated,
            "host_platform": self.host_platform,
            "guest_platform": self.guest_platform,
            "network_policy": self.network_policy,
        }


@dataclass
class SandboxProcess:
    process: asyncio.subprocess.Process
    requested_argv: tuple[str, ...]
    effective_argv: tuple[str, ...]
    runtime: SandboxRuntimeInfo
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def pid(self) -> int:
        return int(self.process.pid)

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def communicate(self, stdin: bytes | None) -> tuple[bytes, bytes]:
        return await self.process.communicate(stdin)

    async def wait(self) -> int:
        return await self.process.wait()


class SandboxExecutor(Protocol):
    """Provider boundary for one run-scoped execution environment."""

    def describe(self) -> SandboxRuntimeInfo: ...

    async def spawn(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        relative_cwd: str,
        host_environment: Mapping[str, str],
        runtime_environment: Mapping[str, str],
        stdin_enabled: bool,
        operation_id: str,
    ) -> SandboxProcess: ...

    async def terminate(self, execution: SandboxProcess) -> None: ...


async def _terminate_host_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass


class NativeSandboxExecutor:
    """Host executor used when OS isolation is not selected.

    The explicit ``isolated=False`` descriptor prevents callers and audit
    projections from mistaking argv validation for an operating-system sandbox.
    """

    def describe(self) -> SandboxRuntimeInfo:
        return SandboxRuntimeInfo(
            backend="native",
            isolated=False,
            host_platform=sys.platform,
            guest_platform=sys.platform,
            network_policy="host",
        )

    async def spawn(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        relative_cwd: str,
        host_environment: Mapping[str, str],
        runtime_environment: Mapping[str, str],
        stdin_enabled: bool,
        operation_id: str,
    ) -> SandboxProcess:
        del relative_cwd, runtime_environment, operation_id
        creationflags = 0
        if os.name == "nt":
            import subprocess

            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(host_environment),
            stdin=asyncio.subprocess.PIPE if stdin_enabled else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name != "nt"),
            creationflags=creationflags,
        )
        return SandboxProcess(
            process=process,
            requested_argv=argv,
            effective_argv=argv,
            runtime=self.describe(),
        )

    async def terminate(self, execution: SandboxProcess) -> None:
        await _terminate_host_process(execution.process)


def _container_name(operation_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", str(operation_id).lower()).strip("-.")
    return f"webagent-{normalized[-48:] or 'run'}"


# Semantic network policies map onto Docker network modes.  ``none`` keeps the
# process fully offline; ``open`` attaches the container to the default bridge
# so model-authored scripts may reach the network; ``target`` (authorized
# origin + task callback + DNS only) is not implemented yet and fails closed
# instead of silently degrading to an open network.
_NETWORK_POLICY_DOCKER_MODES = {
    "none": "none",
    "open": "bridge",
    "bridge": "bridge",
    "host": "host",
}
_ALLOWED_NETWORK_POLICIES = {"none", "open", "bridge", "host", "target"}


def _docker_network_mode(policy: str) -> str:
    mode = _NETWORK_POLICY_DOCKER_MODES.get(policy)
    if mode is None:
        if policy == "target":
            raise SandboxExecutorError(
                "network policy 'target' is not implemented yet; "
                "configure 'none' or 'open'"
            )
        raise SandboxExecutorError(
            "docker network policy must be one of: "
            + ", ".join(sorted(_ALLOWED_NETWORK_POLICIES))
        )
    return mode


class DockerSandboxExecutor:
    """Run a process in a fresh, resource-bounded Linux container."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        image: str,
        network_policy: str = "none",
        docker_executable: str = "docker",
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 128,
    ) -> None:
        if not str(image or "").strip():
            raise SandboxExecutorError("docker sandbox requires an explicit image")
        network = str(network_policy or "none").strip().lower()
        # Validate eagerly and fail closed on unsupported or unimplemented
        # policies; the Docker network mode is resolved per spawn.
        _docker_network_mode(network)
        self.workspace_root = Path(workspace_root).resolve()
        self.image = str(image).strip()
        self.network_policy = network
        self.docker_executable = str(docker_executable or "docker")
        self.memory = str(memory or "512m")
        self.cpus = max(0.1, float(cpus))
        self.pids_limit = max(16, int(pids_limit))

    def describe(self) -> SandboxRuntimeInfo:
        return SandboxRuntimeInfo(
            backend="docker",
            isolated=True,
            host_platform=sys.platform,
            guest_platform="linux",
            network_policy=self.network_policy,
        )

    async def spawn(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        relative_cwd: str,
        host_environment: Mapping[str, str],
        runtime_environment: Mapping[str, str],
        stdin_enabled: bool,
        operation_id: str,
    ) -> SandboxProcess:
        del cwd
        name = _container_name(operation_id)
        container_cwd = "/workspace"
        if relative_cwd not in {"", "."}:
            container_cwd += "/" + relative_cwd.replace("\\", "/").strip("/")
        command = [
            self.docker_executable,
            "run",
            "--rm",
            "--pull",
            "never",
            "--init",
            "--name",
            name,
            "--workdir",
            container_cwd,
            "--mount",
            f"type=bind,source={self.workspace_root},target=/workspace",
            "--network",
            _docker_network_mode(self.network_policy),
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--pids-limit",
            str(self.pids_limit),
            "--cap-drop",
            "ALL",
            "--read-only",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=67108864",
        ]
        # Bind-mounted private files belong to the service user. With all
        # capabilities dropped, container root cannot bypass their mode bits.
        if os.name != "nt":
            command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        for key, value in sorted(runtime_environment.items()):
            command.extend(["--env", f"{key}={value}"])
        command.extend([self.image, *argv])
        creationflags = 0
        if os.name == "nt":
            import subprocess

            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(self.workspace_root),
                env=dict(host_environment),
                stdin=asyncio.subprocess.PIPE if stdin_enabled else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name != "nt"),
                creationflags=creationflags,
            )
        except OSError as exc:
            raise SandboxExecutorError(f"docker sandbox could not start: {exc}") from exc
        return SandboxProcess(
            process=process,
            requested_argv=argv,
            effective_argv=tuple(command),
            runtime=self.describe(),
            metadata={"container_name": name, "image": self.image},
        )

    async def terminate(self, execution: SandboxProcess) -> None:
        name = str(execution.metadata.get("container_name") or "")
        if name:
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    self.docker_executable,
                    "rm",
                    "-f",
                    name,
                    env={
                        key: os.environ[key]
                        for key in ("PATH", "SystemRoot", "TEMP", "TMP")
                        if key in os.environ
                    },
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(cleanup.communicate(), timeout=5)
            except (OSError, asyncio.TimeoutError):
                pass
        await _terminate_host_process(execution.process)


def build_sandbox_executor(
    workspace_root: str | Path,
    *,
    mode: str = "native",
    image: str = "",
    network_policy: str = "none",
) -> SandboxExecutor:
    normalized = str(mode or "native").strip().lower()
    if normalized == "auto":
        normalized = "docker" if str(image or "").strip() else "native"
    if normalized == "native":
        return NativeSandboxExecutor()
    if normalized == "docker":
        return DockerSandboxExecutor(
            workspace_root,
            image=image,
            network_policy=network_policy,
        )
    raise SandboxExecutorError(f"unsupported coding sandbox mode: {normalized}")
