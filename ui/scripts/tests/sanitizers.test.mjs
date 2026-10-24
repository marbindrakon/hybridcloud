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
