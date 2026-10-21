// Error model (C4): Kubernetes Status bodies -> K8sApiError -> error class.
import { test } from 'node:test';
import assert from 'node:assert/strict';

const m = await import(`${process.env.SHELL_BUNDLE_DIR}/errors.mjs`);

const status = (code, reason, message, causes) =>
  JSON.stringify({ kind: 'Status', apiVersion: 'v1', status: 'Failure', code, reason, message, ...(causes ? { details: { causes } } : {}) });

test('403 Status body keeps reason and message, classifies as forbidden', () => {
  const e = m.parseStatusBody(403, 'Forbidden', status(403, 'Forbidden', 'cloudvirts.hybridsovereign.redhat is forbidden: User "u" cannot list resource "cloudvirts"'), { kind: 'CloudVirt', verb: 'list', namespace: 'entity-x' });
  assert.equal(e.status, 403);
  assert.equal(e.reason, 'Forbidden');
  assert.equal(e.kind, 'CloudVirt');
  assert.match(e.message, /^K8s API error 403: cloudvirts/);
  assert.equal(m.classifyError(e), 'forbidden');
  assert.equal(m.isForbiddenError(e), true);
});

test('401, 404, 409, 410, 500, 502, 503 classes', () => {
  assert.equal(m.classifyError(m.parseStatusBody(401, 'Unauthorized', '')), 'unauthenticated');
  assert.equal(m.classifyError(m.parseStatusBody(404, 'Not Found', status(404, 'NotFound', 'x not found'))), 'notFound');
  assert.equal(m.classifyError(m.parseStatusBody(410, 'Gone', '')), 'notFound');
  assert.equal(m.classifyError(m.parseStatusBody(409, 'Conflict', status(409, 'Conflict', 'the object has been modified'))), 'conflict');
  assert.equal(m.classifyError(m.parseStatusBody(409, 'Conflict', status(409, 'AlreadyExists', 'already exists'))), 'invalid');
  assert.equal(m.classifyError(m.parseStatusBody(500, 'Internal Server Error', '')), 'server');
  assert.equal(m.classifyError(m.parseStatusBody(502, 'Bad Gateway', JSON.stringify({ error: 'upstream failed', reference: 'ref-1a2b' }))), 'unavailable');
  assert.equal(m.classifyError(m.parseStatusBody(503, 'Service Unavailable', '')), 'unavailable');
});

test('422 keeps the field causes for forms', () => {
  const e = m.parseStatusBody(422, 'Unprocessable Entity', status(422, 'Invalid', 'HybridNetwork.hybridsovereign.redhat "n" is invalid', [
    { reason: 'FieldValueInvalid', message: 'Invalid value: "10.0.0.0/33"', field: 'spec.prefixes[0]' },
  ]));
  assert.equal(m.classifyError(e), 'invalid');
  assert.equal(e.causes.length, 1);
  assert.equal(e.causes[0].field, 'spec.prefixes[0]');
  assert.equal(e.detail, 'HybridNetwork.hybridsovereign.redhat "n" is invalid');
});

test('dashboard {error, reference} body and non-JSON bodies', () => {
  const e = m.parseStatusBody(502, 'Bad Gateway', JSON.stringify({ error: 'Upstream unavailable', reference: 'r-123' }));
  assert.equal(e.detail, 'Upstream unavailable');
  assert.equal(e.reference, 'r-123');
  const h = m.parseStatusBody(500, 'Internal Server Error', '<html>oops</html>');
  assert.equal(h.detail, 'Internal Server Error');
});

test('network failure is status 0 and unavailable', () => {
  const e = m.networkError(new TypeError('Failed to fetch'), { kind: 'Team' });
  assert.equal(e.status, 0);
  assert.equal(m.classifyError(e), 'unavailable');
  assert.equal(m.classifyError(new TypeError('Failed to fetch')), 'unavailable');
});

test('legacy Error messages still classify', () => {
  assert.equal(m.statusOf(new Error('K8s API error 403: nope')), 403);
  assert.equal(m.classifyError(new Error('K8s API error 404: gone')), 'notFound');
  assert.equal(m.classifyError(new Error('something else')), 'server');
});

test('Warning headers are parsed', () => {
  const h = new Headers({ warning: '299 - "spec.foo: deprecated", 299 - "second \\"quoted\\""' });
  assert.deepEqual(m.parseWarningHeaders(h), ['spec.foo: deprecated', 'second "quoted"']);
  assert.equal(m.parseWarningHeaders(new Headers()), undefined);
});
