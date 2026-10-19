"""MCP adapter for browser primitives, independent of any pentest platform."""

from contextlib import AsyncExitStack, asynccontextmanager
import json

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .engine.protocol import ToolDefinition
from .specs import ToolSpec

BROWSER_TOOLS = frozenset({"browser_navigate", "browser_snapshot", "browser_click", "browser_fill",
                           "browser_press", "browser_evaluate", "browser_wait"})


class BrowserTools:
    name = "browser"

    def __init__(self, client, definitions):
        self.client = client
        self.definitions = definitions
        self.registry = {name: ToolSpec(name=name, display_name=name, runtime="mcp_browser", category="browser",
                         risk_level="low" if name in {"browser_snapshot", "browser_wait"} else "high",
                         input_schema=definition.input_schema) for name, definition in definitions.items()}

    def specs(self):
        return dict(self.registry)

    def model_definitions(self):
        return dict(self.definitions)

    async def execute(self, tool_name, input_data, state):
        del state
        try:
            result = await self.client.call_tool(tool_name, arguments=input_data)
        except Exception as exc:
            # A disconnected request can already have changed the browser.
            raise TimeoutError("Browser transport lost; execution outcome is unknown") from exc
        blocks = [block.text for block in result.content if getattr(block, "type", "") == "text"]
        try:
            payload = json.loads("\n".join(blocks))
        except (TypeError, ValueError) as exc:
            raise TimeoutError("Browser returned an invalid response; execution outcome is unknown") from exc
        return {"success": not result.isError and payload.get("status") == "succeeded",
                "error": payload.get("error", ""), "execution_state": payload.get("execution_state", "completed"),
                "event_summary": {"action": tool_name, "tool_output": payload}}


@asynccontextmanager
async def browser_tools(url: str, origins: list[str]):
    async with AsyncExitStack() as stack:
        # Local execution traffic must not accidentally traverse a system proxy.
        http = await stack.enter_async_context(httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(45, read=300)))
        reader, writer, _ = await stack.enter_async_context(streamable_http_client(url, http_client=http, terminate_on_close=True))
        client = await stack.enter_async_context(ClientSession(reader, writer))
        await client.initialize()
        try:
            configured = await client.call_tool("browser_set_scope", arguments={"origins": origins})
            if configured.isError:
                raise ValueError("Browser scope configuration failed")
            listed = await client.list_tools()
            definitions = {tool.name: ToolDefinition(name=tool.name, description=tool.description or "",
                           input_schema=tool.inputSchema) for tool in listed.tools if tool.name in BROWSER_TOOLS}
            if set(definitions) != BROWSER_TOOLS:
                raise ValueError("Browser server does not provide the seven required primitives")
            yield BrowserTools(client, definitions)
        finally:
            try:
                await client.call_tool("browser_close", arguments={})
            except Exception:
                # The transport context below still sends DELETE on normal exit.
                pass
