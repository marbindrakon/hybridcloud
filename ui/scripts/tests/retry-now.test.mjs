// Retry now from the tenant dashboard: the server writes a fresh reconcileNow value on every request.
// The operator honours each distinct value once and keeps the annotation, so a repeated literal
// value (the old client's 'true') would be handled only the first time.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const UI = process.env.UI_ROOT;
const ANNOTATION = 'ansible.sdk.operatorframework.io/reconcileNow';

function fakeApiServer() {
  const patches = [];
  const server = http.createServer((req, res) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => {
      patches.push({ method: req.method, url: req.url, body: data ? JSON.parse(data) : null });
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ kind: 'CloudVirt', metadata: { name: 'vm' } }));
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, patches })));
}

function call(handler, body) {
  return new Promise((resolve) => {
    const res = {
      code: 200,
      status(c) { this.code = c; return this; },
      json(b) { resolve({ code: this.code, body: b }); return this; },
    };
    handler({ headers: { 'x-forwarded-access-token': 'tok' }, body, params: {}, query: {} }, res);
  });
}

test('every Retry now writes a new timestamp, whatever value the client sends', async () => {
  const { server, patches } = await fakeApiServer();
  try {
    const { createK8sHandlers } = await import(
      pathToFileURL(path.join(UI, 'packages/tenant-dashboard/server/k8s-proxy.js')).href);
    const k8s = createK8sHandlers(`http://127.0.0.1:${server.address().port}`);
    const req = { kind: 'CloudVirt', name: 'vm', namespace: 'entity-uxr-corp', annotation: ANNOTATION };
    const first = await call(k8s.patchAnnotation, { ...req, value: 'true' });
    await new Promise((r) => setTimeout(r, 5));
    const second = await call(k8s.patchAnnotation, { ...req, value: 'true' });
    await new Promise((r) => setTimeout(r, 5));
    const third = await call(k8s.patchAnnotation, req);
    assert.deepEqual([first.code, second.code, third.code], [200, 200, 200]);
    const values = patches.map((p) => p.body.metadata.annotations[ANNOTATION]);
    assert.equal(values.length, 3);
    for (const v of values) assert.ok(!Number.isNaN(Date.parse(v)) && v !== 'true', `not a timestamp: ${v}`);
    assert.equal(new Set(values).size, 3, 'repeated requests wrote the same value');
    assert.ok(patches.every((p) => p.method === 'PATCH'
      && p.url === '/apis/hybridsovereign.redhat/v1alpha1/namespaces/entity-uxr-corp/cloudvirts/vm'));
  } finally {
    server.close();
  }
});

test('only the reconcileNow annotation is accepted', async () => {
  const { createK8sHandlers } = await import(
    pathToFileURL(path.join(UI, 'packages/tenant-dashboard/server/k8s-proxy.js')).href);
  const k8s = createK8sHandlers('http://127.0.0.1:9');
  const out = await call(k8s.patchAnnotation,
    { kind: 'CloudVirt', name: 'vm', namespace: 'entity-uxr-corp', annotation: 'other/annotation', value: 'x' });
  assert.equal(out.code, 400);
});
