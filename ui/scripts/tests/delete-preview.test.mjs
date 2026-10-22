// Delete dialog (GT-F8, OD41): the dependants a delete takes with it, and when the name must be typed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'deletePreview.mjs'));
const obj = (kind, name, spec = {}, status = {}) => ({ kind, metadata: { name, namespace: 'entity-x' }, spec, status });
const np = (name, network, kind, env) => obj('NetworkPlacement', name, { network, backend: { kind, name: env } });
const placements = [np('n1-vm', 'n1', 'CloudVirt', 'vm'), np('n1-os', 'n1', 'CloudOSO', 'os'), np('n2-vm', 'n2', 'CloudVirt', 'vm')];

test('a network lists its placements and asks for its name', () => {
  const p = m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n1'), { placements });
  assert.deepEqual(p.sections.map((s) => s.key), ['placements']);
  assert.deepEqual(p.sections[0].entries.map((e) => e.name), ['n1-vm', 'n1-os']);
  assert.equal(p.realized, true);
  const lone = m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n3'), { placements });
  assert.equal(lone.realized, false);
  assert.equal(m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n3', {}, { realization: { l3vni: 5 } }), { placements }).realized, true);
});

test('a VM environment lists placements, site namespaces and DNS zone', () => {
  const vm = obj('CloudVirt', 'vm', {}, { namespaces: [{ name: 'web', namespace: 'x-vm-web' }], dns: { zone: 'vm.x.example' } });
  const p = m.deletePreview('CloudVirt', vm, { placements });
  assert.deepEqual(p.sections.map((s) => s.key), ['placements', 'namespaces', 'dnsZone']);
  assert.deepEqual(p.sections[0].entries.map((e) => e.name), ['n1-vm', 'n2-vm']);
  assert.deepEqual(p.sections[1].entries.map((e) => e.name), ['x-vm-web']);
  assert.equal(p.realized, true);
});

test('an OpenStack environment lists its project; an unrealized one keeps the one-click confirm', () => {
  const os = m.deletePreview('CloudOSO', obj('CloudOSO', 'os2', {}, { projectName: 'x-os2' }), { placements });
  assert.deepEqual(os.sections.map((s) => s.key), ['cloudProject']);
  assert.equal(os.realized, true);
  assert.equal(m.deletePreview('CloudAWS', obj('CloudAWS', 'a'), {}).realized, false);
});

test('an access group lists tenant roles, tool roles and access lists that name it; no typing', () => {
  const related = [
    obj('AAPOrg', 'automation', { aapAdminRbac: ['admins'], aapViewerRbac: ['viewers'] }),
    obj('Team', 'platform', { teamAdmin: ['admins'], teamViewerRbac: ['viewers'] }),
    obj('Assignment', 'a1', { toolRbac: { assignmentAdmin: 'admins' } }),
  ];
  const tenant = obj('Entity', 'x', { namespaceRbac: { entityAdmin: ['admins'], auditor: ['viewers'] } });
  const p = m.deletePreview('Rbac', obj('Rbac', 'admins'), { related, tenant });
  assert.deepEqual(p.sections.map((s) => [s.key, s.entries.map((e) => `${e.kind ?? ''}/${e.name}/${e.detail ?? ''}`)]), [
    ['tenantRoles', ['/entityAdmin/']],
    ['toolRoles', ['AAPOrg/automation/aapAdminRbac', 'Assignment/a1/assignmentAdmin']],
    ['accessLists', ['Team/platform/teamAdmin']],
  ]);
  assert.equal(p.realized, false);
});

test('a team setup lists the access its lists grant', () => {
  const p = m.deletePreview('Team', obj('Team', 'platform', { teamAdmin: ['admins'], teamMemberRbac: ['devs'] }));
  assert.deepEqual(p.sections[0].entries.map((e) => `${e.name}:${e.detail}`), ['admins:teamAdmin', 'devs:teamMemberRbac']);
  assert.equal(p.realized, false);
});

test('a placement lists the subnet it holds', () => {
  const p = m.deletePreview('NetworkPlacement', obj('NetworkPlacement', 'n1-vm', {}, { prefixes: ['10.1.0.0/24'], site: 'site-a' }));
  assert.deepEqual(p.sections[0].entries, [{ name: '10.1.0.0/24', detail: 'site-a' }]);
  assert.equal(p.realized, true);
});
