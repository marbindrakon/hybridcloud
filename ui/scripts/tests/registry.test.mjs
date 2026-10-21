// Navigation registry (C1): uniqueness, label keys, removed kinds, paths, legacy redirects.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const B = process.env.SHELL_BUNDLE_DIR;
const UI = process.env.UI_ROOT;
const reg = await import(`${B}/registry.mjs`);
const paths = await import(`${B}/paths.mjs`);
const en = JSON.parse(fs.readFileSync(`${UI}/packages/shared/src/i18n/locales/en.json`, 'utf8'));
const fr = JSON.parse(fs.readFileSync(`${UI}/packages/shared/src/i18n/locales/fr.json`, 'utf8'));
const get = (o, k) => k.split('.').reduce((a, p) => (a == null ? a : a[p]), o);
const HOSTS = ['tenant-dashboard', 'admin-dashboard', 'tenant-console', 'admin-console'];
const PAGES = new Set(['tenant-home', 'list', 'detail', 'create', 'network-map', 'platform-overview', 'fabric-map', 'integrations', 'ui-health']);

test('ids are unique and kebab-case', () => {
  const ids = reg.NAV_REGISTRY.map((e) => e.id);
  assert.equal(new Set(ids).size, ids.length);
  for (const id of ids) assert.match(id, /^[a-z][a-z0-9-]*$/);
});

test('routes are unique per host and scope root', () => {
  for (const host of HOSTS) {
    const seen = new Map();
    for (const e of reg.entriesForHost(host)) {
      const root = e.persona.includes('platform') && !e.persona.includes('tenant') ? 'platform' : 'tenant';
      const key = `${root}:${e.route}`;
      assert.ok(!seen.has(key), `${host}: ${e.id} and ${seen.get(key)} share ${key}`);
      seen.set(key, e.id);
    }
  }
});

test('every label and section key exists in en and fr', () => {
  for (const e of reg.NAV_REGISTRY) {
    assert.equal(typeof get(en, e.labelKey), 'string', `en ${e.labelKey}`);
    assert.equal(typeof get(fr, e.labelKey), 'string', `fr ${e.labelKey}`);
  }
  for (const s of reg.NAV_SECTIONS) {
    assert.equal(typeof get(en, s.labelKey), 'string', `en ${s.labelKey}`);
    assert.equal(typeof get(fr, s.labelKey), 'string', `fr ${s.labelKey}`);
  }
});

test('no page for retired kinds; pages are known; kind entries can have a detail route', () => {
  for (const e of reg.NAV_REGISTRY) {
    assert.ok(PAGES.has(e.page), `${e.id} page ${e.page}`);
    for (const k of e.kinds ?? []) assert.ok(!['Persona', 'TransportLink', 'Iaac'].includes(k), `${e.id} lists ${k}`);
    if (e.kinds?.length) assert.ok(!e.route.includes(':'), `${e.id}: kind entries need a static route for ${e.route}/:name`);
  }
  // placements: no nav item (owner brief: the Attach dialog is the way in)
  assert.equal(reg.entryById('placements').nav, false);
  // every kind with a list entry is reachable by routeFor
  for (const k of ['CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP', 'HybridNetwork', 'NetworkPlacement', 'PlatformOpenshift', 'Team', 'Project', 'Assignment', 'Vault', 'VaultKV', 'AAPOrg', 'QuayOrg', 'Rbac', 'Entity', 'CloudInfrastructure', 'HybridFabric', 'CloudGateway', 'RbacConfig', 'AAPConfig', 'QuayConfig']) {
    assert.ok(reg.entryForKind(k), k);
  }
  assert.ok(reg.entryById('ui-health').kinds.includes('UIHealthChecker'));
});

test('platform entries need a platform admin and only render on admin hosts', () => {
  for (const e of reg.NAV_REGISTRY.filter((x) => x.persona.includes('platform'))) {
    assert.equal(e.enabledWhen, 'platformAdmin', e.id);
    assert.deepEqual(reg.hostsOf(e).sort(), ['admin-console', 'admin-dashboard']);
  }
});

test('paths per host and scope', () => {
  const t = { kind: 'tenant', name: 'acme-corp' };
  assert.equal(paths.pathFor('tenant-dashboard', 'networks', t), '/t/acme-corp/networks');
  assert.equal(paths.pathFor('tenant-dashboard', 'networks', t, { name: 'core' }), '/t/acme-corp/networks/core');
  assert.equal(paths.pathFor('tenant-dashboard', 'home', t), '/t/acme-corp');
  assert.equal(paths.pathFor('admin-dashboard', 'networks', { kind: 'all' }), '/all/networks');
  assert.equal(paths.pathFor('admin-dashboard', 'sites', { kind: 'global' }, { name: 'iaas1' }), '/platform/sites/iaas1');
  assert.equal(paths.pathFor('admin-dashboard', 'platform-overview', { kind: 'global' }), '/platform');
  assert.equal(paths.pathFor('tenant-console', 'env-vm', t), '/hybridsovereign/environments/vm/ns/entity-acme-corp');
  assert.equal(paths.pathFor('tenant-console', 'env-vm', { kind: 'all' }), '/hybridsovereign/environments/vm/all-namespaces');
  assert.equal(paths.pathFor('tenant-console', 'home', t), '/hybridsovereign/home/ns/entity-acme-corp');
  assert.equal(paths.pathFor('admin-console', 'tenants', { kind: 'global' }), '/hybridsovereign/platform/tenants');
  assert.equal(paths.routeFor('tenant-dashboard', 'CloudVirt', t, 'p1'), '/t/acme-corp/environments/vm/p1');
  assert.equal(paths.createPathFor('tenant-dashboard', 'cloudvirt', t), '/t/acme-corp/create/cloudvirt');
  assert.equal(paths.createPathFor('admin-dashboard', 'entity', { kind: 'global' }), '/platform/create/entity');
});

test('legacy paths are unique, map to existing entries, and resolve', () => {
  const seen = new Set();
  for (const l of paths.LEGACY_ROUTES) {
    const key = `${l.host}:${l.from}`;
    assert.ok(!seen.has(key), key);
    seen.add(key);
    if (l.to) assert.ok(reg.entryById(l.to), `${key} -> ${l.to}`);
  }
  const cur = { kind: 'tenant', name: 'acme-corp' };
  const go = (host, p) => {
    const m = paths.matchLegacy(host, p);
    return m && paths.legacyTarget(host, m, cur);
  };
  assert.equal(go('tenant-dashboard', 'cloudvirt'), '/t/acme-corp/environments/vm');
  assert.equal(go('tenant-dashboard', 'networks/core'), '/t/acme-corp/networks/core');
  assert.equal(go('tenant-dashboard', 'create/team'), '/t/acme-corp/create/team');
  assert.equal(go('tenant-dashboard', 'personas'), undefined);
  assert.equal(go('admin-dashboard', 'entities'), '/platform/tenants');
  assert.equal(go('admin-dashboard', 'networking/networks'), '/all/networks');
  assert.equal(go('admin-dashboard', 'clouds/cloudvirt/entity-acme-corp/p1'), '/t/acme-corp/environments/vm/p1');
  assert.equal(go('admin-dashboard', 'operators/rbacs/entity-acme-corp/viewers'), '/t/acme-corp/access/groups/viewers');
  assert.equal(go('admin-dashboard', 'networking/transport'), undefined);
  assert.equal(go('admin-dashboard', 'create/hybridfabric'), '/platform/create/hybridfabric');
  assert.equal(paths.matchLegacy('admin-dashboard', 'no/such/path'), undefined);
});
