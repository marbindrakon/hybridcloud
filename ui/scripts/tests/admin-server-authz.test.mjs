// Admin dashboard server: every /api route is platform-only on the caller's token,
// and the former service-account endpoints are gone (UX-097, I-14).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import net from 'node:net';

const UI = process.env.UI_ROOT;
let api; let apiUrl; let proc; let base;
const seen = [];

const freePort = () => new Promise((r) => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });

before(async () => {
  api = http.createServer((req, res) => {
    const auth = req.headers.authorization || '';
    seen.push(auth);
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      res.setHeader('Content-Type', 'application/json');
      if (req.url.startsWith('/apis/authorization.k8s.io/v1/selfsubjectaccessreviews')) {
        const ra = JSON.parse(body).spec.resourceAttributes;
        const ok = auth === 'Bearer admin' && ra.resource === 'hybridfabrics' && ra.verb === 'list' && ra.namespace === 'sovereign-cloud' && ra.group === 'hybridsovereign.redhat';
        res.end(JSON.stringify({ status: { allowed: ok } }));
        return;
      }
      if (auth === 'Bearer sa-token') { res.statusCode = 500; res.end('{}'); return; }
      res.end(JSON.stringify({ kind: 'List', items: [] }));
    });
  });
  await new Promise((r) => api.listen(0, '127.0.0.1', r));
  apiUrl = `http://127.0.0.1:${api.address().port}`;
  const port = await freePort();
  base = `http://127.0.0.1:${port}`;
  proc = spawn(process.execPath, [`${UI}/packages/admin-dashboard/server/index.js`], {
    env: { ...process.env, PORT: String(port), OCP_SERVICES_SERVER: apiUrl },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('admin server did not start')), 8000);
    proc.stdout.on('data', (d) => { if (String(d).includes('listening')) { clearTimeout(t); resolve(); } });
    proc.on('exit', (c) => reject(new Error(`admin server exited ${c}`)));
  });
});
after(() => { proc?.kill(); api.close(); });

const ROUTES = [
  ['GET', '/api/user'],
  ['GET', '/api/k8s/apis/hybridsovereign.redhat/v1alpha1/namespaces/sovereign-cloud/entities'],
  ['POST', '/api/uihealth/probe'],
  ['GET', '/api/overview/crs'],
  ['GET', '/api/routes'],
  ['GET', '/api/operators'],
  ['GET', '/api/rbacconfigs'],
  ['GET', '/api/entities'],
  ['POST', '/api/entities'],
  ['GET', '/api/entities/acme/rbacs'],
  ['POST', '/api/k8s/patch-annotation'],
  ['GET', '/api/routes/healthcheck?url=http://127.0.0.1:1/'],
  ['POST', '/api/routes/healthcheck/batch'],
];
const call = (method, p, token) => fetch(`${base}${p}`, {
  method,
  headers: { ...(token ? { 'x-forwarded-access-token': token } : {}), 'content-type': 'application/json' },
  ...(method === 'POST' ? { body: JSON.stringify({}) } : {}),
});

test('no token: 401 on every route', async () => {
  for (const [m, p] of ROUTES) assert.equal((await call(m, p)).status, 401, `${m} ${p}`);
});

test('authenticated non-admin: 403 on every route', async () => {
  for (const [m, p] of ROUTES) assert.equal((await call(m, p, 'nobody')).status, 403, `${m} ${p}`);
});

test('platform admin: live routes answer, removed SA routes are 404', async () => {
  assert.equal((await call('GET', '/api/user', 'admin')).status, 200);
  assert.equal((await call('GET', ROUTES[1][1], 'admin')).status, 200);
  assert.equal((await call('POST', '/api/uihealth/probe', 'admin')).status, 400); // empty targets
  for (const [m, p] of ROUTES.slice(3)) {
    if (p.startsWith('/api/k8s/patch-annotation')) {
      // now just a raw-proxy path outside /api|/apis: refused
      assert.equal((await call(m, p, 'admin')).status, 400, `${m} ${p}`);
      continue;
    }
    assert.equal((await call(m, p, 'admin')).status, 404, `${m} ${p}`);
  }
});

test('the server never calls the API with anything but the caller token', async () => {
  assert.ok(seen.every((a) => a === 'Bearer admin' || a === 'Bearer nobody'), JSON.stringify([...new Set(seen)]));
});
