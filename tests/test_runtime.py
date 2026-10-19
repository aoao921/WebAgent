import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace

import pytest

from webagent.agent import WebHarness, WebPolicy, web_origin
from webagent.engine.harness import FinalOutputRejected
from webagent.engine.protocol import AgentJob, AgentMessage, AgentRun, AgentSpec, ModelResponse, ToolCall, ToolDefinition
from webagent.engine.response_adapter import decode_tool_call, normalize_response
from webagent.engine.tooling import ToolGateway, ToolSession
from webagent.specs import ToolSpec
from webagent.store import CheckpointStore, public_event


class Backend:
    def __init__(self, *, unknown=False):
        self.count = 0
        self.unknown = unknown
        self.definition = ToolDefinition(name="browser_snapshot", description="Read", input_schema={"type": "object", "additionalProperties": False, "properties": {}})
        self.registry = {"browser_snapshot": ToolSpec(name="browser_snapshot", display_name="Read", runtime="test", category="test", input_schema=self.definition.input_schema)}

    async def execute(self, name, arguments, state):
        self.count += 1
        return {"success": not self.unknown, "value": self.count,
                "execution_state": "outcome_unknown" if self.unknown else "completed"}


class Policy:
    async def authorize(self, session, call, definition):
        return dict(call.arguments)


def session_for(store, backend):
    return ToolSession(session_id="test", run_id="run", task_id="task", agent_name="webagent", target="https://example.test/",
                       allowed_tools=frozenset(backend.registry), state={}, executor=backend, policy=Policy(),
                       definitions={"browser_snapshot": backend.definition}, checkpoint_store=store)


def test_snapshots_are_fresh_and_audited(tmp_path):
    store = CheckpointStore(tmp_path / "state.db")
    backend = Backend()
    session = session_for(store, backend)
    call = ToolCall(id="read", name="browser_snapshot", arguments={})
    first = asyncio.run(ToolGateway().execute(session, call))
    second = asyncio.run(ToolGateway().execute(session, call))
    assert first.output["value"] == 1 and second.output["value"] == 2
    assert store.get_operation(first.operation_id)["status"] == "succeeded"


def test_unknown_runtime_outcome_is_not_reexecuted(tmp_path):
    store = CheckpointStore(tmp_path / "state.db")
    backend = Backend(unknown=True)
    first = asyncio.run(ToolGateway().execute(session_for(store, backend), ToolCall(id="a", name="browser_snapshot")))
    assert first.status.value == "outcome_unknown"
    assert store.get_operation(first.operation_id)["status"] == "outcome_unknown"
    second = asyncio.run(ToolGateway().execute(session_for(store, backend), ToolCall(id="b", name="browser_snapshot")))
    assert second.status.value == "outcome_unknown" and backend.count == 1


@pytest.mark.parametrize("name,args", [("python_run", {"code": "print(1)"}), ("browser_snapshot", {"extra": True})])
def test_gateway_rejects_unknown_tools_and_invalid_arguments(tmp_path, name, args):
    backend = Backend()
    observation = asyncio.run(ToolGateway().execute(session_for(CheckpointStore(tmp_path / "db"), backend), ToolCall(id="bad", name=name, arguments=args)))
    assert observation.status.value == "blocked" and backend.count == 0


def test_prepare_operation_is_atomic(tmp_path):
    store = CheckpointStore(tmp_path / "state.db")
    with ThreadPoolExecutor(max_workers=4) as executor:
        rows = list(executor.map(lambda _: store.prepare_operation(operation_id="one", task_id="task"), range(16)))
    assert len(store.rows("operation")) == 1
    assert all(row["operation_id"] == "one" for row in rows)


@pytest.mark.parametrize("raw", ['[]', '{', '"not an object"', 'null'])
def test_invalid_provider_arguments_remain_protocol_errors(raw):
    call = decode_tool_call("x", "browser_snapshot", raw)
    assert call.protocol_error and call.raw_arguments == raw


def test_response_adapter_does_not_extract_json_from_prose():
    response = ModelResponse(message=AgentMessage(role="assistant", content='Here is JSON: {"summary":"guess"}'))
    normalized = normalize_response(response, [], expect_json=True, call_prefix="test")
    # Domain validation sees the original response; no guessed JSON replaces it.
    assert normalized.message.content == response.message.content


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "https://user:password@example.test/", "example.test"])
def test_origin_rejects_non_web_and_embedded_credentials(url):
    with pytest.raises(ValueError):
        web_origin(url)


def test_origin_canonicalization():
    assert web_origin("https://EXAMPLE.test:443/a") == "https://example.test"
    assert web_origin("http://[::1]:8080/") == "http://[::1]:8080"


def test_navigation_policy_checks_origin_before_execution():
    policy = WebPolicy(["https://example.test"], None)
    with pytest.raises(ValueError):
        asyncio.run(policy.authorize(None, ToolCall(id="n", name="browser_navigate", arguments={"url": "https://foreign.test/"}), None))


def test_public_audit_contains_only_metadata():
    from webagent.engine.protocol import AgentEvent
    event = AgentEvent(type="tool_completed", run_id="run", agent_name="webagent", task_id="task", tool_name="browser_fill",
                       data={"arguments": {"value": "private-password"}, "output": {"page": "private-page"}, "operation_id": "op:1"})
    value = public_event(event)
    encoded = json.dumps(value)
    assert "private-password" not in encoded and "private-page" not in encoded
    assert value["operation_id"] == "op:1"


def test_terminal_cannot_cite_invented_operations(tmp_path):
    backend = Backend()
    store = CheckpointStore(tmp_path / "db")
    job = AgentJob(job_id="run", task_id="task", agent_name="webagent", target="https://example.test/", objective="Read")
    spec = AgentSpec(name="webagent", role="web", objective="Read", model="fixture", system_prompt="Read")
    harness = WebHarness(job=job, store=store, spec=spec, model_client=None, tool_session=session_for(store, backend))
    run = AgentRun(run_id="run", job=job, spec_name="webagent")
    with pytest.raises(FinalOutputRejected):
        asyncio.run(harness.finalize(run, '{"summary":"done","evidence_operation_ids":["invented"]}'))


def test_model_adapter_preserves_tool_ids_and_usage():
    from webagent.engine.model_client import OpenAICompatibleAgentModelClient
    from webagent.engine.protocol import ModelRequest
    sent = {}
    async def create(**kwargs):
        sent.update(kwargs)
        message = SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="call:1", function=SimpleNamespace(name="browser_snapshot", arguments="{}"))])
        return SimpleNamespace(id="response:1", choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    client = OpenAICompatibleAgentModelClient(api_key="fixture", base_url="https://example.test/v1", default_model="fixture",
                client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    response = asyncio.run(client.complete(ModelRequest(model="fixture", messages=[AgentMessage(role="user", content="Read")], tools=[Backend().definition])))
    assert response.message.tool_calls[0].id == "call:1" and response.usage.total_tokens == 15
    assert sent["tools"][0]["function"]["name"] == "browser_snapshot"


def test_loop_stops_at_tool_budget_and_persists_run(tmp_path):
    from webagent.engine.loop import AgentLoop
    from webagent.engine.protocol import AgentLimits
    class RepeatingModel:
        async def complete(self, request):
            return ModelResponse(message=AgentMessage(role="assistant", tool_calls=[ToolCall(id="read", name="browser_snapshot")]))
    store = CheckpointStore(tmp_path / "db")
    backend = Backend()
    job = AgentJob(job_id="run", task_id="task", agent_name="webagent", target="https://example.test/", objective="Read",
                   input={"allowed_origins": ["https://example.test"]})
    spec = AgentSpec(name="webagent", role="web", objective="Read", model="fixture", system_prompt="Read",
                     allowed_tools=frozenset(backend.registry), limits=AgentLimits(max_tool_calls=1))
    harness = WebHarness(job=job, store=store, spec=spec, model_client=RepeatingModel(), tool_session=session_for(store, backend))
    result = asyncio.run(AgentLoop().execute(spec=spec, job=job, harness=harness))
    assert result.status.value == "limit_exceeded" and backend.count == 1
    assert store.get_agent_run("task", "run")["status"] == "limit_exceeded"


def test_successful_operation_replay_uses_saved_result_after_restart(tmp_path):
    store = CheckpointStore(tmp_path / "db")
    backend = Backend()
    # Use a generic action name to distinguish it from fresh snapshot reads.
    backend.registry["browser_click"] = ToolSpec(name="browser_click", display_name="Click", runtime="test", category="test")
    session = session_for(store, backend)
    session.definitions["browser_click"] = ToolDefinition(name="browser_click", description="Click")
    first = asyncio.run(ToolGateway().execute(session, ToolCall(id="a", name="browser_click")))
    resumed = session_for(store, backend)
    resumed.definitions["browser_click"] = session.definitions["browser_click"]
    second = asyncio.run(ToolGateway().execute(resumed, ToolCall(id="new-provider-id", name="browser_click")))
    assert backend.count == 1
    assert first.operation_id == second.operation_id and second.output["replayed"]


def test_context_read_can_recover_an_original_observation():
    from webagent.engine.context_tool import ContextReadBackend
    job = AgentJob(job_id="run", task_id="task", agent_name="webagent", target="https://example.test", objective="Read")
    run = AgentRun(run_id="run", job=job, spec_name="webagent", messages=[AgentMessage(role="tool", content="original observation")])
    output = asyncio.run(ContextReadBackend(run).execute("context_read", {"message_index": 0, "field": "content"}, {}))
    assert "original observation" in json.dumps(output)
