// Session end and /api/ui-config (shared/server/session.js).
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';

const UI = process.env.UI_ROOT;
const s = await import(`${UI}/packages/shared/server/session.js`);
const express = createRequire(`${UI}/package.json`)('express');

test('token object name is sha256~ + base64url(sha256(body))', () => {
  const body = 'abcDEF123';
  const want = `sha256~${crypto.createHash('sha256').update(body).digest('base64url')}`;
  assert.equal(s.oauthTokenObjectName(`sha256~${body}`), want);
  assert.ok(!want.includes('='));
});

test('ui-config takes only https URLs from env, empty by default', () => {
  assert.deepEqual(s.uiConfigFromEnv({}), { helpBaseUrl: '', consoleUrl: '' });
  assert.deepEqual(s.uiConfigFromEnv({ SHC_HELP_BASE_URL: 'https://docs.example/docs/', SHC_CONSOLE_URL: 'javascript:alert(1)' }), { helpBaseUrl: 'https://docs.example/docs', consoleUrl: '' });
});

let api; let app; let base; const deleted = [];
before(async () => {
  api = http.createServer((req, res) => {
    deleted.push({ method: req.method, url: req.url, auth: req.headers.authorization });
    res.statusCode = 200;
    res.setHeader('Content-Type', 'application/json');
    res.end('{}');
  });
  await new Promise((r) => api.listen(0, '127.0.0.1', r));
  const e = express();
  s.mountSessionRoutes(e, { apiServer: `http://127.0.0.1:${api.address().port}` });
  await new Promise((r) => { app = e.listen(0, '127.0.0.1', r); });
  base = `http://127.0.0.1:${app.address().port}`;
});
after(() => { api.close(); app.close(); });

test('POST /api/session/end revokes the caller token with that token', async () => {
  const tok = 'sha256~secretpart';
  const r = await fetch(`${base}/api/session/end`, { method: 'POST', headers: { 'x-forwarded-access-token': tok } });
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { redirect: '/oauth/sign_out' });
  assert.equal(deleted.length, 1);
  assert.equal(deleted[0].method, 'DELETE');
  assert.equal(deleted[0].url, `/apis/oauth.openshift.io/v1/useroauthaccesstokens/${encodeURIComponent(s.oauthTokenObjectName(tok))}`);
  assert.equal(deleted[0].auth, `Bearer ${tok}`);
  assert.ok(!deleted[0].url.includes('secretpart'));
});

test('POST /api/session/end without a token is 401', async () => {
  assert.equal((await fetch(`${base}/api/session/end`, { method: 'POST' })).status, 401);
});
