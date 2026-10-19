import http from 'node:http';
import crypto from 'node:crypto';
import { chromium } from 'playwright';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { captureSnapshot } from './snapshotEngine.js';

const schema = (properties, required = []) => ({ type: 'object', properties, required, additionalProperties: false });
const text = { type: 'string', minLength: 1 };
const tools = [
  { name: 'browser_set_scope', description: 'Configure allowed HTTP origins before opening a browser context.',
    inputSchema: schema({ origins: { type: 'array', minItems: 1, maxItems: 16, items: text } }, ['origins']) },
  { name: 'browser_navigate', description: 'Navigate to an allowed URL and return a fresh semantic snapshot.', inputSchema: schema({ url: text }, ['url']) },
  { name: 'browser_snapshot', description: 'Observe the current page and get fresh element refs.', inputSchema: schema({}) },
  { name: 'browser_click', description: 'Click a visible element ref from the latest snapshot.', inputSchema: schema({ ref: text }, ['ref']) },
  { name: 'browser_fill', description: 'Fill an editable element from the latest snapshot.', inputSchema: schema({ ref: text, value: { type: 'string' } }, ['ref', 'value']) },
  { name: 'browser_press', description: 'Press a keyboard key and observe the resulting page.', inputSchema: schema({ key: text }, ['key']) },
  { name: 'browser_evaluate', description: 'Evaluate a bounded JavaScript expression in the current page. Network stays inside allowed origins.',
    inputSchema: schema({ expression: { ...text, maxLength: 64000 } }, ['expression']) },
  { name: 'browser_wait', description: 'Wait briefly and obtain a new snapshot.', inputSchema: schema({ seconds: { type: 'number', minimum: 0, maximum: 5 } }) },
  { name: 'browser_close', description: 'Close this session browser context.', inputSchema: schema({}) },
];

const sessions = new Map();
const maxSessions = Math.max(1, Number(process.env.MAX_MCP_SESSIONS || 16));
const idleMs = Math.max(1000, Number(process.env.MCP_SESSION_IDLE_TTL_MS || 1800000));
let browser;
let launching;
let pending = 0;
let shuttingDown = false;

function origin(url) {
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error('Only HTTP(S) URLs without embedded credentials are supported');
  }
  return parsed.origin;
}

function permitted(state, url) {
  try { return state.origins.has(origin(url)); } catch { return false; }
}

async function contextFor(state) {
  if (state.closed || (state.context && (!browser?.isConnected() || !state.page || state.page.isClosed()))) {
    throw Object.assign(new Error('Browser context unavailable; start a new session.'), { code: 'context_unavailable' });
  }
  if (state.context) return state.page;
  if (!browser?.isConnected()) {
    launching ||= chromium.launch({ headless: process.env.BROWSER_HEADLESS !== '0',
      ...(process.env.BROWSER_EXECUTABLE_PATH ? { executablePath: process.env.BROWSER_EXECUTABLE_PATH } : {}) });
    try { browser = await launching; } finally { launching = null; }
  }
  state.context = await browser.newContext({ serviceWorkers: 'block', acceptDownloads: false });
  await state.context.route('**/*', async route => {
    if (!permitted(state, route.request().url())) return await route.abort('blockedbyclient');
    try {
      // Playwright route.continue can follow redirects without a second route
      // callback. Fetch one hop only and inspect Location before the next request.
      const reply = await route.fetch({ maxRedirects: 0, timeout: 15000 });
      const location = reply.headers().location;
      if (reply.status() >= 300 && reply.status() < 400 && location &&
          !permitted(state, new URL(location, route.request().url()).href)) {
        return await route.abort('blockedbyclient');
      }
      await route.fulfill({ response: reply });
    } catch {
      await route.abort('failed').catch(() => {});
    }
  });
  if (state.context.routeWebSocket) {
    await state.context.routeWebSocket('**/*', socket => {
      const httpUrl = socket.url().replace(/^ws:/, 'http:').replace(/^wss:/, 'https:');
      if (permitted(state, httpUrl)) socket.connectToServer(); else socket.close();
    });
  }
  state.page = await state.context.newPage();
  state.page.setDefaultTimeout(10000);
  state.page.setDefaultNavigationTimeout(20000);
  return state.page;
}

async function snapshot(state) {
  const value = await captureSnapshot(state.page, { refPrefix: `s${++state.generation}-` });
  state.refs = value.ref_map || {};
  return { url: state.page.url(), snapshot: String(value.yaml || '').slice(0, 64000),
    snapshot_truncated: String(value.yaml || '').length > 64000 };
}

function locator(state, ref) {
  const selector = state.refs[String(ref)];
  if (!selector) throw Object.assign(new Error('Unknown or stale ref; obtain a fresh snapshot.'), { code: 'stale_ref' });
  return state.page.locator(selector).first();
}

function response(value, isError = false) {
  return { content: [{ type: 'text', text: JSON.stringify(value) }], isError };
}

async function closeContext(state) {
  state.closed = true;
  const context = state.context;
  state.context = null;
  state.page = null;
  state.refs = {};
  if (context) await context.close().catch(() => {});
}

async function perform(state, name, args) {
  let started = false;
  try {
    if (!tools.some(tool => tool.name === name)) throw new Error('Unknown browser tool');
    if (name === 'browser_set_scope') {
      if (state.context || state.closed) throw new Error('Scope must be set before creating a browser context');
      if (!Array.isArray(args.origins) || !args.origins.length || args.origins.length > 16) throw new Error('Expected 1 to 16 origins');
      state.origins = new Set(args.origins.map(origin));
      return response({ status: 'succeeded', origins: [...state.origins] });
    }
    if (name === 'browser_close') {
      await closeContext(state);
      return response({ status: 'succeeded', closed: true });
    }
    if (name === 'browser_navigate') {
      const targetOrigin = origin(args.url);
      if (!state.origins.size) state.origins.add(targetOrigin);
      if (!permitted(state, args.url)) throw new Error('URL outside this session allowed origins');
    }
    if (!state.origins.size) throw new Error('Navigate or set scope before other browser actions');
    const page = await contextFor(state);
    let result;
    if (name === 'browser_navigate') {
      state.refs = {};
      started = true;
      await page.goto(args.url, { waitUntil: 'domcontentloaded' });
    } else if (name === 'browser_click' || name === 'browser_fill') {
      const element = locator(state, args.ref);
      if (!await element.isVisible() || await element.isDisabled()) throw new Error('Ref is not an enabled visible element');
      started = true;
      if (name === 'browser_fill') await element.fill(String(args.value ?? ''));
      else await element.click();
      await page.waitForTimeout(100);
    } else if (name === 'browser_press') {
      if (typeof args.key !== 'string' || args.key.length > 80) throw new Error('Invalid key');
      started = true;
      await page.keyboard.press(args.key);
    } else if (name === 'browser_evaluate') {
      if (typeof args.expression !== 'string' || !args.expression || args.expression.length > 64000) throw new Error('Invalid expression');
      started = true;
      result = await Promise.race([
        page.evaluate(args.expression),
        new Promise((_, reject) => {
          const timer = setTimeout(() => reject(new Error('Evaluation timed out')), 10000);
          timer.unref();
        }),
      ]);
      const encoded = JSON.stringify(result ?? null);
      if (encoded.length > 32000) result = { truncated: true, preview: encoded.slice(0, 32000) };
    } else if (name === 'browser_wait') {
      await page.waitForTimeout(Math.min(5, Math.max(0, Number(args.seconds ?? 0.5))) * 1000);
    }
    if (!permitted(state, page.url())) throw new Error('Current page left the configured origins');
    const view = await snapshot(state);
    return response({ status: 'succeeded', execution_state: 'completed', ...view, ...(result === undefined ? {} : { result }) });
  } catch (error) {
    // A timed-out action might still have changed the application. Never retry it here.
    const unknown = started;
    if (unknown) await closeContext(state);
    return response({ status: 'failed', error: String(error.message || error).slice(0, 500),
      code: error.code || 'browser_action_failed', execution_state: unknown ? 'outcome_unknown' : 'not_started',
      session_invalid: state.closed }, true);
  }
}

function createServer(state) {
  const server = new Server({ name: 'webagent-browser', version: '0.1.0' }, { capabilities: { tools: {} } });
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));
  server.setRequestHandler(CallToolRequestSchema, request => {
    const operation = state.queue.then(async () => {
      state.active += 1;
      try { return await perform(state, request.params.name, request.params.arguments || {}); }
      finally { state.active -= 1; state.lastUsed = Date.now(); }
    });
    state.queue = operation.catch(() => {});
    return operation;
  });
  return server;
}

async function dispose(id, closeTransport = true) {
  const entry = sessions.get(id);
  if (!entry) return;
  sessions.delete(id);
  await entry.state.queue;
  await closeContext(entry.state);
  if (closeTransport) await entry.transport.close().catch(() => {});
}

const httpServer = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url || '/', 'http://localhost');
    if (url.pathname === '/health') {
      res.setHeader('content-type', 'application/json');
      return res.end(JSON.stringify({ status: 'ok', sessions: sessions.size, browser_connected: !!browser?.isConnected() }));
    }
    if (url.pathname !== '/mcp') { res.statusCode = 404; return res.end('Not found'); }
    const id = String(req.headers['mcp-session-id'] || '');
    if (id) {
      const entry = sessions.get(id);
      if (!entry) { res.statusCode = 404; return res.end('Unknown session'); }
      entry.state.lastUsed = Date.now();
      return await entry.transport.handleRequest(req, res);
    }
    if (req.method !== 'POST' || shuttingDown) { res.statusCode = 400; return res.end('Initialize a session first'); }
    if (sessions.size + pending >= maxSessions) { res.statusCode = 429; return res.end('Session capacity reached'); }
    pending += 1;
    try {
      const state = { origins: new Set(), refs: {}, generation: 0, queue: Promise.resolve(), lastUsed: Date.now(), active: 0, closed: false };
      const server = createServer(state);
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: () => crypto.randomUUID(),
        onsessioninitialized: sessionId => sessions.set(sessionId, { state, transport, server }) });
      transport.onclose = () => { if (transport.sessionId) void dispose(transport.sessionId, false); };
      await server.connect(transport);
      await transport.handleRequest(req, res);
      if (!transport.sessionId) await server.close();
    } finally { pending -= 1; }
  } catch {
    if (!res.headersSent) res.writeHead(500);
    res.end('Browser transport error');
  }
});

const cleanup = setInterval(() => {
  for (const [id, entry] of sessions) {
    if (!entry.state.active && Date.now() - entry.state.lastUsed > idleMs) void dispose(id);
  }
}, 1000);
cleanup.unref();

async function shutdown() {
  if (shuttingDown) return;
  shuttingDown = true;
  clearInterval(cleanup);
  await Promise.all([...sessions.keys()].map(id => dispose(id)));
  if (browser) await browser.close().catch(() => {});
  httpServer.close();
  httpServer.closeAllConnections();
}

const host = process.env.MCP_HOST || '127.0.0.1';
httpServer.listen(Number(process.env.MCP_PORT || 8931), host, () => {
  console.log(JSON.stringify({ url: `http://${host}:${httpServer.address().port}/mcp` }));
});
process.once('SIGINT', shutdown);
process.once('SIGTERM', shutdown);
