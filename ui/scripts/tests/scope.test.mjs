// Scope from identity (C2): group parsing, scope computation, landing rules.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const g = await import(`${process.env.SHELL_BUNDLE_DIR}/parseGroups.mjs`);
const sp = await import(`${process.env.SHELL_BUNDLE_DIR}/scopeProvider.mjs`);

test('dotted tenant groups name their tenant', () => {
  assert.equal(g.tenantOfGroup('acme-corp.acme-admins'), 'acme-corp');
  assert.equal(g.tenantOfGroup('uxr-corp.uxr-devs'), 'uxr-corp');
  assert.equal(g.tenantOfGroup('a.b.c'), 'a');
});

test('non-tenant groups are ignored', () => {
  for (const x of ['system:authenticated', 'system:authenticated:oauth', 'sovereign-admin',
    'acme-corp-platform-engineering-admins', '.x', 'acme.', 'x--y.z', 'Acme.admins', `${'a'.repeat(57)}.g`]) {
    assert.equal(g.tenantOfGroup(x), null, x);
  }
  assert.equal(g.tenantOfGroup(`${'a'.repeat(56)}.g`), 'a'.repeat(56));
});

test('candidates keep first-seen order and collect groups', () => {
  const c = g.tenantCandidatesFromGroups(['system:authenticated', 'b.x', 'a.y', 'b.z', 'b.x']);
  assert.deepEqual(c, [{ entity: 'b', groups: ['b.x', 'b.z'] }, { entity: 'a', groups: ['a.y'] }]);
});

test('namespace helpers', () => {
  assert.equal(g.namespaceOfTenant('acme-corp'), 'entity-acme-corp');
  assert.equal(g.tenantOfNamespace('entity-acme-corp'), 'acme-corp');
  assert.equal(g.tenantOfNamespace('sovereign-cloud'), null);
});

const T = (name, via = 'group') => ({ name, namespace: `entity-${name}`, groups: [`${name}.x`], via });
const id = (tenants, reader = false, admin = false) => ({ username: 'u', groups: [], tenants, platform: { reader, admin } });

test('computeScope', () => {
  assert.deepEqual(sp.computeScope(undefined, true, { kind: 'unset' }), { kind: 'none', reason: 'loading' });
  assert.equal(sp.computeScope(id([]), false, { kind: 'unset' }).reason, 'no-role');
  assert.equal(sp.computeScope(id([T('a')]), false, { kind: 'tenant', name: 'a' }).kind, 'tenant');
  const denied = sp.computeScope(id([T('a')]), false, { kind: 'tenant', name: 'acme-corp' });
  assert.deepEqual(denied, { kind: 'none', reason: 'no-access', requested: 'acme-corp' });
  assert.equal(sp.computeScope(id([T('a')]), false, { kind: 'all' }).reason, 'no-access');
  assert.equal(sp.computeScope(id([], true), false, { kind: 'all' }).kind, 'all');
  assert.equal(sp.computeScope(id([], true, false), false, { kind: 'global' }).reason, 'no-access');
  assert.equal(sp.computeScope(id([], true, true), false, { kind: 'global' }).kind, 'global');
  assert.equal(sp.computeScope(id([T('a'), T('b')]), false, { kind: 'unset' }).reason, 'unselected');
});

test('landingScope: one tenant, platform, readers', () => {
  assert.deepEqual(sp.landingScope(id([T('a')])), { kind: 'tenant', name: 'a' });
  assert.equal(sp.landingScope(id([T('a'), T('b')])), null);
  assert.deepEqual(sp.landingScope(id([T('a', 'platform'), T('b', 'platform')], true)), { kind: 'all' });
  assert.deepEqual(sp.landingScope(id([], true, true), 'platform'), { kind: 'global' });
  assert.equal(sp.landingScope(id([])), null);
});

test('scoped paths', () => {
  assert.deepEqual(sp.parseScopedPath('/t/acme-corp/networks/core'), { requested: { kind: 'tenant', name: 'acme-corp' }, rest: 'networks/core' });
  assert.deepEqual(sp.parseScopedPath('/all/environments/vm'), { requested: { kind: 'all' }, rest: 'environments/vm' });
  assert.deepEqual(sp.parseScopedPath('/platform/sites'), { requested: { kind: 'global' }, rest: 'sites' });
  assert.equal(sp.parseScopedPath('/networks').requested.kind, 'unset');
  assert.equal(sp.parseScopedPath('/t/Bad_Name/x').requested.kind, 'unset');
  assert.equal(sp.pageOf('networks/core'), 'networks');
  assert.equal(sp.pageOf('environments/vm/p1'), 'environments/vm');
  assert.equal(sp.pageOf('create/cloudvirt'), '');
  assert.equal(sp.scopePrefix({ kind: 'tenant', name: 'a' }), '/t/a');
});
