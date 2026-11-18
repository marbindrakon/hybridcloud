// The installation of an OpenShift cluster on its page (status/install.ts): the callout lines per stage,
// the warning while a failed attempt is retried, "longer than usual", the stale line, the stopped callout,
// the retry button's label and confirmation, and which Configuration fields may change in which stage.
// Objects are shaped as the platform writes status.install; texts come from the en catalogue.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const B = process.env.SHELL_BUNDLE_DIR;
const ins = await import(path.join(B, 'install.mjs'));
const dur = await import(path.join(B, 'durations.mjs'));
const meta = await import(path.join(B, 'specFieldMeta.mjs'));
const en = JSON.parse(fs.readFileSync(path.join(process.env.UI_ROOT, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));

// i18next's lookup for en: count picks _one / _other; a key missing from the catalogue fails the test
const get = (k) => k.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), en);
function t(key, opts = {}) {
  let s = get(key);
  if (typeof opts.count === 'number' && typeof s !== 'string') s = get(`${key}_${opts.count === 1 ? 'one' : 'other'}`);
  if (typeof s !== 'string') {
    if (opts.defaultValue === undefined) throw new Error(`missing catalogue key ${key}`);
    s = opts.defaultValue;
  }
  return s.replace(/\{\{\s*(\w+)\s*\}\}/g, (_m, k) => (opts[k] !== undefined ? String(opts[k]) : ''));
}

const NOW = Date.parse('2026-11-18T12:00:00Z');
const ago = (sec) => new Date(NOW - sec * 1000).toISOString();

/** A cluster as the platform writes it while it installs (status.install as in the API). */
function cluster({ phase = 'Creating', install, reason = 'Installing', message, summary, status = {}, spec } = {}) {
  return {
    apiVersion: 'hybridsovereign.redhat/v1alpha1',
    kind: 'PlatformOpenshift',
    metadata: { name: 'demo', namespace: 'tenant-a', generation: 1 },
    spec: spec ?? { type: 'openstack', openstack: { environment: 'env-a', controlPlaneCount: 3, workerCount: 3 } },
    status: {
      phase,
      summary: summary ?? 'The cluster is being installed.',
      conditions: [{ type: 'Ready', status: 'False', reason, ...(message ? { message } : {}) }],
      ...(install ? { install } : {}),
      ...status,
    },
  };
}

const installing = (extra = {}) => ({
  stage: 'Installing', attempt: 1, attemptsLimit: 3, run: 1, startedAt: ago(120), observedAt: ago(10), ...extra,
});

const QUOTA = 'Attempt 1 of 3 failed: the site reports too little quota: Cores (72 needed, limit 64). Attempt 2 starts shortly.';

test('the estimate reads "Usually 40–60 minutes." from one place', () => {
  const usual = dur.usualCreateDuration('PlatformOpenshift');
  assert.equal(dur.usualText(t, usual), 'Usually 40–60 minutes.');
  assert.equal(dur.usualText(t, dur.usualDuration('PlatformOpenshift', 'Creating')), 'Usually 40–60 minutes.');
  assert.deepEqual(dur.CLUSTER_INSTALL_MINUTES, { min: 40, max: 60 });
  // without the catalogue the default text names the same numbers
  assert.equal(dur.usualText((k, o) => o.defaultValue.replace(/\{\{(\w+)\}\}/g, (_m, x) => o[x]), usual), 'Usually 40–60 minutes.');
  // other kinds keep their texts
  assert.equal(dur.usualText(t, dur.usualCreateDuration('HybridNetwork')), 'Usually under a minute.');
});

test('elapsed time is steady: seconds, then whole minutes, then hours and minutes', () => {
  const e = (sec) => ins.elapsedText(t, sec * 1000);
  assert.equal(e(0), '0 seconds');
  assert.equal(e(1), '1 second');
  assert.equal(e(40), '40 seconds');
  assert.equal(e(60), '1 minute');
  assert.equal(e(12 * 60 + 59), '12 minutes');
  assert.equal(e(84 * 60), '84 minutes');
  assert.equal(e(119 * 60 + 59), '119 minutes');
  assert.equal(e(120 * 60), '2 hours');
  assert.equal(e(125 * 60), '2 hours 5 minutes');
  assert.equal(e(6 * 3600 + 60), '6 hours 1 minute');
});

test('callout lines per stage', () => {
  const p = (install, phase) => ins.installProgress(t, cluster({ install, phase }), NOW);

  const prep = p({ stage: 'Preparing', run: 1, startedAt: ago(40) });
  assert.equal(prep.stage, 'Preparing: checking the environment and reserving addresses.');
  assert.equal(prep.times, 'Started 40 seconds ago.');
  assert.equal(prep.variant, 'info');
  assert.equal(prep.showEstimate, true);
  assert.equal(prep.longer, undefined);
  assert.equal(prep.stale, undefined);

  const inst = p(installing());
  assert.equal(inst.stage, 'Installing, attempt 1 of 3.');
  assert.equal(inst.times, 'Started 2 minutes ago. Last checked 10 seconds ago.');
  assert.equal(inst.failure, undefined);

  assert.equal(p(installing({ attemptsLimit: undefined })).stage, 'Installing, attempt 1.');
  assert.equal(p({ stage: 'Installing' }).stage, 'Installing.');
  assert.equal(p({ stage: 'Installing' }).times, undefined);

  assert.equal(p({ stage: 'CleaningUp', run: 2, observedAt: ago(5) }).stage, 'Removing the stopped installation.');
  assert.equal(p({ stage: 'CleaningUp', run: 2, observedAt: ago(5) }).times, 'Last checked 5 seconds ago.');
  assert.equal(
    p({ stage: 'Finishing', attempt: 1, attemptsLimit: 3, startedAt: ago(38 * 60) }).stage,
    'Finishing: the cluster is installed; the platform is connecting it.',
  );
  // a retried installation that changes the spec reads Updating: the same lines
  assert.equal(p(installing(), 'Updating').stage, 'Installing, attempt 1 of 3.');
});

test('no installation lines outside a running installation', () => {
  const p = (o) => ins.installProgress(t, o, NOW);
  assert.equal(p(cluster()), null, 'no status.install (a cluster written before this release)');
  assert.equal(p(cluster({ install: { stage: 'Installed', attempt: 1, attemptsLimit: 3 }, phase: 'Ready' })), null);
  assert.equal(p(cluster({ install: { stage: 'Stopped', attempt: 3, attemptsLimit: 3 }, phase: 'Failed' })), null);
  // a Retry request queued on a stopped installation: the stage still says Stopped, the queue line says the rest
  assert.equal(p(cluster({ install: { stage: 'Stopped' }, phase: 'Creating', reason: 'AwaitingEDA' })), null);
  assert.equal(p(cluster({ install: installing(), phase: 'Deleting' })), null);
  assert.equal(p(cluster({ install: installing(), phase: 'Waiting' })), null);
  assert.equal(p(cluster({ install: { ...installing(), stage: 'Warming' } })), null, 'an unknown stage word');
  assert.equal(p(cluster({ install: 'Installing' })), null, 'a malformed install');
});

test('a failed attempt while the installer retries: warning style and the message', () => {
  const o = cluster({
    install: installing({ attempt: 2, startedAt: ago(3 * 60), lastFailure: { attempt: 1, reason: 'QuotaExceeded', message: QUOTA, time: ago(30) } }),
    reason: 'InstallRetrying',
    message: QUOTA,
    summary: 'An installation attempt failed; the installer is trying again. The message says what failed.',
  });
  const r = ins.installProgress(t, o, NOW);
  assert.equal(r.variant, 'warning');
  assert.equal(r.failure, QUOTA);
  assert.equal(r.stage, 'Installing, attempt 2 of 3.');
  assert.equal(r.times, 'Started 3 minutes ago. Last checked 10 seconds ago.');
  assert.equal(r.showEstimate, true);
  // the condition without a message: the last failure's own sentence
  const bare = cluster({ install: installing({ lastFailure: { attempt: 1, message: QUOTA } }), reason: 'InstallRetrying' });
  assert.equal(ins.installProgress(t, bare, NOW).failure, QUOTA);
  // a kept last failure under another reason is no warning
  const kept = cluster({ install: installing({ attempt: 2, lastFailure: { attempt: 1, message: QUOTA } }), reason: 'Installing' });
  const k = ins.installProgress(t, kept, NOW);
  assert.equal(k.variant, 'info');
  assert.equal(k.failure, undefined);
});

test('past the usual time: "taking longer than usual" instead of the estimate', () => {
  const at = (sec, extra = {}) => ins.installProgress(t, cluster({ install: installing({ startedAt: ago(sec), ...extra }) }), NOW);
  const onTime = at(60 * 60);
  assert.equal(onTime.longer, undefined);
  assert.equal(onTime.showEstimate, true);
  const late = at(60 * 60 + 1, { attempt: 2 });
  assert.equal(late.longer, 'This is taking longer than usual. The installer tries up to 3 times; if it stops, this page says why.');
  assert.equal(late.showEstimate, false);
  assert.equal(late.times, 'Started 60 minutes ago. Last checked 10 seconds ago.');
  assert.equal(at(84 * 60).times, 'Started 84 minutes ago. Last checked 10 seconds ago.');
  assert.equal(
    at(90 * 60, { attemptsLimit: undefined }).longer,
    'This is taking longer than usual. The installer tries again by itself; if it stops, this page says why.',
  );
  assert.equal(at(90 * 60, { stage: 'Finishing' }).longer, 'This is taking longer than usual.');
  // the threshold is the estimate's upper end
  assert.equal(dur.CLUSTER_INSTALL_MINUTES.max, 60);
});

test('a last check older than 15 minutes is said plainly', () => {
  const at = (sec) => ins.installProgress(t, cluster({ install: installing({ observedAt: ago(sec) }) }), NOW);
  const fresh = at(15 * 60);
  assert.equal(fresh.stale, undefined);
  assert.equal(fresh.times, 'Started 2 minutes ago. Last checked 15 minutes ago.');
  const old = at(20 * 60);
  assert.equal(old.stale, 'The state shown was last checked 20 minutes ago.');
  assert.equal(old.times, 'Started 2 minutes ago.', 'the stale line replaces "Last checked"');
  assert.equal(ins.INSTALL_STALE_AFTER_MINUTES, 15);
});

test('a stopped installation: title and the line about Retry installation', () => {
  const stopped = cluster({
    phase: 'Failed',
    reason: 'QuotaExceeded',
    summary: "The environment's quota is too small for this cluster. Make the cluster smaller or have the quota raised, then retry.",
    install: { stage: 'Stopped', attempt: 3, attemptsLimit: 3, run: 1, startedAt: ago(7 * 60), endedAt: ago(5) },
  });
  assert.deepEqual(ins.installStoppedCallout(t, stopped), {
    title: 'Installation stopped',
    retryLine:
      'Retry installation uses the settings on the Configuration tab. To change the network or the address ranges, delete this cluster and create it again.',
  });
  // other failures keep the general title
  assert.equal(ins.installStoppedCallout(t, cluster({ phase: 'Failed', reason: 'ProvisionFailed' })), null);
  assert.equal(ins.installStoppedCallout(t, cluster({ phase: 'Failed', install: installing() })), null);
  assert.equal(ins.installStoppedCallout(t, { ...stopped, metadata: { ...stopped.metadata, deletionTimestamp: ago(1) } }), null);
});

test('the button reads Retry installation on a stopped installation, Retry now otherwise', () => {
  const label = (o) => t(ins.retryLabelKey(o));
  assert.equal(label(cluster({ phase: 'Failed', install: { stage: 'Stopped' } })), 'Retry installation');
  for (const stage of ['Preparing', 'CleaningUp', 'Installing', 'Finishing', 'Installed']) {
    assert.equal(label(cluster({ install: { stage } })), 'Retry now', stage);
  }
  assert.equal(label(cluster()), 'Retry now');
  assert.equal(label({ kind: 'CloudVirt', metadata: { name: 'v' }, status: { phase: 'Failed' } }), 'Retry now');
});

test('the confirmation names what happens and the node counts', () => {
  const c = ins.retryConfirmation(t, { type: 'openstack', openstack: { environment: 'env-a', controlPlaneCount: 3, workerCount: 0 } });
  assert.deepEqual(c, {
    title: 'Start the installation again?',
    body: 'What the stopped installation left on the site is removed first. The cluster keeps its name, addresses and DNS names.',
    settings: 'Settings: 3 control plane nodes, 0 workers.',
    confirm: 'Start the installation again',
  });
  assert.equal(ins.retryConfirmation(t, { type: 'aws', aws: { controlPlaneCount: 1, workerCount: 1 } }).settings, 'Settings: 1 control plane node, 1 worker.');
  assert.equal(ins.retryConfirmation(t, { type: 'gcp', gcp: { controlPlaneCount: 3, workerCount: 2 } }).settings, 'Settings: 3 control plane nodes, 2 workers.');
  assert.equal(
    ins.retryConfirmation(t, { type: 'openstack', openstack: { environment: 'env-a' } }).settings,
    'Settings: the default number of control plane nodes, the default number of workers.',
  );
  assert.equal(
    ins.retryConfirmation(t, {}).settings,
    'Settings: the default number of control plane nodes, the default number of workers.',
  );
});

test('install-time fields: open before the installation and when stopped, read-only otherwise', () => {
  const st = (install, status = {}) => ins.installEditState(cluster({ install, status }));
  assert.equal(st(undefined), 'open', 'a new cluster before its first check');
  assert.equal(st({ stage: 'Preparing', run: 1 }), 'open', 'a check refused the cluster: the tenant corrects it');
  assert.equal(st({ stage: 'Stopped' }), 'stopped');
  assert.equal(st({ stage: 'Stopped' }, { installStarted: true }), 'stopped');
  for (const stage of ['CleaningUp', 'Installing', 'Finishing']) assert.equal(st({ stage }), 'installing', stage);
  assert.equal(st({ stage: 'Preparing' }, { installStarted: true }), 'installing');
  assert.equal(st({ stage: 'Installed' }), 'installed');
  // clusters from before this release: no status.install
  assert.equal(st(undefined, { installStarted: true }), 'installing');
  assert.equal(st(undefined, { clusterInstalled: true }), 'installed');
  assert.equal(st(undefined, { installStarted: true, clusterInstalled: true }), 'installed');
  assert.equal(st(undefined, { installStarted: false, clusterInstalled: false }), 'open');
  // texts of the read-only states and of the stopped note
  assert.equal(t('install.noteInstalling'), 'The cluster is being installed. These settings can be changed if the installation stops.');
  assert.equal(t('install.setAtInstallation'), 'Set at installation.');
  assert.equal(t('install.noteStopped'), 'The installation stopped. Saving a change here starts a new installation with these settings.');
});

test('the install-time fields are pinned; every changeable provider field of the form is one of them', () => {
  assert.deepEqual([...ins.INSTALL_TIME_FIELDS], [
    'openstack.controlPlaneCount', 'openstack.workerCount', 'openstack.controlPlaneFlavor', 'openstack.workerFlavor',
    'openstack.ocpImageSet', 'openstack.glanceImageOverride', 'openstack.trustBundle',
    'aws.region', 'aws.network', 'aws.publish', 'aws.clusterType', 'aws.controlPlaneCount', 'aws.workerCount',
    'aws.controllerFlavor', 'aws.workerFlavor',
    'gcp.region', 'gcp.network', 'gcp.publish', 'gcp.controlPlaneCount', 'gcp.workerCount', 'gcp.controllerFlavor',
    'gcp.workerFlavor',
  ]);
  const fields = meta.KIND_SPEC_META.PlatformOpenshift.fields;
  for (const f of fields) {
    const provider = /^(openstack|aws|gcp)\./.test(f.path);
    if (provider && !f.immutable) assert.ok(ins.isInstallTimeField(f.path), `${f.path} is changeable but not install-time`);
    if (!provider) assert.equal(ins.isInstallTimeField(f.path), false, `${f.path} is applied to an installed cluster`);
  }
  for (const p of ['toolRbac.clusterAdminRbac', 'clusterViewerRbac', 'deletionProtection', 'type']) {
    assert.equal(ins.isInstallTimeField(p), false, p);
  }
});

test('the reader keeps well-formed values only', () => {
  const o = cluster({
    install: {
      stage: 'Installing', attempt: '2', attemptsLimit: 3, run: 1, startedAt: ago(60), observedAt: '', endedAt: 7,
      lastFailure: { attempt: 1, reason: 'QuotaExceeded', message: QUOTA, time: ago(30), extra: 'x' },
    },
  });
  assert.deepEqual(ins.installOf(o), {
    stage: 'Installing', attempt: 2, attemptsLimit: 3, run: 1, startedAt: ago(60),
    lastFailure: { attempt: 1, reason: 'QuotaExceeded', message: QUOTA, time: ago(30) },
  });
  assert.equal(ins.installOf(cluster()), undefined);
  assert.equal(ins.isInstallStopped(cluster({ install: { stage: 'Stopped' } })), true);
  assert.equal(ins.isInstallStopped(null), false);
});
