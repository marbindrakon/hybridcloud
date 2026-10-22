// Delete dialog (GT-F8, OD41): what a delete removes, what it waits for, what it keeps, and when the name must be typed.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'deletePreview.mjs'));
const obj = (kind, name, spec = {}, status = {}) => ({ kind, metadata: { name, namespace: 'entity-x' }, spec, status });
const np = (name, network, kind, env) => obj('NetworkPlacement', name, { network, backend: { kind, name: env } });
const placements = [np('n1-vm', 'n1', 'CloudVirt', 'vm'), np('n1-os', 'n1', 'CloudOSO', 'os'), np('n2-vm', 'n2', 'CloudVirt', 'vm')];
const vmEnv = obj('CloudVirt', 'vm', { namespaces: [{ name: 'web', network: 'n1' }, { name: 'db', network: 'n2' }] });
const keys = (p) => p.sections.map((s) => [s.group, s.key]);

test('a network waits for its placements, the VM environments naming it and its edge connections; nothing is deleted with it', () => {
  const objects = {
    NetworkPlacement: placements,
    CloudVirt: [vmEnv],
    AttachmentCircuit: [obj('AttachmentCircuit', 'c1', { network: 'n1' }), obj('AttachmentCircuit', 'c2', { network: 'n2' })],
  };
  const p = m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n1'), { objects });
  assert.deepEqual(keys(p), [
    ['waits', 'waitPlacements'],
    ['waits', 'waitVmEnvironments'],
    ['waits', 'waitCircuits'],
  ]);
  assert.deepEqual(p.sections[0].entries, [
    { kind: 'NetworkPlacement', name: 'n1-vm', detail: 'vm', detailKind: 'CloudVirt' },
    { kind: 'NetworkPlacement', name: 'n1-os', detail: 'os', detailKind: 'CloudOSO' },
  ]);
  assert.deepEqual(p.sections[1].entries, [{ kind: 'CloudVirt', name: 'vm', detail: 'web' }]);
  assert.deepEqual(p.sections[2].entries.map((e) => e.name), ['c1']);
  assert.ok(!p.sections.some((s) => s.group === 'deleted'), 'placements are blockers, not deleted with the network');
  // blockers alone do not ask for typing; a realized network does
  assert.equal(p.realized, false);
  assert.equal(m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n1', {}, { realization: { l3vni: 5 } }), { objects }).realized, true);
  assert.deepEqual(m.previewReads('HybridNetwork', obj('HybridNetwork', 'n1')).map((r) => r.kind), [
    'NetworkPlacement',
    'CloudVirt',
    'AttachmentCircuit',
  ]);
});

test('a VM environment removes its site namespaces (once empty of VMs) and DNS zone, and keeps its placements', () => {
  const vm = obj('CloudVirt', 'vm', {}, { namespaces: [{ name: 'web', namespace: 'x-vm-web' }], dns: { zone: 'vm.x.example' } });
  const p = m.deletePreview('CloudVirt', vm, { objects: { NetworkPlacement: placements } });
  assert.deepEqual(keys(p), [
    ['deleted', 'namespaces'],
    ['deleted', 'dnsZone'],
    ['kept', 'keptPlacements'],
  ]);
  assert.deepEqual(p.sections[0].entries.map((e) => e.name), ['x-vm-web']);
  assert.deepEqual(p.sections[2].entries.map((e) => e.name), ['n1-vm', 'n2-vm']);
  assert.equal(p.realized, true);
});

test('an OpenStack environment deletes its project and waits for its placements and clusters', () => {
  const objects = {
    NetworkPlacement: placements,
    PlatformOpenshift: [obj('PlatformOpenshift', 'c1', { openstack: { environment: 'os' } }), obj('PlatformOpenshift', 'c2', { aws: { environment: 'os' } })],
  };
  const os = m.deletePreview('CloudOSO', obj('CloudOSO', 'os', {}, { projectName: 'x-os' }), { objects });
  assert.deepEqual(keys(os), [
    ['deleted', 'cloudProject'],
    ['waits', 'waitPlacements'],
    ['waits', 'waitClusters'],
  ]);
  assert.deepEqual(os.sections[1].entries.map((e) => e.name), ['n1-os']);
  assert.deepEqual(os.sections[2].entries.map((e) => e.name), ['c1']);
  assert.equal(os.realized, true);
  const aws = m.deletePreview('CloudAWS', obj('CloudAWS', 'a'), { objects: { PlatformOpenshift: [obj('PlatformOpenshift', 'c3', { aws: { environment: 'a' } })] } });
  assert.deepEqual(keys(aws), [['waits', 'waitClusters']]);
  assert.equal(aws.realized, false);
});

test('a placement on a VM environment waits while a namespace of the environment uses its network', () => {
  const p = m.deletePreview(
    'NetworkPlacement',
    obj('NetworkPlacement', 'n1-vm', { network: 'n1', backend: { kind: 'CloudVirt', name: 'vm' } }, { prefixes: ['10.1.0.0/24'], site: 'site-a' }),
    { objects: { CloudVirt: [vmEnv] } },
  );
  assert.deepEqual(keys(p), [
    ['deleted', 'subnet'],
    ['waits', 'waitSiteNamespace'],
  ]);
  assert.deepEqual(p.sections[0].entries, [{ name: '10.1.0.0/24', detail: 'site-a' }]);
  assert.deepEqual(p.sections[1].entries, [{ kind: 'CloudVirt', name: 'vm', detail: 'web' }]);
  assert.equal(p.realized, true);
  assert.deepEqual(m.previewReads('NetworkPlacement', obj('NetworkPlacement', 'p', { backend: { kind: 'CloudOSO', name: 'os' } })), []);
});

test('an access group lists tenant roles, tool roles and access lists that name it', () => {
  const objects = {
    AAPOrg: [obj('AAPOrg', 'automation', { aapAdminRbac: ['admins'], aapViewerRbac: ['viewers'] })],
    Team: [obj('Team', 'platform', { teamAdmin: ['admins'], teamViewerRbac: ['viewers'] })],
    Assignment: [obj('Assignment', 'a1', { toolRbac: { assignmentAdmin: 'admins' } })],
  };
  const tenant = obj('Entity', 'x', { namespaceRbac: { entityAdmin: ['admins'], auditor: ['viewers'] } });
  const p = m.deletePreview('Rbac', obj('Rbac', 'admins'), { objects, tenant });
  const access = p.sections.filter((s) => s.group === 'access');
  assert.deepEqual(access.map((s) => [s.key, s.entries.map((e) => `${e.kind ?? ''}/${e.name}/${e.detail ?? ''}`)]), [
    ['tenantRoles', ['/entityAdmin/']],
    ['toolRoles', ['AAPOrg/automation/aapAdminRbac', 'Assignment/a1/assignmentAdmin']],
    ['accessLists', ['Team/platform/teamAdmin']],
  ]);
});

test('a team setup lists the access its lists grant', () => {
  const p = m.deletePreview('Team', obj('Team', 'platform', { teamAdmin: ['admins'], teamMemberRbac: ['devs'] }));
  const grants = p.sections.find((s) => s.key === 'scopeGrants');
  assert.deepEqual(grants.entries.map((e) => `${e.name}:${e.detail}`), ['admins:teamAdmin', 'devs:teamMemberRbac']);
  assert.equal(p.realized, false);
});
