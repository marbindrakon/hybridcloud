// Raw /api/k8s proxy (C6): path normalisation, watch detection and the hook's limits, against a fake API server.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';

const UI = process.env.UI_ROOT;
const { createApiK8sProxy, normaliseK8sPath } = await import(`${UI}/packages/shared/server/apiK8sProxy.js`);
const tenantSafe = await import(`${UI}/packages/shared/server/tenantSafe.js`);
const require = createRequire(`${UI}/package.json`);
const express = require('express');

const BASE = '/apis/hybridsovereign.redhat/v1alpha1';
const secretObj = (kind, name) => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1', kind, metadata: { name, namespace: 'entity-t1' },
  spec: kind === 'CloudOSO' ? { vaultPath: 'p/acct' } : { openstack: { pullSecretVaultPath: 'p/ps' } },
  status: { phase: 'Ready', accountVaultPath: 'p/acct', projectId: 'p1' },
});

let api; let app; let appServer; let appUrl; const seen = [];
before(async () => {
  api = http.createServer((req, res) => {
    seen.push(req.url);
    const u = new URL(req.url, 'http://x');
    res.setHeader('Content-Type', 'application/json');
    if (u.pathname === '/apis/x/broken') return res.end('{"kind":"CloudOSO", "status": {"accountVaultPath": "p/');
    if (u.pathname === '/apis/x/big') return res.end(JSON.stringify({ kind: 'List', items: [], pad: 'x'.repeat(5000) }));
    if (u.pathname === '/apis/x/stream') {
      // a watch the proxy did not refuse: two events in one write, one split over writes
      res.setHeader('Content-Type', 'application/json;stream=watch');
      const a = JSON.stringify({ type: 'ADDED', object: secretObj('CloudOSO', 'a') });
      const b = JSON.stringify({ type: 'MODIFIED', object: secretObj('PlatformOpenshift', 'b') });
      res.write(`${a}\n${b.slice(0, 20)}`);
      setTimeout(() => res.end(`${b.slice(20)}\n`), 20);
      return undefined;
    }
    if (u.pathname === '/apis/x/streambad') {
      res.setHeader('Content-Type', 'application/json;stream=watch');
      res.write(`${JSON.stringify({ type: 'ADDED', object: secretObj('CloudOSO', 'a') })}\nnot json\n`);
      setTimeout(() => res.end(`${JSON.stringify({ type: 'ADDED', object: secretObj('CloudOSO', 'c') })}\n`), 20);
      return undefined;
    }
    if (u.pathname.startsWith(`${BASE}/`)) {
      return res.end(JSON.stringify({ kind: 'CloudOSOList', items: [secretObj('CloudOSO', 'a')] }));
    }
    return res.end(JSON.stringify({ kind: 'List', items: [secretObj('CloudOSO', 'a')] }));
  });
  await new Promise((r) => api.listen(0, '127.0.0.1', r));
  const apiUrl = `http://127.0.0.1:${api.address().port}`;
  app = express();
  app.use('/t', createApiK8sProxy(apiUrl, { transformBody: tenantSafe.transformBody, maxBodyBytes: 4096 }));
  app.use('/plain', createApiK8sProxy(apiUrl));
  await new Promise((r) => { appServer = app.listen(0, '127.0.0.1', r); });
  appUrl = `http://127.0.0.1:${appServer.address().port}`;
});
after(() => { api.close(); appServer.close(); });

const H = { 'x-forwarded-access-token': 'tok' };
const get = async (p) => {
  const r = await fetch(`${appUrl}${p}`, { headers: H });
  const text = await r.text();
  let body = null;
  try { body = JSON.parse(text); } catch { body = null; }
  return { status: r.status, text, body };
};
const leaks = (text) => /VaultPath|vaultPath/.test(text);
// fetch() resolves dot segments (also %2E) itself; send these paths as written
const rawGet = (p) => new Promise((resolve, reject) => {
  const r = http.get({ host: '127.0.0.1', port: appServer.address().port, path: p, headers: H }, (res) => {
    res.resume();
    res.on('end', () => resolve({ status: res.statusCode }));
  });
  r.on('error', reject);
});

test('normaliseK8sPath: decodes, collapses and re-encodes; refuses encoded slashes and dot segments', () => {
  assert.equal(normaliseK8sPath(`${BASE}/namespaces/e/cloud%6Fsos`).path, `${BASE}/namespaces/e/cloudosos`);
  assert.equal(normaliseK8sPath(`/apis//hybridsovereign.redhat/v1alpha1//cloudosos/`).path, `${BASE}/cloudosos`);
  assert.equal(normaliseK8sPath(`${BASE}/cloudosos?labelSelector=a%3Db&limit=5`).path, `${BASE}/cloudosos?labelSelector=a%3Db&limit=5`);
  for (const bad of [`${BASE}/namespaces/e%2Fcloudosos`, `${BASE}/x/../cloudosos`, `${BASE}/./cloudosos`, `${BASE}/x/%2e%2e/cloudosos`, `${BASE}/a\\b`, `${BASE}/%zz`, `${BASE}/x%5Cy`]) {
    assert.ok('error' in normaliseK8sPath(bad), bad);
  }
});

test('normaliseK8sPath: watch is any first value but 0/false, or the watch segment', () => {
  const w = (q) => normaliseK8sPath(`${BASE}/cloudosos${q}`).watch;
  for (const q of ['?watch=true', '?watch=1', '?watch=yes', '?watch=TRUE', '?watch=', '?watch', '?watch=false%20', '?watch=1&watch=false']) assert.equal(w(q), true, q);
  for (const q of ['', '?watch=0', '?watch=false', '?watch=FALSE', '?watch=False&watch=true', '?watcher=true']) assert.equal(w(q), false, q);
  assert.equal(normaliseK8sPath(`${BASE}/watch/namespaces/e/cloudosos`).watch, true);
  assert.equal(normaliseK8sPath('/api/v1/watch/namespaces/e/configmaps').watch, true);
  assert.equal(normaliseK8sPath('/api/v1/namespaces/watch/configmaps').watch, false);
});

test('proxy forwards the normalised path', async () => {
  seen.length = 0;
  const r = await get(`/plain${BASE}//namespaces/entity-t1/cloud%6Fsos/?limit=1`);
  assert.equal(r.status, 200);
  assert.deepEqual(seen, [`${BASE}/namespaces/entity-t1/cloudosos?limit=1`]);
});

test('proxy refuses encoded slashes and dot segments before forwarding', async () => {
  seen.length = 0;
  for (const p of [`${BASE}/namespaces/entity-t1%2Fcloudosos`, `${BASE}/x/..%2Fcloudosos`, `${BASE}/x/%2E%2E/cloudosos`, `${BASE}/x/../cloudosos`, `${BASE}/./cloudosos`]) {
    const r = await rawGet(`/t${p}`);
    assert.equal(r.status, 400, p);
  }
  assert.deepEqual(seen, []);
});

test('tenant hook: encoded and trailing-slash spellings are filtered', async () => {
  for (const p of [`${BASE}/namespaces/entity-t1/cloud%6Fsos`, `${BASE}/namespaces/entity-t1/cloudosos/`, `${BASE}//namespaces/entity-t1/cloudosos`]) {
    const r = await get(`/t${p}`);
    assert.equal(r.status, 200, p);
    assert.equal(r.body.items[0].status.projectId, 'p1');
    assert.ok(!leaks(r.text), p);
  }
});

test('tenant hook: every watch spelling on the filtered plurals is refused before forwarding', async () => {
  seen.length = 0;
  for (const q of ['watch=yes', 'watch=TRUE', 'watch=', 'watch=on', 'watch=1&watch=0']) {
    const r = await get(`/t${BASE}/namespaces/entity-t1/cloud%6Fsos?${q}`);
    assert.equal(r.status, 400, q);
  }
  assert.deepEqual(seen, []);
});

test('tenant hook: other paths are filtered by object kind', async () => {
  const r = await get('/t/apis/x/list');
  assert.equal(r.status, 200);
  assert.equal(r.body.items[0].status.projectId, 'p1');
  assert.ok(!leaks(r.text));
});

test('tenant hook: a watch stream that is not refused is filtered event by event', async () => {
  const r = await get('/t/apis/x/stream?watch=yes');
  assert.equal(r.status, 200);
  const events = r.text.trim().split('\n').map((l) => JSON.parse(l));
  assert.deepEqual(events.map((e) => e.type), ['ADDED', 'MODIFIED']);
  assert.ok(!leaks(r.text));
  assert.equal(events[1].object.status.phase, 'Ready');
});

test('tenant hook: an unparsable watch event ends the stream', async () => {
  const r = await get('/t/apis/x/streambad?watch=true');
  const lines = r.text.trim().split('\n');
  assert.equal(lines.length, 1);
  assert.ok(!leaks(r.text));
});

test('tenant hook: unparsable JSON is refused with 502, never sent raw', async () => {
  const r = await get('/t/apis/x/broken');
  assert.equal(r.status, 502);
  assert.equal(r.body.kind, 'Status');
  assert.ok(!leaks(r.text));
});

test('tenant hook: an answer over the size limit is refused with 502', async () => {
  const r = await get('/t/apis/x/big');
  assert.equal(r.status, 502);
  assert.match(r.body.reference, /^ref-/);
});

test('without a hook the answer passes unchanged', async () => {
  const r = await get('/plain/apis/x/broken');
  assert.equal(r.status, 200);
});
