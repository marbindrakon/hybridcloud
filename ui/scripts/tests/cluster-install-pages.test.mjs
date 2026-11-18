// The cluster page's installation texts as rendered: the real StatusCallout and Configuration tab
// (GenericSpecEditor) rendered on the server (react-dom/server) from clusters shaped as the platform
// writes status.install. The logic behind them is tested in cluster-install.test.mjs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const en = JSON.parse(fs.readFileSync(path.join(UI, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));
const I = en.install;

const entry = `import { renderToStaticMarkup } from 'react-dom/server';
import { createElement } from 'react';
import { StatusCallout } from './components/StatusCallout';
import { GenericSpecEditor } from './forms/GenericSpecEditor';
import { KIND_STATUS_KEYS } from './components/KindStatusDetails';
export const callout = (kind, item) => renderToStaticMarkup(createElement(StatusCallout, { kind, item }));
export const editor = (item, readOnly = false) => renderToStaticMarkup(createElement(GenericSpecEditor, { kind: 'PlatformOpenshift', namespace: item.metadata.namespace, item, onSaved: () => {}, readOnly }));
export { KIND_STATUS_KEYS };`;
const bundle = path.join(OUT, 'clusterInstallPages.mjs');
execFileSync(path.join(UI, 'node_modules/.bin/esbuild'), [
  '--bundle', '--format=esm', '--platform=node', '--log-level=error', '--jsx=automatic', '--loader=tsx',
  '--loader:.json=json', '--loader:.css=empty', '--loader:.svg=dataurl', '--define:process.env.NODE_ENV="production"',
  "--banner:js=import { createRequire as __shcRequire } from 'module'; const require = __shcRequire(import.meta.url);",
  '--sourcefile=entry.tsx', `--outfile=${bundle}`,
], { cwd: path.join(UI, 'packages/shared/src'), input: entry });
const pages = await import(bundle);

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

const ago = (sec) => new Date(Date.now() - sec * 1000).toISOString();
const QUOTA = 'Attempt 1 of 3 failed: the site reports too little quota: Cores (72 needed, limit 64). Attempt 2 starts shortly.';

function cluster({ phase = 'Creating', install, reason = 'Installing', message, summary = 'The cluster is being installed.', status = {}, openstack } = {}) {
  return {
    apiVersion: 'hybridsovereign.redhat/v1alpha1',
    kind: 'PlatformOpenshift',
    metadata: { name: 'demo', namespace: 'tenant-a', generation: 1 },
    spec: { type: 'openstack', openstack: { environment: 'env-a', controlPlaneCount: 3, workerCount: 3, ...openstack }, clusterViewerRbac: [] },
    status: {
      phase,
      summary,
      conditions: [{ type: 'Ready', status: phase === 'Ready' ? 'True' : 'False', reason, ...(message ? { message } : {}) }],
      ...(install ? { install } : {}),
      ...status,
    },
  };
}

test('callout while installing: stage, attempt, start, last check and the estimate', () => {
  const html = pages.callout('PlatformOpenshift', cluster({
    install: { stage: 'Installing', attempt: 1, attemptsLimit: 3, run: 1, startedAt: ago(125), observedAt: ago(10) },
  }));
  const s = text(html);
  assert.ok(s.includes(en.callout.creating), s);
  assert.ok(s.includes('Installing, attempt 1 of 3.'), s);
  assert.ok(s.includes('Started 2 minutes ago.'), s);
  assert.match(s, /Last checked 1\d seconds ago\./);
  assert.ok(s.includes('Usually 40–60 minutes.'), s);
  assert.ok(html.includes('pf-m-info'), 'info style');

  const prep = text(pages.callout('PlatformOpenshift', cluster({ install: { stage: 'Preparing', run: 1, startedAt: ago(40) } })));
  assert.ok(prep.includes(I.stagePreparing), prep);
  assert.match(prep, /Started 4\d seconds ago\./);
});

test('callout while a failed attempt is retried: warning style and the failed attempt', () => {
  const html = pages.callout('PlatformOpenshift', cluster({
    install: { stage: 'Installing', attempt: 2, attemptsLimit: 3, run: 1, startedAt: ago(180), observedAt: ago(5),
      lastFailure: { attempt: 1, reason: 'QuotaExceeded', message: QUOTA, time: ago(30) } },
    reason: 'InstallRetrying',
    message: QUOTA,
    summary: 'An installation attempt failed; the installer is trying again. The message says what failed.',
  }));
  assert.ok(html.includes('pf-m-warning'), 'warning style');
  const s = text(html);
  assert.ok(s.includes(QUOTA), s);
  assert.ok(s.includes('Installing, attempt 2 of 3.'), s);
});

test('callout past the usual time and with an old last check', () => {
  const s = text(pages.callout('PlatformOpenshift', cluster({
    install: { stage: 'Installing', attempt: 2, attemptsLimit: 3, run: 1, startedAt: ago(84 * 60 + 5), observedAt: ago(20 * 60 + 5) },
  })));
  assert.ok(s.includes(fill(I.longerThanUsual, { limit: 3 })), s);
  assert.ok(!s.includes('Usually 40–60 minutes.'), 'the estimate gives way');
  assert.ok(s.includes('Started 84 minutes ago.'), s);
  assert.ok(s.includes('The state shown was last checked 20 minutes ago.'), s);
  assert.ok(!s.includes('Last checked'), s);
});

test('callout when the installation stopped; other failures keep the general title', () => {
  const stopped = cluster({
    phase: 'Failed', reason: 'QuotaExceeded',
    summary: "The environment's quota is too small for this cluster. Make the cluster smaller or have the quota raised, then retry.",
    message: 'Attempt 3 of 3: the site reports too little quota: Cores (72 needed, limit 64). The installer stopped after 3 attempts.',
    install: { stage: 'Stopped', attempt: 3, attemptsLimit: 3, run: 1, startedAt: ago(420), endedAt: ago(60) },
  });
  stopped.status.conditions.push({ type: 'Stalled', status: 'True', reason: 'QuotaExceeded', message: stopped.status.conditions[0].message });
  const html = pages.callout('PlatformOpenshift', stopped);
  const s = text(html);
  assert.ok(html.includes('pf-m-danger'));
  assert.ok(s.startsWith(`Danger alert: ${I.stoppedTitle} `), s);
  assert.ok(!s.includes(en.callout.failed), s);
  assert.ok(s.includes('The installer stopped after 3 attempts.'), s);
  assert.ok(s.includes('QuotaExceeded'), s);
  assert.ok(s.includes(I.retryUses), s);
  const other = text(pages.callout('PlatformOpenshift', cluster({ phase: 'Failed', reason: 'ProvisionFailed', summary: 'Creating or updating did not complete.' })));
  assert.ok(other.includes(en.callout.failed), other);
  assert.ok(!other.includes(I.retryUses), other);
});

test('callout past the time allowed: the condition decides, the installer may still run', () => {
  const o = cluster({
    phase: 'Failed', reason: 'InstallOverdue',
    summary: 'The installation did not finish in the time allowed. You can retry it or delete the cluster.',
    install: { stage: 'Installing', attempt: 2, attemptsLimit: 3, run: 1, startedAt: ago(6 * 3600 + 60), observedAt: ago(20) },
  });
  o.status.conditions.push({ type: 'Stalled', status: 'True', reason: 'InstallOverdue' });
  const html = pages.callout('PlatformOpenshift', o);
  const s = text(html);
  assert.ok(html.includes('pf-m-danger'));
  assert.ok(s.startsWith(`Danger alert: ${I.unfinishedTitle} `), s);
  assert.ok(s.includes(I.unfinishedLine), s);
  assert.ok(!s.includes(I.retryUses), s);
  // the install-time fields stay read-only (the API refuses them)
  assert.equal(input(pages.editor(o), 'PlatformOpenshift-openstack.workerCount').disabled, true);
});

test('an installed cluster and other kinds show nothing new', () => {
  const ready = cluster({ phase: 'Ready', reason: 'AsExpected', summary: 'Ready.', install: { stage: 'Installed', attempt: 1, attemptsLimit: 3, run: 1 } });
  assert.equal(pages.callout('PlatformOpenshift', ready), '');
  // the overview grid leaves status.install to the callout
  assert.ok(pages.KIND_STATUS_KEYS.PlatformOpenshift.includes('install'));
  // a cluster written before this release while it installs: the old callout
  const old = text(pages.callout('PlatformOpenshift', cluster({ reason: 'InProgress', summary: 'Applying the change.' })));
  assert.equal(old, `Info alert: ${en.callout.creating} Applying the change. Usually 40–60 minutes.`);
});

/** The input of a field (by its id) and whether it is disabled. */
function input(html, id) {
  const m = new RegExp(`<input[^>]*id="${id.replace('.', '\\.')}"[^>]*>`).exec(html);
  return m ? { disabled: /\sdisabled=""/.test(m[0]) } : null;
}

test('Configuration tab: install-time fields editable before the installation and when stopped', () => {
  const open = pages.editor(cluster({ status: {} }));
  assert.equal(input(open, 'PlatformOpenshift-openstack.workerCount').disabled, false);
  assert.equal(input(open, 'PlatformOpenshift-openstack.workerFlavor').disabled, false);
  assert.ok(!text(open).includes(I.noteInstalling));
  assert.ok(!text(open).includes(I.noteStopped));

  const stopped = pages.editor(cluster({ phase: 'Failed', install: { stage: 'Stopped' }, status: { installStarted: true } }));
  assert.equal(input(stopped, 'PlatformOpenshift-openstack.workerCount').disabled, false);
  assert.equal(input(stopped, 'PlatformOpenshift-openstack.controlPlaneCount').disabled, false);
  assert.ok(text(stopped).includes(I.noteStopped));
  // fields fixed after create stay fixed
  assert.equal(input(stopped, 'PlatformOpenshift-openstack.environment').disabled, true);
});

test('Configuration tab: read-only while installing and on an installed cluster', () => {
  for (const stage of ['CleaningUp', 'Installing', 'Finishing']) {
    const html = pages.editor(cluster({ install: { stage } }));
    for (const f of ['controlPlaneCount', 'workerCount', 'controlPlaneFlavor', 'workerFlavor']) {
      assert.equal(input(html, `PlatformOpenshift-openstack.${f}`).disabled, true, `${stage} ${f}`);
    }
    assert.ok(text(html).includes(I.noteInstalling), stage);
    assert.ok(!text(html).includes(I.setAtInstallation), stage);
  }
  const installed = pages.editor(cluster({ phase: 'Ready', install: { stage: 'Installed' } }));
  assert.equal(input(installed, 'PlatformOpenshift-openstack.workerCount').disabled, true);
  assert.equal(text(installed).split(I.setAtInstallation).length - 1, 4, 'one hint per install-time field');
  assert.ok(!text(installed).includes(I.noteInstalling));
  // an installed cluster from before this release: status.clusterInstalled, no status.install
  const legacy = pages.editor(cluster({ phase: 'Ready', status: { clusterInstalled: true, installStarted: true } }));
  assert.equal(input(legacy, 'PlatformOpenshift-openstack.workerCount').disabled, true);
  assert.ok(text(legacy).includes(I.setAtInstallation));
  // one that was installing before this release
  const started = pages.editor(cluster({ status: { installStarted: true } }));
  assert.equal(input(started, 'PlatformOpenshift-openstack.workerCount').disabled, true);
  assert.ok(text(started).includes(I.noteInstalling));
});
