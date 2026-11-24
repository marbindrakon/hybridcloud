// The admin's link to an automation job: built from the job's id and the dashboard chart's setting
// automationJobUrlTemplate (served at /api/ui-config); status carries no address. No setting, no link.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const OUT = process.env.SHELL_BUNDLE_DIR;
const m = await import(path.join(OUT, 'jobLink.mjs'));
const s = await import(`${UI}/packages/shared/server/session.js`);
const chart = path.resolve(UI, '..', 'charts', 'charts', 'sovereign-cloud-dashboard');
const T = 'https://automation.example.com/execution/jobs/playbook/{jobId}/output';

test('the link is the template with the job id; no template or no id: no link', () => {
  assert.equal(m.automationJobUrl(T, '42'), 'https://automation.example.com/execution/jobs/playbook/42/output');
  assert.equal(m.automationJobUrl(T, 42), 'https://automation.example.com/execution/jobs/playbook/42/output');
  assert.equal(m.automationJobUrl(T, 'a/b?c'), 'https://automation.example.com/execution/jobs/playbook/a%2Fb%3Fc/output');
  assert.equal(m.automationJobUrl('', '42'), undefined);
  assert.equal(m.automationJobUrl(undefined, '42'), undefined);
  assert.equal(m.automationJobUrl(T, ''), undefined);
  assert.equal(m.automationJobUrl(T, undefined), undefined);
  assert.equal(m.automationJobUrl('https://automation.example.com/jobs', '42'), undefined, 'a template without {jobId}');
  assert.equal(m.automationJobUrl('javascript:alert({jobId})', '42'), undefined);
});

test('the server serves the setting only as an https URL with {jobId}', () => {
  assert.equal(s.uiConfigFromEnv({ SHC_AUTOMATION_JOB_URL_TEMPLATE: T }).automationJobUrlTemplate, T);
  assert.equal(s.uiConfigFromEnv({ SHC_AUTOMATION_JOB_URL_TEMPLATE: ' ' + T + ' ' }).automationJobUrlTemplate, T);
  for (const bad of ['http://automation.example.com/{jobId}', 'https://automation.example.com/jobs', 'javascript:{jobId}', '']) {
    assert.equal(s.uiConfigFromEnv({ SHC_AUTOMATION_JOB_URL_TEMPLATE: bad }).automationJobUrlTemplate, '', bad);
  }
});

test('the diagnostics read no URL from status', () => {
  const src = fs.readFileSync(path.join(UI, 'packages/shared/src/components/ResourceDetail.tsx'), 'utf8');
  const block = src.slice(src.indexOf('export function DiagnosticsBlock'), src.indexOf('function SpecEditor'));
  assert.ok(block.includes('automationJobUrl(automationJobUrlTemplate, j.jobId)'));
  assert.doesNotMatch(block, /j\.url|\.url\b/);
});

function helmAvailable() {
  try {
    execFileSync('helm', ['version', '--short'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}
const skip = helmAvailable() ? false : 'helm not installed';
const render = (sets = []) =>
  execFileSync('helm', ['template', 't', chart, ...sets.flatMap((x) => ['--set', x])], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

test('chart: no default; set, it reaches the server; a wrong value fails the render', { skip }, () => {
  const values = fs.readFileSync(path.join(chart, 'values.yaml'), 'utf8');
  assert.match(values, /^automationJobUrlTemplate: ""$/m);
  assert.doesNotMatch(render(), /SHC_AUTOMATION_JOB_URL_TEMPLATE/);
  const out = render([`automationJobUrlTemplate=${T}`]);
  assert.match(out, new RegExp(`- name: SHC_AUTOMATION_JOB_URL_TEMPLATE\\n\\s+value: "${T.replace(/[.?{}/]/g, '\\$&')}"`));
  assert.throws(() => render(['automationJobUrlTemplate=http://automation.example.com/{jobId}']), /automationJobUrlTemplate must be an https URL/);
});
