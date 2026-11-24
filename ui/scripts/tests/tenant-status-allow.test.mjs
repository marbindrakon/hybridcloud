// One allow-list of status fields for every kind a tenant reads (shared/server/tenantStatusAllow.js): the
// tenant dashboard's server applies it to its raw proxy and to its own /api/<plural> routes, and the tenant pages
// of the dashboards and console plugins apply it in the browser (redactForTenant: the overview and the JSON tab).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const allow = await import(`${UI}/packages/shared/server/tenantStatusAllow.js`);
const safe = await import(`${UI}/packages/shared/server/tenantSafe.js`);

const entry = `export { redactForTenant } from './components/KindStatusDetails';`;
const bundle = path.join(OUT, 'tenantRedact.mjs');
execFileSync(path.join(UI, 'node_modules/.bin/esbuild'), [
  '--bundle', '--format=esm', '--platform=node', '--log-level=error', '--jsx=automatic', '--loader=tsx',
  '--loader:.json=json', '--loader:.css=empty', '--loader:.svg=dataurl', '--define:process.env.NODE_ENV="production"',
  "--banner:js=import { createRequire as __shcRequire } from 'module'; const require = __shcRequire(import.meta.url);",
  '--sourcefile=entry.tsx', `--outfile=${bundle}`,
], { cwd: path.join(UI, 'packages/shared/src'), input: entry });
const page = await import(bundle);

const KINDS_BY_PLURAL = Object.fromEntries(Object.keys(allow.TENANT_STATUS_ALLOW).map((k) => [`${k.toLowerCase()}s`, k]));

test('every kind the tenant roles let people read has an allow-list', () => {
  const vars = fs.readFileSync(path.join(UI, '..', 'eda/rulebooks/roles/entity_provision/vars/main.yml'), 'utf8');
  const plurals = new Set([...vars.matchAll(/^\s+- ([a-z]+s)\s*$/gm)].map((x) => x[1]));
  plurals.add('tenantprofiles');
  assert.ok(plurals.size >= 17, `found ${plurals.size}`);
  for (const p of plurals) assert.ok(KINDS_BY_PLURAL[p], `${p} has a tenant status allow-list`);
});

const entries = (list) => list.map((e) => (Array.isArray(e) ? e[0] : e));
test('no list keeps the platform bookkeeping, the slug or platform ids', () => {
  for (const [kind, list] of Object.entries(allow.TENANT_STATUS_ALLOW)) {
    const keys = [...allow.COMMON_STATUS_KEYS, ...entries(list)];
    for (const k of ['edaJobs', 'delivery', 'slug', 'dnsProjectId', 'dnsZoneId', 'route53ZoneId', 'centralApiServer',
      'oidcClientId', 'serviceAccountIssuer', 'authMethodId', 'orgId', 'groupId', 'realm', 'vaultInternalUrl',
      'adminPolicy', 'readerPolicy', 'l3Vni', 'l2Vni', 'cloudGateway', 'config']) {
      assert.ok(!keys.includes(k), `${kind}: ${k}`);
    }
    if (kind !== 'NetworkPlacement') assert.ok(!keys.includes('realization'), `${kind}: realization`);
  }
});

const cluster = () => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'PlatformOpenshift',
  metadata: { name: 'web', namespace: 'entity-acme' },
  spec: { type: 'openstack', openstack: { environment: 'prod' } },
  status: {
    phase: 'Ready', summary: 'Ready.', apiURL: 'https://api.web.example:6443', buildName: 'web-1a2b3c',
    dns: { provider: 'designate', projectId: 'dp-1', records: [{ zoneId: 'z-1', zone: 'prod.acme.example.', name: 'api.web', type: 'A' }] },
    floatingIPs: { network: 'net-1', api: { id: 'f-1', address: '192.0.2.10' }, ingress: { id: 'f-2', address: '192.0.2.11' } },
    oidcClientId: 'client-1', serviceAccountIssuer: 'https://issuer.example/web',
    edaJobs: [{ name: 'entity-ready', jobId: '42', status: 'successful' }],
    delivery: { jobId: '42' },
  },
});

test('a tenant page shows the cluster without dns.projectId, ids of the site or platform addresses', () => {
  const shown = page.redactForTenant('PlatformOpenshift', cluster());
  assert.equal('projectId' in shown.status.dns, false);
  assert.deepEqual(shown.status.dns.records, [{ zone: 'prod.acme.example.', name: 'api.web', type: 'A' }]);
  assert.deepEqual(shown.status.floatingIPs, { api: { address: '192.0.2.10' }, ingress: { address: '192.0.2.11' } });
  for (const k of ['oidcClientId', 'serviceAccountIssuer', 'edaJobs', 'delivery']) assert.equal(k in shown.status, false, k);
  assert.equal(shown.status.apiURL, 'https://api.web.example:6443');
  // the page's JSON tab renders this same object: nothing else of the status reaches it
  assert.deepEqual(Object.keys(shown.status).sort(), ['apiURL', 'buildName', 'dns', 'floatingIPs', 'phase', 'summary']);
  // the tenant dashboard's server gives the same status
  assert.deepEqual(safe.tenantSafe('PlatformOpenshift', cluster()).status, shown.status);
});

test('kinds that passed whole before are filtered too (a network, a registry organization)', () => {
  const net = { kind: 'HybridNetwork', metadata: { name: 'core', namespace: 'entity-acme' },
    status: { phase: 'Ready', mtu: 1450, realization: { vpnTarget: 51003, routeTarget: '65010:51003' }, edaJobs: [{ jobId: '1' }] } };
  assert.deepEqual(page.redactForTenant('HybridNetwork', net).status, { phase: 'Ready', mtu: 1450 });
  const org = { kind: 'QuayOrg', metadata: { name: 'images', namespace: 'entity-acme' },
    status: { phase: 'Ready', orgName: 'acme_images', orgUrl: 'https://registry.example/organization/acme_images', oidcClientId: 'c', config: 'quay-main' } };
  assert.deepEqual(page.redactForTenant('QuayOrg', org).status, { phase: 'Ready', orgName: 'acme_images', orgUrl: 'https://registry.example/organization/acme_images' });
});

test('a kind without a list (a platform kind) keeps its status, without secret-looking keys', () => {
  const fab = { kind: 'HybridFabric', metadata: { name: 'f' }, status: { nodes: [{ name: 'n1' }], tokenSecret: 'x' } };
  assert.deepEqual(page.redactForTenant('HybridFabric', fab).status, { nodes: [{ name: 'n1' }] });
});

test('the dashboard routes /api/<plural>: objects and arrays through the allow-list; other routes untouched', () => {
  const mw = safe.tenantSafeResponses();
  const run = (p, body) => {
    let sent;
    const res = { json: (b) => { sent = b; return b; } };
    mw({ path: p }, res, () => {});
    res.json(body);
    return sent;
  };
  assert.equal('projectId' in run('/platformopenshifts/web', cluster()).status.dns, false);
  const list = run('/platformopenshifts', [cluster(), cluster()]);
  assert.equal(list.length, 2);
  assert.ok(list.every((c) => !('edaJobs' in c.status)));
  const other = { items: [{ status: { edaJobs: [1] } }] };
  assert.equal(run('/mtv-catalog', other), other);
  assert.equal(run('/entities', other), other);
});
