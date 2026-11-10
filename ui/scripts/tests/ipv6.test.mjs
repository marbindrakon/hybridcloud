// Per-family addressing (forms/ipv6.ts): the CRD mirror, the derived mode, the hints, the site
// capability gate of the placement IPv6 inputs and the guest configuration reference.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const m = await import(path.join(process.env.SHELL_BUNDLE_DIR, 'ipv6.mjs'));
const load = (l) => JSON.parse(fs.readFileSync(path.join(process.env.UI_ROOT, `packages/shared/src/i18n/locales/${l}.json`), 'utf8'));
const tOf = (cat) => (key, opts = {}) => {
  const s = key.split('.').reduce((n, p) => (n && typeof n === 'object' ? n[p] : undefined), cat);
  const out = typeof s === 'string' ? s : opts.defaultValue ?? key;
  return String(out).replace(/\{\{\s*(\w+)\s*\}\}/g, (_m, k) => (opts[k] !== undefined ? String(opts[k]) : ''));
};
const tEn = tOf(load('en'));
const KIND_NAME = /\b(HybridNetwork|NetworkPlacement|CloudVirt|CloudOSO|HybridFabric)\b/;
const codes = (errs) => errs.map((e) => e.code).sort();

test('mode follows the fields: platform block, tenant IPv6 prefix, neither', () => {
  assert.equal(m.deriveIpv6Mode({ prefixes: ['10.60.0.0/16'] }), 'None');
  assert.equal(m.deriveIpv6Mode({ prefixes: ['10.60.0.0/16', 'fd12:3456:789a::/48'] }), 'Tenant');
  assert.equal(m.deriveIpv6Mode({ prefixes: ['2001:db8:abcd::/48'] }), 'Tenant');
  assert.equal(m.deriveIpv6Mode({ prefixes: ['10.60.0.0/16'], platformPrefixes: [{ family: 6 }] }), 'Platform');
  assert.equal(m.deriveIpv6Mode({ platformPrefixes: [{ family: 6, prefixLength: 56 }] }), 'Platform');
  assert.equal(m.deriveIpv6Mode(undefined), 'None');
});

test('the reported mode wins over the derived one', () => {
  const n = { metadata: { name: 'n' }, spec: { prefixes: ['10.0.0.0/16'] }, status: { ipv6: { mode: 'Tenant' } } };
  assert.deepEqual(m.networkIpv6Mode(n), { mode: 'Tenant', reported: true });
  assert.deepEqual(m.networkIpv6Mode({ ...n, status: {} }), { mode: 'None', reported: false });
});

test('accepted addressing: IPv4 only, dual-stack tenant, IPv6-only tenant ULA, platform only, IPv4 + platform', () => {
  assert.deepEqual(m.checkNetworkAddressing(['10.60.0.0/16']), []);
  assert.deepEqual(m.checkNetworkAddressing(['10.60.0.0/16', 'fd12:3456:789a::/48']), []);
  assert.deepEqual(m.checkNetworkAddressing(['fc00::/32']), []);
  assert.deepEqual(m.checkNetworkAddressing(['3fff:ffff::/32']), []);
  assert.deepEqual(m.checkNetworkAddressing([], [{ family: 6 }]), []);
  assert.deepEqual(m.checkNetworkAddressing(['10.60.0.0/16'], [{ family: 6, prefixLength: 56 }]), []);
  assert.deepEqual(m.checkNetworkAddressing([], [{ family: 6, prefixLength: 56, prefix: '2001:db8:0:100::/56' }]), []);
  assert.deepEqual(m.checkNetworkAddressing([], [{ family: 6, prefix: '2001:db8:0:100::/56' }]), []);
});

test('refused addressing mirrors the CRD messages', () => {
  assert.deepEqual(codes(m.checkNetworkAddressing([])), ['noSpace']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['10.60.0.1/16'])), ['notCanonical']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['FD12::/48'])), ['notCanonical']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['10.0.0.0/30'])), ['length']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['fd12::/65'])), ['length']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['::/32'])), ['scope']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['fe80::/48'])), ['scope']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['ff00::/32'])), ['scope']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['4000::/32'])), ['scope']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['10.0.0.0/16', '10.0.0.0/16'])), ['duplicate']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['10.0.0.0/16', '10.0.1.0/24'])), ['overlap']);
  const nine = Array.from({ length: 9 }, (_v, i) => `10.${i}.0.0/16`);
  assert.deepEqual(codes(m.checkNetworkAddressing(nine)), ['tooMany']);
  assert.deepEqual(codes(m.checkNetworkAddressing(['fd12::/48'], [{ family: 6 }])), ['twoIpv6Sources']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefixLength: 50 }])), ['platformLength']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefixLength: 44 }])), ['platformLength']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefix: '2001:db8::1/56' }])), ['platformPrefix']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefix: '10.0.0.0/16' }])), ['platformPrefix']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefix: '2001:db8::/50' }])), ['platformPrefixLength']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6, prefixLength: 60, prefix: '2001:db8::/56' }])), ['platformMismatch']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 4 }])), ['platformFamily']);
  assert.deepEqual(codes(m.checkNetworkAddressing([], [{ family: 6 }, { family: 6 }])), ['platformDuplicateFamily']);
});

test('job-only rules are hints: reserved ranges, overlap with the tenant\'s other networks, offered sizes, pins', () => {
  const other = { metadata: { name: 'core' }, spec: { prefixes: ['10.60.0.0/16', 'fd12:3456:789a::/48'] } };
  const h = m.networkAddressingHints({ prefixes: ['fd12:3456:789a:100::/56'], others: [other], egressInternet: true });
  assert.deepEqual(h.map((x) => x.code).sort(), ['reservedRanges', 'tenantNoInternet', 'tenantOverlap']);
  assert.equal(h.find((x) => x.code === 'tenantOverlap').network, 'core');
  assert.deepEqual(m.networkAddressingHints({ prefixes: ['10.61.0.0/16'], others: [other] }), []);
  const offer = { blockLengths: [56, 60], defaultBlock: 56 };
  const p = m.networkAddressingHints({ prefixes: [], platform: [{ family: 6, prefixLength: 52 }], offer });
  assert.deepEqual(p.map((x) => x.code), ['blockNotOffered']);
  assert.deepEqual(m.networkAddressingHints({ prefixes: [], platform: [{ family: 6, prefixLength: 60 }], offer }), []);
  assert.deepEqual(
    m.networkAddressingHints({ prefixes: [], platform: [{ family: 6, prefix: '2001:db8::/56' }] }).map((x) => x.code),
    ['pinnedBlock'],
  );
});

test('the fabric offer is read from status.realization.ipam.pools (list or map); none without an IPv6 pool', () => {
  // no pool rows reported (yet): the form cannot tell
  assert.equal(m.fabricIpv6Offer({ status: {} }), undefined);
  assert.equal(m.fabricIpv6Offer({ status: { realization: { model: 'x' } } }), undefined);
  assert.equal(m.fabricIpv6Offer({ status: { realization: { ipam: {} } } }), undefined);
  assert.equal(m.fabricIpv6Offer(undefined), undefined);
  // rows reported and none of them IPv6: the fabric offers no platform IPv6 space
  assert.equal(m.fabricIpv6Offer({ status: { realization: { ipam: { pools: [] } } } }), null);
  assert.equal(m.fabricIpv6Offer({ status: { realization: { ipam: { pools: [{ family: 4, blockLengths: [24] }] } } } }), null);
  assert.deepEqual(
    m.fabricIpv6Offer({ status: { realization: { ipam: { pools: [{ name: 'p', family: 6, blockLengths: [60, 56], defaultBlock: 56 }] } } } }),
    { blockLengths: [56, 60], defaultBlock: 56 },
  );
  assert.deepEqual(
    m.fabricIpv6Offer({ status: { realization: { ipam: { pools: { p: { family: 6, blockLengths: [56] } } } } } }),
    { blockLengths: [56] },
  );
  assert.equal(m.profileIpv6Offer({ status: {} }), null);
  assert.deepEqual(m.profileIpv6Offer({ status: { ipv6Platform: { blockLengths: [56, 60], defaultBlockLength: 56 } } }), {
    blockLengths: [56, 60],
    defaultBlock: 56,
  });
});

test('site capabilities from the platform view and from the tenant view', () => {
  assert.deepEqual(m.capsFromInfrastructure({ status: { capabilities: { evpn: true } } }), m.NO_IPV6);
  assert.deepEqual(m.capsFromInfrastructure({ status: { capabilities: { ipv6: false, ipv6Only: true } } }), m.NO_IPV6);
  assert.deepEqual(
    m.capsFromInfrastructure({ status: { capabilities: { ipv6: true, ipv6Only: true, ipv6NetworkSource: true, ipv6ProviderSource: true } } }),
    { ipv6: true, ipv6Only: true, sources: ['Network', 'Provider'], translation: false },
  );
  assert.deepEqual(m.capsFromProfileSite({ name: 's', type: 'openshift', state: 'Available', capabilities: ['HybridNetworking'] }), m.NO_IPV6);
  assert.deepEqual(m.capsFromProfileSite({ name: 's', type: 'aws', state: 'Available', capabilities: ['IPv6', 'IPv6ProviderSource'] }), {
    ipv6: true,
    ipv6Only: false,
    sources: ['Provider'],
    translation: false,
  });
  assert.equal(m.environmentSiteName({ spec: { cloudRef: { name: 'site-a' } } }), 'site-a');
  assert.equal(m.environmentSiteName({ spec: {}, status: { site: { name: 'site-b' } } }), 'site-b');
  assert.equal(m.environmentSiteName({ spec: {} }), undefined);
});

test('placement IPv6 inputs only where the site reports IPv6 and the network has IPv6 (none today)', () => {
  const none = m.placementIpv6Offer({ kind: 'CloudVirt', caps: m.NO_IPV6, networkMode: 'Platform' });
  assert.equal(none.show, false);
  assert.equal(none.block, 'siteNoIpv6');
  assert.equal(m.placementIpv6Offer({ kind: 'CloudVirt', caps: undefined, networkMode: 'Tenant' }).block, 'siteUnknown');
  const caps = { ipv6: true, ipv6Only: false, sources: ['Network', 'Provider'], translation: false };
  assert.equal(m.placementIpv6Offer({ kind: 'CloudVirt', caps, networkMode: 'None' }).block, 'networkNoIpv6');
  const virt = m.placementIpv6Offer({ kind: 'CloudVirt', caps, networkMode: 'Tenant' });
  assert.equal(virt.show, true);
  assert.deepEqual(virt.sources, []);
  assert.deepEqual(virt.lengths, [64]);
  assert.equal(virt.sizeRequired, true);
  // Provider needs a platform-assigned network
  assert.deepEqual(m.placementIpv6Offer({ kind: 'CloudAWS', caps, networkMode: 'Tenant' }).sources, ['Network']);
  const aws = m.placementIpv6Offer({ kind: 'CloudAWS', caps, networkMode: 'Platform' });
  assert.deepEqual(aws.sources, ['Network', 'Provider']);
  assert.deepEqual(aws.lengths, [60, 56]);
  assert.equal(aws.sizeRequired, false);
});

test('spec.ipv6 from the placement form', () => {
  assert.deepEqual(m.buildPlacementIpv6({ kind: 'CloudVirt', source: '', prefixLength: '64', pinned: '' }), { prefixLength: 64 });
  assert.deepEqual(m.buildPlacementIpv6({ kind: 'CloudVirt', source: '', prefixLength: '', pinned: 'fd00:1:0:1::/64' }), {
    prefix: 'fd00:1:0:1::/64',
  });
  assert.equal(m.buildPlacementIpv6({ kind: 'CloudVirt', source: '', prefixLength: '', pinned: '' }), null);
  assert.equal(m.buildPlacementIpv6({ kind: 'CloudOSO', source: 'Provider', prefixLength: '', pinned: '' }), null);
  assert.deepEqual(m.buildPlacementIpv6({ kind: 'CloudAWS', source: 'Provider', prefixLength: '60', pinned: '' }), { source: 'Provider' });
  assert.deepEqual(m.buildPlacementIpv6({ kind: 'CloudAWS', source: '', prefixLength: '', pinned: '' }), {});
  assert.deepEqual(m.buildPlacementIpv6({ kind: 'CloudAWS', source: 'Network', prefixLength: '56', pinned: '' }), {
    prefixLength: 56,
    source: 'Network',
  });
  assert.equal(m.isPinnedIpv6Prefix('fd00:1:0:1::/64', [64], ['fd00:1::/48']), true);
  assert.equal(m.isPinnedIpv6Prefix('fd00:2:0:1::/64', [64], ['fd00:1::/48']), false);
  assert.equal(m.isPinnedIpv6Prefix('fd00:1::/60', [64]), false);
});

test('placement subnets: status.subnets, else status.prefixes', () => {
  const p = { metadata: { name: 'p' }, spec: { network: 'n' }, status: { prefixes: ['10.60.1.0/24', 'fd00:1:0:1::/64'] } };
  assert.deepEqual(m.placementSubnets(p), [
    { family: 4, prefix: '10.60.1.0/24' },
    { family: 6, prefix: 'fd00:1:0:1::/64' },
  ]);
  const q = { ...p, status: { prefixes: ['10.60.1.0/24'], subnets: [{ family: 4, prefix: '10.60.1.0/24', gateway: '10.60.1.1', source: 'Network' }] } };
  assert.equal(m.placementSubnet(q, 4).gateway, '10.60.1.1');
  assert.equal(m.placementSubnet(q, 6), undefined);
  const refused = { status: { conditions: [{ type: 'Ready', status: 'False', reason: 'FamilyUnavailable', message: 'IPv6 is not available on this environment\'s site' }] } };
  assert.deepEqual(m.familyUnavailable(refused), { message: 'IPv6 is not available on this environment\'s site' });
  assert.equal(m.familyUnavailable({ status: { conditions: [{ reason: 'Ready' }] } }), null);
});

test('placement translation: path and the provider translator source when reported, nothing without a prefix', () => {
  const at = (translation) => ({ metadata: { name: 'p' }, spec: { network: 'n' }, status: { ipv6: { translation } } });
  assert.deepEqual(m.placementTranslation(at({ path: 'Fabric', pref64: '2001:db8:64::/96', resolvers: ['2001:db8::53'] })), {
    path: 'Fabric',
    pref64: '2001:db8:64::/96',
    resolvers: ['2001:db8::53'],
  });
  assert.deepEqual(
    m.placementTranslation(at({ path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['fd00:db8::253', ''], ipv4Source: ' 192.0.2.7 ' })),
    { path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['fd00:db8::253'], ipv4Source: '192.0.2.7' },
  );
  // written before path existed: prefix and resolvers only
  assert.deepEqual(m.placementTranslation(at({ pref64: '64:ff9b::/96' })), { pref64: '64:ff9b::/96', resolvers: [] });
  assert.equal(m.placementTranslation(at(null)), null);
  assert.equal(m.placementTranslation(at({ path: 'Provider', ipv4Source: '192.0.2.7' })), null);
  assert.equal(m.placementTranslation(undefined), null);
  // the path reads in display words in both catalogues
  for (const l of ['en', 'fr']) {
    const t = tOf(load(l));
    for (const k of ['ipv6.path_Fabric', 'ipv6.path_Provider', 'ipv6.translationPrefix', 'ipv6.translationSource']) {
      assert.notEqual(t(k), k, `${l}: ${k}`);
    }
  }
});

test('placement families: IPv4 stays unless the site offers IPv6-only; IPv6-only networks need such a site', () => {
  const hidden = m.placementIpv6Offer({ kind: 'CloudVirt', caps: m.NO_IPV6, networkMode: 'Tenant' });
  assert.deepEqual(m.placementFamilies({ offer: hidden, networkHasIpv4: true, includeIpv4: false, includeIpv6: true }), { ipv4: true, ipv6: false });
  assert.deepEqual(m.placementFamilies({ offer: hidden, networkHasIpv4: false, includeIpv4: true, includeIpv6: true }), {
    ipv4: false,
    ipv6: false,
    block: 'noFamilyHere',
  });
  const dual = m.placementIpv6Offer({ kind: 'CloudVirt', caps: { ipv6: true, ipv6Only: false, sources: [], translation: false }, networkMode: 'Tenant' });
  assert.deepEqual(m.placementFamilies({ offer: dual, networkHasIpv4: true, includeIpv4: false, includeIpv6: true }), { ipv4: true, ipv6: true });
  assert.equal(m.placementFamilies({ offer: dual, networkHasIpv4: false, includeIpv4: false, includeIpv6: true }).block, 'needIpv6Only');
  const only = m.placementIpv6Offer({ kind: 'CloudVirt', caps: { ipv6: true, ipv6Only: true, sources: [], translation: false }, networkMode: 'Tenant' });
  assert.deepEqual(m.placementFamilies({ offer: only, networkHasIpv4: true, includeIpv4: false, includeIpv6: true }), { ipv4: false, ipv6: true });
  assert.deepEqual(m.placementFamilies({ offer: only, networkHasIpv4: false, includeIpv4: true, includeIpv6: false }), { ipv4: false, ipv6: true });
  assert.equal(m.placementFamilies({ offer: only, networkHasIpv4: true, includeIpv4: false, includeIpv6: false }).ipv4, true);
});

test('IPv6ModeInUse on the network page: the job message once, with the modes, in display words', () => {
  const msg = "HybridNetwork g6-v6only: the network's IPv6 mode can't change while it has placements; remove them or create a new network (it is Tenant, the spec asks for Platform)";
  const out = m.ipv6ReasonText(tEn, 'IPv6ModeInUse', msg);
  assert.equal(out, "network g6-v6only: the network's IPv6 mode can't change while it has placements; remove them or create a new network (it is Tenant, the spec asks for Platform)");
  assert.doesNotMatch(out, KIND_NAME);
  // the words alone when there is no message; the words are the message's sentence
  assert.equal(m.ipv6ReasonText(tEn, 'IPv6ModeInUse'), "The network's IPv6 mode can't change while it has placements; remove them or create a new network.");
  // a message that adds something else still follows the words, in display words
  assert.equal(m.ipv6ReasonText(tEn, 'PrefixInUse', 'HybridNetwork n: fd00::/64 overlaps fd00::/48 of network a'),
    'A prefix overlaps one already in use. network n: fd00::/64 overlaps fd00::/48 of network a');
});

test('the network page names the IPv6 mode with the create form\'s words (en, fr)', () => {
  for (const l of ['en', 'fr']) {
    const cat = load(l).ipv6;
    assert.equal(cat.mode_Tenant, cat.choiceTenant, l);
    assert.equal(cat.mode_Platform, cat.choicePlatform, l);
    assert.equal(cat.mode_None, cat.choiceNone, l);
  }
});

test('Address family not available: the catalogue words with the job message in display words, no kind name', () => {
  const p = { status: { conditions: [{ type: 'Ready', status: 'False', reason: 'FamilyUnavailable',
    message: "NetworkPlacement g6-v6only-vms: IPv6 is not available on this environment's site (iaas1)" }] } };
  const out = m.familyUnavailableText(tEn, p);
  assert.equal(out, [
    load('en').ipv6.familyUnavailableBody,
    "Network placement g6-v6only-vms: IPv6 is not available on this environment's site (iaas1).",
    load('en').ipv6.familyUnavailableFix,
  ].join(' '));
  assert.doesNotMatch(out, KIND_NAME);
  const net = { status: { conditions: [{ reason: 'FamilyUnavailable', message: 'NetworkPlacement p: the network has no IPv6 address space' }] } };
  assert.match(m.familyUnavailableText(tEn, net), /Network placement p: the network has no IPv6 address space\./);
  // without a message: what happened and the fix only
  assert.equal(m.familyUnavailableText(tEn, { status: { conditions: [{ reason: 'FamilyUnavailable' }] } }),
    `${load('en').ipv6.familyUnavailableBody} ${load('en').ipv6.familyUnavailableFix}`);
  assert.equal(m.familyUnavailableText(tEn, { status: { conditions: [{ reason: 'Ready' }] } }), null);
});

// ------------------------------------------------------------------ guest configuration reference

const placementAt = (kind, subnets, ipv6 = {}, extra = {}) => ({
  metadata: { name: 'p' },
  spec: { network: 'n', backend: kind ? { kind, name: 'e' } : undefined },
  status: { subnets, ipv6, ...extra },
});
const ONLY6 = [{ family: 6, prefix: '2001:db8:0:2::/64', source: 'Network' }];
const DUAL = [{ family: 4, prefix: '10.60.1.0/24' }, { family: 6, prefix: '2001:db8:0:1::/64' }];
const FABRIC_TR = { path: 'Fabric', pref64: ' 2001:db8:64::/96 ', resolvers: ['2001:db8:53::10', ' 2001:db8:53::11 ', ''] };
const allFacts = (r) => [...r.facts.environment, ...r.facts.resolvers, ...r.facts.clat, ...r.facts.every];

test('guest reference: none without an IPv6 subnet; values only from the placement status', () => {
  assert.equal(m.guestReference(placementAt('CloudVirt', [{ family: 4, prefix: '10.60.1.0/24' }])), null);
  assert.equal(m.guestReference(undefined), null);
  const r = m.guestReference(placementAt('CloudVirt', ONLY6, { addressing: 'DHCPv6', translation: FABRIC_TR }, { mtu: 1400 }));
  assert.equal(r.environment, 'CloudVirt');
  assert.equal(r.ipv6Only, true);
  assert.equal(r.addressing, 'DHCPv6');
  assert.equal(r.mtu, 1400);
  assert.equal(r.translationState, 'available');
  assert.deepEqual(r.translation, { path: 'Fabric', pref64: '2001:db8:64::/96', resolvers: ['2001:db8:53::10', '2001:db8:53::11'] });
  // nothing the status does not report: no MTU, no addressing
  const bare = m.guestReference(placementAt('CloudVirt', ONLY6, { translation: FABRIC_TR }, { mtu: 'x' }));
  assert.equal('mtu' in bare, false);
  assert.equal('addressing' in bare, false);
  assert.equal('mtu' in m.guestReference(placementAt('CloudVirt', ONLY6, {}, { mtu: 0 })), false);
  // written before subnets existed: status.prefixes
  assert.equal(m.guestReference({ spec: { backend: { kind: 'CloudOSO' } }, status: { prefixes: ['2001:db8:0:9::/64'] } }).ipv6Only, true);
});

test('guest reference: translation available, none, not needed (the states the placement status can carry)', () => {
  const st = (subnets, ipv6) => m.guestReference(placementAt('CloudVirt', subnets, ipv6)).translationState;
  assert.equal(st(ONLY6, { translation: FABRIC_TR }), 'available');
  // the job writes no translation key when none is offered and while a provider's translator is pending (its
  // merge patch drops the empty value): both read as none
  assert.equal(st(ONLY6, {}), 'none');
  assert.equal(st(ONLY6, { addressing: 'DHCPv6', internet: { state: 'unavailable', path: 'Fabric', reason: 'NotOffered' } }), 'none');
  // a translation without a prefix is not usable
  assert.equal(st(ONLY6, { translation: { path: 'Provider' } }), 'none');
  // dual-stack: IPv4 is native, translation is never shown even if the status had one
  const dual = m.guestReference(placementAt('CloudVirt', DUAL, { translation: FABRIC_TR }));
  assert.deepEqual([dual.translationState, dual.translation, dual.ipv6Only], ['notNeeded', null, false]);
  assert.deepEqual(dual.facts.resolvers, ['dualStack']);
  assert.deepEqual(dual.facts.clat, []);
  assert.deepEqual(m.guestReference(placementAt('CloudVirt', ONLY6, {})).facts.resolvers, ['noTranslation']);
  // the CLAT facts only with translation
  assert.deepEqual(m.guestReference(placementAt('CloudOSO', ONLY6, {})).facts.clat, []);
  assert.deepEqual(m.guestReference(placementAt('CloudOSO', ONLY6, { translation: FABRIC_TR })).facts.clat, [
    'clat.when',
    'clat.prefix',
    'clat.noPref64',
    'clat.address',
    'clat.mtu',
    'clat.package',
    'clat.dnssec',
  ]);
});

test('guest reference: the facts follow the environment type and the translation path', () => {
  const PROVIDER_TR = { path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['169.254.169.254'], ipv4Source: '192.0.2.7' };
  const facts = (kind, subnets, ipv6) => m.guestReference(placementAt(kind, subnets, ipv6)).facts;
  const virt = facts('CloudVirt', ONLY6, { translation: FABRIC_TR });
  assert.ok(virt.environment.includes('virt.eui64') && virt.environment.includes('virt.matchByMac') && virt.environment.includes('virt.inlineLimit'));
  assert.deepEqual(virt.resolvers, ['virt.onlyTranslation']);
  assert.deepEqual(virt.every, ['icmpv6']);
  // the tenant writes the network configuration on OpenShift Virtualization only
  for (const kind of ['CloudOSO', 'CloudAWS', 'CloudGCP', 'Other']) assert.ok(!allFacts(m.guestReference(placementAt(kind, ONLY6, { translation: FABRIC_TR }))).some((k) => k.startsWith('virt.')), kind);
  const oso = facts('CloudOSO', ONLY6, { translation: FABRIC_TR });
  assert.deepEqual(oso.environment, ['oso.address', 'oso.configDrive', 'oso.noRaResolvers', 'oso.foreignRa']);
  assert.deepEqual(oso.resolvers, ['oso.resolvers']);
  assert.deepEqual(facts('CloudAWS', ONLY6, { translation: FABRIC_TR }).resolvers, ['aws.resolversFabric']);
  assert.deepEqual(facts('CloudAWS', ONLY6, { translation: PROVIDER_TR }).resolvers, ['aws.resolversProvider']);
  assert.deepEqual(facts('CloudAWS', ONLY6, { translation: FABRIC_TR }).environment, ['aws.address', 'aws.metadata']);
  assert.deepEqual(facts('CloudAWS', DUAL, {}).environment, ['aws.address']);
  const gcp = m.guestReference(placementAt('CloudGCP', [{ family: 6, prefix: '2001:db8:0:4::/64', source: 'Provider' }], { translation: PROVIDER_TR }));
  assert.deepEqual(gcp.facts.environment, ['gcp.address', 'gcp.images']);
  assert.deepEqual(gcp.facts.resolvers, ['gcp.resolvers']);
  assert.equal(gcp.translation.ipv4Source, '192.0.2.7');
  // the provider's router advertisements are not the platform's: no claim about PREF64 there
  assert.ok(gcp.facts.clat.includes('clat.prefix') && !gcp.facts.clat.includes('clat.noPref64'));
  const other = m.guestReference(placementAt(undefined, ONLY6, { translation: FABRIC_TR }));
  assert.equal(other.environment, 'other');
  assert.deepEqual(other.facts.environment, ['other.address']);
  assert.deepEqual(other.facts.resolvers, []);
});

test('guest reference: shown behind the site capability check, for every environment type', () => {
  const p = placementAt('CloudOSO', DUAL, {});
  const on = { ipv6: true, ipv6Only: false, sources: [], translation: false };
  assert.equal(m.showsGuestReference(p, m.NO_IPV6), false);
  assert.equal(m.showsGuestReference(p, undefined), false);
  assert.equal(m.showsGuestReference(p, on), true);
  assert.equal(m.showsGuestReference(placementAt('CloudOSO', [{ family: 4, prefix: '10.0.0.0/24' }], {}), on), false);
});

test('guest reference: every label and fact reads in both catalogues, with no configuration to paste', () => {
  const kinds = ['CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP', undefined];
  const variants = [
    [ONLY6, { translation: FABRIC_TR }],
    [ONLY6, { translation: { path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['fd00:db8::253'] } }],
    [ONLY6, {}],
    [DUAL, {}],
  ];
  const keys = new Set();
  const envs = new Set();
  for (const kind of kinds) {
    for (const [subnets, ipv6] of variants) {
      const r = m.guestReference(placementAt(kind, subnets, ipv6));
      envs.add(r.environment);
      for (const k of allFacts(r)) keys.add(`ipv6.guest.fact.${k}`);
      keys.add(`ipv6.guest.translation_${r.translationState}`);
    }
  }
  for (const e of envs) keys.add(`ipv6.guest.environment_${e}`);
  for (const k of ['title', 'help', 'environment', 'mtu', 'translation', 'pref64', 'resolvers', 'factsEnvironment', 'factsResolvers', 'factsClat', 'factsEvery', 'docLink', 'docText', 'copy', 'copied']) {
    keys.add(`ipv6.guest.${k}`);
  }
  // the guest has no cloud-config, script or manifest to copy any more
  const pasteable = /#cloud-config|runcmd|write_files|packages:|apiVersion|stringData|plat-prefix=|\n/;
  for (const l of ['en', 'fr']) {
    const cat = load(l);
    const t = tOf(cat);
    for (const k of keys) {
      assert.notEqual(t(k), k, `${l}: ${k}`);
      assert.doesNotMatch(t(k), KIND_NAME, `${l}: ${k}`);
      assert.doesNotMatch(t(k), pasteable, `${l}: ${k}`);
    }
    assert.equal(cat.ipv6.cloudInit, undefined, `${l}: the old snippet's strings are gone`);
    // the CLAT package note: subscriptions or additional repositories
    assert.match(t('ipv6.guest.fact.clat.package'), l === 'en' ? /subscription/ : /abonnement/);
  }
  assert.match(tEn('ipv6.guest.fact.clat.mtu'), /1260/);
});

test('guest reference: the snippet builders are gone; the documentation link names a page and heading that exist', () => {
  for (const name of ['cloudInitSnippet', 'showsCloudInitSnippet', 'proposeMacAddress', 'normalizeMacAddress', 'ignoreAutoDnsScript', 'CLOUD_INIT_INLINE_MAX_BYTES']) {
    assert.equal(m[name], undefined, name);
  }
  const [file, anchor] = m.GUEST_REFERENCE_DOC.split('#');
  const docs = path.join(process.env.UI_ROOT, '..', 'docs');
  const page = fs.readFileSync(path.join(docs, file), 'utf8');
  const slugs = page
    .split('\n')
    .filter((l) => /^#{1,6} /.test(l))
    .map((l) => l.replace(/^#+ /, '').trim().toLowerCase().replace(/[^\w\- ]/g, '').replace(/ /g, '-'));
  assert.ok(slugs.includes(anchor), `${file}#${anchor}`);
});
