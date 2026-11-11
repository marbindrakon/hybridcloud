// Which networks a cluster can use, and the reason shown when one cannot (the cluster preflight's
// refusals and the network's PlatformReachable condition), per provider.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

const nc = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'clusterNetworkChoice.mjs'));

const reach = (status, reason = status === 'True' ? 'Attachable' : 'NotRequested', message = '') => [
  { type: 'Ready', status: 'True', reason: 'Ready' },
  { type: 'PlatformReachable', status, reason, message },
];
const network = (name, conditions = reach('True'), prefixes = ['10.20.0.0/16']) => ({
  metadata: { name },
  spec: { prefixes },
  status: { phase: 'Ready', conditions },
});
const placement = (netName, env, { kind = 'CloudOSO', ready = true, prefixes = ['10.20.1.0/24'], routers, state } = {}) => ({
  metadata: { name: `${netName}-${env}` },
  spec: { network: netName, backend: { kind, name: env }, ...(routers === undefined ? {} : { use: { routerAttachments: routers } }) },
  status: {
    phase: ready ? 'Ready' : 'Creating',
    prefixes,
    ...(state ? { use: { routerAttachments: { state } } } : {}),
  },
});
const profile = (status = {}) => ({ status });
const opts = (over) => ({ provider: 'openstack', environment: 'oso', networks: [], placements: [], profile: null, ...over });
const reasons = (list) => Object.fromEntries(list.map((o) => [o.name, o.block?.reason ?? null]));

test('OpenStack lists only networks placed on the environment, usable ones first', () => {
  const list = nc.hybridNetworkOptions(
    opts({
      networks: [network('b'), network('a'), network('c')],
      placements: [placement('b', 'oso', { routers: true, state: 'ready' }), placement('a', 'oso', { routers: false }), placement('c', 'other', { routers: true, state: 'ready' })],
    }),
  );
  assert.deepEqual(list.map((o) => o.name), ['b', 'a']);
  assert.deepEqual(reasons(list), { b: null, a: 'RouterAttachmentsOff' });
  assert.equal(list[1].block.params.placement, 'a-oso');
  assert.equal(list[0].subnet4, '10.20.1.0/24');
});

test('OpenStack refusals: placement not ready, no IPv4, router attachments being set up', () => {
  const list = nc.hybridNetworkOptions(
    opts({
      networks: [network('wait'), network('v6'), network('setup')],
      placements: [
        placement('wait', 'oso', { ready: false, routers: true, state: 'ready' }),
        placement('v6', 'oso', { prefixes: ['fd00:1::/64'], routers: true, state: 'ready' }),
        placement('setup', 'oso', { routers: true, state: 'pending' }),
      ],
    }),
  );
  assert.deepEqual(reasons(list), { wait: 'WaitingForPlacement', v6: 'PlacementHasNoIPv4', setup: 'WaitingForPlacement' });
});

test('OpenStack refusals from PlatformReachable, with the profile filling the message values', () => {
  const p = profile({
    managedClusterRanges: ['10.0.0.0/8'],
    reservedRanges: [{ prefix: '198.51.100.0/24', purpose: "Networks the platform's management connects from", rule: 'Refused' }],
  });
  const pl = (n) => placement(n, 'oso', { routers: true, state: 'ready' });
  const list = nc.hybridNetworkOptions(
    opts({
      profile: p,
      networks: [
        network('off', reach('False', 'NotRequested')),
        network('outside', reach('False', 'OutsideAttachableRanges')),
        network('overlap', reach('False', 'InfrastructureOverlap'), ['198.51.100.0/24']),
        network('noservice', reach('False', 'CircuitNotDeployed')),
        network('unknown', [{ type: 'Ready', status: 'True' }]),
      ],
      placements: ['off', 'outside', 'overlap', 'noservice', 'unknown'].map(pl),
    }),
  );
  assert.deepEqual(reasons(list), {
    off: 'NotRequested',
    outside: 'OutsideAttachableRanges',
    overlap: 'InfrastructureOverlap',
    noservice: 'CircuitNotDeployed',
    unknown: 'PlatformAccessUnknown',
  });
  const byName = Object.fromEntries(list.map((o) => [o.name, o.block]));
  assert.equal(byName.outside.params.ranges, '10.0.0.0/8');
  assert.equal(byName.overlap.params.range, '198.51.100.0/24');
});

test('a placement subnet outside the managed cluster ranges is refused even when the network is reachable', () => {
  const list = nc.hybridNetworkOptions(
    opts({
      profile: profile({ managedClusterRanges: ['10.64.0.0/10'] }),
      networks: [network('n')],
      placements: [placement('n', 'oso', { routers: true, state: 'ready' })],
    }),
  );
  assert.equal(list[0].block.reason, 'OutsideAttachableRanges');
  // absent list = any range
  const any = nc.hybridNetworkOptions(opts({ profile: profile({}), networks: [network('n')], placements: [placement('n', 'oso', { routers: true, state: 'ready' })] }));
  assert.equal(any[0].block, null);
});

test('AWS and Google Cloud: External needs only a ready IPv4 placement; Internal also platform access', () => {
  for (const [provider, kind] of [['aws', 'CloudAWS'], ['gcp', 'CloudGCP']]) {
    const base = {
      provider,
      environment: 'env',
      networks: [network('n', reach('False', 'NotRequested'))],
      placements: [placement('n', 'env', { kind })],
    };
    assert.equal(nc.hybridNetworkOptions(opts({ ...base, publish: 'External' }))[0].block, null, provider);
    assert.equal(nc.hybridNetworkOptions(opts({ ...base, publish: 'Internal' }))[0].block.reason, 'NotRequested', provider);
    // a CloudOSO placement of the same name is not one of this environment
    assert.equal(nc.hybridNetworkOptions(opts({ ...base, placements: [placement('n', 'env')] })).length, 0);
  }
});

test('external networks of the site: default first, IPv6-only refused, absent list = unknown', () => {
  const p = profile({
    sites: [
      {
        name: 'site-osp',
        type: 'openstack',
        state: 'Available',
        externalNetworks: [
          { name: 'zeta', families: [4] },
          { name: 'v6only', families: [6] },
          { name: 'public', families: [4, 6], default: true },
        ],
      },
      { name: 'site-old', type: 'openstack', state: 'Available' },
    ],
  });
  const list = nc.externalNetworkOptions(p, 'site-osp');
  assert.deepEqual(list.map((o) => [o.name, o.default, o.block?.reason ?? null]), [
    ['public', true, null],
    ['v6only', false, 'NoIPv4'],
    ['zeta', false, null],
  ]);
  assert.equal(nc.defaultExternalChoice(list), 'public');
  assert.equal(nc.emptyChoiceBlock('openstack', list), null);
  assert.equal(nc.externalNetworkOptions(p, 'site-old'), null);
  assert.equal(nc.emptyChoiceBlock('openstack', null), null);
  const noDefault = nc.externalNetworkOptions(profile({ sites: [{ name: 's', externalNetworks: [{ name: 'x' }] }] }), 's');
  assert.equal(nc.defaultExternalChoice(noDefault), undefined);
  assert.equal(nc.emptyChoiceBlock('openstack', noDefault).reason, 'NetworkChoiceRequired');
  assert.equal(nc.emptyChoiceBlock('aws', noDefault), null);
});

test('every refusal of the design has a text key; unknown reasons fall back to the API message', () => {
  const t = (k, o) => `${k}${o && Object.keys(o).length ? JSON.stringify(o) : ''}`;
  for (const r of [
    'RouterAttachmentsOff', 'WaitingForPlacement', 'PlacementHasNoIPv4', 'NoIPv4', 'PlatformAccessOff', 'NotRequested',
    'OutsideAttachableRanges', 'ServiceNotOffered', 'CircuitNotDeployed', 'HubEndpointNotAdmitted', 'WaitingForPlatformAccess',
    'AttachPending', 'InfrastructureOverlap', 'AddressConflict', 'PrefixReserved', 'ServicePrefixInUse', 'NetworkChoiceRequired',
    'ExternalNetworkNotListed', 'InUse',
  ]) {
    assert.ok(nc.reasonKey(r).startsWith('clusterNet.reason.'), r);
  }
  assert.equal(nc.reasonText(t, { reason: 'SomethingElse', message: 'the API says so' }), 'the API says so');
  // a text that needs values the UI does not have shows the API's message instead
  assert.equal(nc.reasonText(t, { reason: 'InfrastructureOverlap', params: { range: '' }, message: 'overlaps 198.51.100.0/24' }), 'overlaps 198.51.100.0/24');
  assert.match(nc.reasonText(t, { reason: 'ExternalNetworkNotListed', params: { name: 'x' } }), /ExternalNetworkNotListed.*"name":"x"/);
});

test('a typed network prefix in a refused range gets the PrefixReserved text; stepped-around rows do not refuse', () => {
  const reserved = [
    { prefix: '198.51.100.0/24', purpose: 'p', rule: 'Refused' },
    { prefix: '10.30.0.0/16', purpose: 'q', rule: 'SteppedAround', site: 'site-osp' },
    { prefix: '10.40.0.0/16', purpose: 'r', rule: 'SteppedAround' },
  ];
  const t = (k, o) => `${k}${JSON.stringify(o)}`;
  assert.match(nc.refusedPrefixText(t, '198.51.0.0/16', reserved), /PrefixReserved.*198\.51\.100\.0\/24/);
  assert.equal(nc.refusedPrefixText(t, '10.30.0.0/16', reserved), null);
  assert.equal(nc.refusedPrefixText(t, '10.40.0.0/24', reserved), null);
  assert.equal(nc.refusedPrefixText(t, '10.50.0.0/16', undefined), null);
});

test('Review R2 L6: a network the platform is still connecting (AttachPending) is offered, as the job accepts it', () => {
  const list = nc.hybridNetworkOptions(
    opts({
      profile: profile({}),
      networks: [network('pending', reach('False', 'AttachPending')), network('off', reach('False', 'NotRequested'))],
      placements: ['pending', 'off'].map((n) => placement(n, 'oso', { routers: true, state: 'ready' })),
    }),
  );
  assert.deepEqual(reasons(list), { pending: null, off: 'NotRequested' });
});
