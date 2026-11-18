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

// The API's rule on a cluster's install-time fields refuses the change with its own sentence; the page shows it
// without the API's lead-in.
const RULE = 'The cluster is being installed or is installed. Sizes, flavors, release and image can be changed before the installation starts or after it has stopped.';

test('objectRuleMessages: the sentence of a rule on the whole object, from the causes', () => {
  for (const field of [undefined, '', '<nil>', 'spec', '.spec']) {
    const err = new K8sApiError(422, `PlatformOpenshift.hybridsovereign.redhat "demo" is invalid: <nil>: Invalid value: "object": ${RULE}`, {
      reason: 'Invalid',
      causes: [{ reason: 'FieldValueInvalid', field, message: `Invalid value: "object": ${RULE}` }],
    });
    assert.deepEqual(ae.objectRuleMessages(err), [RULE], String(field));
  }
  const forbidden = new K8sApiError(422, 'x is invalid', { causes: [{ field: 'spec', message: `Forbidden: ${RULE}` }] });
  assert.deepEqual(ae.objectRuleMessages(forbidden), [RULE]);
});

test('objectRuleMessages: from the message when the answer has no causes', () => {
  const err = new K8sApiError(422, `PlatformOpenshift.hybridsovereign.redhat "demo" is invalid: <nil>: Invalid value: "object": ${RULE}`, { reason: 'Invalid' });
  assert.deepEqual(ae.objectRuleMessages(err), [RULE]);
});

test('objectRuleMessages: nothing for refusals of a field, other errors or plain errors', () => {
  const field = new K8sApiError(422, 'x is invalid', { causes: [{ field: 'spec.openstack.workerCount', message: 'Invalid value: -1: should be greater than or equal to 0' }] });
  assert.deepEqual(ae.objectRuleMessages(field), []);
  assert.deepEqual(ae.objectRuleMessages(new K8sApiError(409, 'conflict', {})), []);
  assert.deepEqual(ae.objectRuleMessages(new Error(RULE)), []);
  assert.deepEqual(ae.objectRuleMessages(undefined), []);
});
