// The tenant dashboard's spec sanitizers must pass every spec field of their
// kind's CRD (UX-038, UX-040, UX-089). A full spec is built from each CRD's
// spec.properties (operator/config/crd/bases) and run through the sanitizer:
// every leaf field must survive, except the declared platform-only fields.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const root = process.env.UI_ROOT;
const specsPath = process.env.CRD_SPECS_JSON;
const san = await import(path.join(root, 'packages/tenant-dashboard/server/sanitizers.js'));

const specs = specsPath && fs.existsSync(specsPath) ? JSON.parse(fs.readFileSync(specsPath, 'utf8')) : null;

/** Sample values the sanitizers accept for fields with formats the schema does not spell out. */
const SAMPLES = {
  'HybridNetwork.prefixes': ['10.60.0.0/16'],
  'HybridNetwork.mtu': 1400,
  'NetworkPlacement.ipv4.prefix': '10.60.1.0/24',
  'NetworkPlacement.ipv4.prefixLength': 24,
  'NetworkPlacement.ipv6.prefix': 'fd00:1::/64',
  'NetworkPlacement.ipv6.prefixLength': 64,
  'NetworkPlacement.backend.kind': 'CloudVirt',
  'CloudVirt.quota.cpu': '8',
  'CloudVirt.quota.memory': '16Gi',
  'CloudVirt.quota.storage': '100Gi',
  'CloudVirt.quota.virtualMachines': 4,
  'PlatformOpenshift.openstack.controlPlaneCount': 3,
  'PlatformOpenshift.openstack.workerCount': 3,
  'PlatformOpenshift.aws.controlPlaneCount': 3,
  'PlatformOpenshift.aws.workerCount': 2,
  'PlatformOpenshift.gcp.controlPlaneCount': 3,
  'PlatformOpenshift.gcp.workerCount': 2,
  'PlatformOpenshift.networking.clusterNetwork': '10.128.0.0/14',
  'PlatformOpenshift.networking.serviceNetwork': '10.96.0.0/16',
  'PlatformOpenshift.networking.machineNetwork': '10.0.0.0/16',
  'PlatformOpenshift.networking.hostPrefix': 23,
};

function sampleFor(kind, dotted, schema) {
  const key = `${kind}.${dotted}`;
  if (key in SAMPLES) return SAMPLES[key];
  if (Array.isArray(schema.enum) && schema.enum.length) return schema.enum[0];
  switch (schema.type) {
    case 'string':
      return 'a1';
    case 'integer':
    case 'number':
      return typeof schema.minimum === 'number' ? schema.minimum : 1;
    case 'boolean':
      return true;
    case 'array': {
      const item = schema.items || { type: 'string' };
      return [sampleFor(kind, `${dotted}[]`, item)];
    }
    case 'object': {
      const out = {};
      for (const [k, v] of Object.entries(schema.properties || {})) out[k] = sampleFor(kind, dotted ? `${dotted}.${k}` : k, v);
      return out;
    }
    default:
      return 'a1';
  }
}

/** Leaf paths of a value (arrays count as one leaf unless they hold objects). */
function leaves(value, prefix = '') {
  if (Array.isArray(value)) {
    if (value.length && value[0] && typeof value[0] === 'object') return leaves(value[0], `${prefix}[]`);
    return [prefix];
  }
  if (value && typeof value === 'object') {
    const keys = Object.keys(value);
    if (!keys.length) return [prefix];
    return keys.flatMap((k) => leaves(value[k], prefix ? `${prefix}.${k}` : k));
  }
  return [prefix];
}

test('every sanitizer passes every CRD spec field except the platform-only ones', (t) => {
  if (!specs) {
    t.skip('CRD_SPECS_JSON not set (run through ui/scripts/test-shell.sh)');
    return;
  }
  for (const [kind, sanitize] of Object.entries(san.SANITIZERS)) {
    const schema = specs[kind];
    assert.ok(schema, `CRD for ${kind} found`);
    const full = sampleFor(kind, '', schema);
    const result = sanitize(full);
    assert.ok(!result.error, `${kind}: the sanitizer refused a full spec: ${result.error}`);
    const kept = new Set(leaves(result.spec));
    const dropped = leaves(full).filter((l) => !kept.has(l));
    const allowed = san.PLATFORM_ONLY_FIELDS[kind] || [];
    assert.deepEqual(
      dropped.filter((d) => !allowed.includes(d)),
      [],
      `${kind}: fields dropped by the sanitizer (add them to sanitizers.js or to PLATFORM_ONLY_FIELDS with a reason)`,
    );
  }
});

test('config references are optional and validated when present (platform default connection)', () => {
  assert.deepEqual(san.sanitizeRbacSpec({ description: 'Ops team' }), { spec: { description: 'Ops team' } });
  assert.deepEqual(san.sanitizeRbacSpec({ config: 'keycloak-x' }).spec, { config: 'keycloak-x' });
  assert.ok(san.sanitizeRbacSpec({ config: 'Bad Name' }).error);
  assert.deepEqual(san.sanitizeAAPOrgSpec({ aapAdminRbac: ['admins'] }).spec, { aapAdminRbac: ['admins'] });
  assert.deepEqual(san.sanitizeQuayOrgSpec({}).spec, {});
  assert.deepEqual(san.sanitizeVaultSpec({}).spec, { ha: true });
});

test('CloudAWS needs only cloudRef; the retired account fields are not required or passed', () => {
  assert.deepEqual(san.sanitizeCloudAWSSpec({ cloudRef: { name: 'aws-site' } }), { spec: { cloudRef: { name: 'aws-site' } } });
  assert.deepEqual(san.sanitizeCloudAWSSpec({ cloudRef: { name: 'aws-site' }, account: '1', vaultPath: 'x', baseDomain: 'y' }).spec, {
    cloudRef: { name: 'aws-site' },
  });
  assert.ok(san.sanitizeCloudAWSSpec({ account: '1' }).error);
});

test('network placements accept every environment kind of the CRD', () => {
  for (const kind of ['CloudOSO', 'CloudVirt', 'CloudAWS', 'CloudGCP']) {
    const r = san.sanitizeNetworkPlacementSpec({ network: 'core', backend: { kind, name: 'env1' }, ipv4: { prefixLength: 24 } });
    assert.ok(!r.error, kind);
  }
  assert.ok(san.sanitizeNetworkPlacementSpec({ network: 'core', backend: { kind: 'Entity', name: 'x' }, ipv4: { prefixLength: 24 } }).error);
});

test('per-family addressing: platform IPv6 space, IPv6-only networks and placements, the IPv6 source', () => {
  // a network may have only platform-assigned IPv6, only its own prefixes, or both
  assert.deepEqual(san.sanitizeHybridNetworkSpec({ platformPrefixes: [{ family: 6 }] }).spec, { platformPrefixes: [{ family: 6 }] });
  assert.deepEqual(
    san.sanitizeHybridNetworkSpec({ prefixes: ['10.60.0.0/16'], platformPrefixes: [{ family: 6, prefixLength: 56, prefix: '2001:db8::/56' }] }).spec,
    { prefixes: ['10.60.0.0/16'], platformPrefixes: [{ family: 6, prefixLength: 56, prefix: '2001:db8::/56' }] },
  );
  assert.deepEqual(san.sanitizeHybridNetworkSpec({ prefixes: ['fd12::/48'] }).spec, { prefixes: ['fd12::/48'] });
  assert.ok(san.sanitizeHybridNetworkSpec({ prefixes: [] }).error);
  assert.ok(san.sanitizeHybridNetworkSpec({}).error);
  assert.ok(san.sanitizeHybridNetworkSpec({ platformPrefixes: [{ family: 4 }] }).error);
  assert.ok(san.sanitizeHybridNetworkSpec({ platformPrefixes: [{ family: 6, prefixLength: 40 }] }).error);
  const np = (spec) => san.sanitizeNetworkPlacementSpec({ network: 'core', backend: { kind: 'CloudAWS', name: 'env1' }, ...spec });
  assert.deepEqual(np({ ipv6: { source: 'Provider' } }).spec.ipv6, { source: 'Provider' });
  assert.deepEqual(np({ ipv6: {} }).spec.ipv6, {});
  assert.equal(np({ ipv6: { prefixLength: 60 } }).spec.ipv4, undefined);
  assert.ok(np({ ipv6: { source: 'Elsewhere' } }).error);
  assert.ok(np({}).error);
  assert.ok(np({ ipv4: {} }).error);
});

test('service role lists: all of them on create; an update keeps an empty list (clears it)', () => {
  const kv = san.sanitizeVaultKVSpec({
    vault: 'v1',
    vaultAdminRbac: ['a'],
    vaultReaderRbac: ['r'],
    vaultOpsRbac: ['o'],
    vaultDeveloperRbac: ['d'],
  });
  assert.deepEqual(kv.spec, { vault: 'v1', vaultAdminRbac: ['a'], vaultReaderRbac: ['r'], vaultOpsRbac: ['o'], vaultDeveloperRbac: ['d'] });
  assert.deepEqual(san.sanitizeAAPOrgSpec({ aapViewerRbac: [] }, { update: true }).spec, { aapViewerRbac: [] });
  assert.deepEqual(san.sanitizeAAPOrgSpec({ aapViewerRbac: [] }).spec, {});
  assert.ok(san.sanitizeQuayOrgSpec({ quayAdminRbac: ['Not Valid'] }).error);
});

test('projects keep all three access lists; assignments keep toolRbac', () => {
  const p = san.sanitizeProjectSpec({ projectAdmin: ['a'], projectDeveloperRbac: ['d'], projectViewerRbac: ['v'], displayName: 'P' });
  assert.deepEqual(p.spec, { displayName: 'P', projectAdmin: ['a'], projectDeveloperRbac: ['d'], projectViewerRbac: ['v'] });
  const a = san.sanitizeAssignmentSpec({ team: 't', projects: ['p'], openshift: 'c', toolRbac: { assignmentAdmin: 'admins', assignmentOps: '' } });
  assert.deepEqual(a.spec, { team: 't', projects: ['p'], openshift: 'c', toolRbac: { assignmentAdmin: 'admins' } });
});

test('platform-only fields are never sent and an edit keeps their stored values', () => {
  const oso = san.sanitizeCloudOSOSpec({ project: 'p', vaultPath: 'oso/x', route53VaultPath: 'r53' });
  assert.deepEqual(oso.spec, { project: 'p' });
  assert.deepEqual(san.keepPlatformOnlyFields('CloudOSO', oso.spec, { project: 'old', vaultPath: 'oso/x' }), { project: 'p', vaultPath: 'oso/x' });
  const po = san.sanitizePlatformOpenshiftSpec({ type: 'openstack', openstack: { environment: 'e', sshKeyVaultPath: 'k' } });
  assert.deepEqual(po.spec.openstack, { environment: 'e' });
  const kept = san.keepPlatformOnlyFields('PlatformOpenshift', po.spec, { openstack: { environment: 'e', sshKeyVaultPath: 'k' } });
  assert.equal(kept.openstack.sshKeyVaultPath, 'k');
  // An openstack block removed by the edit is not re-created for the platform-only field alone.
  const awsOnly = san.keepPlatformOnlyFields('PlatformOpenshift', { type: 'aws', aws: { environment: 'a' } }, { openstack: { sshKeyVaultPath: 'k' } });
  assert.equal(awsOnly.openstack, undefined);
});

test('clusters: the network, real node counts, address settings; control plane at least 1 everywhere', () => {
  const os = san.sanitizePlatformOpenshiftSpec({
    type: 'openstack',
    openstack: { environment: 'e', network: 'web', controlPlaneCount: 3, workerCount: 0 },
    networking: { clusterNetwork: '10.128.0.0/14', hostPrefix: 23, serviceNetwork: '10.96.0.0/16', machineNetwork: '10.0.0.0/16' },
  });
  assert.deepEqual(os.spec.openstack, { environment: 'e', network: 'web', controlPlaneCount: 3, workerCount: 0 });
  assert.deepEqual(os.spec.networking, { clusterNetwork: '10.128.0.0/14', hostPrefix: 23, serviceNetwork: '10.96.0.0/16', machineNetwork: '10.0.0.0/16' });
  assert.equal(san.sanitizePlatformOpenshiftSpec({ openstack: { environment: 'e', externalNetwork: 'public' } }).spec.openstack.externalNetwork, 'public');
  assert.match(san.sanitizePlatformOpenshiftSpec({ openstack: { environment: 'e', network: 'Not A Name' } }).error, /spec\.openstack\.network/);
  for (const cloud of ['aws', 'gcp']) {
    const ok = san.sanitizePlatformOpenshiftSpec({ [cloud]: { environment: 'e', network: 'web', publish: 'Internal', controlPlaneCount: 1, workerCount: 0 } });
    assert.deepEqual(ok.spec[cloud], { environment: 'e', network: 'web', publish: 'Internal', controlPlaneCount: 1, workerCount: 0 });
    // 0 control plane nodes used to pass on AWS and Google Cloud
    assert.match(san.sanitizePlatformOpenshiftSpec({ [cloud]: { environment: 'e', controlPlaneCount: 0 } }).error, /controlPlaneCount.*from 1/);
    assert.match(san.sanitizePlatformOpenshiftSpec({ [cloud]: { environment: 'e', workerCount: -1 } }).error, /workerCount/);
  }
  assert.match(san.sanitizePlatformOpenshiftSpec({ openstack: { environment: 'e', controlPlaneCount: 0 } }).error, /controlPlaneCount/);
  assert.match(san.sanitizePlatformOpenshiftSpec({ networking: { clusterNetwork: 'fd00::/48' } }).error, /clusterNetwork must be an IPv4 prefix/);
  assert.match(san.sanitizePlatformOpenshiftSpec({ networking: { hostPrefix: 30 } }).error, /hostPrefix must be an integer from 16 to 28/);
  assert.match(san.sanitizePlatformOpenshiftSpec({ networking: 'x' }).error, /spec.networking must be an object/);
  assert.equal(san.sanitizePlatformOpenshiftSpec({ networking: {} }).spec.networking, undefined);
});

test('networks pass platformAccess (a boolean); placements pass spec.use (booleans)', () => {
  assert.equal(san.sanitizeHybridNetworkSpec({ prefixes: ['10.60.0.0/16'], platformAccess: false }).spec.platformAccess, false);
  assert.equal('platformAccess' in san.sanitizeHybridNetworkSpec({ prefixes: ['10.60.0.0/16'] }).spec, false);
  assert.match(san.sanitizeHybridNetworkSpec({ prefixes: ['10.60.0.0/16'], platformAccess: 'yes' }).error, /platformAccess/);
  const base = { network: 'n', backend: { kind: 'CloudOSO', name: 'e' }, ipv4: { prefixLength: 24 } };
  assert.deepEqual(san.sanitizeNetworkPlacementSpec({ ...base, use: { virtualMachines: false, routerAttachments: true } }).spec.use, {
    virtualMachines: false,
    routerAttachments: true,
  });
  assert.equal('use' in san.sanitizeNetworkPlacementSpec(base).spec, false);
  assert.match(san.sanitizeNetworkPlacementSpec({ ...base, use: { routerAttachments: 1 } }).error, /spec\.use\.routerAttachments/);
  assert.match(san.sanitizeNetworkPlacementSpec({ ...base, use: [] }).error, /spec\.use must be an object/);
});

test('raw writes: an edge connection of the platform kind is refused from tenants, in every body form', () => {
  const p = '/apis/hybridsovereign.redhat/v1alpha1/namespaces/entity-example-corp/attachmentcircuits';
  const refusal = /ceKind platform/;
  assert.match(san.tenantWriteRefusal(p, 'POST', { spec: { ceKind: 'platform', network: 'n' } }), refusal);
  assert.match(san.tenantWriteRefusal(`${p}/c1`, 'PUT', { spec: { ceKind: 'platform' } }), refusal);
  assert.match(san.tenantWriteRefusal(`${p}/c1`, 'PATCH', { spec: { ceKind: 'platform' } }), refusal);
  assert.match(san.tenantWriteRefusal(`${p}/c1`, 'PATCH', [{ op: 'replace', path: '/spec/ceKind', value: 'platform' }]), refusal);
  assert.match(san.tenantWriteRefusal(`${p}/c1`, 'PATCH', [{ op: 'replace', path: '/spec', value: { ceKind: 'platform' } }]), refusal);
  assert.match(san.tenantWriteRefusal(`${p}?dryRun=All`, 'POST', { spec: { ceKind: 'platform' } }), refusal);
  assert.equal(san.tenantWriteRefusal(p, 'POST', { spec: { ceKind: 'customer', network: 'n' } }), null);
  assert.equal(san.tenantWriteRefusal(p, 'POST', { spec: { network: 'n' } }), null);
  assert.equal(san.tenantWriteRefusal(`${p}/c1`, 'DELETE', undefined), null);
  assert.equal(san.tenantWriteRefusal('/apis/hybridsovereign.redhat/v1alpha1/namespaces/x/hybridnetworks', 'POST', { spec: { ceKind: 'platform' } }), null);
  assert.throws(() => san.guardTenantWrite(p, 'POST', { spec: { ceKind: 'platform' } }), (e) => e.status === 403);
  assert.doesNotThrow(() => san.guardTenantWrite(p, 'POST', { spec: { ceKind: 'customer' } }));
});
