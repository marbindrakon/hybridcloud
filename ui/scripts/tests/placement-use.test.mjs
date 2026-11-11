// The placement settings "Use for virtual machines" and "Use for router attachments" (OpenStack only).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const pu = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'placementUse.mjs'));

test('only OpenStack environments offer the settings; the others send nothing', () => {
  for (const kind of ['CloudVirt', 'CloudAWS', 'CloudGCP']) {
    assert.equal(pu.placementUseOffered(kind), false, kind);
    assert.deepEqual(pu.placementUseSpec(kind, { virtualMachines: false, routerAttachments: true }), {}, kind);
    assert.equal(pu.placementUseProblem(kind, { virtualMachines: false, routerAttachments: false }), null, kind);
  }
  assert.equal(pu.placementUseOffered('CloudOSO'), true);
});

test('the defaults send nothing (an existing create is unchanged); a change sends both uses', () => {
  assert.deepEqual(pu.placementUseSpec('CloudOSO', pu.PLACEMENT_USE_DEFAULTS), {});
  assert.deepEqual(pu.placementUseSpec('CloudOSO', { virtualMachines: false, routerAttachments: true }), {
    use: { virtualMachines: false, routerAttachments: true },
  });
  assert.deepEqual(pu.placementUseSpec('CloudOSO', { virtualMachines: true, routerAttachments: true }), {
    use: { virtualMachines: true, routerAttachments: true },
  });
});

test('neither use is refused before submit (the API refuses it too)', () => {
  assert.equal(pu.placementUseProblem('CloudOSO', { virtualMachines: false, routerAttachments: false }), 'needOne');
  assert.equal(pu.placementUseProblem('CloudOSO', { virtualMachines: false, routerAttachments: true }), null);
});

test('the uses in effect and their states: spec, else the defaults; the reported state per use', () => {
  const p = (spec, status) => ({ metadata: { name: 'p' }, spec: { network: 'n', backend: { kind: 'CloudOSO', name: 'e' }, ...spec }, status });
  assert.deepEqual(pu.placementUseOf(p({})), { virtualMachines: true, routerAttachments: false });
  assert.deepEqual(pu.placementUseOf(p({ use: { routerAttachments: true } })), { virtualMachines: true, routerAttachments: true });
  const rows = pu.placementUseRows(
    p({ use: { virtualMachines: false, routerAttachments: true } }, {
      use: {
        virtualMachines: { state: 'removing', reason: 'InUse', message: 'port of server example-vm' },
        routerAttachments: { state: 'ready' },
      },
    }),
  );
  assert.deepEqual(rows.map((r) => [r.use, r.wanted, r.state, r.reason ?? '']), [
    ['virtualMachines', false, 'removing', 'InUse'],
    ['routerAttachments', true, 'ready', ''],
  ]);
  const virt = { metadata: { name: 'v' }, spec: { network: 'n', backend: { kind: 'CloudVirt', name: 'e' } } };
  assert.deepEqual(pu.placementUseRows(virt), []);
});
