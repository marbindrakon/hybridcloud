// forms/apiErrors.ts: causes of an API refusal land on the form field that writes the path.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const dir = process.env.SHELL_BUNDLE_DIR;
const ae = await import(path.join(dir, 'apiErrors.mjs'));
const { K8sApiError } = await import(path.join(dir, 'errors.mjs'));

test('normaliseFieldPath strips the leading dot and list indexes', () => {
  assert.equal(ae.normaliseFieldPath('.spec.toolRbac.editRbac[0]'), 'spec.toolRbac.editRbac');
  assert.equal(ae.normaliseFieldPath('spec.ipv4.prefix'), 'spec.ipv4.prefix');
  assert.equal(ae.normaliseFieldPath(undefined), '');
});

test('the longest registered prefix claims a cause; the rest stay in the summary', () => {
  const err = new K8sApiError(422, 'NetworkPlacement.hybridsovereign.redhat "x" is invalid', {
    reason: 'Invalid',
    causes: [
      { field: 'spec.ipv4.prefix', message: 'must be /16 to /29' },
      { field: 'spec.ipv4', message: 'needs prefixLength or prefix' },
      { field: 'metadata.name', message: 'must be no more than 63 characters' },
      { field: 'spec.other', message: 'unknown' },
    ],
  });
  const r = ae.fieldErrorsFrom(err, { 'spec.ipv4': 'size', 'spec.ipv4.prefix': 'pinned', 'metadata.name': 'name' });
  assert.deepEqual(r.byField, { pinned: 'must be /16 to /29', size: 'needs prefixLength or prefix', name: 'must be no more than 63 characters' });
  assert.deepEqual(ae.causeLines(r.other), ['spec.other: unknown']);
  assert.deepEqual(ae.fieldErrorsFrom(new Error('plain'), { spec: 'x' }), { byField: {}, other: [] });
});
