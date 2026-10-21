// Tenant dashboard write options: Strict field validation on platform writes,
// dryRun passthrough and Warning header forwarding (writeOptions.js).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const root = process.env.UI_ROOT;
const wo = await import(path.join(root, 'packages/tenant-dashboard/server/writeOptions.js'));

function fakeRes() {
  const headers = {};
  return {
    headers,
    headersSent: false,
    setHeader(k, v) {
      headers[k] = v;
    },
    json(body) {
      this.body = body;
      this.headersSent = true;
      return this;
    },
  };
}

function inRequest(query, fn) {
  const req = { query };
  const res = fakeRes();
  return new Promise((resolve) => {
    wo.writeOptionsMiddleware(req, res, async () => {
      await fn(res);
      resolve(res);
    });
  });
}

test('platform writes carry fieldValidation=Strict; reads and other APIs do not', async () => {
  await inRequest({}, () => {
    assert.deepEqual(wo.upstreamWriteQuery('POST', '/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/rbacs'), { fieldValidation: 'Strict' });
    assert.deepEqual(wo.upstreamWriteQuery('PATCH', '/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/rbacs/a'), { fieldValidation: 'Strict' });
    assert.equal(wo.upstreamWriteQuery('GET', '/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/rbacs'), undefined);
    assert.equal(wo.upstreamWriteQuery('POST', '/api/v1/namespaces/e/secrets'), undefined);
  });
});

test('a dry-run request sends dryRun=All on every write and marks the answer', async () => {
  const res = await inRequest({ dryRun: 'All' }, (r) => {
    assert.deepEqual(wo.upstreamWriteQuery('POST', '/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/rbacs'), {
      fieldValidation: 'Strict',
      dryRun: 'All',
    });
    assert.deepEqual(wo.upstreamWriteQuery('DELETE', '/apis/hybridsovereign.redhat/v1alpha1/namespaces/e/rbacs/a'), { dryRun: 'All' });
    assert.ok(wo.currentIsDryRun());
    r.json({ ok: true });
  });
  assert.equal(res.headers['X-Dry-Run'], 'All');
});

test('upstream warnings are returned as Warning headers', async () => {
  const res = await inRequest({}, async (r) => {
    await Promise.resolve();
    wo.recordUpstreamWarnings(['unknown field "spec.foo"', 'unknown field "spec.foo"', 'deprecated: "x"']);
    r.json({});
  });
  assert.equal(res.headers.Warning, '299 - "unknown field \\"spec.foo\\"", 299 - "deprecated: \\"x\\""');
  assert.equal(wo.currentIsDryRun(), false, 'no context outside a request');
});
