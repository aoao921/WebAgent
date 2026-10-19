# WebAgent

[![CI](https://github.com/aoao921/WebAgent/actions/workflows/ci.yml/badge.svg)](https://github.com/aoao921/WebAgent/actions/workflows/ci.yml)

A lightweight web agent extracted from AIPentest. Give it a goal and a URL; the
model observes semantic browser snapshots, chooses actions, inspects results, and
continues until it can answer or explain a blocker.

**Less is more:** the runtime provides browser primitives, workspace files,
optional sandboxed Python, budgets and audit records. The model owns the plan.

[中文说明](README.zh-CN.md) · [Technical specification](TECHNICAL_SPECIFICATION.md)

## What is included

- Semantic page snapshots with fresh element refs and useful page structure.
- Seven browser tools: navigate, snapshot, click, fill, press, evaluate and wait.
- Isolated Chromium contexts for concurrent MCP clients.
- A provider-neutral agent loop with OpenAI-compatible Chat Completions adapters.
- Bounded context with original observation readback, finite repair and budgets.
- Run workspaces and optional **offline Docker** Python execution.
- SQLite transcripts, operation identities, artifact hashes and public event metadata.
- An offline demo using a scripted model and a real browser.

This repository publishes the general agent and browser components. It has its own
Git history and no dependency on the AIPentest backend or its deployed services.

## Quick start

Requirements: Python 3.11+, Node.js 20+, and Chromium. Docker is optional.

```bash
git clone https://github.com/aoao921/WebAgent.git
cd WebAgent
python -m venv .venv
```

Activate the environment:

```bash
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install and start the browser server in the first terminal:

```bash
python -m pip install -e '.[dev]'
cd browser
npm ci
npx playwright install chromium
npm start
```

On Linux, `npx playwright install --with-deps chromium` also installs OS browser
dependencies. To use an existing Chromium binary, set `BROWSER_EXECUTABLE_PATH`.
The browser server listens on `http://127.0.0.1:8931/mcp`; health is `/health`.
It is a local execution service without platform authentication.
Browser settings such as `BROWSER_EXECUTABLE_PATH` are process environment variables
for `npm start`; the root `.env` is loaded by the Python client only.

In the second terminal, activate the same Python environment and run from the
repository root:

```bash
webagent demo
```

The demo starts a temporary local page, fills a name, clicks Save and checks the
result through the real browser. **Its model is scripted**: it checks installation
and the execution chain, not an LLM's reasoning ability. No model key is needed.

## Use a model

Copy `.env.example` to `.env` in the repository root, then configure:

```dotenv
LLM_API_KEY=your-own-key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=your-tool-calling-model
```

```bash
webagent run --url https://example.com --goal "Read the page and summarize what it describes"
```

The model must support tool calls through Chat Completions. Compatibility with
every provider/model combination is not guaranteed. Configure extra origins only
when the task needs them, for example an explicitly authorized login origin:

```bash
webagent run --url https://example.com --allow-origin https://login.example.com --goal "Complete my web task"
```

HTTP requests, redirects and subresources are checked against the session's origin
allowlist. This may block third-party assets until their origins are explicitly
added. Page text is treated as untrusted data; origin limits do not solve every
form of prompt injection or prevent unwanted actions within an allowed site.

## Optional coding capability

```bash
docker pull python:3.12-slim
webagent run --url https://example.com --goal "Read the page and analyze the extracted data" --coding
```

`--coding` adds `python_run`. Python executes in Docker with the run workspace
mounted, networking disabled, a read-only container root and limited resources.
The model uses browser tools for website access. Without `--coding`, only browser
tools and workspace list/read/write are exposed; there is no host process tool.

## Python API

```python
import asyncio
from webagent.agent import WebAgent

async def main():
    result = await WebAgent().run(
        url="https://example.com",
        goal="Summarize the page using observations from the browser",
    )
    print(result.output)

asyncio.run(main())
```

Model and browser connections are closed on completion, failure or cancellation.
The browser server manages independent contexts for separate runs. Browser login
state is ephemeral and is not reconstructed from SQLite after a process restart.

## Audit records

Each run writes to `var/<run_id>/`:

| File | Contents |
| --- | --- |
| `events.jsonl` | Public event metadata: timestamps, tool names, status and operation IDs |
| `state.sqlite3` | Private transcript, operation results, process and artifact records |
| `result.json` | Final result and recorded observations; private task data |
| `workspace/` | Run files and generated artifacts |

```bash
webagent inspect RUN_ID
```

Public event metadata omits tool argument values, page text, script source and
model reasoning. Private transcripts retain task inputs and observations and may
contain secrets from the task. The runtime directory is gitignored; do not publish
it as an example trace. Audit records support traceability, not tamper-proof logging.

## Development

```bash
python -m pytest -q
python -m ruff check src tests
python -m build
cd browser
npm test
npm audit --omit=dev
```

The Python browser integration test is opt-in:

```bash
# Linux / macOS
WEBAGENT_BROWSER_TEST=1 python -m pytest -q -m integration
# PowerShell
$env:WEBAGENT_BROWSER_TEST='1'
python -m pytest -q -m integration
```

Docker integration additionally uses `WEBAGENT_DOCKER_TEST=1`. CI runs a real
Chromium demo plus the tests. See the technical specification for verified scope
and current limitations.

## License

MIT. Original generic runtime and semantic snapshot implementation are extracted
from AIPentest by aoao921; see [NOTICE](NOTICE). Dependencies retain their licenses.
