// The platform-health banner of the admin pages, rendered on the server (react-dom/server) from
// what the reader reads (the index shc-platform-health with clusters.json, and per listed cluster
// the ConfigMap shc-platform-health-<cluster> with report.json, as the operations layer writes it),
// and its reads through the API path with a fake fetch.
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
export const banner = (read, nowMs) => renderToStaticMarkup(createElement(PlatformHealthBannerView, { read, nowMs }));
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
// What the reader reads: every cluster of the index with its report's ConfigMap data.
const ok = (value) => ({ state: 'read', data: value === undefined ? {} : { 'report.json': value } });
function read(reports) {
  return { clusters: Object.keys(reports), reports };
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
  assert.equal(m.banner(read({ hub: ok(report('hub')), services: ok(report('services')) }), NOW), '');
  assert.equal(m.banner(read({}), NOW), '');
});

test('index absent or refused (reporting off, no read): nothing is shown', () => {
  assert.equal(m.banner(null, NOW), '');
  assert.equal(m.banner(undefined, NOW), '');
});

test('index answered with an error: "platform health cannot be read", not the silence of reporting off', () => {
  const html = m.banner({ clusters: null, reports: {}, indexFailed: true }, NOW);
  assert.match(html, /data-index-failed="true"/);
  assert.ok(text(html).includes(P.indexFailed));
  assert.doesNotMatch(html, /data-index-unreadable/);
});

test('index answered with a list that cannot be read: one notice, nothing dropped silently', () => {
  for (const data of [{}, { 'clusters.json': '{not json' }, { 'clusters.json': '{"hub": 1}' },
    { 'clusters.json': '["Hub"]' }, { 'clusters.json': '[1]' }, { other: '[]' }]) {
    assert.equal(m.parseIndex(data), null, JSON.stringify(data));
  }
  assert.deepEqual(m.parseIndex({ 'clusters.json': '["services","hub","hub"]' }), ['services', 'hub']);
  const html = m.banner({ clusters: null, reports: {} }, NOW);
  assert.match(html, /data-index-unreadable="true"/);
  assert.ok(text(html).includes(P.unreadableIndex));
});

test('one warning: the product sentence, the details, since when and the cluster', () => {
  const html = m.banner(read({
    hub: ok(report('hub', [fault('ShcVolumeFilling', 'warning', { details: ['database volume 85 % used'] })])),
  }), NOW);
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
  const html = m.banner(read({
    hub: ok(report('hub', [fault('ShcEventActivationsNotRunning', 'critical', { details: ['activation acme-events: failed, not restarted'] })])),
  }), NOW);
  assert.match(html, /pf-m-danger/);
  const s = text(html);
  assert.ok(s.includes(P.fault.ShcEventActivationsNotRunning));
  assert.ok(s.includes('activation acme-events: failed, not restarted'));
});

test('several: critical before warning before info, across clusters', () => {
  const html = m.banner(read({
    services: ok(report('services', [fault('ShcBackupStale', 'warning'), fault('ShcFabricNodesNotRead', 'info')])),
    hub: ok(report('hub', [fault('ShcAutomationAPIUnavailable', 'critical'), fault('ShcVolumeFilling', 'warning')])),
  }), NOW);
  const ids = [...html.matchAll(/data-fault-id="(\w+)"/g)].map((x) => x[1]);
  assert.deepEqual(ids.slice(0, 1), ['ShcAutomationAPIUnavailable']);
  assert.equal(ids.length, 4);
  assert.equal(ids[3], 'ShcFabricNodesNotRead');
  const s = text(html);
  assert.ok(s.includes(fill(P.onCluster, { cluster: 'hub' })));
  assert.ok(s.includes(fill(P.onCluster, { cluster: 'services' })));
});

test('an unknown id: the alert summary is shown; without a summary, the id', () => {
  const s = text(m.banner(read({ hub: ok(report('hub', [fault('ShcSomethingNew', 'warning', { summary: 'Something new is wrong' })])) }), NOW));
  assert.ok(s.includes('Something new is wrong'));
  const s2 = text(m.banner(read({ hub: ok(report('hub', [fault('ShcSomethingNew', 'warning', { summary: undefined })])) }), NOW));
  assert.ok(s2.includes(fill(P.unknownFault, { id: 'ShcSomethingNew' })));
});

// Every state of one cluster's report: what the banner shows for it (other clusters are unaffected).
const CREATED = at(90);
const STATES = [
  // [state, report read, shown as]
  ['fresh, no fault', ok(report('hub')), 'nothing'],
  ['fresh, a fault', ok(report('hub', [fault('ShcVolumeFilling', 'warning')])), 'fault'],
  ['written 14 min ago, interval 5 min', ok(report('hub', [], { writtenMinAgo: 14 })), 'nothing'],
  ['written 16 min ago, interval 5 min: late', ok(report('hub', [], { writtenMinAgo: 16 })), 'late'],
  ['late, with its last fault', ok(report('hub', [fault('ShcBackupStale', 'warning')], { writtenMinAgo: 60 })), 'late+fault'],
  ['written 9 min in the future (clock skew)', ok(report('hub', [], { writtenMinAgo: -9 })), 'nothing'],
  ['written 11 min in the future', ok(report('hub', [fault('ShcBackupStale', 'warning')], { writtenMinAgo: -11 })), 'unreadable'],
  ['never written, ConfigMap made at a known time', { state: 'read', data: {}, createdAt: CREATED }, 'never-since'],
  ['never written, ConfigMap without data', { state: 'read', data: null }, 'never'],
  ['never written, empty value', ok(''), 'never'],
  ['ConfigMap absent', { state: 'absent' }, 'never'],
  ['not fetched (refused, failed)', { state: 'unreadable' }, 'unreadable'],
  ['not JSON', ok('{not json'), 'unreadable'],
  ['another schemaVersion', ok(JSON.stringify({ schemaVersion: 2, cluster: 'hub', writtenAt: at(1), intervalSeconds: 300, faults: [] })), 'unreadable'],
  ['another cluster\'s name', ok(report('services', [fault('ShcBackupStale', 'warning')])), 'unreadable'],
  ['no writtenAt', ok(JSON.stringify({ schemaVersion: 1, cluster: 'hub', intervalSeconds: 300, faults: [] })), 'unreadable'],
  ['no interval', ok(JSON.stringify({ schemaVersion: 1, cluster: 'hub', writtenAt: at(1), faults: [] })), 'unreadable'],
  ['faults not a list', ok(JSON.stringify({ schemaVersion: 1, cluster: 'hub', writtenAt: at(1), intervalSeconds: 300, faults: 'many' })), 'unreadable'],
  ['missing from the reads', undefined, 'unreadable'],
  // the report's own interval counts only up to the longest believed one (default five minutes)
  ['interval claimed 1e9 s, written 16 min ago: late', ok(report('hub', [], { writtenMinAgo: 16, intervalSeconds: 1e9 })), 'late'],
  ['interval claimed 1e9 s, written 14 min ago', ok(report('hub', [], { writtenMinAgo: 14, intervalSeconds: 1e9 })), 'nothing'],
  ['interval 10 min, written 16 min ago, default ceiling: late', ok(report('hub', [], { writtenMinAgo: 16, intervalSeconds: 600 })), 'late'],
  ['interval 1 min, written 4 min ago: late', ok(report('hub', [], { writtenMinAgo: 4, intervalSeconds: 60 })), 'late'],
];

test('every state of a cluster\'s report shows as the ruling says, beside a healthy cluster', () => {
  for (const [name, r, want] of STATES) {
    const reads = { hub: r, services: ok(report('services', [fault('ShcVaultPodsNotReady', 'critical')])) };
    const html = m.banner(read(reads), NOW);
    const s = text(html);
    const shows = {
      fault: /data-fault-id="Shc(VolumeFilling|BackupStale)"/.test(html),
      late: /data-late-report="hub"/.test(html),
      never: /data-never-written="hub"/.test(html),
      unreadable: /data-unreadable-report="hub"/.test(html),
    };
    const expected = {
      fault: want === 'fault' || want === 'late+fault',
      late: want === 'late' || want === 'late+fault',
      never: want === 'never' || want === 'never-since',
      unreadable: want === 'unreadable',
    };
    assert.deepEqual(shows, expected, name);
    // the other cluster's critical fault always shows, first
    assert.match(html, /data-fault-id="ShcVaultPodsNotReady"/, name);
    if (want === 'never-since') assert.ok(s.includes(P.neverWritten.split('{{cluster}}')[0].trim()) && !s.includes(fill(P.neverWrittenNoTime, { cluster: 'hub' })), name);
    if (want === 'never') assert.ok(s.includes(fill(P.neverWrittenNoTime, { cluster: 'hub' })), name);
    if (want === 'unreadable') assert.ok(s.includes(fill(P.unreadableReport, { cluster: 'hub' })), name);
    if (want === 'late+fault') assert.ok(html.indexOf('data-late-report') < html.indexOf('data-fault-id="ShcBackupStale"'), name);
  }
});

test('the longest believed interval comes from the dashboard\'s setting', () => {
  const r = (writtenMinAgo, intervalSeconds, maxIntervalSeconds) => ({
    clusters: ['hub'], reports: { hub: ok(report('hub', [], { writtenMinAgo, intervalSeconds })) },
    ...(maxIntervalSeconds === undefined ? {} : { maxIntervalSeconds }),
  });
  const late = (x) => /data-late-report="hub"/.test(m.banner(x, NOW));
  assert.equal(m.DEFAULT_MAX_INTERVAL_SECONDS, 300);
  // a 30-minute writer with the setting at 30 minutes: late only after 90 minutes
  assert.equal(late(r(60, 1800, 1800)), false);
  assert.equal(late(r(91, 1800, 1800)), true);
  // the same writer with the default: late after 15 minutes
  assert.equal(late(r(16, 1800)), true);
  // a forged interval with the setting at 30 minutes: late after 90 minutes, not never
  assert.equal(late(r(91, 1e9, 1800)), true);
  // a five-minute writer is late after fifteen whatever the setting
  assert.equal(late(r(16, 300, 1800)), true);
  assert.equal(late(r(14, 300, 1800)), false);
});

test('a fault without an id is dropped, the report still shows', () => {
  const view = m.platformHealthView(read({ hub: ok(report('hub', [fault('ShcVaultPodsNotReady', 'critical'), { severity: 'critical' }, 'junk'])) }), NOW);
  assert.equal(view.faults.length, 1);
  assert.deepEqual(view.unreadable, []);
});

test('alerts that reach no receiver show only when the report says false', () => {
  assert.equal(m.banner(read({ hub: ok(report('hub', [], { alertsDelivered: null })) }), NOW), '');
  assert.equal(m.banner(read({ hub: ok(report('hub', [], { alertsDelivered: true })) }), NOW), '');
  const s = text(m.banner(read({ hub: ok(report('hub', [], { alertsDelivered: false })) }), NOW));
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

test('the read: the index, then each listed report, through the configured API base and namespace', async () => {
  const base = '/api/kubernetes/api/v1/namespaces/acme-platform/configmaps/';
  const SETTINGS = { namespace: 'acme-platform', maxReportIntervalMinutes: 30 };
  const answers = (table) => {
    const calls = [];
    const fn = async (url) => {
      calls.push(url);
      const a = table[url.slice(url.lastIndexOf('/') + 1)];
      if (a instanceof Error) throw a;
      const [status, body] = a || [404, { kind: 'Status', code: 404 }];
      return { ok: status >= 200 && status < 300, status, json: async () => body };
    };
    return { calls, fn };
  };
  const cm = (data, extra = {}) => [200, { kind: 'ConfigMap', metadata: { creationTimestamp: CREATED }, data, ...extra }];
  const { calls, fn } = answers({
    'shc-platform-health': cm({ 'clusters.json': '["services","hub","edge","far"]' }),
    'shc-platform-health-services': cm({ 'report.json': report('services') }),
    'shc-platform-health-hub': [403, { kind: 'Status', code: 403 }],
    'shc-platform-health-far': new Error('console fetch refused'),
  });
  m.configureK8sClient({ baseUrl: '/api/kubernetes', fetchFn: fn });
  const r = await m.readPlatformHealth(SETTINGS);
  assert.equal(calls[0], `${base}shc-platform-health`);
  assert.equal(r.maxIntervalSeconds, 1800);
  assert.deepEqual(calls.slice(1).sort(), ['edge', 'far', 'hub', 'services'].map((c) => `${base}shc-platform-health-${c}`));
  assert.deepEqual(r.clusters, ['services', 'hub', 'edge', 'far']);
  assert.equal(r.reports.services.state, 'read');
  assert.equal(r.reports.services.createdAt, CREATED);
  assert.deepEqual(r.reports.hub, { state: 'unreadable' });
  assert.deepEqual(r.reports.edge, { state: 'absent' });
  assert.deepEqual(r.reports.far, { state: 'unreadable' });
  const view = m.platformHealthView(r, NOW);
  assert.deepEqual(view.unreadable, ['far', 'hub']);
  assert.deepEqual(view.neverWritten, [{ cluster: 'edge' }]);

  // every answer to the index: absent, refused or no session: nothing; an error or a failed read: indexFailed
  // (and no report is read)
  const INDEX_STATES = [
    ['absent (reporting off)', [404, { kind: 'Status', code: 404 }], 'nothing'],
    ['refused (no read)', [403, { kind: 'Status', code: 403 }], 'nothing'],
    ['no session', [401, { kind: 'Status', code: 401 }], 'nothing'],
    ['server error', [500, { kind: 'Status', code: 500 }], 'failed'],
    ['unavailable', [503, { kind: 'Status', code: 503 }], 'failed'],
    ['too many requests', [429, { kind: 'Status', code: 429 }], 'failed'],
    ['read failed (network)', new Error('down'), 'failed'],
  ];
  for (const [name, idx, want] of INDEX_STATES) {
    const a = answers({ 'shc-platform-health': idx });
    m.configureK8sClient({ baseUrl: '/api/k8s', fetchFn: a.fn });
    const got = await m.readPlatformHealth({ namespace: 'acme-platform' });
    if (want === 'nothing') assert.equal(got, null, name);
    else {
      assert.deepEqual(got, { clusters: null, reports: {}, indexFailed: true }, name);
      assert.match(m.banner(got, NOW), /data-index-failed="true"/, name);
    }
    assert.equal(a.calls.length, 1, name);
  }
  // no platform namespace in the dashboard's settings (they could not be read): indexFailed, nothing fetched
  const none = answers({});
  m.configureK8sClient({ fetchFn: none.fn });
  assert.deepEqual(await m.readPlatformHealth({}), { clusters: null, reports: {}, indexFailed: true });
  assert.equal(none.calls.length, 0);
  // the index answered without a readable list: the notice, no report read
  const bad = answers({ 'shc-platform-health': cm({ 'clusters.json': 'hub,services' }) });
  m.configureK8sClient({ fetchFn: bad.fn });
  assert.deepEqual(await m.readPlatformHealth({ namespace: 'acme-platform' }), { clusters: null, reports: {} });
  assert.equal(bad.calls.length, 1);
  // a report answered with something that is not a ConfigMap cannot be read
  assert.deepEqual(m.reportReadOf({ kind: 'Status' }), { state: 'unreadable' });
});

// The dashboard chart: the platform namespace and the longest believed report interval reach the server.
const chart = path.join(UI, '..', 'charts/charts/sovereign-cloud-dashboard');
function helmAvailable() {
  try {
    execFileSync('helm', ['version', '--short'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}
const render = (sets = []) =>
  execFileSync('helm', ['template', 't', chart, ...sets.flatMap((x) => ['--set', x])], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

test('chart: the namespace and the longest report interval (5 minutes by default) reach the server', { skip: helmAvailable() ? false : 'helm not installed' }, () => {
  const values = fs.readFileSync(path.join(chart, 'values.yaml'), 'utf8');
  assert.match(values, /^platformHealthMaxReportIntervalMinutes: 5$/m);
  const out = render(['namespace=acme-platform']);
  assert.match(out, /- name: SHC_PLATFORM_NAMESPACE\n\s+value: "acme-platform"/);
  assert.match(out, /- name: SHC_PLATFORM_HEALTH_MAX_REPORT_INTERVAL_MINUTES\n\s+value: "5"/);
  assert.match(render(['platformHealthMaxReportIntervalMinutes=30']), /SHC_PLATFORM_HEALTH_MAX_REPORT_INTERVAL_MINUTES\n\s+value: "30"/);
  for (const bad of ['0', '31', '2.5']) {
    assert.throws(() => render([`platformHealthMaxReportIntervalMinutes=${bad}`]), /must be a whole number of minutes from 1 to 30/, bad);
  }
  assert.throws(() => render(['namespace=']), /namespace is required/);
});
