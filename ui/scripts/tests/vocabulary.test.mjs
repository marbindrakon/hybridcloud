// Display vocabulary for runtime-built labels (PGT-F2, U-1..U-3): kind terms, object references,
// API text and field keys come out in the words of docs/content/terms.yaml, never as API kinds.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const v = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'vocabulary.mjs'));
const phase = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'phase.mjs'));
const terms = JSON.parse(fs.readFileSync(process.env.TERMS_JSON, 'utf8'));
const load = (l) => JSON.parse(fs.readFileSync(path.join(process.env.UI_ROOT, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const tOf = (cat) => (key, opts = {}) => {
  const s = key.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), cat);
  const out = typeof s === 'string' ? s : opts.defaultValue ?? key;
  return String(out).replace(/\{\{\s*(\w+)\s*\}\}/g, (_m, k) => (opts[k] !== undefined ? String(opts[k]) : ''));
};
const t = tOf(load('en'));
const tFr = tOf(load('fr'));
const tenantKinds = Object.entries(terms.kinds)
  .filter(([k, e]) => (e.audience === 'tenant' || k === 'Entity' || k === 'CloudInfrastructure') && e.state !== 'not-implemented')
  .map(([k]) => k);

test('kind terms in running text are the terms.yaml term and plural', () => {
  for (const kind of tenantKinds) {
    assert.equal(v.kindTermInText(t, kind), terms.kinds[kind].term, kind);
    assert.equal(v.kindTermInText(t, kind, 'many'), terms.kinds[kind].plural, kind);
    if (terms.kinds[kind].dense) assert.equal(v.kindTermInText(t, kind, 'dense'), terms.kinds[kind].dense, kind);
  }
});

test('an unknown or missing kind gives a generic word, never the API word', () => {
  assert.equal(v.kindTerm(t, 'NoSuchKind'), 'Object');
  assert.equal(v.kindTerm(t, undefined, 'many'), 'Objects');
  assert.equal(v.kindTerm(tFr, 'NoSuchKind'), 'Objet');
  assert.equal(v.kindTerm(t, 'CloudVirt', 'dense'), 'VM environment');
  assert.equal(v.kindTerm(t, 'Team', 'dense'), 'Team setup');
});

test('object references read as term and name', () => {
  assert.deepEqual(v.parseObjectRef('CloudVirt/g-vmenv-1'), { kind: 'CloudVirt', name: 'g-vmenv-1' });
  assert.deepEqual(v.parseObjectRef('CloudVirt / g-vmenv-1'), { kind: 'CloudVirt', name: 'g-vmenv-1' });
  assert.deepEqual(v.parseObjectRef('HybridNetwork entity-x/net-a'), { kind: 'HybridNetwork', namespace: 'entity-x', name: 'net-a' });
  assert.equal(v.parseObjectRef('waiting for the site'), null);
  assert.equal(v.parseObjectRef('Unknown/x'), null);
  assert.equal(v.objectLabel(t, 'CloudVirt/g-vmenv-1', { dense: true }), 'VM environment g-vmenv-1');
  assert.equal(v.objectLabel(t, { kind: 'HybridNetwork', name: 'net-a' }), 'Network net-a');
  assert.equal(v.objectLabel(t, 'free text naming a NetworkPlacement'), 'free text naming a network placement');
});

test('API text names kinds by their terms', () => {
  assert.equal(v.displayText(t, 'The delete waits for: CloudVirt/g-vmenv-1'), 'The delete waits for: VM environment g-vmenv-1');
  assert.equal(v.displayText(t, 'network g-net-a via NetworkPlacement g-a-vm'), 'network g-net-a via network placement g-a-vm');
  assert.equal(v.displayText(t, 'two HybridNetworks and one Entity'), 'two networks and one tenant');
  assert.equal(v.displayText(t, 'OpenShift cluster ready'), 'OpenShift cluster ready');
});

test('field keys read as labels in the vocabulary', () => {
  assert.equal(v.fieldLabel(t, 'entity'), 'Tenant');
  assert.equal(v.fieldLabel(t, 'attributes.entity'), 'Attributes · Tenant');
  assert.equal(v.fieldLabel(t, 'billingId'), 'Billing ID');
  assert.equal(v.fieldLabel(t, 'attributes.billing-id'), 'Attributes · Billing ID');
  assert.equal(v.fieldLabel(t, 'features.argo'), 'Features · Argo');
  assert.equal(v.fieldLabel(t, 'cloudVirtRef'), 'VM environment ref');
  assert.equal(v.fieldLabel(t, 'rbacs[0]'), 'Access groups');
  assert.equal(v.fieldLabel(t, 'Name'), 'Name');
  assert.equal(v.fieldLabel(tFr, 'entity'), 'Locataire');
});

test('deletionBlockedBy strings in the short form are split into kind and name', () => {
  const item = { status: { deletionBlockedBy: ['CloudVirt/g-vmenv-1', 'still in use', { kind: 'NetworkPlacement', name: 'p' }] } };
  assert.deepEqual(phase.deletionBlockers(item), [
    { kind: 'CloudVirt', name: 'g-vmenv-1', namespace: undefined },
    { name: 'still in use' },
    { kind: 'NetworkPlacement', name: 'p', namespace: undefined, reason: undefined },
  ]);
});
