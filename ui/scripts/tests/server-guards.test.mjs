// Guards on the dashboard servers: no upstream status mapped to 502, no service-account token use.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const serverFiles = ['tenant-dashboard', 'admin-dashboard', 'shared']
  .flatMap((p) => {
    const dir = path.join(UI, 'packages', p, 'server');
    return fs.existsSync(dir) ? fs.readdirSync(dir).filter((f) => f.endsWith('.js')).map((f) => path.join(dir, f)) : [];
  });

test('only the shared error helpers answer 502', () => {
  const allowed = new Set(['errors.js', 'apiK8sProxy.js']);
  for (const f of serverFiles) {
    if (allowed.has(path.basename(f))) continue;
    const text = fs.readFileSync(f, 'utf8');
    assert.ok(!/status\(502\)/.test(text), `${f} maps an answer to 502 itself; use sendUpstreamError`);
  }
});

test('no server reads the pod service-account token', () => {
  for (const f of serverFiles) {
    const text = fs.readFileSync(f, 'utf8');
    assert.ok(!/serviceaccount\/token|getSAToken|central-reader/.test(text), `${f} uses a service-account or central reader token`);
  }
});

test('the admin server gates every /api route on the platform review', () => {
  const text = fs.readFileSync(path.join(UI, 'packages/admin-dashboard/server/index.js'), 'utf8');
  const gate = text.indexOf('app.use("/api/", requireAuth, requirePlatform)');
  assert.ok(gate > 0, 'requirePlatform not mounted on /api/');
  const firstRoute = text.search(/app\.(get|post|put|patch|delete|use)\("\/api\/(?!",)/);
  assert.ok(firstRoute === -1 || firstRoute >= gate, 'an /api route is registered before the platform gate');
});
