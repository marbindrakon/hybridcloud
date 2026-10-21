// k8sFetchWithWarnings with a fetch function that throws non-2xx answers (the console's consoleFetch HttpError).
import { test } from 'node:test';
import assert from 'node:assert/strict';

const B = process.env.SHELL_BUNDLE_DIR;
const k8s = await import(`${B}/k8s.mjs`);
const errors = await import(`${B}/errors.mjs`);
const overview = await import(`${B}/overviewCrs.mjs`);

// Shaped like @openshift-console/dynamic-plugin-sdk HttpError (code, message, response, json).
class HttpError extends Error {
  constructor(message, code, response, json) {
    super(message);
    this.name = 'HttpError';
    this.code = code;
    this.response = response;
    this.json = json;
  }
}
const kstatus = (code, reason, message, causes) => ({ kind: 'Status', apiVersion: 'v1', status: 'Failure', code, reason, message, ...(causes ? { details: { causes } } : {}) });
const throwing = (err) => async () => { throw err; };
const headers = (warning) => new Headers(warning ? { warning } : {});

test('a thrown 403 keeps its status, reason and message, and fires the forbidden listeners', async () => {
  let fired = 0;
  const off = k8s.addForbiddenListener(() => { fired += 1; });
  k8s.configureK8sClient({ baseUrl: '/api/kubernetes', apiStyle: 'raw', fetchFn: throwing(new HttpError('Access denied due to cluster policy.', 403, { headers: headers() }, kstatus(403, 'Forbidden', 'cloudvirts is forbidden: User "u" cannot list'))) });
  await assert.rejects(k8s.k8sFetchJson('/api/kubernetes/apis/x', undefined, { kind: 'CloudVirt', verb: 'list' }), (err) => {
    assert.equal(err.status, 403);
    assert.equal(err.reason, 'Forbidden');
    assert.match(err.detail, /cannot list/);
    assert.equal(err.kind, 'CloudVirt');
    assert.equal(errors.classifyError(err), 'forbidden');
    return true;
  });
  assert.equal(fired, 1);
  off();
});

test('a thrown 422 keeps the field causes and the Warning headers', async () => {
  const causes = [{ field: 'spec.size', message: 'Unsupported value', reason: 'FieldValueNotSupported' }];
  k8s.configureK8sClient({ fetchFn: throwing(new HttpError('Error "Unsupported value" for field "spec.size".', 422, { headers: headers('299 - "unknown field spec.x"') }, kstatus(422, 'Invalid', 'X "n" is invalid', causes))) });
  await assert.rejects(k8s.k8sFetchJson('/api/kubernetes/apis/x', { method: 'POST' }), (err) => {
    assert.equal(err.status, 422);
    assert.equal(err.reason, 'Invalid');
    assert.deepEqual(err.causes, causes);
    assert.deepEqual(err.warnings, ['unknown field spec.x']);
    assert.equal(errors.classifyError(err), 'invalid');
    return true;
  });
});

test('thrown 404 and 409 classify as notFound and conflict; a non-JSON answer keeps its status', async () => {
  for (const [code, reason, cls] of [[404, 'NotFound', 'notFound'], [409, 'Conflict', 'conflict'], [409, 'AlreadyExists', 'invalid']]) {
    k8s.configureK8sClient({ fetchFn: throwing(new HttpError('x', code, { headers: headers() }, kstatus(code, reason, `upstream ${code}`))) });
    await assert.rejects(k8s.k8sFetchJson('/api/kubernetes/apis/x'), (err) => err.status === code && errors.classifyError(err) === cls);
  }
  k8s.configureK8sClient({ fetchFn: throwing(new HttpError('Service Unavailable', 503, { headers: headers() })) });
  await assert.rejects(k8s.k8sFetchJson('/api/kubernetes/apis/x'), (err) => err.status === 503 && err.detail === 'Service Unavailable');
});

test('transport failures and timeouts stay status 0 (unavailable) and fire no forbidden listener', async () => {
  let fired = 0;
  const off = k8s.addForbiddenListener(() => { fired += 1; });
  const timeout = Object.assign(new Error('Call to /x timed out after 60000ms.'), { name: 'TimeoutError', url: '/x', ms: 60000 });
  const abort = new DOMException('aborted', 'AbortError');
  const node = Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' });
  for (const e of [new TypeError('Failed to fetch'), timeout, abort, node]) {
    k8s.configureK8sClient({ fetchFn: throwing(e) });
    await assert.rejects(k8s.k8sFetchJson('/api/kubernetes/apis/x'), (err) => err.status === 0 && errors.classifyError(err) === 'unavailable');
  }
  assert.equal(fired, 0);
  off();
});

test('a Response-returning fetch still works (dashboards)', async () => {
  k8s.configureK8sClient({ baseUrl: '/api/k8s', fetchFn: async () => new Response(JSON.stringify(kstatus(403, 'Forbidden', 'no')), { status: 403, headers: { 'content-type': 'application/json' } }) });
  await assert.rejects(k8s.k8sFetchJson('/api/k8s/apis/x'), (err) => err.status === 403 && err.reason === 'Forbidden');
  k8s.configureK8sClient({ fetchFn: async () => new Response(JSON.stringify({ items: [] }), { status: 200 }) });
  assert.deepEqual(await k8s.k8sFetchJson('/api/k8s/apis/x'), { items: [] });
  k8s.configureK8sClient({ fetchFn: undefined });
});

test('admin overview: thrown 403 counts the kind as forbidden, thrown 404 as empty, without retries', async () => {
  const seen = [];
  const fetchFn = async (url) => {
    seen.push(url);
    if (url.includes('/hybridfabrics')) throw new HttpError('Not Found', 404, { headers: headers() }, kstatus(404, 'NotFound', 'nope'));
    throw new HttpError('Access denied due to cluster policy.', 403, { headers: headers() }, kstatus(403, 'Forbidden', 'forbidden'));
  };
  const started = Date.now();
  const out = await overview.fetchOverviewCRs(fetchFn, '/api/kubernetes', {});
  assert.ok(Date.now() - started < 300, 'a final answer is not retried');
  assert.ok(out.forbiddenKinds.includes('Entity'));
  assert.ok(seen.some((u) => u.includes('/hybridfabrics')));
  assert.ok(!out.forbiddenKinds.includes('HybridFabric'));
  assert.equal(seen.filter((u) => u.includes('/entities')).length, 1);
});
