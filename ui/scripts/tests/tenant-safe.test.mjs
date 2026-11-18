// Tenant status allow-list for CloudOSO and PlatformOpenshift (shared/server/tenantSafe.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';

const UI = process.env.UI_ROOT;
const m = await import(`${UI}/packages/shared/server/tenantSafe.js`);

const SECRET_RE = /VaultPath|VaultProperty|vaultPaths|Secret$/;

function keyPaths(obj, prefix = '') {
  if (Array.isArray(obj)) return obj.flatMap((v, i) => keyPaths(v, `${prefix}[${i}]`));
  if (obj === null || typeof obj !== 'object') return [];
  return Object.entries(obj).flatMap(([k, v]) => [`${prefix}${k}`, ...keyPaths(v, `${prefix}${k}.`)]);
}
const secretKeys = (obj) => keyPaths(obj).filter((p) => SECRET_RE.test(p.split('.').pop().replace(/\[\d+\]$/, '')));

// shaped like an environment written before the jobs stopped recording Vault paths (values are placeholders)
const cloudoso = () => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'CloudOSO',
  metadata: { name: 'env1', namespace: 'entity-t1' },
  spec: { cloudRef: { name: 'site-a' }, vaultPath: 'p/acct', route53VaultPath: 'p/r53' },
  status: {
    status: 'ready', ready: true, phase: 'Ready', summary: 'Ready.', message: 'ok',
    conditions: [{ type: 'Ready', status: 'True', reason: 'EDASucceeded' }],
    cloudInfrastructure: 'site-a', projectId: 'p1', projectName: 't1_env1', domainId: 'd1', domainName: 'dom',
    appCredentialId: 'ac1', apiFloatingIPId: 'f1', ingressFloatingIPId: 'f2', apiVIP: '192.0.2.10',
    accountVaultPath: 'p/acct', route53VaultPath: '', vaultPaths: ['oso/projects/env1/clouds-config'],
    dns: { mode: 'native', zone: 'env1.t1.example.', zoneId: 'z1', dnsConfig: 'global', dnsConfigSecret: 'p/dns',
      delegated: true, nameservers: ['ns1.example.'] },
    delivery: { attempts: 1 }, edaJobs: [{ id: 1 }], futureField: 'x',
    site: { name: 'site-a', console: 'https://console.example', credentialsRef: { vaultPath: 'p/x' } },
  },
});

const platform = () => ({
  apiVersion: 'hybridsovereign.redhat/v1alpha1',
  kind: 'PlatformOpenshift',
  metadata: { name: 'c1', namespace: 'entity-t1' },
  spec: { type: 'openstack', openstack: { environment: 'env1', pullSecretVaultPath: 'p/ps', pullSecretVaultProperty: 'pp', sshKeyVaultPath: 'p/ssh', workerCount: 3 } },
  status: {
    ready: true, apiURL: 'https://api.c1.example:6443', consoleURL: 'https://console.c1.example',
    adminPasswordVaultPath: 'p/a', kubeconfigVaultPath: 'p/k', secretsVaultPath: 'p/s', vaultPaths: ['p/1'],
    dns: { provider: 'designate', accountVaultPath: 'p/dns', projectId: 'dp', records: [{ name: 'api', zoneId: 'z' }] },
    realization: { notes: { innerSecret: 'x', kept: 'y' } },
  },
});

test('CloudOSO: no Vault paths or secret references, allowed keys untouched', () => {
  const out = m.tenantSafe('CloudOSO', cloudoso());
  assert.deepEqual(secretKeys(out), []);
  assert.equal(out.status.projectId, 'p1');
  assert.equal(out.status.dns.zoneId, 'z1');
  assert.equal(out.status.dns.dnsConfig, 'global');
  assert.deepEqual(out.status.conditions, cloudoso().status.conditions);
  assert.equal(out.status.phase, 'Ready');
  assert.deepEqual(out.status.site, { name: 'site-a', console: 'https://console.example' });
  for (const k of ['delivery', 'edaJobs', 'futureField']) assert.equal(k in out.status, false, k);
  assert.deepEqual(out.spec, { cloudRef: { name: 'site-a' } });
  assert.deepEqual(out.metadata, cloudoso().metadata);
});

test('PlatformOpenshift: status and spec Vault fields removed, nested secret-looking keys too', () => {
  const out = m.tenantSafe('PlatformOpenshift', platform());
  assert.deepEqual(secretKeys(out), []);
  assert.equal(out.status.apiURL, 'https://api.c1.example:6443');
  assert.deepEqual(out.status.dns, { provider: 'designate', projectId: 'dp', records: [{ name: 'api', zoneId: 'z' }] });
  assert.deepEqual(out.status.realization, { notes: { kept: 'y' } });
  assert.deepEqual(out.spec.openstack, { environment: 'env1', workerCount: 3 });
});

test('lists are filtered item by item; other kinds pass unchanged', () => {
  const list = { kind: 'CloudOSOList', items: [cloudoso(), cloudoso()] };
  const out = m.tenantSafe('CloudOSO', list);
  assert.equal(out.items.length, 2);
  assert.deepEqual(secretKeys(out), []);
  const other = { kind: 'CloudVirt', status: { someVaultPath: 'x' } };
  assert.equal(m.tenantSafe('CloudVirt', other), other);
});

test('transformBody: filtered paths, watch refused, other paths untouched', () => {
  const base = '/apis/hybridsovereign.redhat/v1alpha1';
  const item = m.transformBody(`${base}/namespaces/entity-t1/cloudosos/env1`, cloudoso(), {});
  assert.deepEqual(secretKeys(item), []);
  const st = m.transformBody(`${base}/namespaces/entity-t1/platformopenshifts/c1/status`, platform(), {});
  assert.deepEqual(secretKeys(st), []);
  const all = m.transformBody(`${base}/cloudosos?limit=50`, { kind: 'CloudOSOList', items: [cloudoso()] }, {});
  assert.deepEqual(secretKeys(all), []);
  assert.throws(() => m.transformBody(`${base}/namespaces/entity-t1/cloudosos?watch=true`, null, {}), (e) => e.status === 400);
  assert.throws(() => m.transformBody(`${base}/watch/namespaces/entity-t1/cloudosos`, null, {}), (e) => e.status === 400);
  assert.equal(m.transformBody(`${base}/namespaces/entity-t1/cloudvirts?watch=true`, null, {}), null);
  const other = { kind: 'CloudVirt', status: { x: 1 } };
  assert.equal(m.transformBody(`${base}/namespaces/entity-t1/cloudvirts/v1`, other, {}), other);
  const statusBody = { kind: 'Status', code: 404 };
  assert.equal(m.transformBody(`${base}/namespaces/entity-t1/cloudosos/nope`, statusBody, {}), statusBody);
});

test('transformBody: filters by object kind on paths it does not recognise', () => {
  const base = '/apis/hybridsovereign.redhat/v1alpha1';
  // encoded, doubled or trailing-slash spellings normalise to the filtered path
  for (const p of [`${base}/namespaces/entity-t1/cloud%6Fsos/env1`, `${base}//namespaces/entity-t1/cloudosos/`, `${base}/namespaces/entity-t1/cloudosos/env1/`]) {
    assert.equal(m.filteredKindForPath(p), 'CloudOSO', p);
  }
  assert.equal(m.filteredKindForPath(`${base}/namespaces/x/cloudosos%2Fenv1`), null);
  // an unknown path still filters the object by its own kind
  const one = m.transformBody('/apis/other/v1/things', cloudoso(), {});
  assert.deepEqual(secretKeys(one), []);
  const list = m.transformBody('/apis/other/v1/things', { kind: 'PlatformOpenshiftList', items: [platform()] }, {});
  assert.deepEqual(secretKeys(list), []);
  // a generic List with mixed items: each item by its own kind
  const virt = { kind: 'CloudVirt', status: { someVaultPath: 'x' } };
  const mixed = m.transformBody('/apis/other/v1/things', { kind: 'List', items: [cloudoso(), virt, platform()] }, {});
  assert.deepEqual(secretKeys(mixed.items[0]), []);
  assert.deepEqual(secretKeys(mixed.items[2]), []);
  assert.equal(mixed.items[1], virt);
  // items without a kind on a filtered path use the path's kind
  const bare = cloudoso();
  delete bare.kind;
  const byPath = m.transformBody(`${base}/cloudosos`, { kind: 'CloudOSOList', items: [bare] }, {});
  assert.deepEqual(secretKeys(byPath), []);
});

test('transformBody: watch events are filtered by the object kind', () => {
  for (const type of ['ADDED', 'MODIFIED', 'DELETED']) {
    const ev = m.transformBody('/apis/other/v1/things?watch=1', { type, object: cloudoso() }, {});
    assert.equal(ev.type, type);
    assert.deepEqual(secretKeys(ev), []);
    assert.equal(ev.object.status.projectId, 'p1');
  }
  const err = { type: 'ERROR', object: { kind: 'Status', code: 410 } };
  assert.deepEqual(m.transformBody('/apis/other/v1/things?watch=1', err, {}), err);
  const virt = { type: 'ADDED', object: { kind: 'CloudVirt', status: { someVaultPath: 'x' } } };
  assert.deepEqual(m.transformBody('/apis/other/v1/things?watch=1', virt, {}), virt);
});

// p-osoui: what the environment card, the placement page and the cluster page read, and what tenants no longer see
test('CloudOSO: status.access and the new endpoints keys pass; appCredentialId does not', () => {
  const obj = cloudoso();
  obj.status.access = {
    signInClient: 'openstack-site-a',
    groupDomain: 'tenants',
    groups: [
      { rbac: 'developers', group: 't1.developers', access: 'Edit', role: 'member', state: 'Granted' },
      { rbac: 'auditors', group: 't1.auditors', access: 'View', role: 'reader', state: 'Waiting', message: 'The access group has no group in the sign-in directory yet.' },
    ],
    futureSecret: 'x',
  };
  obj.status.endpoints = {
    console: 'https://identity.example/v3/auth/OS-FEDERATION/identity_providers/p/protocols/openid/websso?origin=https://dashboard.example/auth/websso/',
    signIn: { method: 'TenantSSO', idpName: 'Example Platform', start: 'IdentityProvider' },
    signInGroups: ['t1.developers'],
    api: 'https://identity.example', project: 't1_env1', projectId: 'p1', domain: 'dom', domainId: 'd1',
    region: 'RegionOne', location: 'Frankfurt',
    apiCredentials: { state: 'Offered', validMinutesAfterSignIn: 1440, source: 'Site' },
  };
  const out = m.tenantSafe('CloudOSO', obj);
  assert.equal('appCredentialId' in out.status, false);
  assert.deepEqual(out.status.access, { signInClient: 'openstack-site-a', groupDomain: 'tenants', groups: obj.status.access.groups });
  assert.deepEqual(out.status.endpoints, obj.status.endpoints);
  assert.deepEqual(secretKeys(out), []);
  // a malformed access (not an object) carries nothing
  const odd = cloudoso();
  odd.status.access = ['x'];
  assert.equal('access' in m.tenantSafe('CloudOSO', odd).status, false);
});

test('CloudOSO: a key added later to a group entry or to the endpoints does not reach tenants (R25)', async () => {
  const obj = cloudoso();
  obj.status.access = {
    signInClient: 'openstack-site-a', groupDomain: 'tenants', removing: ['t1.old'],
    groups: [{ rbac: 'developers', group: 't1.developers', access: 'Edit', role: 'member', state: 'Granted', groupId: 'g-1', note: 'x' }],
  };
  obj.status.endpoints = { console: 'https://d.example', internalUrl: 'https://keystone.internal', signIn: { method: 'TenantSSO', mappingId: 'm1' },
    apiCredentials: { state: 'Offered', validMinutesAfterSignIn: 60, source: 'Site', policy: 'x' } };
  const out = m.tenantSafe('CloudOSO', obj);
  assert.deepEqual(out.status.access, { signInClient: 'openstack-site-a', groupDomain: 'tenants', removing: ['t1.old'],
    groups: [{ rbac: 'developers', group: 't1.developers', access: 'Edit', role: 'member', state: 'Granted' }] });
  assert.deepEqual(out.status.endpoints, { console: 'https://d.example', signIn: { method: 'TenantSSO' },
    apiCredentials: { state: 'Offered', validMinutesAfterSignIn: 60, source: 'Site' } });
  // what the jobs write passes whole (tests/fixtures/osoui-status.json, made by the role tests)
  const fix = JSON.parse((await import('node:fs')).readFileSync(`${UI}/../tests/fixtures/osoui-status.json`, 'utf8'));
  for (const [name, st] of Object.entries(fix)) {
    const [kind] = name.split('/');
    const o = kind === 'CloudOSO' ? cloudoso() : platform();
    Object.assign(o.status, st);
    const safe = m.tenantSafe(kind, o).status;
    for (const k of Object.keys(st)) assert.deepEqual(safe[k], st[k], `${name} ${k}`);
  }
});

test('PlatformOpenshift: status.credential keeps ids and names only', () => {
  const obj = platform();
  obj.status.credential = { id: 'ac9d', user: 'shc-c1', userId: 'u1', domain: 'tenants', domainId: 'd1', createdAt: '2026-11-14T10:00:00Z', secret: 's', password: 'p', vaultPath: 'p/c' };
  const out = m.tenantSafe('PlatformOpenshift', obj);
  assert.deepEqual(out.status.credential, { id: 'ac9d', user: 'shc-c1', userId: 'u1', domain: 'tenants', domainId: 'd1', createdAt: '2026-11-14T10:00:00Z' });
  assert.deepEqual(secretKeys(out), []);
});

test('NetworkPlacement is not filtered: status.siteNetwork reaches the placement page', () => {
  const np = { kind: 'NetworkPlacement', status: { siteNetwork: { name: 'hn10042', id: 'n1', project: 'hybrid-fabric', subnets: [{ family: 4, prefix: '10.60.1.0/24', gateway: '10.60.1.1', id: 's1' }] } } };
  assert.equal(m.tenantSafe('NetworkPlacement', np), np);
  assert.equal(m.transformBody('/apis/hybridsovereign.redhat/v1alpha1/namespaces/entity-t1/networkplacements/p1', np, {}), np);
});
