// Attach dialog (GT-F3): which networks an environment can take.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const a = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'attachable.mjs'));

const vm = (spec, status) => ({ metadata: { name: 'vm' }, spec: { namespaces: spec }, status: { namespaces: status } });

test('a CloudVirt takes only the networks its namespaces name', () => {
  const env = vm([{ name: 'web', network: 'p1' }, { name: 'db' }], []);
  assert.equal(a.attachBlock('CloudVirt', env, 'p1'), null);
  assert.equal(a.attachBlock('CloudVirt', env, 'p2'), 'noNamespace');
  assert.deepEqual([...a.networksTakenBy('CloudVirt', env)], ['p1']);
});

test('a CloudVirt without spec namespaces falls back to status.namespaces', () => {
  const env = vm([], [{ name: 'web', namespace: 't-vm-web', network: 'p2' }]);
  assert.equal(a.attachBlock('CloudVirt', env, 'p2'), null);
  assert.equal(a.attachBlock('CloudVirt', env, 'p1'), 'noNamespace');
});

test('other environment types and unknown environments take any network', () => {
  for (const kind of ['CloudOSO', 'CloudAWS', 'CloudGCP']) assert.equal(a.attachBlock(kind, vm([], []), 'p1'), null);
  assert.equal(a.attachBlock('CloudVirt', undefined, 'p1'), null);
  assert.equal(a.networksTakenBy('CloudOSO', vm([], [])), null);
  assert.equal(a.attachBlock('CloudVirt', vm([], []), ''), null);
});
