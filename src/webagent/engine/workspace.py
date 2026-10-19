"""Run-scoped workspace primitives for coding-capable agents.

The workspace is deliberately independent from the repository checkout.  Agent
code can create research scripts and derived artifacts, but it cannot resolve a
path outside the run root or silently overwrite a file changed by another
operation.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import difflib
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from webagent.store import CheckpointStore


class WorkspaceError(ValueError):
    """A workspace operation was invalid or exceeded its limits."""


class WorkspaceConflict(WorkspaceError):
    """The caller's base hash no longer matches the current file."""


@dataclass(frozen=True)
class WorkspaceLimits:
    max_files: int = 512
    max_file_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024
    max_read_bytes: int = 1 * 1024 * 1024
    max_path_chars: int = 240


@dataclass(frozen=True)
class WorkspaceDescriptor:
    workspace_id: str
    task_id: str
    run_id: str
    root: str
    mode: str = "ephemeral"

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "mode": self.mode,
            # The absolute host path is runtime metadata and is not returned to
            # the model by any coding tool.
            "root": self.root,
        }


@dataclass(frozen=True)
class WorkspaceArtifact:
    artifact_id: str
    path: str
    sha256: str
    size: int
    media_type: str
    operation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
            "operation_id": self.operation_id,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_identifier(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or ""))[:120]
    return cleaned or fallback


class WorkspaceRuntime:
    """Filesystem capability bound to one task/run workspace."""

    def __init__(
        self,
        descriptor: WorkspaceDescriptor,
        *,
        limits: WorkspaceLimits | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.limits = limits or WorkspaceLimits()
        self.root = Path(descriptor.root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_store = checkpoint_store
        self._artifacts: dict[str, WorkspaceArtifact] = {}
        if checkpoint_store is not None:
            for raw in checkpoint_store.list_workspace_artifacts(descriptor.workspace_id):
                artifact = WorkspaceArtifact(
                    artifact_id=str(raw.get("artifact_id") or ""),
                    path=str(raw.get("path") or ""),
                    sha256=str(raw.get("sha256") or ""),
                    size=int(raw.get("size") or 0),
                    media_type=str(raw.get("media_type") or "application/octet-stream"),
                    operation_id=str(raw.get("operation_id") or ""),
                )
                if artifact.artifact_id:
                    self._artifacts[artifact.artifact_id] = artifact

    @classmethod
    def for_run(
        cls,
        *,
        task_id: str,
        run_id: str,
        base_dir: str | Path | None = None,
        mode: str = "ephemeral",
        limits: WorkspaceLimits | None = None,
        checkpoint_store: CheckpointStore | None = None,
    ) -> "WorkspaceRuntime":
        if not str(task_id or "").strip() or not str(run_id or "").strip():
            raise WorkspaceError("workspace requires task_id and run_id")
        root_base = Path(base_dir) if base_dir is not None else (
            Path(__file__).resolve().parents[3] / "var" / "workspaces"
        )
        task_part = _safe_identifier(task_id, "task")
        run_part = _safe_identifier(run_id, "run")
        root = (root_base / task_part / run_part).resolve()
        descriptor = WorkspaceDescriptor(
            workspace_id=f"workspace:{task_part}:{run_part}",
            task_id=str(task_id),
            run_id=str(run_id),
            root=str(root),
            mode=str(mode or "ephemeral"),
        )
        return cls(descriptor, limits=limits, checkpoint_store=checkpoint_store)

    def _relative(self, value: str) -> str:
        raw = str(value or "").strip().replace("\\", "/")
        if not raw or len(raw) > self.limits.max_path_chars:
            raise WorkspaceError("workspace path is empty or too long")
        candidate = Path(raw)
        if raw.startswith("/") or candidate.is_absolute() or re.match(r"^[A-Za-z]:", raw):
            raise WorkspaceError("workspace path must be relative")
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if not parts:
            if raw == ".":
                return "."
            raise WorkspaceError("workspace path traversal is not allowed")
        if any(part == ".." for part in parts):
            raise WorkspaceError("workspace path traversal is not allowed")
        relative = "/".join(parts)
        resolved = (self.root / Path(*parts)).resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError("workspace path resolves outside the run root") from exc
        return relative

    def _path(self, value: str, *, allow_missing: bool = True) -> tuple[str, Path]:
        relative = self._relative(value)
        path = self.root / Path(*relative.split("/"))
        if not allow_missing and not path.is_file():
            raise WorkspaceError(f"workspace file does not exist: {relative}")
        return relative, path

    def _check_file_size(self, data: bytes, *, replacing_bytes: int = 0) -> None:
        if len(data) > self.limits.max_file_bytes:
            raise WorkspaceError("workspace file exceeds the per-file limit")
        total = sum(
            item.stat().st_size
            for item in self.root.rglob("*")
            if item.is_file()
        )
        if total - max(0, int(replacing_bytes)) + len(data) > self.limits.max_total_bytes:
            raise WorkspaceError("workspace exceeds its total byte limit")
        files = sum(1 for item in self.root.rglob("*") if item.is_file())
        if files >= self.limits.max_files:
            raise WorkspaceError("workspace exceeds its file-count limit")

    def _current_bytes(self, path: Path) -> bytes:
        if not path.is_file():
            return b""
        data = path.read_bytes()
        if len(data) > self.limits.max_read_bytes:
            raise WorkspaceError("workspace file exceeds the read limit")
        return data

    def list(self, prefix: str = "") -> list[dict[str, Any]]:
        if prefix:
            relative, path = self._path(prefix)
            if not path.exists():
                return []
            roots: Iterable[Path] = path.rglob("*") if path.is_dir() else (path,)
        else:
            relative, path = "", self.root
            roots = path.rglob("*")
        rows: list[dict[str, Any]] = []
        for item in roots:
            if item.is_symlink():
                continue
            rel = item.relative_to(self.root).as_posix()
            if item.is_file():
                rows.append({
                    "path": rel,
                    "kind": "file",
                    "size": item.stat().st_size,
                    "sha256": _sha256(item.read_bytes()),
                })
            elif item.is_dir():
                rows.append({"path": rel, "kind": "directory"})
        del relative
        return sorted(rows, key=lambda row: str(row["path"]))[: self.limits.max_files]

    def read(self, path: str, *, encoding: str = "utf-8") -> dict[str, Any]:
        relative, resolved = self._path(path, allow_missing=False)
        data = self._current_bytes(resolved)
        try:
            content = data.decode(encoding)
            binary = False
        except UnicodeDecodeError:
            content = base64.b64encode(data).decode("ascii")
            binary = True
        return {
            "path": relative,
            "content": content,
            "binary": binary,
            "size": len(data),
            "sha256": _sha256(data),
        }

    def _write_bytes(
        self,
        path: str,
        data: bytes,
        *,
        base_sha256: str | None,
        operation_id: str,
        media_type: str = "text/plain",
    ) -> dict[str, Any]:
        relative, resolved = self._path(path)
        if resolved.exists() and resolved.is_dir():
            raise WorkspaceError("workspace path is a directory")
        existing = resolved.read_bytes() if resolved.is_file() else b""
        current_hash = _sha256(existing)
        if base_sha256 is not None and str(base_sha256) != current_hash:
            raise WorkspaceConflict(
                f"workspace base hash mismatch for {relative}: expected {base_sha256}, current {current_hash}"
            )
        self._check_file_size(data, replacing_bytes=len(existing) if resolved.exists() else 0)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".webagent-", dir=str(resolved.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, resolved)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        digest = _sha256(data)
        # Include the normalized path in the identity.  Identical content in
        # two files must remain two independently addressable artifacts.
        artifact_key = _sha256(f"{relative}\0{digest}".encode("utf-8"))[:24]
        artifact_id = f"artifact:{self.descriptor.workspace_id}:{artifact_key}"
        artifact = WorkspaceArtifact(
            artifact_id=artifact_id,
            path=relative,
            sha256=digest,
            size=len(data),
            media_type=str(media_type or "application/octet-stream")[:160],
            operation_id=str(operation_id or "workspace-write"),
        )
        self._artifacts[artifact_id] = artifact
        if self.checkpoint_store is not None:
            self.checkpoint_store.save_workspace_artifact(
                artifact_id=artifact.artifact_id,
                workspace_id=self.descriptor.workspace_id,
                task_id=self.descriptor.task_id,
                run_id=self.descriptor.run_id,
                path=artifact.path,
                sha256=artifact.sha256,
                size=artifact.size,
                media_type=artifact.media_type,
                operation_id=artifact.operation_id,
            )
            self.checkpoint_store.record_workspace_mutation(
                mutation_id=f"mutation:{self.descriptor.workspace_id}:{operation_id}:{digest[:16]}",
                workspace_id=self.descriptor.workspace_id,
                task_id=self.descriptor.task_id,
                run_id=self.descriptor.run_id,
                operation_id=operation_id,
                path=relative,
                mutation_type="write",
                base_sha256=current_hash,
                new_sha256=digest,
                status="succeeded",
            )
        return {
            "path": relative,
            "artifact": artifact.to_dict(),
            "previous_sha256": current_hash,
            "sha256": digest,
            "size": len(data),
        }

    def write(
        self,
        path: str,
        content: str,
        *,
        base_sha256: str | None = None,
        operation_id: str = "workspace-write",
        media_type: str = "text/plain",
    ) -> dict[str, Any]:
        data = str(content).encode("utf-8")
        return self._write_bytes(
            path,
            data,
            base_sha256=base_sha256,
            operation_id=operation_id,
            media_type=media_type,
        )

    def edit(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        base_sha256: str | None = None,
        operation_id: str = "workspace-edit",
    ) -> dict[str, Any]:
        current = self.read(path)
        if current["binary"]:
            raise WorkspaceError("workspace edit only supports UTF-8 text files")
        content = str(current["content"])
        old = str(old_text)
        if not old or content.count(old) != 1:
            raise WorkspaceError("workspace edit requires exactly one matching old_text")
        if base_sha256 is None:
            base_sha256 = str(current["sha256"])
        return self.write(
            path,
            content.replace(old, str(new_text), 1),
            base_sha256=base_sha256,
            operation_id=operation_id,
        )

    def apply_patch(
        self,
        path: str,
        patch: str,
        *,
        base_sha256: str | None = None,
        operation_id: str = "workspace-patch",
    ) -> dict[str, Any]:
        current = self.read(path)
        if current["binary"]:
            raise WorkspaceError("workspace patch only supports UTF-8 text files")
        old_lines = str(current["content"]).splitlines(keepends=True)
        patch_lines = str(patch).splitlines(keepends=True)
        hunks: list[tuple[int, int, list[str]]] = []
        header = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        index = 0
        while index < len(patch_lines):
            match = header.match(patch_lines[index].rstrip("\r\n"))
            if not match:
                index += 1
                continue
            start = int(match.group(1)) - 1
            index += 1
            body: list[str] = []
            while index < len(patch_lines) and not patch_lines[index].startswith("@@ "):
                line = patch_lines[index]
                if line[:1] in {" ", "+", "-"}:
                    body.append(line)
                index += 1
            hunks.append((start, int(match.group(2) or "1"), body))
        if not hunks:
            raise WorkspaceError("workspace patch contains no unified diff hunks")
        result: list[str] = []
        cursor = 0
        for start, old_count, body in hunks:
            if start < cursor or start > len(old_lines):
                raise WorkspaceError("workspace patch hunk is out of range")
            result.extend(old_lines[cursor:start])
            consumed = 0
            for line in body:
                marker, value = line[:1], line[1:]
                if marker == " ":
                    if start + consumed >= len(old_lines) or old_lines[start + consumed] != value:
                        raise WorkspaceConflict("workspace patch context does not match current file")
                    result.append(value)
                    consumed += 1
                elif marker == "-":
                    if start + consumed >= len(old_lines) or old_lines[start + consumed] != value:
                        raise WorkspaceConflict("workspace patch removal does not match current file")
                    consumed += 1
                elif marker == "+":
                    result.append(value)
            if consumed != old_count:
                raise WorkspaceError("workspace patch hunk line count is invalid")
            cursor = start + consumed
        result.extend(old_lines[cursor:])
        if base_sha256 is None:
            base_sha256 = str(current["sha256"])
        return self.write(
            path,
            "".join(result),
            base_sha256=base_sha256,
            operation_id=operation_id,
        )

    def diff(self, path: str, content: str | None = None) -> dict[str, Any]:
        current = self.read(path)
        if content is None:
            return {"path": current["path"], "sha256": current["sha256"], "diff": ""}
        if current["binary"]:
            raise WorkspaceError("workspace diff only supports UTF-8 text files")
        diff = "".join(
            difflib.unified_diff(
                str(current["content"]).splitlines(keepends=True),
                str(content).splitlines(keepends=True),
                fromfile=current["path"],
                tofile=current["path"],
            )
        )
        return {"path": current["path"], "sha256": current["sha256"], "diff": diff[:64_000]}

    def remove(
        self,
        path: str,
        *,
        base_sha256: str | None = None,
        operation_id: str = "workspace-remove",
    ) -> dict[str, Any]:
        relative, resolved = self._path(path, allow_missing=False)
        current = resolved.read_bytes()
        digest = _sha256(current)
        if base_sha256 is not None and str(base_sha256) != digest:
            raise WorkspaceConflict("workspace remove base hash does not match current file")
        resolved.unlink()
        if self.checkpoint_store is not None:
            self.checkpoint_store.record_workspace_mutation(
                mutation_id=f"mutation:{self.descriptor.workspace_id}:{operation_id}:{digest[:16]}",
                workspace_id=self.descriptor.workspace_id,
                task_id=self.descriptor.task_id,
                run_id=self.descriptor.run_id,
                operation_id=operation_id,
                path=relative,
                mutation_type="remove",
                base_sha256=digest,
                new_sha256=None,
                status="succeeded",
            )
        return {"path": relative, "removed": True, "sha256": digest}

    def artifacts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._artifacts.values()]

    def snapshot(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "artifacts": self.artifacts(),
            "files": self.list(),
        }


def workspace_from_state(
    state: dict[str, Any],
    *,
    task_id: str,
    run_id: str,
    mode: str = "ephemeral",
    checkpoint_store: CheckpointStore | None = None,
) -> WorkspaceRuntime:
    raw = state.get("agent_workspace")
    if isinstance(raw, dict):
        descriptor = raw.get("descriptor") if isinstance(raw.get("descriptor"), dict) else raw
        if (
            str(descriptor.get("task_id") or "") == str(task_id)
            and str(descriptor.get("run_id") or "") == str(run_id)
            and str(descriptor.get("root") or "")
        ):
            return WorkspaceRuntime(
                WorkspaceDescriptor(
                    workspace_id=str(descriptor.get("workspace_id") or f"workspace:{task_id}:{run_id}"),
                    task_id=str(task_id),
                    run_id=str(run_id),
                    root=str(descriptor["root"]),
                    mode=str(descriptor.get("mode") or mode),
                ),
                checkpoint_store=checkpoint_store,
            )
    runtime = WorkspaceRuntime.for_run(
        task_id=task_id,
        run_id=run_id,
        mode=mode,
        checkpoint_store=checkpoint_store,
    )
    state["agent_workspace"] = {"descriptor": runtime.descriptor.to_dict()}
    return runtime
