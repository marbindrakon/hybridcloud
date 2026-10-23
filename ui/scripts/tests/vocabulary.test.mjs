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

// The formats the teardowns write (hybridnetwork_teardown, networkplacement_teardown, cloudvirt_teardown).
const PRODUCER_BLOCKERS = [
  'HybridFabric shc-platform/fab-a (internet egress)',
  'CloudGateway shc-platform/gw-a (VRF net-a)',
  'VirtualMachine entity-x-vms/vm-1',
];

test('teardown references with a detail are split into kind, name and detail', () => {
  assert.deepEqual(v.parseObjectRef(PRODUCER_BLOCKERS[0]), {
    kind: 'HybridFabric', namespace: 'shc-platform', name: 'fab-a', detail: 'internet egress',
  });
  assert.deepEqual(v.parseObjectRef(PRODUCER_BLOCKERS[1]), {
    kind: 'CloudGateway', namespace: 'shc-platform', name: 'gw-a', detail: 'VRF net-a',
  });
  assert.deepEqual(v.parseObjectRef(PRODUCER_BLOCKERS[2]), { kind: 'VirtualMachine', namespace: 'entity-x-vms', name: 'vm-1' });
  assert.deepEqual(v.parseObjectRef('CloudVirt/g-vmenv-1: still in use'), { kind: 'CloudVirt', name: 'g-vmenv-1', detail: 'still in use' });
  assert.equal(v.objectLabel(t, PRODUCER_BLOCKERS[0]), `${v.kindTerm(t, 'HybridFabric')} shc-platform/fab-a (internet egress)`);
  assert.equal(v.objectLabel(t, PRODUCER_BLOCKERS[2]), 'Virtual machine entity-x-vms/vm-1');
  assert.equal(v.objectLabel(tFr, PRODUCER_BLOCKERS[2]), 'Machine virtuelle entity-x-vms/vm-1');
  assert.equal(v.displayText(t, 'Deletion waits for 2 VM(s) on c: VirtualMachine ns/a, VirtualMachine ns/b'),
    'Deletion waits for 2 VM(s) on c: virtual machine ns/a, virtual machine ns/b');
});

test('a reference without a kind is shown through displayText', () => {
  assert.equal(v.objectLabel(t, { name: 'HybridFabric shc-platform/fab-a is busy' }), `${v.kindTermInText(t, 'HybridFabric')} shc-platform/fab-a is busy`);
});

test('teardown deletionBlockedBy entries show no API kind word on the page', () => {
  const item = { status: { deletionBlockedBy: [...PRODUCER_BLOCKERS, 'port p1 (compute:nova)'] } };
  const blockers = phase.deletionBlockers(item);
  assert.deepEqual(blockers[0], { kind: 'HybridFabric', namespace: 'shc-platform', name: 'fab-a', reason: 'internet egress' });
  assert.deepEqual(blockers[1], { kind: 'CloudGateway', namespace: 'shc-platform', name: 'gw-a', reason: 'VRF net-a' });
  for (const b of blockers) {
    // the way StatusWaitBlocks and the Deleting callout build the line
    const label = v.objectLabel(t, { kind: b.kind, name: b.name ?? '', namespace: b.namespace, detail: b.reason }, { dense: true });
    assert.doesNotMatch(label, /\b(HybridFabric|CloudGateway|VirtualMachine)\b/, label);
  }
});

// PGT2-F3: the cluster assignment's words, the site connection in running text, request messages.
test('cluster assignment fields and its summary read as plain words', () => {
  assert.equal(v.fieldLabel(t, 'openshift'), 'OpenShift cluster');
  assert.equal(v.fieldLabel(t, 'openshiftReady'), 'Cluster ready');
  assert.equal(v.fieldLabel(t, 'teamReady'), 'Team ready');
  assert.equal(
    v.displayText(t, 'Team=platform(ready=True), Projects=1, Openshift=(ready=False)'),
    'Team setup platform is ready; 1 app project; no OpenShift cluster is named yet.',
  );
  assert.equal(
    v.displayText(t, 'Team=platform(ready=False),  Projects=2, Openshift=oc1(ready=True)'),
    'Team setup platform is not ready yet; 2 app projects; OpenShift cluster oc1 is ready.',
  );
  const fr = v.displayText(tFr, 'Team=platform(ready=True), Projects=1, Openshift=(ready=False)');
  assert.ok(!/Team=|Openshift|ready=/.test(fr), fr);
  assert.equal(v.linkedSummary(t, 'Team setup=platform'), null);
});

test('the site gateway reads as the site connection in running text, in one case', () => {
  assert.equal(
    v.displayText(t, '10.205.0.0/24 on iaas1 through CloudGateway iaas1 (VNI 51004)'),
    '10.205.0.0/24 on iaas1 through site connection iaas1 (VNI 51004)',
  );
  assert.equal(v.kindTermInText(t, 'CloudGateway', 'many'), 'site connections');
  assert.equal(v.kindTermInText(t, 'CloudInfrastructure'), terms.kinds.CloudInfrastructure.term);
  assert.equal(v.inText('Hybrid Fabric'), 'hybrid fabric');
  assert.equal(v.inText('OpenShift cluster'), 'OpenShift cluster');
  assert.equal(v.inText('AAP organization'), 'AAP organization');
});

test('a Published request message is read through the reasons table; the event id is detail', () => {
  const r = phase.requestMessage('RbacCreateRequested event 7ed3c40e-b033-4fb4-985e-9c9fcdcbc9e0', 'Published');
  assert.equal(r.code, 'RbacCreateRequested');
  assert.equal(r.event, '7ed3c40e-b033-4fb4-985e-9c9fcdcbc9e0');
  assert.equal(r.summary, phase.reasonEntry('Published').summary);
  const d = phase.requestMessage('NetworkPlacementDeleteRequested event ee4c41fe', 'Published');
  assert.equal(d.summary, phase.reasonEntry('DeletionRequested').summary);
  assert.equal(phase.requestMessage('Team=platform(ready=True)', 'Published'), null);
});
