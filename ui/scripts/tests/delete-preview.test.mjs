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
  // OD47: the name is typed, with the access sentence under the field
  assert.equal(p.realized, true);
  assert.equal(p.typeHelp, 'access');
});

// OD47, PGT-F5: team setup and app project deletes list their dependants and ask for the name.
test('a team setup lists its cluster assignments and their app projects, which stay', () => {
  const asg = (name, team, projects, extra = {}) => ({ ...obj('Assignment', name, { team, projects }), ...extra });
  const objects = {
    Assignment: [
      asg('a1', 'platform', ['web', 'api']),
      asg('a2', 'platform', ['api', 'batch']),
      asg('a3', 'other', ['ops']),
      { ...asg('a4', 'platform', ['gone']), metadata: { name: 'a4', namespace: 'entity-x', deletionTimestamp: '2026-10-23T00:00:00Z' } },
    ],
  };
  const p = m.deletePreview('Team', obj('Team', 'platform', { teamViewerRbac: ['auditors'] }), { objects });
  assert.deepEqual(keys(p), [
    ['deleted', 'external'],
    ['affected', 'teamAssignments'],
    ['affected', 'teamProjects'],
    ['access', 'scopeGrants'],
  ]);
  assert.deepEqual(ext(p), [['signInGroups', '', {}]]);
  assert.deepEqual(p.sections[1].entries, [{ kind: 'Assignment', name: 'a1' }, { kind: 'Assignment', name: 'a2' }]);
  assert.deepEqual(p.sections[2].entries.map((e) => `${e.kind}/${e.name}`), ['Project/api', 'Project/batch', 'Project/web']);
  assert.deepEqual(p.sections[3].entries.map((e) => `${e.name}:${e.detail}`), ['auditors:teamViewerRbac']);
  assert.ok(!p.sections.some((s) => s.group === 'deleted' && s.entries.some((e) => e.kind)), 'assignments and projects are not deleted');
  assert.deepEqual(m.previewReads('Team', obj('Team', 'platform')), [{ kind: 'Assignment', namespace: 'entity-x' }]);
  // nothing names it: still typed, still says what goes
  const lone = m.deletePreview('Team', obj('Team', 'lone'));
  assert.deepEqual(keys(lone), [['deleted', 'external']]);
  assert.equal(lone.realized, true);
});

test('an app project lists the cluster assignments that list it and the team setups whose access ends', () => {
  const objects = {
    Assignment: [
      obj('Assignment', 'a1', { team: 'platform', projects: ['web'] }),
      obj('Assignment', 'a2', { team: 'ops', projects: ['web', 'api'] }),
      obj('Assignment', 'a3', { team: 'platform', projects: ['api'] }),
    ],
  };
  const p = m.deletePreview('Project', obj('Project', 'web', { projectAdmin: ['admins'], projectDeveloperRbac: ['devs'] }), { objects });
  assert.deepEqual(keys(p), [
    ['deleted', 'external'],
    ['affected', 'projectAssignments'],
    ['access', 'scopeGrants'],
    ['access', 'projectTeams'],
  ]);
  assert.deepEqual(p.sections[1].entries.map((e) => e.name), ['a1', 'a2']);
  assert.deepEqual(p.sections[2].entries.map((e) => `${e.name}:${e.detail}`), ['admins:projectAdmin', 'devs:projectDeveloperRbac']);
  assert.deepEqual(p.sections[3].entries, [{ kind: 'Team', name: 'ops' }, { kind: 'Team', name: 'platform' }]);
  assert.equal(p.realized, true);
  assert.equal(p.typeHelp, 'access');
  assert.deepEqual(m.previewReads('Project', obj('Project', 'web')).map((r) => r.kind), ['Assignment']);
});

// PGT2-F2: the VMs each site namespace holds, from the environment's status.
test('a VM environment lists the VMs its site namespaces hold, with the count when the list is cut', () => {
  // status.virtualMachines: per namespace the complete count and the sorted, capped names
  const vm = obj('CloudVirt', 'vm', {}, {
    namespaces: [{ name: 'web', namespace: 'x-vm-web' }, { name: 'db', namespace: 'x-vm-db' }, { name: 'empty', namespace: 'x-vm-empty' }],
    virtualMachines: {
      observedAt: '2026-10-23T10:00:00Z',
      total: 9,
      truncated: true,
      namespaces: [
        { name: 'web', namespace: 'x-vm-web', count: 5, names: ['web-1', 'web-2'], truncated: true },
        { name: 'db', namespace: 'x-vm-db', count: 1, names: ['db-1'], truncated: false },
        { name: 'empty', namespace: 'x-vm-empty', count: 0, names: [], truncated: false },
        // left the spec but still holds VMs
        { name: '', namespace: 'x-vm-old', count: 3, names: [], truncated: true },
      ],
    },
  });
  const p = m.deletePreview('CloudVirt', vm);
  assert.deepEqual(keys(p), [
    ['deleted', 'namespaces'],
    ['waits', 'waitVms'],
  ]);
  assert.deepEqual(p.sections[1].entries, [
    { name: 'x-vm-web', items: ['web-1', 'web-2'], more: 3 },
    { name: 'x-vm-db', items: ['db-1'], more: 0 },
    { name: 'x-vm-old', items: [], more: 3 },
  ]);
  // a status without the VM fields lists none (older releases)
  const old = m.deletePreview('CloudVirt', obj('CloudVirt', 'vm', {}, { namespaces: [{ name: 'web', namespace: 'x-vm-web' }] }));
  assert.ok(!old.sections.some((s) => s.key === 'waitVms'));
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
  // R3U-F1: the dialog reads the delete job's record (provisioned, or a policy name), not this run's readiness.
  assert.deepEqual(ext(m.deletePreview('Assignment', obj('Assignment', 'a3', { openshift: 'c1', projects: ['web'] }, { assignmentProvisioned: 'True', clusterName: 'c1', ready: false }))),
    [['assignment', 'web', { cluster: 'c1' }]]);
  // the ACM policy it recorded is named (status.acmPolicyName, tenant-prefixed since OD48)
  for (const st of [
    { assignmentProvisioned: false, acmPolicyName: 'x-a3', clusterName: 'c1' },
    { acmPolicyName: 'x-a3', openshiftReady: false },
  ]) {
    assert.deepEqual(ext(m.deletePreview('Assignment', obj('Assignment', 'a3', { openshift: 'c1', projects: ['web'] }, st))),
      [['assignment', 'web', { cluster: 'c1' }], ['acmPolicy', 'x-a3', {}]]);
  }
  // delivered, but no cluster is named any more: it still says something was put on a cluster
  assert.deepEqual(ext(m.deletePreview('Assignment', obj('Assignment', 'a4', { projects: ['web'] }, { acmPolicyName: 'x-a4' }))),
    [['assignmentUnnamedCluster', '', {}], ['acmPolicy', 'x-a4', {}]]);
  // P3R-O1: never delivered: one sentence, no second line under the name field
  const never = m.deletePreview('Assignment', obj('Assignment', 'a5', { openshift: 'c1' }));
  assert.equal(never.realized, true);
  assert.equal(never.typeHelp, null);
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
  // OD47: team setups and app projects ask for the name
  assert.equal(m.deletePreview('Team', obj('Team', 't')).realized, true);
  assert.equal(m.deletePreview('Project', obj('Project', 'p')).realized, true);
});
