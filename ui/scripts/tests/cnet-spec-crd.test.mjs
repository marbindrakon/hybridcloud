// What the cluster form and the placement settings send, against the CRD bases (CRD_SPECS_JSON): every field the
// UI writes exists in the kind's spec schema with the type it is sent as (the reverse of the sanitizer drift test).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const cs = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'clusterSpec.mjs'));
const pu = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'placementUse.mjs'));
const specsPath = process.env.CRD_SPECS_JSON;

const TYPES = { string: 'string', number: 'integer', boolean: 'boolean' };

/** Every leaf of a sent object as [dotted path, schema node or undefined, value]. */
function leaves(schema, value, at = '') {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return Object.entries(value).flatMap(([k, v]) => leaves(schema?.properties?.[k], v, at ? `${at}.${k}` : k));
  }
  if (Array.isArray(value)) return value.flatMap((v) => leaves(schema?.items, v, `${at}[]`));
  return [[at, schema, value]];
}

function problems(schema, spec) {
  return leaves(schema, spec)
    .filter(([, node, v]) => !node || (node.type && node.type !== TYPES[typeof v]))
    .map(([p, node, v]) => `${p}: ${node ? `${node.type}, sent ${typeof v}` : 'not in the CRD'}`);
}

const full = (provider, network) => ({
  provider,
  environment: 'example-env',
  network,
  publish: 'Internal',
  controlPlaneCount: '3',
  workerCount: '0',
  networking: {
    ...cs.EMPTY_NETWORKING,
    clusterNetwork: '10.128.0.0/14',
    hostPrefix: '23',
    serviceNetwork: '172.30.0.0/16',
    ...(provider === 'openstack' || !network ? { machineNetwork: '10.0.0.0/16' } : {}),
  },
});

test('the cluster spec the form sends is declared by the PlatformOpenshift CRD', (t) => {
  if (!specsPath) {
    t.skip('CRD_SPECS_JSON not set (run through ui/scripts/test-shell.sh)');
    return;
  }
  const schema = JSON.parse(fs.readFileSync(specsPath, 'utf8')).PlatformOpenshift;
  const sent = [
    full('openstack', 'network:web'),
    full('openstack', 'external:public'),
    full('openstack', ''),
    full('aws', 'network:web'),
    full('aws', ''),
    full('gcp', 'network:web'),
    full('gcp', ''),
  ].map((input) => cs.buildClusterSpec(input));
  assert.ok(sent.some((s) => s.openstack?.network === 'web'), 'the hybrid network choice is sent');
  assert.ok(sent.some((s) => s.openstack?.externalNetwork === 'public'), 'the external network choice is sent');
  for (const spec of sent) assert.deepEqual(problems(schema, spec), [], JSON.stringify(spec));
});

test('the placement uses the dialog sends are declared by the NetworkPlacement CRD', (t) => {
  if (!specsPath) {
    t.skip('CRD_SPECS_JSON not set (run through ui/scripts/test-shell.sh)');
    return;
  }
  const schema = JSON.parse(fs.readFileSync(specsPath, 'utf8')).NetworkPlacement;
  const spec = pu.placementUseSpec('CloudOSO', { virtualMachines: false, routerAttachments: true });
  assert.ok(spec.use);
  assert.deepEqual(problems(schema, spec), []);
});
