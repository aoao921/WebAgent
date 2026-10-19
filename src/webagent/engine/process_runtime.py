"""Process execution capability for coding-capable Agents.

Commands are argv-only and run with a minimal environment inside a
run-scoped workspace.  This module intentionally does not expose a shell.
"""

from __future__ import annotations

import asyncio
import ctypes
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
from typing import Any, Mapping

from webagent.store import CheckpointStore

from .sandbox_executor import (
    NativeSandboxExecutor,
    SandboxExecutor,
    SandboxExecutorError,
)
from .workspace import WorkspaceError, WorkspaceRuntime


class ProcessRuntimeError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessLimits:
    timeout_seconds: float = 30.0
    max_stdout_chars: int = 32_000
    max_stderr_chars: int = 16_000
    max_argv_items: int = 64
    max_arg_chars: int = 8_000
    lease_seconds: float = 30.0
    heartbeat_interval_seconds: float = 5.0


@dataclass(frozen=True)
class ProcessResult:
    status: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    elapsed_ms: int
    error: str = ""
    runtime: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "runtime": dict(self.runtime or {}),
        }


_BLOCKED_EXECUTABLES = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "bash",
        "sh",
        "zsh",
        "fish",
    }
)


def _truncate(value: bytes, limit: int) -> tuple[str, bool]:
    text = value.decode("utf-8", errors="replace")
    return text[:limit], len(text) > limit


class ProcessRuntime:
    """Execute one non-shell process in a WorkspaceRuntime."""

    def __init__(
        self,
        workspace: WorkspaceRuntime,
        *,
        limits: ProcessLimits | None = None,
        allowed_executables: set[str] | frozenset[str] | None = None,
        allowed_script_interpreters: set[str] | frozenset[str] | None = None,
        allowed_env_keys: set[str] | frozenset[str] | None = None,
        executor: SandboxExecutor | None = None,
    ) -> None:
        self.workspace = workspace
        self.limits = limits or ProcessLimits()
        self.allowed_executables = {
            str(value).lower() for value in (allowed_executables or set()) if str(value)
        }
        self.allowed_script_interpreters = {
            str(value).lower()
            for value in (allowed_script_interpreters or set())
            if str(value)
        }
        self.allowed_env_keys = {
            str(value) for value in (allowed_env_keys or set()) if str(value)
        }
        self.executor = executor or NativeSandboxExecutor()
        self.checkpoint_store: CheckpointStore | None = workspace.checkpoint_store
        if self.checkpoint_store is not None:
            self.reconcile_running_processes()
            self.checkpoint_store.mark_expired_process_runtimes(
                task_id=workspace.descriptor.task_id,
                run_id=workspace.descriptor.run_id,
            )

    def _lease_until(self) -> str:
        seconds = max(3.0, float(self.limits.lease_seconds))
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()

    async def _heartbeat_loop(self, process_id: str) -> None:
        interval = max(1.0, float(self.limits.heartbeat_interval_seconds))
        while True:
            await asyncio.sleep(interval)
            if self.checkpoint_store is not None:
                self.checkpoint_store.heartbeat_process_runtime(
                    process_id, self._lease_until()
                )

    def reconcile_running_processes(self) -> list[dict[str, Any]]:
        """Reconcile persisted running rows without replaying or guessing outcomes.

        A missing PID means the parent process lost the child before persisting a
        terminal result, so the only correct state is ``outcome_unknown``. A
        live PID must also match the recorded executable and creation token;
        otherwise the PID may have been reused and the result is unknown.
        """
        store = self.checkpoint_store
        if store is None:
            return []
        rows = store.list_process_runtimes(
            task_id=self.workspace.descriptor.task_id,
            run_id=self.workspace.descriptor.run_id,
            statuses=("running",),
        )
        reconciled: list[dict[str, Any]] = []
        for row in rows:
            pid = int(row.get("pid") or 0)
            current = self.capture_process_identity(pid)
            alive = bool(current.get("alive"))
            expected_identity = self._decode_identity(row.get("identity_json"))
            mismatch = alive and self._identity_mismatch(expected_identity, current)
            if not alive or mismatch:
                reason = (
                    "persisted process PID is no longer alive; outcome is unknown"
                    if not alive
                    else "persisted process PID identity changed; outcome is unknown"
                )
                store.finish_process_runtime(
                    str(row["process_id"]),
                    status="outcome_unknown",
                    error=reason,
                )
                row = dict(row)
                row["status"] = "outcome_unknown"
                row["error"] = reason
            reconciled.append({
                "process_id": str(row["process_id"]),
                "pid": pid,
                "alive": alive,
                "identity_match": not mismatch,
                "status": str(row.get("status") or ""),
            })
        return reconciled

    async def terminate_orphan_process(self, process_id: str) -> dict[str, Any]:
        """Explicitly terminate an unknown process only after identity checks."""
        store = self.checkpoint_store
        if store is None:
            raise ProcessRuntimeError("process ledger is unavailable")
        row = store.get_process_runtime(process_id)
        if row is None:
            raise ProcessRuntimeError(f"unknown process runtime: {process_id}")
        if str(row.get("status") or "") != "outcome_unknown":
            raise ProcessRuntimeError("only outcome_unknown processes may be terminated")
        pid = int(row.get("pid") or 0)
        current = self.capture_process_identity(pid)
        if not current.get("alive"):
            return {"process_id": process_id, "pid": pid, "terminated": False, "reason": "not_alive"}
        expected = self._decode_identity(row.get("identity_json"))
        if self._identity_mismatch(expected, current):
            return {
                "process_id": process_id,
                "pid": pid,
                "terminated": False,
                "reason": "identity_mismatch",
            }
        await self._terminate_pid(pid)
        store.finish_process_runtime(
            process_id,
            status="outcome_unknown",
            error="orphan process terminated explicitly; execution outcome remains unknown",
        )
        return {"process_id": process_id, "pid": pid, "terminated": True, "reason": "terminated"}

    @classmethod
    def capture_process_identity(cls, pid: int) -> dict[str, Any]:
        """Return a conservative process identity without invoking a shell."""
        if pid <= 0:
            return {"pid": pid, "alive": False}
        if os.name == "nt":
            return cls._windows_process_identity(pid)
        proc = Path(f"/proc/{pid}")
        if not proc.exists():
            return {"pid": pid, "alive": False}
        executable = ""
        start_token = ""
        try:
            executable = str((proc / "exe").resolve())
        except OSError:
            pass
        try:
            stat = (proc / "stat").read_text(encoding="utf-8")
            fields = stat[stat.rfind(")") + 2 :].split()
            if len(fields) > 19:
                start_token = str(fields[19])
        except (OSError, UnicodeError, IndexError):
            pass
        return {"pid": pid, "alive": True, "executable": executable, "start_token": start_token}

    @staticmethod
    def _windows_process_identity(pid: int) -> dict[str, Any]:
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return {"pid": pid, "alive": False}
        try:
            size = wintypes.DWORD(32_768)
            buffer = ctypes.create_unicode_buffer(size.value)
            executable = ""
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                executable = buffer.value
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            start_token = ""
            if kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                start_token = str(
                    (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
                )
            return {
                "pid": pid,
                "alive": True,
                "executable": executable,
                "start_token": start_token,
            }
        finally:
            kernel32.CloseHandle(handle)

    @staticmethod
    def _identity_mismatch(expected: dict[str, Any], current: dict[str, Any]) -> bool:
        expected_token = str(expected.get("start_token") or "")
        current_token = str(current.get("start_token") or "")
        if expected_token and current_token and expected_token != current_token:
            return True
        expected_executable = str(expected.get("executable") or "").lower()
        current_executable = str(current.get("executable") or "").lower()
        return bool(expected_executable and current_executable and expected_executable != current_executable)

    @staticmethod
    def _decode_identity(value: Any) -> dict[str, Any]:
        if not isinstance(value, str) or not value:
            return {}
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _executable_name(value: str) -> str:
        return Path(str(value)).name.lower()

    def _validate_argv(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        script_interpreter: bool = False,
    ) -> tuple[str, ...]:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ProcessRuntimeError("process argv must be a non-empty list")
        if len(argv) > self.limits.max_argv_items:
            raise ProcessRuntimeError("process argv has too many arguments")
        values = tuple(str(item) for item in argv)
        if any(not item or len(item) > self.limits.max_arg_chars for item in values):
            raise ProcessRuntimeError("process argument is empty or too long")
        executable = self._executable_name(values[0])
        if executable in _BLOCKED_EXECUTABLES and not (
            script_interpreter and executable in self.allowed_script_interpreters
        ):
            raise ProcessRuntimeError("shell executables are not available in ProcessRuntime")
        if self.allowed_executables and executable not in self.allowed_executables and not (
            script_interpreter and executable in self.allowed_script_interpreters
        ):
            raise ProcessRuntimeError(f"executable is not allowed: {executable}")
        return values

    async def run_script(
        self,
        script_path: str,
        *,
        interpreter: str = "bash",
        args: list[str] | tuple[str, ...] = (),
        cwd: str = ".",
        env: Mapping[str, Any] | None = None,
        timeout_seconds: float | None = None,
        operation_id: str = "script-run",
    ) -> dict[str, Any]:
        """Run one workspace-local script through an explicitly allowed interpreter."""
        relative, resolved = self.workspace._path(script_path, allow_missing=False)
        if not resolved.is_file():
            raise ProcessRuntimeError(f"script is not a workspace file: {relative}")
        interpreter_name = self._executable_name(interpreter)
        if interpreter_name not in self.allowed_script_interpreters:
            raise ProcessRuntimeError(f"script interpreter is not allowed: {interpreter_name}")
        command = [interpreter_name, relative, *[str(item) for item in args]]
        self._validate_argv(command, script_interpreter=True)
        return await self.run(
            command,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
            operation_id=operation_id,
            _script_interpreter=True,
        )

    def _environment(
        self,
        env: Mapping[str, Any] | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        supplied = env if isinstance(env, Mapping) else {}
        unknown = set(str(key) for key in supplied) - self.allowed_env_keys
        if unknown:
            raise ProcessRuntimeError(
                "process environment keys are not allowed: " + ", ".join(sorted(unknown))
            )
        # Do not inherit credentials or the caller's complete environment.
        host = {
            key: os.environ[key]
            for key in ("PATH", "SystemRoot", "TEMP", "TMP")
            if key in os.environ
        }
        runtime = {str(key): str(value) for key, value in supplied.items()}
        host.update(runtime)
        return host, runtime

    async def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str = ".",
        env: Mapping[str, Any] | None = None,
        stdin: str = "",
        timeout_seconds: float | None = None,
        operation_id: str = "process-run",
        _script_interpreter: bool = False,
    ) -> dict[str, Any]:
        command = self._validate_argv(argv, script_interpreter=_script_interpreter)
        try:
            relative_cwd, cwd_path = self.workspace._path(cwd)
        except WorkspaceError as exc:
            raise ProcessRuntimeError(str(exc)) from exc
        if not cwd_path.exists() or not cwd_path.is_dir():
            raise ProcessRuntimeError(f"process cwd is not a workspace directory: {relative_cwd}")
        host_environment, runtime_environment = self._environment(env)
        timeout = max(
            0.1,
            min(
                float(timeout_seconds or self.limits.timeout_seconds),
                self.limits.timeout_seconds,
            ),
        )
        started = asyncio.get_running_loop().time()
        runtime_info = self.executor.describe().to_dict()
        try:
            execution = await self.executor.spawn(
                command,
                cwd=cwd_path,
                relative_cwd=relative_cwd,
                host_environment=host_environment,
                runtime_environment=runtime_environment,
                stdin_enabled=bool(stdin),
                operation_id=operation_id,
            )
            runtime_info = execution.runtime.to_dict()
        except (OSError, ValueError, SandboxExecutorError) as exc:
            elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
            return ProcessResult(
                status="failed",
                argv=command,
                cwd=relative_cwd,
                exit_code=None,
                stdout="",
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                elapsed_ms=elapsed,
                error=f"{type(exc).__name__}: {exc}"[:800],
                runtime=runtime_info,
            ).to_dict()

        process_id = f"process:{operation_id}"
        heartbeat_task: asyncio.Task[None] | None = None
        if self.checkpoint_store is not None:
            try:
                identity = self.capture_process_identity(execution.pid)
                identity["runtime"] = runtime_info
                if execution.metadata:
                    identity["executor_metadata"] = dict(execution.metadata)
                self.checkpoint_store.start_process_runtime(
                    process_id=process_id,
                    operation_id=operation_id,
                    task_id=self.workspace.descriptor.task_id,
                    run_id=self.workspace.descriptor.run_id,
                    pid=execution.pid,
                    argv=command,
                    identity=identity,
                    cwd=relative_cwd,
                    lease_until=self._lease_until(),
                )
                heartbeat_task = asyncio.create_task(self._heartbeat_loop(process_id))
            except Exception as exc:
                await self.executor.terminate(execution)
                return ProcessResult(
                    status="failed",
                    argv=command,
                    cwd=relative_cwd,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    elapsed_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    error=f"process ledger unavailable: {type(exc).__name__}: {exc}"[:800],
                    runtime=runtime_info,
                ).to_dict()

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                execution.communicate(stdin.encode("utf-8") if stdin else None),
                timeout=timeout,
            )
            stdout, stdout_truncated = _truncate(stdout_bytes, self.limits.max_stdout_chars)
            stderr, stderr_truncated = _truncate(stderr_bytes, self.limits.max_stderr_chars)
            status = "succeeded" if execution.returncode == 0 else "failed"
            error = "" if status == "succeeded" else f"process exited with code {execution.returncode}"
            result = ProcessResult(
                status=status,
                argv=command,
                cwd=relative_cwd,
                exit_code=execution.returncode,
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                elapsed_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                error=error[:800],
                runtime=runtime_info,
            ).to_dict()
            if self.checkpoint_store is not None:
                self.checkpoint_store.finish_process_runtime(
                    process_id,
                    status=status,
                    exit_code=execution.returncode,
                    error=error[:800] or None,
                )
            return result
        except asyncio.TimeoutError:
            await self.executor.terminate(execution)
            result = ProcessResult(
                status="timed_out",
                argv=command,
                cwd=relative_cwd,
                exit_code=None,
                stdout="",
                stderr="",
                stdout_truncated=False,
                stderr_truncated=False,
                elapsed_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                error="process exceeded its execution timeout",
                runtime=runtime_info,
            ).to_dict()
            if self.checkpoint_store is not None:
                self.checkpoint_store.finish_process_runtime(
                    process_id,
                    status="timed_out",
                    error=result["error"],
                )
            return result
        except asyncio.CancelledError:
            await self.executor.terminate(execution)
            if self.checkpoint_store is not None:
                self.checkpoint_store.finish_process_runtime(
                    process_id,
                    status="cancelled",
                    error="process execution cancelled",
                )
            raise
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task

    @staticmethod
    async def _terminate_pid(pid: int) -> None:
        if pid <= 0:
            return
        try:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.communicate(), timeout=5)
            else:
                os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            return
