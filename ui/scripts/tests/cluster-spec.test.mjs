// The OpenShift cluster create form (F4-01, F4-02, F4-05): the spec it sends, the node counts,
// the region, the network choice per provider and the address checks of the cluster's preflight.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const cs = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'clusterSpec.mjs'));
const nc = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'clusterNetworkChoice.mjs'));

const base = (over = {}) => ({
  provider: 'openstack',
  environment: 'example-oso',
  network: '',
  publish: 'External',
  controlPlaneCount: '',
  workerCount: '',
  networking: { ...cs.EMPTY_NETWORKING },
  ...over,
});

/* ---------------------------------------------------------------- counts */

test('0 workers is sent as 0 on every provider (a compact cluster)', () => {
  for (const provider of ['openstack', 'aws', 'gcp']) {
    const spec = cs.buildClusterSpec(base({ provider, controlPlaneCount: '3', workerCount: '0' }));
    assert.equal(spec[provider].workerCount, 0, provider);
    assert.equal(spec[provider].controlPlaneCount, 3, provider);
  }
});

test('empty counts are left out (the installation default applies)', () => {
  for (const provider of ['openstack', 'aws', 'gcp']) {
    const spec = cs.buildClusterSpec(base({ provider }));
    assert.equal('workerCount' in spec[provider], false, provider);
    assert.equal('controlPlaneCount' in spec[provider], false, provider);
  }
});

test('counts the API refuses are refused with a reason and never sent', () => {
  assert.equal(cs.countProblem('0', 'controlPlane'), 'min');
  assert.equal(cs.countProblem('-1', 'worker'), 'min');
  assert.equal(cs.countProblem('1.5', 'worker'), 'integer');
  assert.equal(cs.countProblem('abc', 'controlPlane'), 'integer');
  assert.equal(cs.countProblem('1001', 'worker'), 'max');
  assert.equal(cs.countProblem('', 'controlPlane'), null);
  assert.equal(cs.countProblem('1', 'controlPlane'), null);
  assert.equal(cs.countProblem('0', 'worker'), null);
  const spec = cs.buildClusterSpec(base({ controlPlaneCount: '0', workerCount: '-2' }));
  assert.equal('controlPlaneCount' in spec.openstack, false);
  assert.equal('workerCount' in spec.openstack, false);
  const p = cs.clusterFormProblems(base({ controlPlaneCount: '0' }), { provider: 'openstack', hasNetwork: false, networkSpace: [], reserved: [] });
  assert.equal(p.controlPlane, 'min');
});

/* ---------------------------------------------------------------- region and shape */

test('the region is never sent', () => {
  for (const provider of ['aws', 'gcp']) {
    const spec = cs.buildClusterSpec(base({ provider }));
    assert.equal('region' in spec[provider], false, provider);
  }
});

test('the AWS spec keeps its cluster type; Google Cloud has none', () => {
  assert.equal(cs.buildClusterSpec(base({ provider: 'aws' })).aws.clusterType, 'standalone');
  assert.equal('clusterType' in cs.buildClusterSpec(base({ provider: 'gcp' })).gcp, false);
});

test('the access groups are sent only when one is chosen', () => {
  assert.equal('toolRbac' in cs.buildClusterSpec(base({ toolRbac: { clusterAdminRbac: [] } })), false);
  const spec = cs.buildClusterSpec(base({ toolRbac: { clusterAdminRbac: ['admins'], clusterViewerRbac: [] } }));
  assert.deepEqual(spec.toolRbac, { clusterAdminRbac: ['admins'] });
});

/* ---------------------------------------------------------------- network choice */

test('OpenStack: a network of the tenant or an external network of the site, never both', () => {
  const onNet = cs.buildClusterSpec(base({ network: cs.choiceValue({ kind: 'network', name: 'web' }) }));
  assert.equal(onNet.openstack.network, 'web');
  assert.equal('externalNetwork' in onNet.openstack, false);
  assert.equal('publish' in onNet.openstack, false);
  const onExt = cs.buildClusterSpec(base({ network: 'external:public' }));
  assert.equal(onExt.openstack.externalNetwork, 'public');
  assert.equal('network' in onExt.openstack, false);
  const none = cs.buildClusterSpec(base());
  assert.equal('network' in none.openstack, false);
  assert.equal('externalNetwork' in none.openstack, false);
});

test('AWS and Google Cloud: a network with its publish mode; without a network nothing is sent', () => {
  for (const provider of ['aws', 'gcp']) {
    const spec = cs.buildClusterSpec(base({ provider, network: 'network:web', publish: 'Internal' }));
    assert.equal(spec[provider].network, 'web');
    assert.equal(spec[provider].publish, 'Internal');
    const bare = cs.buildClusterSpec(base({ provider, publish: 'Internal' }));
    assert.equal('network' in bare[provider], false);
    assert.equal('publish' in bare[provider], false);
    const p = cs.clusterFormProblems(base({ provider, publish: 'Internal' }), { provider, hasNetwork: false, networkSpace: [], reserved: [] });
    assert.equal(p.publish, 'internalNeedsNetwork');
  }
});

test('choice values round-trip; anything else is no choice', () => {
  for (const c of [{ kind: 'network', name: 'a' }, { kind: 'external', name: 'b:c' }]) assert.deepEqual(cs.parseChoice(cs.choiceValue(c)), c);
  assert.deepEqual(cs.parseChoice(''), { kind: 'none' });
  assert.deepEqual(cs.parseChoice('other:x'), { kind: 'none' });
});

/* ---------------------------------------------------------------- address settings */

const ctx = (over = {}) => ({ provider: 'openstack', hasNetwork: true, networkSpace: ['10.20.0.0/16', 'fd00:20::/48'], reserved: [], ...over });
const net = (over = {}) => ({ ...cs.EMPTY_NETWORKING, ...over });

test('address settings are sent only when typed; hostPrefix as a number', () => {
  assert.equal('networking' in cs.buildClusterSpec(base()), false);
  const spec = cs.buildClusterSpec(base({ networking: net({ clusterNetwork: '10.128.0.0/14', hostPrefix: '23', serviceNetwork: '10.96.0.0/16' }) }));
  assert.deepEqual(spec.networking, { clusterNetwork: '10.128.0.0/14', hostPrefix: 23, serviceNetwork: '10.96.0.0/16' });
});

test('the machine network is not sent with an AWS or Google Cloud network', () => {
  const spec = cs.buildClusterSpec(base({ provider: 'aws', network: 'network:web', networking: net({ machineNetwork: '10.0.0.0/16' }) }));
  assert.equal(spec.networking, undefined);
  const problems = cs.checkClusterNetworking(net({ machineNetwork: '10.0.0.0/16' }), ctx({ provider: 'gcp' }));
  assert.deepEqual(problems.map((p) => p.code), ['notWithNetwork']);
  const own = cs.buildClusterSpec(base({ provider: 'aws', networking: net({ machineNetwork: '10.0.0.0/16' }) }));
  assert.deepEqual(own.networking, { machineNetwork: '10.0.0.0/16' });
});

test('format, canonical form, lengths and host prefix', () => {
  const codes = (n, c = ctx()) => cs.checkClusterNetworking(net(n), c).map((p) => `${p.field}:${p.code}`);
  assert.deepEqual(codes({ clusterNetwork: 'nope' }), ['clusterNetwork:cidr']);
  assert.deepEqual(codes({ clusterNetwork: '10.129.0.0/14' }), ['clusterNetwork:canonical']);
  assert.deepEqual(codes({ clusterNetwork: '10.128.0.0/23' }), ['clusterNetwork:length']);
  assert.deepEqual(codes({ serviceNetwork: '10.96.0.0/11' }), ['serviceNetwork:length']);
  assert.deepEqual(codes({ machineNetwork: '10.0.0.0/28' }), ['machineNetwork:length']);
  assert.deepEqual(codes({ hostPrefix: '15' }), ['hostPrefix:hostPrefixRange']);
  assert.deepEqual(codes({ hostPrefix: '14' }), ['hostPrefix:hostPrefixRange']);
  // the host prefix must be longer than the pod network (OpenShift's default /14 when it is empty)
  assert.deepEqual(codes({ clusterNetwork: '10.128.0.0/18', hostPrefix: '18' }), ['hostPrefix:hostPrefixShort']);
  assert.deepEqual(codes({ hostPrefix: '23' }), []);
  assert.deepEqual(codes({ clusterNetwork: 'fd00::/48' }), ['clusterNetwork:cidr']);
});

test('the ranges must not overlap each other or the network (AddressConflict, naming both)', () => {
  const ps = cs.checkClusterNetworking(net({ clusterNetwork: '10.128.0.0/14', serviceNetwork: '10.128.0.0/16' }), ctx());
  assert.deepEqual(
    ps.map((p) => [p.field, p.code, p.other]),
    [
      ['clusterNetwork', 'AddressConflict', 'field:serviceNetwork'],
      ['serviceNetwork', 'AddressConflict', 'field:clusterNetwork'],
    ],
  );
  const onNet = cs.checkClusterNetworking(net({ machineNetwork: '10.20.4.0/24' }), ctx());
  assert.deepEqual(onNet.map((p) => [p.code, p.other]), [['AddressConflict', 'network:10.20.0.0/16']]);
  const t = (k, o) => `${k}|${JSON.stringify(o ?? {})}`;
  const text = nc.networkingProblemText(t, onNet[0]);
  assert.match(text, /clusterNet\.reason\.AddressConflict/);
  assert.match(text, /10\.20\.4\.0\/24/);
  assert.match(text, /clusterNet\.networkOf/);
});

test('a reserved range is refused with PrefixReserved naming the range and its purpose', () => {
  const reserved = [
    { prefix: '198.51.100.0/24', purpose: "Networks the platform's management connects from", rule: 'Refused' },
    { prefix: '10.30.0.0/16', purpose: 'Addresses the site uses itself', rule: 'SteppedAround', site: 'site-osp' },
  ];
  const hit = cs.checkClusterNetworking(net({ serviceNetwork: '198.51.100.0/24' }), ctx({ reserved }));
  assert.deepEqual(hit.map((p) => [p.code, p.range]), [['PrefixReserved', '198.51.100.0/24']]);
  // a site's own range applies on that site only
  assert.equal(cs.checkClusterNetworking(net({ machineNetwork: '10.30.0.0/16' }), ctx({ reserved, site: 'site-aws' })).length, 0);
  assert.equal(cs.checkClusterNetworking(net({ machineNetwork: '10.30.0.0/16' }), ctx({ reserved, site: 'site-osp' }))[0].code, 'PrefixReserved');
  // as the cluster job (shc_fabric.py check-cluster-networks): an AWS or Google Cloud machine network without a
  // network is the cluster's own VPC and is not checked against the reserved ranges; the pod network still is
  for (const provider of ['aws', 'gcp']) {
    assert.equal(cs.checkClusterNetworking(net({ machineNetwork: '198.51.100.0/24' }), ctx({ reserved, provider, hasNetwork: false })).length, 0);
    assert.equal(cs.checkClusterNetworking(net({ clusterNetwork: '198.51.100.0/22' }), ctx({ reserved, provider, hasNetwork: false }))[0].code, 'PrefixReserved');
  }
  const t = (k, o) => `${k}|${JSON.stringify(o ?? {})}`;
  const text = nc.networkingProblemText(t, hit[0]);
  assert.match(text, /PrefixReserved/);
  assert.match(text, /reserved|management connects from/);
});

test('an empty field whose OpenShift default overlaps the network gets a hint, not a refusal', () => {
  const c = ctx({ networkSpace: ['10.0.0.0/8'] });
  assert.equal(cs.checkClusterNetworking(net(), c).length, 0);
  const hints = cs.defaultOverlapHints(net(), c);
  assert.deepEqual(hints.map((h) => h.field), ['clusterNetwork', 'machineNetwork']);
  assert.equal(cs.defaultOverlapHints(net({ clusterNetwork: '10.128.0.0/14' }), ctx()).length, 0);
  // AWS with a network: the machine network is the placement's subnet
  assert.deepEqual(cs.defaultOverlapHints(net(), ctx({ provider: 'aws', networkSpace: ['10.0.0.0/8'] })).map((h) => h.field), ['clusterNetwork']);
});

test('Review R2 L7: an empty field whose default overlaps a typed field gets a hint (the job fills it, then refuses)', () => {
  // clusterNetwork 10.0.0.0/14 typed, machineNetwork left empty: the default machine network 10.0.0.0/16 overlaps it
  const hints = cs.defaultOverlapHints(net({ clusterNetwork: '10.0.0.0/14' }), ctx());
  assert.deepEqual(hints.map((h) => [h.field, h.code, h.other]), [['machineNetwork', 'AddressConflict', 'field:clusterNetwork']]);
  // and the other way round
  const back = cs.defaultOverlapHints(net({ machineNetwork: '10.128.0.0/16' }), ctx());
  assert.deepEqual(back.map((h) => [h.field, h.other]), [['clusterNetwork', 'field:machineNetwork']]);
  // no partner, no hint: AWS with a network has no machine network to type
  assert.equal(cs.defaultOverlapHints(net({ clusterNetwork: '10.0.0.0/14' }), ctx({ provider: 'aws', hasNetwork: true })).length, 0);
});
