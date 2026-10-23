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

test('a network waits for its placements, the VM environments naming it and its edge connections; only its fabric side goes with it', () => {
  const objects = {
    NetworkPlacement: placements,
    CloudVirt: [vmEnv],
    AttachmentCircuit: [obj('AttachmentCircuit', 'c1', { network: 'n1' }), obj('AttachmentCircuit', 'c2', { network: 'n2' })],
  };
  const p = m.deletePreview('HybridNetwork', obj('HybridNetwork', 'n1'), { objects });
  assert.deepEqual(keys(p), [
    ['deleted', 'external'],
    ['waits', 'waitPlacements'],
    ['waits', 'waitVmEnvironments'],
    ['waits', 'waitCircuits'],
  ]);
  assert.deepEqual(p.sections[0].entries, [{ name: '', detail: 'networkFabric' }]);
  assert.deepEqual(p.sections[1].entries, [
    { kind: 'NetworkPlacement', name: 'n1-vm', detail: 'vm', detailKind: 'CloudVirt' },
    { kind: 'NetworkPlacement', name: 'n1-os', detail: 'os', detailKind: 'CloudOSO' },
  ]);
  assert.deepEqual(p.sections[2].entries, [{ kind: 'CloudVirt', name: 'vm', detail: 'web' }]);
  assert.deepEqual(p.sections[3].entries.map((e) => e.name), ['c1']);
  assert.ok(
    !p.sections.some((s) => s.group === 'deleted' && s.entries.some((e) => e.kind)),
    'placements, environments and circuits are blockers, not deleted with the network',
  );
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
  assert.deepEqual(keys(aws), [
    ['deleted', 'external'],
    ['waits', 'waitClusters'],
  ]);
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

// F2: kinds whose teardown removes state outside the API say what goes, and ask for the name.
const ext = (p) => p.sections.filter((s) => s.key === 'external').flatMap((s) => s.entries.map((e) => [e.detail, e.name, e.args ?? {}]));

test('tool objects name what their teardown removes and ask for the name, realized or not', () => {
  const quay = m.deletePreview('QuayOrg', obj('QuayOrg', 'registry'));
  assert.deepEqual(ext(quay), [['quayOrg', 'x-registry', {}]]);
  assert.equal(quay.realized, true);
  assert.deepEqual(ext(m.deletePreview('QuayOrg', obj('QuayOrg', 'registry', {}, { orgName: 'org-1' }))), [['quayOrg', 'org-1', {}]]);
  const aap = m.deletePreview('AAPOrg', obj('AAPOrg', 'automation'));
  assert.deepEqual(ext(aap), [['aapOrg', 'x-automation', {}]]);
  assert.equal(aap.realized, true);
  const kv = m.deletePreview('VaultKV', obj('VaultKV', 'apps', { vault: 'v1' }, { kvPath: 'apps' }));
  assert.deepEqual(ext(kv), [['kvEngine', 'apps', { vault: 'v1' }]]);
  assert.equal(kv.realized, true);
  const vault = m.deletePreview('Vault', obj('Vault', 'v1'), { objects: { VaultKV: [obj('VaultKV', 'apps', { vault: 'v1' }), obj('VaultKV', 'other', { vault: 'v2' })] } });
  assert.deepEqual(keys(vault), [
    ['deleted', 'external'],
    ['kept', 'keptSecretEngines'],
  ]);
  assert.deepEqual(vault.sections[1].entries.map((e) => e.name), ['apps']);
  assert.equal(vault.realized, true);
  const asg = m.deletePreview('Assignment', obj('Assignment', 'a1', { openshift: 'c1', projects: ['web', 'api'] }, { assignmentProvisioned: true }));
  assert.deepEqual(ext(asg), [['assignment', 'web, api', { cluster: 'c1' }]]);
  assert.equal(asg.realized, true);
  // PGT2-F4: an assignment that never reached a cluster says so, with no empty cluster name.
  for (const [specArgs, st] of [[{ projects: ['webshop'] }, {}], [{ openshift: 'c1', projects: ['web'] }, { assignmentProvisioned: false }]]) {
    assert.deepEqual(ext(m.deletePreview('Assignment', obj('Assignment', 'a2', specArgs, st))), [['assignmentNotDelivered', '', {}]]);
  }
});

test('a cluster is deprovisioned and its delete waits for its assignments and placements', () => {
  const objects = {
    Assignment: [obj('Assignment', 'a1', { openshift: 'c1' }), obj('Assignment', 'a2', { openshift: 'c2' })],
    NetworkPlacement: [np('core-c1', 'core', 'PlatformOpenshift', 'c1'), ...placements],
  };
  const p = m.deletePreview('PlatformOpenshift', obj('PlatformOpenshift', 'c1'), { objects });
  assert.deepEqual(keys(p), [
    ['deleted', 'external'],
    ['waits', 'waitAssignments'],
    ['waits', 'waitPlacements'],
  ]);
  assert.deepEqual(ext(p), [['cluster', 'c1', {}]]);
  assert.deepEqual(p.sections[1].entries.map((e) => e.name), ['a1']);
  assert.deepEqual(p.sections[2].entries.map((e) => e.name), ['core-c1']);
  assert.equal(p.realized, true);
  assert.deepEqual(m.previewReads('PlatformOpenshift', obj('PlatformOpenshift', 'c1')).map((r) => r.kind), ['Assignment', 'NetworkPlacement']);
});

test('an access group goes from the identity provider: the name must be typed and it never reads as nothing else', () => {
  const p = m.deletePreview('Rbac', obj('Rbac', 'admins', {}, { group: 'x-admins' }));
  assert.deepEqual(ext(p), [['accessGroup', 'x-admins', {}]]);
  assert.equal(p.realized, true);
});

test('a tenant lists its objects tier by tier, then its namespace; protected objects stop it', () => {
  const tenant = { kind: 'Entity', metadata: { name: 'x', namespace: 'sovereign-cloud' }, spec: {}, status: {} };
  const reads = m.previewReads('Entity', tenant);
  assert.ok(reads.every((r) => r.namespace === 'entity-x'));
  assert.deepEqual(reads.map((r) => r.kind), m.TENANT_TEARDOWN_TIERS.flat());
  const objects = {
    Rbac: [obj('Rbac', 'admins')],
    NetworkPlacement: [np('n1-vm', 'n1', 'CloudVirt', 'vm')],
    CloudVirt: [obj('CloudVirt', 'vm', { deletionProtection: true })],
    QuayOrg: [obj('QuayOrg', 'registry')],
  };
  const p = m.deletePreview('Entity', tenant, { objects });
  assert.deepEqual(keys(p), [
    ['deleted', 'tenantObjects'],
    ['deleted', 'tenantNamespace'],
    ['deleted', 'external'],
    ['waits', 'waitProtected'],
  ]);
  assert.deepEqual(p.sections[0].entries.map((e) => `${e.detail}:${e.kind}/${e.name}`), ['1:NetworkPlacement/n1-vm', '5:QuayOrg/registry', '7:Rbac/admins']);
  assert.ok(p.sections[0].entries.every((e) => e.noLink));
  assert.deepEqual(p.sections[1].entries, [{ name: 'entity-x' }]);
  assert.deepEqual(p.sections[3].entries.map((e) => `${e.detail}:${e.kind}/${e.name}`), ['1:CloudVirt/vm']);
  assert.equal(p.realized, true);
});

test('a VM environment goes in the same tier as its placements, before the clusters and the other environments', () => {
  // a placement on a VM environment waits until the environment's namespaces are gone; the environment's own
  // delete has no guard, so it must not wait for a later tier
  const tierOf = (k) => m.TENANT_TEARDOWN_TIERS.findIndex((t) => t.includes(k));
  assert.equal(tierOf('CloudVirt'), tierOf('NetworkPlacement'));
  assert.ok(tierOf('CloudVirt') < tierOf('PlatformOpenshift'));
  for (const k of ['CloudOSO', 'CloudAWS', 'CloudGCP']) assert.ok(tierOf(k) > tierOf('NetworkPlacement') && tierOf(k) > tierOf('PlatformOpenshift'));
  assert.ok(tierOf('HybridNetwork') > Math.max(tierOf('CloudVirt'), tierOf('CloudOSO')));
  assert.equal(new Set(m.TENANT_TEARDOWN_TIERS.flat()).size, m.TENANT_TEARDOWN_TIERS.flat().length);
});

test('every kind with a teardown says what goes with it', () => {
  const kinds = ['HybridNetwork', 'NetworkPlacement', 'AttachmentCircuit', 'CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP', 'Team', 'Project',
    'QuayOrg', 'AAPOrg', 'VaultKV', 'Vault', 'PlatformOpenshift', 'Assignment', 'Rbac', 'Entity',
    'AAPConfig', 'QuayConfig', 'RbacConfig', 'CloudGateway', 'HybridFabric', 'CloudInfrastructure'];
  for (const k of kinds) {
    const p = m.deletePreview(k, obj(k, 'o'));
    assert.ok(p.sections.some((s) => s.group === 'deleted'), `${k} has a deleted section`);
  }
  // a plain object without a teardown keeps the one-click confirm
  const persona = m.deletePreview('Persona', obj('Persona', 'p'));
  assert.deepEqual(persona.sections, []);
  assert.equal(persona.realized, false);
  // team setups keep the one-click confirm (their access is listed)
  assert.equal(m.deletePreview('Team', obj('Team', 't')).realized, false);
});
