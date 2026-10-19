# WebAgent Technical Specification

Version: 0.1.0
Date: 2026-10-19

## 1. Scope and source

WebAgent extracts the generic agent runtime and semantic browser automation from
AIPentest commit `f6baaa5228023db975e224143314e8292392737b`. The standalone project
supports user-directed web tasks through a model/tool loop. The source project's
Execute and audit contracts (sections 5 and 6) apply to tool boundaries, bounded
resources and traceable results. Pentest orchestration, GTE, scanners, vulnerability
classification and production data are outside this release.

## 2. Milestone and data flow

Release M1: OpenAI-compatible model → generic AgentLoop → schema/policy gateway →
MCP browser or run workspace/Python sandbox → observation → next model decision.
The model chooses actions; the harness owns budgets, persistence and final output.
The browser uses the extracted semantic snapshot engine and separate BrowserContext
per MCP session. SQLite holds operation identities, private transcripts and artifacts;
redacted JSONL events support review.

## 3. Invariants

- A new standalone Git history contains only an explicit source allowlist.
- No production settings, credentials, browser profiles, task data or original Git history.
- Seven browser primitives: navigate, snapshot, click, fill, press, evaluate, wait.
- Session cookies, pages and ref maps are isolated; refs require a current snapshot.
- Configured origins are checked before browser requests, including redirects.
- Python is opt-in, Docker only, network disabled; workspace paths cannot escape root.
- Durable operation IDs precede execution; unknown outcomes are not silently retried.
- Browser sessions are closed on success, failure and cancellation.
- Page content and tool observations are data, never trusted system instructions.

## 4. Gates and acceptance

- Fresh Python install and npm ci work with documented commands.
- Offline demo uses a real local browser and a scripted model, no model key.
- Unit tests cover protocol failures, budgets, idempotency, workspace boundaries,
  unknown outcomes, secret redaction and session cleanup.
- Browser tests cover real click/fill, stale refs, origin limits and session isolation.
- Docker integration verifies script output and operation/artifact links.
- Public export is scanned for credentials, private paths and original domain imports.
- GitHub repository is public under aoao921/WebAgent, main pushed and CI configured.

## 5. Implementation and verification

M1 implementation is complete. The Python runtime, context projection/readback,
model response adapter, workspace and sandbox modules were extracted from the
stated source. The standalone store and WebHarness have no domain imports. The
browser MCP adapter uses the extracted semantic snapshot engine and explicit scope
configuration. Unknown browser outcomes propagate to durable operation state.
Snapshot reads refresh rather than replay stale results; successful actions retain
stable operation replay. All JSON schemas reject unknown properties, and workspace
paths reject rooted Windows paths as well as traversal.

Windows verification: 32 Python tests passed, including the real Chromium demo,
model-failure cleanup, cancellation cleanup and two concurrent agents with separate
audits; one Docker integration was skipped locally. In a separate clean Linux
Python 3.11 environment, all 33 Python tests passed, including real Docker Python,
workspace input, output and operation/artifact attribution. Four Node tests passed
on Windows and Linux with real Chromium: fill/click/stale refs, cookie/storage
isolation, redirect blocking before destination requests and MCP session release.
Ruff, pip check, wheel and source distribution builds passed. npm audit reports
zero known vulnerabilities after compatible dependency updates. A source allowlist
scan found no production host, private user path, private key or token patterns.

Published to the public https://github.com/aoao921/WebAgent repository with MIT
license, Chinese/English quick starts and an offline demo. The initial independent
commit is `b69dfe47fb18e3ce9941024479be363c4ee40251`. GitHub Actions run
https://github.com/aoao921/WebAgent/actions/runs/39197659990 passed all Python,
real Chromium, Docker, lint, dependency audit and package build steps. The public
repository was checked without authentication and has main as its default branch.

## 6. Current limitations

- Browser scopes are origin allowlists, not per-action business authorization.
  Page instructions remain untrusted; prompt-injection resistance is not proven.
- Browser state is ephemeral. SQLite supports audit and operation outcomes, not
  browser profile restoration or automatic task resume after a process restart.
- Public events contain metadata only. Private SQLite, result JSON and workspaces
  can contain sensitive task data; they are never part of the source release.
- Python runs offline in opt-in Docker. There is no script networking SDK, Node
  script sandbox, platform authentication, hosted UI or automatic package install.
- The browser MCP service is local by default. HTTP(S) redirects are manually
  fetched one hop at a time to avoid route.continue redirect bypass; WebSocket
  origins are also restricted. Blob/data URL navigation is unsupported.
- Scripted-model demos validate execution and audit, not a real model's success
  rate. No cross-model benchmark has been performed.
