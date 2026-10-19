"""Offline smoke demo: scripted decisions, real MCP and real Chromium."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import threading

from .agent import WebAgent
from .config import Settings
from .engine.protocol import AgentMessage, ModelResponse, ToolCall

HTML = b'''<!doctype html><html><head><title>WebAgent demo</title></head><body>
<main><h1>Profile</h1><label>Name <input aria-label="Name" id="name"></label>
<button onclick="document.querySelector('#result').textContent='Saved '+document.querySelector('#name').value">Save</button>
<p id="result" role="status">Not saved</p></main></body></html>'''


class DemoModel:
    """Deterministic fixture, deliberately not a live LLM capability evaluation."""

    def __init__(self, url):
        self.url = url
        self.turn = 0
        self.operations = []

    async def complete(self, request):
        self.turn += 1
        if self.turn == 1:
            call = ToolCall(id="open", name="browser_navigate", arguments={"url": self.url})
        else:
            observation = json.loads(request.messages[-1].content)
            if observation["status"] != "succeeded":
                raise RuntimeError("Demo action failed: " + str(observation.get("error")))
            self.operations.append(observation["operation_id"])
            snapshot = observation["output"]["event_summary"]["tool_output"]["snapshot"]
            if self.turn == 2:
                ref = re.search(r'textbox[^\n]*\[ref=([^\]]+)\]', snapshot)
                if not ref:
                    raise RuntimeError("Demo textbox missing from semantic snapshot")
                call = ToolCall(id="fill", name="browser_fill", arguments={"ref": ref.group(1), "value": "Ada"})
            elif self.turn == 3:
                ref = re.search(r'button[^\n]*\[ref=([^\]]+)\]', snapshot)
                if not ref:
                    raise RuntimeError("Demo button missing from semantic snapshot")
                call = ToolCall(id="save", name="browser_click", arguments={"ref": ref.group(1)})
            else:
                if "Saved Ada" not in snapshot:
                    raise RuntimeError("Save postcondition was not observed")
                return ModelResponse(message=AgentMessage(role="assistant", content=json.dumps({
                    "summary": "Filled Name with Ada, clicked Save, and observed Saved Ada.",
                    "evidence_operation_ids": self.operations,
                })))
        return ModelResponse(message=AgentMessage(role="assistant", tool_calls=[call]))


async def run_demo(mcp_url: str, data_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)

        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        agent = WebAgent(Settings(mcp_url=mcp_url, data_dir=data_dir), model_client=DemoModel(url))
        return await agent.run(url=url, goal="Fill Name with Ada, click Save, and verify the saved message")
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
