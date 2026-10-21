// Permission gate (C3): namespaces per kind, matrix lookups, role hints, error texts,
// and the /api/permissions server module against a fake API server.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';

const B = process.env.SHELL_BUNDLE_DIR;
const UI = process.env.UI_ROOT;
const gate = await import(`${B}/permissionGate.mjs`);
const hints = await import(`${B}/roleHints.mjs`);
const et = await import(`${B}/errorText.mjs`);
const errs = await import(`${B}/errors.mjs`);
const { createPermissionsHandler } = await import(`${UI}/packages/shared/server/permissions.js`);
const express = createRequire(`${UI}/package.json`)('express');

test('kinds are evaluated where they live', () => {
  assert.equal(gate.gateNamespaceFor('CloudVirt', 'entity-a'), 'entity-a');
  assert.equal(gate.gateNamespaceFor('HybridFabric', 'entity-a'), 'sovereign-cloud');
  assert.equal(gate.gateNamespaceFor('RbacConfig', 'entity-a'), 'sovereign-cloud-plugins');
  assert.equal(gate.gateNamespaceFor('CloudVirt', '-'), '-');
  assert.ok(gate.kindsForNamespace('entity-a').includes('TenantProfile'));
  assert.ok(!gate.kindsForNamespace('entity-a').includes('Persona'));
  assert.ok(gate.kindsForNamespace('sovereign-cloud').includes('Entity'));
  assert.ok(gate.kindsForNamespace('-').length <= 40);
});

test('matrixAllows', () => {
  const m = { namespace: 'entity-a', allowed: { cloudvirts: { list: true, create: false } } };
  assert.equal(gate.matrixAllows(m, 'CloudVirt', 'list'), true);
  assert.equal(gate.matrixAllows(m, 'CloudVirt', 'create'), false);
  assert.equal(gate.matrixAllows(m, 'CloudVirt', 'delete'), undefined);
  assert.equal(gate.matrixAllows(undefined, 'CloudVirt', 'list'), undefined);
});

test('role hints name the specific role first and never the installation-only role', () => {
  assert.deepEqual(hints.hintRoles('cloudvirts', 'create'), ['cloudVirtAdmin', 'entityAdmin']);
  assert.deepEqual(hints.hintRoles('cloudvirts', 'list'), ['cloudVirtView', 'auditor']);
  assert.deepEqual(hints.hintRoles('rbacs', 'create'), ['identityAdmin', 'entityAdmin']);
  assert.ok(!hints.hintRoles('hybridnetworks', 'list').includes('BobsTeam'));
  assert.deepEqual(hints.hintRoles('nosuchkind', 'list'), []);
});

const t = (k, o) => (o ? `${k}:${JSON.stringify(o)}` : k);
test('errorText follows the section 6 mapping', () => {
  const f = errs.parseStatusBody(403, 'Forbidden', '{"kind":"Status","reason":"Forbidden","message":"no"}');
  const x = et.errorText(f, t, { kindLabel: 'VM environments', tenantLabel: 'Acme', roleNames: ['A', 'B'], contacts: [{ name: 'Ops', email: 'ops@example.com' }], action: 'list' });
  assert.equal(x.cls, 'forbidden');
  assert.match(x.title, /errors.forbiddenList/);
  assert.match(x.body, /access.grantedBy/);
  assert.match(x.body, /ops@example.com/);
  assert.match(x.body, /scope.newGroupHint/);
  assert.equal(et.errorText(errs.parseStatusBody(401, '', ''), t).title, 'errors.sessionEnded');
  assert.equal(et.errorText(errs.parseStatusBody(404, '', ''), t).title, 'errors.notFound');
  assert.equal(et.errorText(errs.parseStatusBody(409, '', '{"reason":"Conflict","message":"m"}'), t).title, 'errors.conflict');
  const inv = et.errorText(errs.parseStatusBody(422, '', '{"reason":"Invalid","message":"bad field"}'), t);
  assert.equal(inv.body, 'bad field');
  assert.match(et.errorText(errs.parseStatusBody(502, '', '{"reference":"ref-1"}'), t).title, /errors.unavailable/);
  assert.match(et.errorText(errs.parseStatusBody(500, '', '{"reference":"ref-9"}'), t).body, /ref-9/);
  assert.equal(et.errorText(errs.networkError(new TypeError('x')), t).title, 'errors.unavailable');
});

let api; let app; let base; let reviews = 0;
before(async () => {
  api = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      reviews += 1;
      const ra = JSON.parse(body).spec.resourceAttributes;
      const ok = req.headers.authorization === 'Bearer dev' && ra.namespace === 'entity-a' && ra.resource === 'cloudvirts' && ['list', 'create'].includes(ra.verb);
      res.statusCode = 201;
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify({ status: { allowed: ok } }));
    });
  });
  await new Promise((r) => api.listen(0, '127.0.0.1', r));
  const e = express();
  e.get('/api/permissions', createPermissionsHandler({ apiServer: `http://127.0.0.1:${api.address().port}` }));
  await new Promise((r) => { app = e.listen(0, '127.0.0.1', r); });
  base = `http://127.0.0.1:${app.address().port}`;
});
after(() => { api.close(); app.close(); });

const get = (q, token = 'dev') => fetch(`${base}/api/permissions?${q}`, { headers: { 'x-forwarded-access-token': token } });

test('/api/permissions answers the matrix and caches it', async () => {
  const r = await get('namespace=entity-a&resources=cloudvirts,hybridnetworks&verbs=list,create,delete');
  assert.equal(r.status, 200);
  const b = await r.json();
  assert.deepEqual(b.allowed.cloudvirts, { list: true, create: true, delete: false });
  assert.deepEqual(b.allowed.hybridnetworks, { list: false, create: false, delete: false });
  const n = reviews;
  await get('namespace=entity-a&resources=hybridnetworks,cloudvirts&verbs=delete,create,list');
  assert.equal(reviews, n, 'second identical query is served from the cache');
  const other = await (await get('namespace=entity-a&resources=cloudvirts&verbs=list', 'nobody')).json();
  assert.equal(other.allowed.cloudvirts.list, false, 'cache is per token');
});

test('/api/permissions validates and bounds its input', async () => {
  assert.equal((await get('namespace=Bad_NS&resources=cloudvirts&verbs=list')).status, 400);
  assert.equal((await get('namespace=entity-a&resources=&verbs=list')).status, 400);
  assert.equal((await get('namespace=entity-a&resources=cloudvirts&verbs=impersonate')).status, 400);
  const many = Array.from({ length: 41 }, (_, i) => `r${i}`).join(',');
  assert.equal((await get(`namespace=entity-a&resources=${many}&verbs=list`)).status, 400);
  assert.equal((await get('namespace=entity-a&resources=cloudvirts&verbs=get,list,watch,create,update,patch,delete')).status, 400);
  assert.equal((await fetch(`${base}/api/permissions?namespace=-&resources=cloudvirts&verbs=list`)).status, 401);
  assert.equal((await get('namespace=-&resources=cloudvirts&verbs=list')).status, 200);
});
