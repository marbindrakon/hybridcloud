// The platform-health banner of the admin pages, rendered on the server (react-dom/server) from
// ConfigMap data shaped as the operations layer writes it (one key <cluster>.json per cluster),
// and its read through the API path with a fake fetch.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const load = (l) => JSON.parse(fs.readFileSync(path.join(UI, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const en = load('en');
const fr = load('fr');
const P = en.platformHealth;

const entry = `import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { PlatformHealthBannerView, readPlatformHealth } from './components/PlatformHealthBanner';
import { configureK8sClient } from './hooks/k8s';
export * from './status/platformHealth';
export const banner = (data, nowMs) => renderToStaticMarkup(createElement(PlatformHealthBannerView, { data, nowMs }));
export { readPlatformHealth, configureK8sClient };`;
const bundle = path.join(OUT, 'platformHealthBanner.mjs');
execFileSync(path.join(UI, 'node_modules/.bin/esbuild'), [
  '--bundle', '--format=esm', '--platform=node', '--log-level=error', '--jsx=automatic', '--loader=tsx',
  '--loader:.json=json', '--loader:.css=empty', '--loader:.svg=dataurl', '--define:process.env.NODE_ENV="production"',
  "--banner:js=import { createRequire as __shcRequire } from 'module'; const require = __shcRequire(import.meta.url);",
  '--sourcefile=entry.tsx', `--outfile=${bundle}`,
], { cwd: path.join(UI, 'packages/shared/src'), input: entry });
const m = await import(bundle);

function text(html) {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim();
}
const fill = (s, vars) => s.replace(/\{\{(\w+)\}\}/g, (_x, k) => String(vars[k]));

const NOW = Date.parse('2030-01-01T12:00:00Z');
const at = (minAgo) => new Date(NOW - minAgo * 60000).toISOString();

function report(cluster, faults = [], { writtenMinAgo = 1, intervalSeconds = 300, alertsDelivered = null } = {}) {
  return JSON.stringify({ schemaVersion: 1, cluster, writtenAt: at(writtenMinAgo), intervalSeconds, faults, alertsDelivered });
}
const fault = (id, severity, extra = {}) => ({ id, severity, since: at(30), object: 'ops/check', summary: `summary of ${id}`, details: [], ...extra });

// Every id the platform's alert rules define today (Shc* alerts of the cluster base chart).
function alertIds() {
  const dir = path.join(UI, '..', 'charts/charts/shc-cluster-base/templates');
  const ids = new Set();
  for (const f of ['_platform-health.tpl', 'ops.yaml', '_fabric-health.tpl']) {
    const p = path.join(dir, f);
    if (!fs.existsSync(p)) continue;
    for (const mm of fs.readFileSync(p, 'utf8').matchAll(/alert:\s*(Shc\w+)/g)) ids.add(mm[1]);
  }
  return [...ids];
}

test('none: nothing is shown when every report is fresh and has no fault', () => {
  assert.equal(m.banner({ 'hub.json': report('hub'), 'services.json': report('services') }, NOW), '');
  assert.equal(m.banner({}, NOW), '');
});

test('absent or unreadable object: nothing is shown', () => {
  assert.equal(m.banner(null, NOW), '');
  assert.equal(m.banner(undefined, NOW), '');
  assert.equal(m.platformHealthData({ kind: 'Status', code: 403 }), null);
  assert.equal(m.platformHealthData('not an object'), null);
});

test('one warning: the product sentence, the details, since when and the cluster', () => {
  const html = m.banner({
    'hub.json': report('hub', [fault('ShcVolumeFilling', 'warning', { details: ['database volume 85 % used'] })]),
  }, NOW);
  const s = text(html);
  assert.match(s, new RegExp(P.fault.ShcVolumeFilling.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  assert.ok(!s.includes('summary of ShcVolumeFilling'), 'the summary is not shown when the id has a sentence');
  assert.ok(s.includes('database volume 85 % used'));
  assert.ok(s.includes(fill(P.onCluster, { cluster: 'hub' })));
  assert.ok(s.includes(P.since.replace('{{time}}', '').trim()));
  assert.match(html, /pf-m-warning/);
  assert.doesNotMatch(html, /pf-m-danger/);
});

test('one critical: shown as danger and names the activation from the details', () => {
  const html = m.banner({
    'hub.json': report('hub', [fault('ShcEventActivationsNotRunning', 'critical', { details: ['activation acme-events: failed, not restarted'] })]),
  }, NOW);
  assert.match(html, /pf-m-danger/);
  const s = text(html);
  assert.ok(s.includes(P.fault.ShcEventActivationsNotRunning));
  assert.ok(s.includes('activation acme-events: failed, not restarted'));
});

test('several: critical before warning before info, across clusters', () => {
  const html = m.banner({
    'services.json': report('services', [fault('ShcBackupStale', 'warning'), fault('ShcFabricNodesNotRead', 'info')]),
    'hub.json': report('hub', [fault('ShcAutomationAPIUnavailable', 'critical'), fault('ShcVolumeFilling', 'warning')]),
  }, NOW);
  const ids = [...html.matchAll(/data-fault-id="(\w+)"/g)].map((x) => x[1]);
  assert.deepEqual(ids.slice(0, 1), ['ShcAutomationAPIUnavailable']);
  assert.equal(ids.length, 4);
  assert.equal(ids[3], 'ShcFabricNodesNotRead');
  const s = text(html);
  assert.ok(s.includes(fill(P.onCluster, { cluster: 'hub' })));
  assert.ok(s.includes(fill(P.onCluster, { cluster: 'services' })));
});

test('an unknown id: the alert summary is shown; without a summary, the id', () => {
  const s = text(m.banner({ 'hub.json': report('hub', [fault('ShcSomethingNew', 'warning', { summary: 'Something new is wrong' })]) }, NOW));
  assert.ok(s.includes('Something new is wrong'));
  const s2 = text(m.banner({ 'hub.json': report('hub', [fault('ShcSomethingNew', 'warning', { summary: undefined })]) }, NOW));
  assert.ok(s2.includes(fill(P.unknownFault, { id: 'ShcSomethingNew' })));
});

test('a late writer: "no report since" after three intervals, not before', () => {
  const late = m.banner({ 'hub.json': report('hub', [], { writtenMinAgo: 16, intervalSeconds: 300 }) }, NOW);
  assert.match(late, /data-late-report="hub"/);
  assert.ok(text(late).includes(P.lateReport.split('{{cluster}}')[0].trim()));
  assert.ok(text(late).includes('hub'));
  assert.equal(m.banner({ 'hub.json': report('hub', [], { writtenMinAgo: 14, intervalSeconds: 300 }) }, NOW), '');
  // A late report's last faults still show, after the late notice of a critical-free banner.
  const both = m.banner({ 'hub.json': report('hub', [fault('ShcBackupStale', 'warning')], { writtenMinAgo: 60 }) }, NOW);
  assert.ok(both.indexOf('data-late-report') < both.indexOf('data-fault-id'));
});

test('a malformed key is left out; the other keys still show', () => {
  const data = {
    'broken.json': '{not json',
    'old.json': JSON.stringify({ schemaVersion: 2, cluster: 'old', writtenAt: at(1), intervalSeconds: 300, faults: [] }),
    'notime.json': JSON.stringify({ schemaVersion: 1, cluster: 'x', intervalSeconds: 300, faults: [fault('ShcBackupStale', 'warning')] }),
    'badfaults.json': JSON.stringify({ schemaVersion: 1, cluster: 'y', writtenAt: at(1), intervalSeconds: 300, faults: 'many' }),
    'hub.json': report('hub', [fault('ShcVaultPodsNotReady', 'critical'), { severity: 'critical' }, 'junk']),
  };
  const view = m.platformHealthView(data, NOW);
  assert.deepEqual(view.malformed.sort(), ['badfaults.json', 'broken.json', 'notime.json', 'old.json']);
  assert.equal(view.faults.length, 1);
  const s = text(m.banner(data, NOW));
  assert.ok(s.includes(P.fault.ShcVaultPodsNotReady));
  assert.ok(!s.includes('broken'));
});

test('alerts that reach no receiver show only when the report says false', () => {
  assert.equal(m.banner({ 'hub.json': report('hub', [], { alertsDelivered: null }) }, NOW), '');
  assert.equal(m.banner({ 'hub.json': report('hub', [], { alertsDelivered: true }) }, NOW), '');
  const s = text(m.banner({ 'hub.json': report('hub', [], { alertsDelivered: false }) }, NOW));
  assert.ok(s.includes(fill(P.alertsNotDelivered, { cluster: 'hub' })));
});

test('every Shc alert id has a sentence in English and French', () => {
  const ids = alertIds();
  assert.ok(ids.length >= 22, `found ${ids.length} alert ids`);
  for (const id of ids) {
    assert.equal(typeof en.platformHealth.fault[id], 'string', `en platformHealth.fault.${id}`);
    assert.equal(typeof fr.platformHealth.fault[id], 'string', `fr platformHealth.fault.${id}`);
  }
  assert.deepEqual(Object.keys(fr.platformHealth.fault).sort(), Object.keys(en.platformHealth.fault).sort());
  const leaves = (o) => Object.keys(o).filter((k) => typeof o[k] === 'string').sort();
  assert.deepEqual(leaves(fr.platformHealth), leaves(en.platformHealth));
});

test('the sentences name no internals', () => {
  for (const [id, s] of Object.entries(en.platformHealth.fault)) {
    assert.doesNotMatch(s, /\b(oc|pod|pods|CronJob|Job|namespace|etcd|Keycloak|Vault|Prometheus|PVC|APIService|FRR|BGP|EDA)\b/, id);
  }
});

test('the read: the ConfigMap through the configured API base; any failure is null', async () => {
  const calls = [];
  const answer = (status, body) => async (url) => {
    calls.push(url);
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  m.configureK8sClient({ baseUrl: '/api/kubernetes', fetchFn: answer(200, { kind: 'ConfigMap', data: { 'hub.json': report('hub') } }) });
  const d = await m.readPlatformHealth();
  assert.deepEqual(Object.keys(d), ['hub.json']);
  assert.equal(calls[0], '/api/kubernetes/api/v1/namespaces/sovereign-cloud/configmaps/shc-platform-health');
  m.configureK8sClient({ baseUrl: '/api/k8s', fetchFn: answer(404, { kind: 'Status', code: 404 }) });
  assert.equal(await m.readPlatformHealth(), null);
  m.configureK8sClient({ fetchFn: answer(403, { kind: 'Status', code: 403 }) });
  assert.equal(await m.readPlatformHealth(), null);
  m.configureK8sClient({ fetchFn: async () => { throw new Error('console fetch refused'); } });
  assert.equal(await m.readPlatformHealth(), null);
  m.configureK8sClient({ fetchFn: answer(200, { kind: 'ConfigMap' }) });
  assert.equal(await m.readPlatformHealth(), null);
});
