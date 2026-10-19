import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';

let child, fixture, foreign, mcpUrl, fixtureUrl, foreignUrl;
let foreignRequests = 0;
const html = `<!doctype html><html><head><title>Fixture</title></head><body><main>
<h1>Profile</h1><label>Name<input aria-label="Name" value="private-field-value"></label>
<button onclick="document.querySelector('#result').textContent='Saved '+document.querySelector('input').value">Save</button>
<p id="result">Not saved</p></main></body></html>`;

before(async () => {
  foreign = http.createServer((_, res) => { foreignRequests += 1; res.end('foreign'); });
  foreign.listen(0, '127.0.0.1');
  await once(foreign, 'listening');
  foreignUrl = `http://127.0.0.1:${foreign.address().port}`;
  fixture = http.createServer((req, res) => {
    if (req.url === '/redirect') { res.writeHead(302, { location: foreignUrl }); return res.end(); }
    res.setHeader('content-type', 'text/html');
    res.end(html);
  });
  fixture.listen(0, '127.0.0.1');
  await once(fixture, 'listening');
  fixtureUrl = `http://127.0.0.1:${fixture.address().port}`;
  child = spawn(process.execPath, ['src/server.js'], {
    cwd: new URL('../', import.meta.url),
    env: { ...process.env, MCP_HOST: '127.0.0.1', MCP_PORT: '0', BROWSER_HEADLESS: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let buffer = '';
  mcpUrl = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('Server did not start')), 10000);
    child.once('error', reject);
    child.stdout.on('data', data => {
      buffer += data;
      if (buffer.includes('\n')) { clearTimeout(timeout); resolve(JSON.parse(buffer.split('\n')[0]).url); }
    });
  });
});

after(async () => {
  if (child) { const exited = once(child, 'exit'); child.kill(); await exited; }
  for (const server of [fixture, foreign]) if (server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
});

async function connect() {
  const transport = new StreamableHTTPClientTransport(new URL(mcpUrl));
  const client = new Client({ name: 'test', version: '1.0.0' });
  await client.connect(transport);
  return { client, transport, async close() {
    try { await transport.terminateSession(); } finally { await client.close(); }
  } };
}

async function call(session, name, args = {}) {
  const result = await session.client.callTool({ name, arguments: args });
  const payload = JSON.parse(result.content[0].text);
  return { ...payload, isError: !!result.isError };
}

function ref(snapshot, role) {
  const match = snapshot.match(new RegExp(`${role}[^\\n]*\\[ref=([^\\]]+)\\]`));
  assert.ok(match, `Missing ${role} in snapshot: ${snapshot}`);
  return match[1];
}

test('semantic refs support fill/click and reject stale refs', async () => {
  const session = await connect();
  try {
    const first = await call(session, 'browser_navigate', { url: fixtureUrl });
    assert.equal(first.status, 'succeeded', JSON.stringify(first));
    assert.ok(!first.snapshot.includes('private-field-value'));
    const filled = await call(session, 'browser_fill', { ref: ref(first.snapshot, 'textbox'), value: 'Ada' });
    assert.equal(filled.status, 'succeeded');
    const stale = await call(session, 'browser_click', { ref: ref(first.snapshot, 'button') });
    assert.equal(stale.code, 'stale_ref');
    assert.equal(stale.execution_state, 'not_started');
    const saved = await call(session, 'browser_click', { ref: ref(filled.snapshot, 'button') });
    assert.equal(saved.status, 'succeeded');
    assert.ok(saved.snapshot.includes('Saved Ada'));
  } finally { await session.close(); }
});

test('sessions isolate cookies and localStorage on the same origin', async () => {
  const [a, b] = await Promise.all([connect(), connect()]);
  try {
    await Promise.all([call(a, 'browser_navigate', { url: fixtureUrl }), call(b, 'browser_navigate', { url: fixtureUrl })]);
    const written = await call(a, 'browser_evaluate', { expression: "localStorage.setItem('owner','A'); document.cookie='owner=A'; 'written'" });
    assert.equal(written.status, 'succeeded');
    const isolated = await call(b, 'browser_evaluate', { expression: "({owner:localStorage.getItem('owner'),cookie:document.cookie})" });
    assert.deepEqual(isolated.result, { owner: null, cookie: '' });
    await a.close();
    const stillAlive = await call(b, 'browser_snapshot');
    assert.equal(stillAlive.status, 'succeeded');
  } finally { await b.close(); await a.client.close(); }
});

test('redirect cannot issue a request outside session origins', async () => {
  const session = await connect();
  try {
    const scope = await call(session, 'browser_set_scope', { origins: [fixtureUrl] });
    assert.equal(scope.status, 'succeeded');
    const before = foreignRequests;
    const rejected = await call(session, 'browser_navigate', { url: fixtureUrl + '/redirect' });
    assert.equal(rejected.isError, true);
    assert.equal(rejected.execution_state, 'outcome_unknown');
    assert.equal(foreignRequests, before);
  } finally { await session.close(); }
});

test('MCP exposes generic browser capabilities and releases sessions', async () => {
  const session = await connect();
  const listed = await session.client.listTools();
  assert.equal(listed.tools.length, 9);
  assert.ok(!listed.tools.some(tool => /xss|scan|request_batch/.test(tool.name)));
  await session.close();
  const response = await fetch(mcpUrl.replace('/mcp', '/health'));
  assert.equal((await response.json()).sessions, 0);
});
