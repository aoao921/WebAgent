import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import threading
import urllib.request

import pytest

from webagent.agent import WebAgent
from webagent.config import Settings
from webagent.demo import DemoModel, HTML, run_demo
from webagent.engine.protocol import AgentMessage, ModelResponse, ToolCall


@pytest.fixture
def browser_server():
    if os.getenv("WEBAGENT_BROWSER_TEST") != "1":
        pytest.skip("Set WEBAGENT_BROWSER_TEST=1 for real browser integration")
    directory = Path(__file__).parents[1] / "browser"
    env = {**os.environ, "MCP_HOST": "127.0.0.1", "MCP_PORT": "0", "BROWSER_HEADLESS": "1"}
    process = subprocess.Popen(["node", "src/server.js"], cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = process.stdout.readline()
        assert line, process.stderr.read()
        yield json.loads(line)["url"]
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def health(url):
    with urllib.request.urlopen(url.replace("/mcp", "/health"), timeout=5) as response:
        return json.load(response)


@pytest.fixture
def fixture_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML)
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.integration
def test_real_demo_and_session_cleanup(browser_server, tmp_path):
    result = asyncio.run(run_demo(browser_server, tmp_path))
    assert result.success, result.error
    assert "Saved Ada" in result.output["summary"]
    assert result.tool_calls == 3
    assert len(result.output["evidence_operation_ids"]) == 3
    assert health(browser_server)["sessions"] == 0
    directory = tmp_path / result.run_id
    assert (directory / "state.sqlite3").is_file()
    public = (directory / "events.jsonl").read_text()
    assert "Ada" not in public
    assert all(item.operation_id for item in result.observations)


@pytest.mark.integration
def test_model_failure_closes_browser_session(browser_server, fixture_page, tmp_path):
    class FailingModel:
        def __init__(self):
            self.calls = 0
        async def complete(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(message=AgentMessage(role="assistant", tool_calls=[ToolCall(id="open", name="browser_navigate", arguments={"url": fixture_page})]))
            raise RuntimeError("fixture model unavailable")
    settings = Settings(mcp_url=browser_server, data_dir=tmp_path)
    result = asyncio.run(WebAgent(settings, model_client=FailingModel()).run(url=fixture_page, goal="Read"))
    assert not result.success and "fixture model unavailable" in result.error
    assert result.observations[0].status.value == "succeeded"
    assert health(browser_server)["sessions"] == 0


@pytest.mark.integration
def test_cancellation_closes_browser_session(browser_server, fixture_page, tmp_path):
    async def exercise():
        ready = asyncio.Event()
        class WaitingModel:
            def __init__(self):
                self.calls = 0
            async def complete(self, request):
                self.calls += 1
                if self.calls == 1:
                    return ModelResponse(message=AgentMessage(role="assistant", tool_calls=[ToolCall(id="open", name="browser_navigate", arguments={"url": fixture_page})]))
                ready.set()
                await asyncio.Event().wait()
        agent = WebAgent(Settings(mcp_url=browser_server, data_dir=tmp_path), model_client=WaitingModel())
        task = asyncio.create_task(agent.run(url=fixture_page, goal="Wait"))
        await asyncio.wait_for(ready.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(exercise())
    assert health(browser_server)["sessions"] == 0


@pytest.mark.integration
def test_two_agents_run_concurrently_with_separate_audits(browser_server, tmp_path):
    async def exercise():
        return await asyncio.gather(run_demo(browser_server, tmp_path), run_demo(browser_server, tmp_path))
    first, second = asyncio.run(exercise())
    assert first.success and second.success
    assert first.run_id != second.run_id
    assert not {row.operation_id for row in first.observations} & {row.operation_id for row in second.observations}
    assert (tmp_path / first.run_id / "state.sqlite3").is_file()
    assert (tmp_path / second.run_id / "state.sqlite3").is_file()
    assert health(browser_server)["sessions"] == 0


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("WEBAGENT_DOCKER_TEST") != "1", reason="Set WEBAGENT_DOCKER_TEST=1 for Docker integration")
def test_real_docker_workspace_and_operation_audit(browser_server, tmp_path):
    from webagent.engine.protocol import AgentMessage, ModelResponse, ToolCall
    from webagent.store import CheckpointStore
    class CodingModel(DemoModel):
        async def complete(self, request):
            self.turn += 1
            if self.turn == 1:
                call = ToolCall(id="write", name="workspace_write", arguments={"path": "input.txt", "content": "42"})
            elif self.turn == 2:
                call = ToolCall(id="python", name="python_run", arguments={"code": "from pathlib import Path; print(int(Path('input.txt').read_text())*2)"})
            else:
                observation = json.loads(request.messages[-1].content)
                assert observation["status"] == "succeeded", observation
                assert "84" in json.dumps(observation["output"])
                return ModelResponse(message=AgentMessage(role="assistant", content=json.dumps({
                    "summary": "Python computed 84", "evidence_operation_ids": [observation["operation_id"]]})))
            return ModelResponse(message=AgentMessage(role="assistant", tool_calls=[call]))
    settings = replace(Settings(), mcp_url=browser_server, data_dir=tmp_path)
    result = asyncio.run(WebAgent(settings, model_client=CodingModel("")).run(url="https://example.test/", goal="Analyze local data", coding=True))
    assert result.success, result.error
    store = CheckpointStore(tmp_path / result.run_id / "state.sqlite3")
    process = next(row for row in result.observations if row.tool_name == "python_run")
    assert store.get_operation(process.operation_id)["status"] == "succeeded"
    assert store.rows("artifact")[0]["operation_id"] == result.observations[0].operation_id
