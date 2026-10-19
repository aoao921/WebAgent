import ast
from pathlib import Path

import pytest

from webagent.engine.workspace import WorkspaceConflict, WorkspaceError, WorkspaceRuntime
from webagent.store import CheckpointStore


@pytest.mark.parametrize("path", ["../secret", "/tmp/outside", "C:/outside", "nested/../../outside"])
def test_workspace_path_cannot_escape_root(tmp_path, path):
    workspace = WorkspaceRuntime.for_run(task_id="task", run_id="run", base_dir=tmp_path)
    with pytest.raises(WorkspaceError):
        workspace.write(path, "value")


def test_workspace_artifact_identity_and_edit_conflicts(tmp_path):
    store = CheckpointStore(tmp_path / "state.db")
    workspace = WorkspaceRuntime.for_run(task_id="task", run_id="run", base_dir=tmp_path, checkpoint_store=store)
    first = workspace.write("one.txt", "same", operation_id="op:one")
    second = workspace.write("two.txt", "same", operation_id="op:two")
    assert first["artifact"]["artifact_id"] != second["artifact"]["artifact_id"]
    with pytest.raises(WorkspaceConflict):
        workspace.write("one.txt", "changed", base_sha256="wrong")
    assert workspace.read("one.txt")["content"] == "same"
    assert len(store.list_workspace_artifacts(workspace.descriptor.workspace_id)) == 2


def test_no_original_domain_imports():
    root = Path(__file__).parents[1] / "src"
    forbidden = {"assessment", "agents", "services", "tool_runtime", "WorkFlow"}
    for file in root.rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden, str(file)
            elif isinstance(node, ast.Import):
                assert not {name.name.split(".")[0] for name in node.names} & forbidden
