// Dashboard server error passthrough (C5) and the raw proxy (C6) against a fake API server.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';

const UI = process.env.UI_ROOT;
const { k8sRequest } = await import(`${UI}/packages/shared/server/upstream.js`);
const { sendUpstreamError } = await import(`${UI}/packages/shared/server/errors.js`);
const { createApiK8sProxy } = await import(`${UI}/packages/shared/server/apiK8sProxy.js`);
const require = createRequire(`${UI}/package.json`);
const express = require('express');

const kstatus = (code, reason, message, causes) => ({ kind: 'Status', apiVersion: 'v1', status: 'Failure', code, reason, message, ...(causes ? { details: { causes } } : {}) });

let api; let apiUrl; let app; let appUrl; let appServer;
before(async () => {
  api = http.createServer((req, res) => {
    const m = /\/status\/(\d{3})/.exec(req.url);
    const code = m ? Number(m[1]) : 200;
    if (req.url.includes('/warn')) res.setHeader('Warning', '299 - "deprecated field"');
    res.setHeader('Content-Type', 'application/json');
    res.statusCode = code;
    if (code === 200) {
      res.end(JSON.stringify({ kind: 'List', items: [{ metadata: { name: 'a' }, status: { vaultPath: 'secret/x', phase: 'Ready' } }] }));
    } else if (code === 422) {
      res.end(JSON.stringify(kstatus(422, 'Invalid', 'X "n" is invalid', [{ field: 'spec.a', message: 'bad', reason: 'FieldValueInvalid' }])));
    } else if (code === 500) {
      res.end(JSON.stringify(kstatus(500, 'InternalError', 'etcd says no')));
    } else {
      res.end(JSON.stringify(kstatus(code, code === 403 ? 'Forbidden' : 'Other', `upstream ${code}`)));
    }
  });
  await new Promise((r) => api.listen(0, '127.0.0.1', r));
  apiUrl = `http://127.0.0.1:${api.address().port}`;

  app = express();
  app.use(express.json());
  app.get('/curated/:code', async (req, res) => {
    try {
      const resp = await k8sRequest({ apiServer: apiUrl, path: `/apis/x/status/${req.params.code}`, token: 't' });
      if (resp.status >= 400) return sendUpstreamError(res, resp, { kind: 'CloudVirt', verb: 'list' });
      res.json(resp.body);
    } catch (err) {
      sendUpstreamError(res, err);
    }
  });
  app.get('/down', async (_req, res) => {
    try {
      await k8sRequest({ apiServer: 'http://127.0.0.1:1', path: '/apis/x', token: 't', timeoutMs: 2000 });
      res.json({});
    } catch (err) {
      sendUpstreamError(res, err);
    }
  });
  const strip = (_path, obj) => {
    for (const i of obj.items || []) delete i.status.vaultPath;
    return obj;
  };
  const refuseWatch = (path, obj) => {
    if (obj === null && path.includes('cloudosos')) throw Object.assign(new Error('watch refused'), { status: 400 });
    return obj;
  };
  app.use('/raw', createApiK8sProxy(apiUrl));
  app.use('/rawt', createApiK8sProxy(apiUrl, { transformBody: strip }));
  app.use('/raww', createApiK8sProxy(apiUrl, { transformBody: refuseWatch }));
  await new Promise((r) => { appServer = app.listen(0, '127.0.0.1', r); });
  appUrl = `http://127.0.0.1:${appServer.address().port}`;
});
after(() => { api.close(); appServer.close(); });

const get = async (p, headers = { 'x-forwarded-access-token': 'tok' }) => {
  const r = await fetch(`${appUrl}${p}`, { headers });
  return { status: r.status, body: await r.json().catch(() => null), warning: r.headers.get('warning') };
};

for (const code of [400, 401, 403, 404, 409, 422]) {
  test(`curated route passes ${code} through with the Status body`, async () => {
    const r = await get(`/curated/${code}`);
    assert.equal(r.status, code);
    assert.equal(r.body.kind, 'Status');
    assert.equal(r.body.code, code);
    if (code === 422) assert.equal(r.body.details.causes[0].field, 'spec.a');
    if (code === 403) assert.equal(r.body.reason, 'Forbidden');
  });
}

test('upstream 5xx becomes 502 with a reference, no upstream detail', async () => {
  const r = await get('/curated/500');
  assert.equal(r.status, 502);
  assert.match(r.body.reference, /^ref-[0-9a-f]{8}$/);
  assert.doesNotMatch(JSON.stringify(r.body), /etcd/);
});

test('transport failure becomes 502 UpstreamUnavailable', async () => {
  const r = await get('/down');
  assert.equal(r.status, 502);
  assert.equal(r.body.reason, 'UpstreamUnavailable');
});

test('raw proxy needs a token', async () => {
  const r = await get('/raw/apis/x', {});
  assert.equal(r.status, 401);
});

test('raw proxy refuses non-API paths', async () => {
  const r = await get('/raw/etc/passwd');
  assert.equal(r.status, 400);
});

test('raw proxy passes 403 and forwards Warning headers', async () => {
  const r = await get('/raw/apis/x/status/403/warn');
  assert.equal(r.status, 403);
  assert.equal(r.body.reason, 'Forbidden');
  assert.match(r.warning || '', /deprecated field/);
});

test('raw proxy accepts a bearer token (console proxy delegation)', async () => {
  const r = await get('/raw/apis/x', { authorization: 'Bearer abc' });
  assert.equal(r.status, 200);
});

test('transformBody rewrites JSON answers', async () => {
  const plain = await get('/raw/apis/x');
  assert.equal(plain.body.items[0].status.vaultPath, 'secret/x');
  const t = await get('/rawt/apis/x');
  assert.equal(t.status, 200);
  assert.equal(t.body.items[0].status.vaultPath, undefined);
  assert.equal(t.body.items[0].status.phase, 'Ready');
});

test('transformBody can refuse a watch before it is forwarded', async () => {
  const r = await get('/raww/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/cloudosos?watch=true');
  assert.equal(r.status, 400);
  assert.equal(r.body.message, 'watch refused');
});
