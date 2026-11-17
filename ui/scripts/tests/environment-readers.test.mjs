// An environment's access groups read that environment (gate part 2, G-5): the Role names one CloudOSO, so a member
// may `get` it but neither list the namespace's environments nor read the tenant's profile. The dashboard then
// (1) recognises the tenant from a SelfSubjectRulesReview that shows named environments, and (2) lists the
// environments the person may read by name, one by one. Names are examples.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const UI = process.env.UI_ROOT;
const id = await import(`${process.env.SHELL_BUNDLE_DIR}/resolveIdentity.mjs`);
const NS = 'entity-acme';
const P = '/apis/hybridsovereign.redhat/v1alpha1/namespaces/entity-acme/cloudosos';
const NAMED = { resourceRules: [
  { verbs: ['create'], apiGroups: ['authorization.k8s.io'], resources: ['selfsubjectrulesreviews'] },
  { verbs: ['get', 'list', 'watch'], apiGroups: ['hybridsovereign.redhat'], resources: ['cloudosos'], resourceNames: ['web'] },
] };

test('a rules review with named environments is a reader; one without names, or of another kind, is not', () => {
  assert.equal(id.readsNamedEnvironments({ status: NAMED }), true);
  assert.equal(id.readsNamedEnvironments({ status: { resourceRules: [
    { verbs: ['get'], apiGroups: ['hybridsovereign.redhat'], resources: ['cloudosos'] }] } }), false);
  assert.equal(id.readsNamedEnvironments({ status: { resourceRules: [
    { verbs: ['get'], apiGroups: ['hybridsovereign.redhat'], resources: ['rbacs'], resourceNames: ['x'] }] } }), false);
  assert.equal(id.readsNamedEnvironments({ status: { resourceRules: [
    { verbs: ['delete'], apiGroups: ['hybridsovereign.redhat'], resources: ['cloudosos'], resourceNames: ['web'] }] } }), false);
  assert.equal(id.readsNamedEnvironments(null), false);
});

test('the tenant of a person who may read one of its environments by name is recognised, its profile unread', async () => {
  const seen = [];
  const answers = (url, body) => {
    if (url.endsWith('/selfsubjectreviews')) return { status: { userInfo: { username: 'dev1', groups: ['acme.developers'] } } };
    if (url.endsWith('/users/~')) return { metadata: { name: 'dev1' } };
    if (url.endsWith('/selfsubjectaccessreviews')) return { status: { allowed: false } };
    if (url.endsWith('/selfsubjectrulesreviews')) {
      assert.equal(body.spec.namespace, NS);
      return { status: NAMED };
    }
    throw new Error(`unexpected ${url}`);
  };
  const saved = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : undefined;
    seen.push(String(url));
    return new Response(JSON.stringify(answers(String(url), body)), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    const me = await id.resolveIdentity();
    assert.deepEqual(me.tenants.map((t) => [t.name, t.namespace, t.via]), [['acme', NS, 'group']]);
    assert.equal(me.tenants[0].displayName, undefined);       // the profile is not read
    assert.ok(!seen.some((u) => u.includes('/tenantprofiles/')));
  } finally {
    globalThis.fetch = saved;
  }
});

test('nobody else is recognised: no named environment, no tenant', async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async (url) => {
    const u = String(url);
    const body = u.endsWith('/selfsubjectreviews') ? { status: { userInfo: { username: 'x', groups: ['acme.other'] } } }
      : u.endsWith('/users/~') ? { metadata: { name: 'x' } }
        : u.endsWith('/selfsubjectaccessreviews') ? { status: { allowed: false } }
          : { status: { resourceRules: [] } };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  };
  try {
    assert.deepEqual((await id.resolveIdentity()).tenants, []);
  } finally {
    globalThis.fetch = saved;
  }
});

function fakeApi({ listStatus = 403, rulesStatus = 201, names = ['web'], getStatus = {} } = {}) {
  const reqs = [];
  const server = http.createServer((req, res) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => {
      const url = req.url.split('?')[0];
      reqs.push({ method: req.method, url, auth: req.headers.authorization });
      const send = (code, body) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(body)); };
      if (req.method === 'GET' && url === P) {
        return send(listStatus, listStatus === 200 ? { items: [] } : { kind: 'Status', code: listStatus, message: 'list answer' });
      }
      if (req.method === 'POST' && url.endsWith('/selfsubjectrulesreviews')) {
        return send(rulesStatus, { status: { resourceRules: [{ verbs: ['get', 'list', 'watch'],
          apiGroups: ['hybridsovereign.redhat'], resources: ['cloudosos'], resourceNames: names }] } });
      }
      const m = url.startsWith(`${P}/`) ? decodeURIComponent(url.slice(P.length + 1)) : null;
      if (req.method === 'GET' && m) {
        const code = getStatus[m] ?? 200;
        if (code !== 200) return send(code, { kind: 'Status', code });
        return send(200, { kind: 'CloudOSO', metadata: { name: m, namespace: NS },
          spec: { vaultPath: 'kv/hidden', region: 'r1' }, status: { phase: 'Ready', endpoints: { console: 'https://dash.example.org' } } });
      }
      return send(404, { kind: 'Status', code: 404 });
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, reqs })));
}

async function listWith(opts) {
  const { server, reqs } = await fakeApi(opts);
  try {
    const { createK8sHandlers } = await import(pathToFileURL(path.join(UI, 'packages/tenant-dashboard/server/k8s-proxy.js')).href);
    const k8s = createK8sHandlers(`http://127.0.0.1:${server.address().port}`);
    return { out: await call(k8s.listCloudOSOs, { namespace: NS }), reqs };
  } finally {
    server.close();
  }
}

test('only a refused list falls back: a 5xx is an error, and no rules review is asked', async () => {
  const { out, reqs } = await listWith({ listStatus: 500 });
  assert.ok(out.code >= 500, `got ${out.code}`);
  assert.ok(!reqs.some((r) => r.url.endsWith('/selfsubjectrulesreviews')));
});

test('a named read or the rules review that fails is an error, never a shorter list', async () => {
  let r = await listWith({ names: ['db', 'web'], getStatus: { db: 503 } });
  assert.ok(r.out.code >= 500, `got ${r.out.code}`);
  r = await listWith({ rulesStatus: 500 });
  assert.ok(r.out.code >= 500, `got ${r.out.code}`);
  // gone or no longer readable: left out, the others listed
  r = await listWith({ names: ['db', 'web'], getStatus: { db: 404 } });
  assert.equal(r.out.code, 200);
  assert.deepEqual(r.out.body.map((o) => o.metadata.name), ['web']);
});

test('only valid object names from the rules are used in a path', async () => {
  const { out, reqs } = await listWith({ names: ['web', '../../secrets', 'Upper', 'a/b', '-x', ''] });
  assert.equal(out.code, 200);
  assert.deepEqual(out.body.map((o) => o.metadata.name), ['web']);
  assert.deepEqual(reqs.filter((r) => r.method === 'GET' && r.url !== P).map((r) => r.url), [`${P}/web`]);
});

function call(handler, query) {
  return new Promise((resolve) => {
    const res = { code: 200, status(c) { this.code = c; return this; }, json(b) { resolve({ code: this.code, body: b }); return this; } };
    handler({ headers: { 'x-forwarded-access-token': 'tok' }, query, params: {} }, res);
  });
}

test('the environments list of a person who may read some by name: those, tenant-safe, with their own token', async () => {
  const { server, reqs } = await fakeApi();
  try {
    const { createK8sHandlers } = await import(pathToFileURL(path.join(UI, 'packages/tenant-dashboard/server/k8s-proxy.js')).href);
    const k8s = createK8sHandlers(`http://127.0.0.1:${server.address().port}`);
    const out = await call(k8s.listCloudOSOs, { namespace: NS });
    assert.equal(out.code, 200);
    assert.deepEqual(out.body.map((o) => o.metadata.name), ['web']);
    assert.equal(out.body[0].spec?.vaultPath, undefined);      // tenant-safe as the list
    assert.equal(out.body[0].status.endpoints.console, 'https://dash.example.org');
    assert.deepEqual(reqs.map((r) => `${r.method} ${r.url}`), [`GET ${P}`,
      'POST /apis/authorization.k8s.io/v1/selfsubjectrulesreviews', `GET ${P}/web`]);
    assert.ok(reqs.every((r) => r.auth === 'Bearer tok'));
  } finally {
    server.close();
  }
});
