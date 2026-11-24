// The rule for names in the forms (ui/packages/shared/src/forms/nameRule.ts) is the jobs' rule
// (eda/common/files/shc_names.py): the same pattern and limits, and the same answer for every name of a table.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

const UI = process.env.UI_ROOT;
const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'nameRule.mjs'));
const ef = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'entityFields.mjs'));
const NAMES_PY = path.join(UI, '..', 'eda', 'common', 'files', 'shc_names.py');
const py = fs.readFileSync(NAMES_PY, 'utf8');
const en = JSON.parse(fs.readFileSync(path.join(UI, 'packages/shared/src/i18n/locales/en.json'), 'utf8'));
const fr = JSON.parse(fs.readFileSync(path.join(UI, 'packages/shared/src/i18n/locales/fr.json'), 'utf8'));

const pyConst = (name) => {
  const mm = py.match(new RegExp(`^${name}\\s*=\\s*(r?"[^"]*"|\\d+)`, 'm'));
  assert.ok(mm, `${name} in shc_names.py`);
  return mm[1].startsWith('r"') ? mm[1].slice(2, -1) : mm[1].startsWith('"') ? mm[1].slice(1, -1) : Number(mm[1]);
};

test('pattern and limits are the ones of shc_names.py', () => {
  assert.equal(m.NAME_PATTERN, pyConst('NAME_PATTERN'));
  assert.equal(m.NAME_MAX, pyConst('NAME_MAX'));
  assert.equal(m.GATEWAY_NAME_MAX, pyConst('GATEWAY_NAME_MAX'));
  assert.equal(m.nameLimitFor('CloudGateway'), m.GATEWAY_NAME_MAX);
  assert.equal(m.nameLimitFor('PlatformOpenshift'), m.NAME_MAX);
  assert.equal(ef.ENTITY_LIMITS.nameMax, m.NAME_MAX);
});

const TABLE = [
  'web', 'a', 'acme-corp', 'corp-web', 'acme', 'w1', 'a-b-c', 'x'.repeat(30), 'x'.repeat(31), 'x'.repeat(21), 'x'.repeat(22),
  '1web', '-web', 'web-', 'acme--corp', 'Web', 'we_b', 'we.b', 'we b', '', 'é', 'web9', 'a1-2b', 'core-prod',
];

test('every name of the table gets the same answer from the forms and from the jobs', () => {
  const items = [];
  for (const v of TABLE) for (const limit of [30, 21]) items.push({ value: v, limit });
  const script = [
    'import json, sys',
    `sys.path.insert(0, ${JSON.stringify(path.dirname(NAMES_PY))})`,
    'import shc_names',
    'items = json.load(sys.stdin)',
    'print(json.dumps([shc_names.check_name(i["value"], "name", i["limit"]) == "" for i in items]))',
  ].join('\n');
  const answers = JSON.parse(execFileSync('python3', ['-I', '-c', script], { input: JSON.stringify(items) }).toString());
  items.forEach((it, i) => {
    assert.equal(m.nameAllowed(it.value, { limit: it.limit }), answers[i], `${JSON.stringify(it.value)} at most ${it.limit}`);
  });
});

test('a cluster on Google Cloud: no "goog" start and no google-like word (the installer refuses them)', () => {
  for (const v of ['goog', 'googly', 'my-google', 'g00gle-web', 'go0gle', 'webgoogle1']) {
    assert.equal(m.nameAllowed(v, { google: true }), false, v);
  }
  for (const v of ['web', 'go-web', 'gogle', 'agoog']) assert.equal(m.nameAllowed(v, { google: true }), true, v);
  assert.equal(m.nameAllowed('googly'), true, 'elsewhere the word is allowed');
});

test('one sentence says the rule, in English and French, with the limit', () => {
  assert.deepEqual(m.nameRuleMessage(), { key: 'form.nameRule.rule', values: { max: 30 } });
  assert.deepEqual(m.nameRuleMessage({ limit: 21 }), { key: 'form.nameRule.rule', values: { max: 21 } });
  assert.equal(m.nameRuleMessage({ google: true }).key, 'form.nameRule.ruleGoogle');
  for (const cat of [en, fr]) {
    for (const k of ['rule', 'ruleGoogle']) assert.match(cat.form.nameRule[k], /\{\{max\}\}/);
  }
  assert.match(en.form.nameRule.rule, /letter first/);
  assert.match(en.attach.nameInvalid, /\{\{max\}\}/);
});

test('the placement name a dialog proposes obeys the rule', () => {
  assert.equal(m.defaultPlacementName('core', 'prod'), 'core-prod');
  const long = m.defaultPlacementName('a'.repeat(30), 'b'.repeat(30));
  assert.ok(m.nameAllowed(long), long);
  assert.equal(long.length, 30);
  assert.ok(long.startsWith('aaaaaaaaaaaaaaa-') || long.startsWith('aaaaaaaaaaaaaa'), long);
  // a short network keeps its whole name; the environment gives up the rest
  const shortNet = m.defaultPlacementName('core', 'production-environment-west-01');
  assert.ok(shortNet.startsWith('core-') && m.nameAllowed(shortNet), shortNet);
  // a cut never ends on a hyphen, and the joint never doubles one
  for (const [n, e] of [['net-work-of-the-tenant-acme-x', 'env-ironment-of-the-tenant-y'], ['abcdefghijklmn-', 'p'.repeat(29)]]) {
    const v = m.defaultPlacementName(n, e);
    assert.ok(m.nameAllowed(v), `${n} + ${e} -> ${v}`);
  }
});

test('the forms apply the rule to the kinds the API rules, and the API server\'s own rule to the others', () => {
  // the kinds the CRDs rule, as the API-server test lists them
  const cel = fs.readFileSync(path.join(UI, '..', 'tests', 'cel-ipam', 'cel_cases_names.py'), 'utf8');
  const block = cel.match(/^RULED = \{([\s\S]*?)\n\}/m);
  assert.ok(block, 'RULED in cel_cases_names.py');
  const ruled = [...block[1].matchAll(/"(\w+)":/g)].map((x) => x[1]).sort();
  assert.deepEqual([...m.RULED_KINDS].sort(), ruled);
  const unruled = cel.match(/^UNRULED = \[([\s\S]*?)\]/m);
  for (const kind of [...unruled[1].matchAll(/"(\w+)"/g)].map((x) => x[1])) {
    const opts = m.nameOptionsFor(kind);
    assert.equal(opts.kubernetes, true, kind);
    assert.ok(m.nameAllowed('web.prod', opts), `${kind}: a dotted name the API takes`);
    assert.ok(m.nameAllowed('x'.repeat(40), opts), `${kind}: a name over 30 the API takes`);
    assert.ok(!m.nameAllowed('Web', opts) && !m.nameAllowed('-web', opts) && !m.nameAllowed('x'.repeat(254), opts), kind);
    assert.equal(m.nameRuleMessage(opts).key, 'form.nameRule.ruleKubernetes');
  }
  for (const kind of ruled) {
    assert.ok(!m.nameOptionsFor(kind).kubernetes, kind);
    assert.ok(!m.nameAllowed('web.prod', m.nameOptionsFor(kind)), kind);
  }
  assert.equal(typeof en.form.nameRule.ruleKubernetes, 'string');
  assert.equal(typeof fr.form.nameRule.ruleKubernetes, 'string');
});
