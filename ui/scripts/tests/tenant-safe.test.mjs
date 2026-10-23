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
