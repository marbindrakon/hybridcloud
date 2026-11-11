// Status library (status/phase.ts): the TypeScript port of derive_phase runs the
// shared fixtures of the Ansible status library, plus the UI-only helpers.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const dir = process.env.SHELL_BUNDLE_DIR;
const root = process.env.UI_ROOT;
const phase = await import(path.join(dir, 'phase.mjs'));

const fixturesPath = path.join(root, '..', 'tests', 'fixtures', 'status-phase-cases.json');

test('derivePhase matches the shared status fixtures (same rules as shc_status.derive_phase)', (t) => {
  if (!fs.existsSync(fixturesPath)) {
    t.skip('tests/fixtures/status-phase-cases.json is not on this branch');
    return;
  }
  const cases = JSON.parse(fs.readFileSync(fixturesPath, 'utf8'));
  assert.ok(cases.length >= 24, 'at least 24 shared cases');
  for (const c of cases) {
    assert.equal(phase.derivePhase(c.object), c.expect.phase, c.name);
    // Without a stored phase, phaseOf is the derivation.
    assert.equal(phase.phaseOf(c.object), c.expect.phase, `${c.name} (phaseOf)`);
  }
});

test('stored status.phase wins, except over a deletion', () => {
  const obj = { metadata: { name: 'a', generation: 1 }, status: { phase: 'Degraded', ready: true } };
  assert.equal(phase.phaseOf(obj), 'Degraded');
  assert.equal(phase.phaseOf({ ...obj, metadata: { ...obj.metadata, deletionTimestamp: '2026-10-21T10:00:00Z' } }), 'Deleting');
  // An unknown word is ignored and the phase is derived.
  assert.equal(phase.phaseOf({ metadata: { name: 'b' }, status: { phase: 'Bogus', ready: true } }), 'Ready');
});

test('summaryOf prefers the stored summary, then the registry sentence, then the phase sentence', () => {
  assert.equal(phase.summaryOf({ metadata: {}, status: { phase: 'Creating', summary: 'Being created.' } }), 'Being created.');
  const waiting = {
    metadata: { generation: 1 },
    status: { conditions: [{ type: 'Ready', status: 'False', reason: 'WaitingForFabric', message: 'raw text' }] },
  };
  assert.equal(phase.summaryOf(waiting), 'Waiting for the network fabric to be ready.');
  assert.equal(phase.summaryOf({ metadata: {}, status: { ready: true } }), 'Ready.');
  const long = 'x '.repeat(200);
  assert.ok(phase.summaryOf({ metadata: {}, status: { phase: 'Failed', summary: long } }).length <= phase.SUMMARY_MAX);
});

// P2AC-O5: a waiting cluster assignment names its team setup (and app projects) when they are not ready.
test('a waiting cluster assignment names what it waits for', () => {
  const asg = (reason, st) => ({
    metadata: { generation: 1 },
    status: {
      phase: 'Waiting',
      summary: 'Waiting for a cluster: name the OpenShift cluster to deploy onto.',
      conditions: [{ type: 'Ready', status: 'False', reason }],
      ...st,
    },
  });
  assert.equal(
    phase.summaryOf(asg('WaitingForCluster', { team: 'platform', teamReady: false, openshift: '', openshiftReady: false, projects: [{ name: 'web', ready: true }] })),
    'Waiting for team setup platform to be ready, and for a cluster: name the OpenShift cluster to deploy onto.',
  );
  // the automation writes the flags as strings too
  assert.equal(
    phase.summaryOf(asg('LinkedNotReady', { team: 'platform', teamReady: 'False', openshift: 'oc1', openshiftReady: 'False', projects: [{ name: 'web', ready: false }, { name: 'api', ready: false }] })),
    'Waiting for team setup platform, app projects web, api and OpenShift cluster oc1 to be ready.',
  );
  assert.equal(
    phase.summaryOf(asg('LinkedNotReady', { team: 'platform', teamReady: true, openshift: 'oc1', openshiftReady: false, projects: [] })),
    'Waiting for OpenShift cluster oc1 to be ready.',
  );
  // only the cluster is missing: the stored sentence stays
  assert.equal(
    phase.summaryOf(asg('WaitingForCluster', { team: 'platform', teamReady: true, openshift: '', openshiftReady: false })),
    'Waiting for a cluster: name the OpenShift cluster to deploy onto.',
  );
  // other kinds and other phases are untouched
  assert.equal(phase.linkedWaitSummary({ metadata: {}, status: { phase: 'Waiting', conditions: [{ type: 'Ready', status: 'False', reason: 'LinkedNotReady' }] } }), null);
  assert.equal(phase.linkedWaitSummary({ ...asg('LinkedNotReady', { teamReady: false, openshiftReady: false }), status: { phase: 'Ready', ready: true, teamReady: false, openshiftReady: false } }), null);
});

test('reasonOf names the deciding condition', () => {
  const stalled = {
    metadata: {},
    status: {
      conditions: [
        { type: 'Ready', status: 'False', reason: 'InProgress' },
        { type: 'Stalled', status: 'True', reason: 'TeardownFailed' },
      ],
    },
  };
  assert.equal(phase.reasonOf(stalled), 'TeardownFailed');
  const deleting = {
    metadata: { deletionTimestamp: '2026-10-21T10:00:00Z' },
    status: { conditions: [{ type: 'DeletionBlocked', status: 'True', reason: 'WaitingForDependents' }] },
  };
  assert.equal(phase.reasonOf(deleting), 'WaitingForDependents');
  assert.equal(phase.reasonOf({ metadata: {}, status: {} }), undefined);
});

test('in-flight phases, blockers, retry and health buckets', () => {
  for (const p of ['Waiting', 'Creating', 'Updating', 'Deleting']) assert.ok(phase.isInFlight(p), p);
  for (const p of ['Ready', 'Degraded', 'Failed']) assert.ok(!phase.isInFlight(p), p);
  assert.deepEqual(phase.deletionBlockers({ status: { deletionBlockedBy: ['net-a', { kind: 'NetworkPlacement', name: 'p1' }] } }), [
    { name: 'net-a' },
    { kind: 'NetworkPlacement', name: 'p1', namespace: undefined, reason: undefined },
  ]);
  const ann = { 'ansible.sdk.operatorframework.io/reconcileNow': '2026-10-21T10:00:00Z' };
  assert.equal(phase.retryQueued({ metadata: { annotations: ann }, status: {} }), true);
  assert.equal(phase.retryQueued({ metadata: { annotations: ann }, status: { lastHandledReconcileRequest: '2026-10-21T10:00:00Z' } }), false);
  assert.equal(phase.healthOfPhase('Degraded'), 'failed');
  assert.equal(phase.healthOfPhase('Waiting'), 'pending');
  assert.equal(phase.healthOfPhase('Deleting'), 'reconciling');
  assert.deepEqual(phase.changeNotApplied({ status: { conditions: [{ type: 'Accepted', status: 'False', reason: 'Immutable', message: 'm' }] } }), {
    reason: 'Immutable',
    message: 'm',
  });
});

test('phaseSinceOf falls back to the deciding transition time', () => {
  assert.equal(phase.phaseSinceOf({ metadata: {}, status: { phaseSince: '2026-10-21T09:00:00Z' } }), '2026-10-21T09:00:00Z');
  assert.equal(
    phase.phaseSinceOf({ metadata: {}, status: { conditions: [{ type: 'Ready', status: 'True', lastTransitionTime: '2026-10-20T00:00:00Z' }] } }),
    '2026-10-20T00:00:00Z',
  );
  assert.equal(phase.phaseSinceOf({ metadata: { deletionTimestamp: '2026-10-21T10:00:00Z' }, status: {} }), '2026-10-21T10:00:00Z');
});

// gate S8 UX 4: the specific refusal (which network, range or field) is shown where the tenant looks first
test('detailOf: the deciding condition says what the summary does not', () => {
  const failed = {
    metadata: { name: 'r1', generation: 1 },
    status: {
      phase: 'Failed',
      conditions: [
        { type: 'Ready', status: 'False', reason: 'ExternalNetworkNotListed',
          message: 'PlatformOpenshift r1: nosuch is not an external network of site site-a; listed: ext-a, ext-b' },
        { type: 'Published', status: 'True', reason: 'Published', message: 'PlatformOpenshiftCreateRequested event e-1' },
        { type: 'Preflight', status: 'False', reason: 'ExternalNetworkNotListed',
          message: 'PlatformOpenshift r1: nosuch is not an external network of site site-a; listed: ext-a, ext-b' },
      ],
    },
  };
  assert.equal(phase.detailOf(failed), 'PlatformOpenshift r1: nosuch is not an external network of site site-a; listed: ext-a, ext-b');
  // a waiting cluster: the Preflight condition's message
  const waiting = {
    metadata: { name: 'r10', generation: 1 },
    status: { phase: 'Waiting', reason: 'WaitingForPlacement',
      conditions: [{ type: 'Preflight', status: 'False', reason: 'WaitingForPlacement',
        message: 'PlatformOpenshift r10: NetworkPlacement ns/p1: router attachments are not ready yet (state pending)' }] },
  };
  assert.match(phase.detailOf(waiting), /router attachments are not ready yet/);
  // a degraded placement: the Degraded condition's message
  const degraded = {
    metadata: { name: 'p1', generation: 1 },
    status: { phase: 'Degraded', conditions: [
      { type: 'Ready', status: 'True', reason: 'Ready', message: '' },
      { type: 'Degraded', status: 'True', reason: 'InUse', message: 'Turning this off waits until the environment no longer uses it: floating IP 192.0.2.15' }] },
  };
  assert.match(phase.detailOf(degraded), /floating IP 192\.0\.2\.15/);
  // nothing to add: ready, a request message, or the summary itself
  assert.equal(phase.detailOf({ metadata: { name: 'a' }, status: { phase: 'Ready', conditions: [] } }), undefined);
  const queued = { metadata: { name: 'b', generation: 1 }, status: { phase: 'Creating', conditions: [
    { type: 'Ready', status: 'False', reason: 'AwaitingEDA', message: 'PlatformOpenshiftCreateRequested event e-2' }] } };
  assert.equal(phase.detailOf(queued), undefined);
  const same = { metadata: { name: 'c', generation: 1 }, status: { phase: 'Failed', conditions: [
    { type: 'Ready', status: 'False', reason: 'NetworkChoiceRequired', message: 'This site has no default external network. Choose a network.' }] } };
  assert.equal(phase.detailOf(same), undefined);
});
