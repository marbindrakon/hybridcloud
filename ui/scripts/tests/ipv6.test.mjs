// Per-family addressing (forms/ipv6.ts): the CRD mirror, the derived mode, the hints, the site
// capability gate of the placement IPv6 inputs and the cloud-init snippet.
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

test('cloud-init snippet: none without an IPv6 subnet; dual-stack DHCP; IPv6-only adds a CLAT', () => {
  const mac = '02:5c:12:34:56:78';
  const v4only = { metadata: { name: 'p' }, spec: { network: 'n' }, status: { subnets: [{ family: 4, prefix: '10.60.1.0/24' }] } };
  assert.equal(m.cloudInitSnippet(v4only), null);
  assert.equal(m.cloudInitSnippet(v4only, { macAddress: mac }), null);
  const dual = {
    metadata: { name: 'p' },
    spec: { network: 'n', backend: { kind: 'CloudVirt', name: 'vm' } },
    status: { subnets: [{ family: 4, prefix: '10.60.1.0/24' }, { family: 6, prefix: '2001:db8:0:1::/64' }] },
  };
  const d = m.cloudInitSnippet(dual, { macAddress: mac });
  assert.equal(
    d.networkData,
    ['version: 2', 'ethernets:', '  primary:', `    match: {macaddress: "${mac}"}`, '    dhcp4: true', '    dhcp6: true', '    accept-ra: true', ''].join('\n'),
  );
  assert.equal(d.userData, null);
  assert.deepEqual([d.tenantNetworkData, d.macAddress, d.clat, d.ignoreAutoDns], [true, mac, false, false]);
  const only = {
    metadata: { name: 'p' },
    spec: { network: 'n', backend: { kind: 'CloudVirt', name: 'vm' } },
    status: {
      subnets: [{ family: 6, prefix: '2001:db8:0:2::/64' }],
      ipv6: { translation: { pref64: '64:ff9b::/96', resolvers: ['2001:db8::53'] } },
    },
  };
  const o = m.cloudInitSnippet(only, { macAddress: mac });
  assert.match(o.networkData, /dhcp4: false/);
  assert.match(o.networkData, /\n {4}nameservers:\n {6}addresses: \["2001:db8::53"\]\n$/);
  assert.match(o.userData, /^#cloud-config\n/);
  assert.match(o.userData, /plat-prefix=64:ff9b::\/96/);
  assert.equal(o.clat, true);
});

test('cloud-init snippet: the network data matches the NIC by the MAC address the VM interface is given', () => {
  const p = (kind, subnets, translation) => ({
    metadata: { name: 'p' },
    spec: { network: 'n', ...(kind ? { backend: { kind, name: 'e' } } : {}) },
    status: { subnets, ipv6: { translation } },
  });
  const only6 = [{ family: 6, prefix: '2001:db8:0:2::/64' }];
  const dual = [{ family: 4, prefix: '10.60.1.0/24' }, { family: 6, prefix: '2001:db8:0:1::/64' }];
  const tr = { path: 'Fabric', pref64: '64:ff9b::/96', resolvers: ['2001:db8::53', '2001:db8::54'] };
  // the tenant's text: either separator, any case, spaces around; stored lowercase with colons
  const s = m.cloudInitSnippet(p('CloudVirt', only6, tr), { macAddress: ' 02-5C-AB-00-00-01 ' });
  assert.equal(s.macAddress, '02:5c:ab:00:00:01');
  assert.match(s.networkData, /^ {2}primary:\n {4}match: \{macaddress: "02:5c:ab:00:00:01"\}\n/m);
  // the VM's interface on the pod network carries the same address (quoted: an unquoted MAC can read as a number)
  assert.equal(s.vmInterface, 'interfaces:\n  - name: default\n    binding: {name: l2bridge}\n    macAddress: "02:5c:ab:00:00:01"\n');
  // no interface name anywhere: no eth0 key, no rename, no name match; the guest script finds the NIC at runtime
  for (const text of [s.networkData, s.vmInterface, s.userData]) {
    assert.doesNotMatch(text, /\beth\d\b|\benp\d|\bens\d|set-name|\bname: "?e/);
  }
  assert.match(s.userData, /ip -6 route show default/);
  // no MAC address, or one that is not a unicast address: no network data and no VM interface, the user data stays
  for (const bad of [undefined, '', 'eth0', '02:5c:ab:00:00', '02:5c:ab:00:00:01:02', '02:5c-ab:00:00:01', '01:00:5e:00:00:01', '00:00:00:00:00:00', 'g2:5c:ab:00:00:01']) {
    const n = m.cloudInitSnippet(p('CloudVirt', only6, tr), { macAddress: bad });
    assert.deepEqual([n.tenantNetworkData, n.macAddress, n.networkData, n.vmInterface], [true, null, null, null], String(bad));
    assert.match(n.userData, /packages: \[clatd\]/);
  }
  // the environment type unknown: as on OpenShift Virtualization (the tenant gives the network data)
  assert.equal(m.cloudInitSnippet(p(undefined, dual, null), { macAddress: '02:00:00:00:00:01' }).tenantNetworkData, true);
  // OpenStack: the config drive carries the network configuration; only the user data, whatever MAC address is given
  const oso = m.cloudInitSnippet(p('CloudOSO', only6, tr), { macAddress: '02:00:00:00:00:01' });
  assert.deepEqual([oso.tenantNetworkData, oso.macAddress, oso.networkData, oso.vmInterface], [false, null, null, null]);
  // the placement's subnet hands out the translation resolvers by DHCPv6 and in the config drive: the CLAT only
  assert.deepEqual([oso.clat, oso.ignoreAutoDns], [true, false]);
  assert.doesNotMatch(oso.userData, /ignore-auto-dns/);
  // AWS, the platform's translation: the VPC's DHCP option set hands out the resolvers; the CLAT only
  const aws = m.cloudInitSnippet(p('CloudAWS', only6, tr));
  assert.deepEqual([aws.networkData, aws.clat, aws.ignoreAutoDns], [null, true, false]);
  assert.equal(aws.userData, '#cloud-config\npackages: [clatd]\nwrite_files:\n  - path: /etc/clatd.conf\n    content: |\n      plat-prefix=64:ff9b::/96\nruncmd:\n  - [systemctl, enable, --now, clatd]\n');
  // OpenShift Virtualization keeps the step: DHCPv6 there also hands out the cluster's DNS service
  assert.equal(m.cloudInitSnippet(p('CloudVirt', only6, tr), { macAddress: '02:00:00:00:00:01' }).ignoreAutoDns, true);
  // ... and nothing to show where there is no user data either
  assert.equal(m.cloudInitSnippet(p('CloudOSO', dual, null), { macAddress: '02:00:00:00:00:01' }), null);
  assert.equal(m.cloudInitSnippet(p('CloudAWS', dual, null)), null);
  // Google Cloud, the provider's path: the CLAT only
  const gcp = m.cloudInitSnippet(p('CloudGCP', [{ family: 6, prefix: '2001:db8:0:4::/64', source: 'Provider' }], { path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['169.254.169.254'] }));
  assert.deepEqual([gcp.networkData, gcp.clat, gcp.ignoreAutoDns], [null, true, false]);
  assert.equal(m.showsCloudInitSnippet(p('CloudOSO', dual, null), { ipv6: true, ipv6Only: false, sources: [], translation: false }), false);
  assert.equal(m.showsCloudInitSnippet(p('CloudOSO', only6, tr), { ipv6: true, ipv6Only: true, sources: [], translation: true }), true);
  assert.equal(m.showsCloudInitSnippet(p('CloudVirt', dual, null), { ipv6: true, ipv6Only: false, sources: [], translation: false }), true);
});

test('cloud-init snippet: every resolver quoted (an address may start or end with "::"), the prefix one token', () => {
  const at = (translation) => ({
    metadata: { name: 'p' },
    spec: { network: 'n', backend: { kind: 'CloudVirt', name: 'vm' } },
    status: { subnets: [{ family: 6, prefix: '2001:db8:0:2::/64' }], ipv6: { translation } },
  });
  const mac = '02:00:00:00:00:01';
  const s = m.cloudInitSnippet(at({ path: 'Fabric', pref64: ' 64:ff9b::/96 ', resolvers: ['2001:db8:53::', ' ::ffff:10.0.0.1 ', '2001:db8::53', '', 7] }), { macAddress: mac });
  // unquoted, "2001:db8:53::" reads as a mapping in a YAML flow sequence and "::ffff:10.0.0.1" does not parse
  assert.equal(
    s.networkData,
    [
      'version: 2',
      'ethernets:',
      '  primary:',
      `    match: {macaddress: "${mac}"}`,
      '    dhcp4: false',
      '    dhcp6: true',
      '    accept-ra: true',
      '    nameservers:',
      '      addresses: ["2001:db8:53::", "::ffff:10.0.0.1", "2001:db8::53"]',
      '',
    ].join('\n'),
  );
  assert.match(s.userData, /\n {4}content: \|\n {6}plat-prefix=64:ff9b::\/96\n {2}- path: /);
  // a prefix that is not a single token would break the literal block of clatd.conf: no CLAT then
  const bad = m.cloudInitSnippet(at({ path: 'Fabric', pref64: '64:ff9b::/96\nruncmd: [reboot]', resolvers: [] }), { macAddress: mac });
  assert.deepEqual([bad.clat, bad.userData], [false, null]);
});

test('MAC addresses: the tenant\'s text normalised, a proposed address locally administered and unicast', () => {
  assert.equal(m.normalizeMacAddress('0A:58:0A:6E:09:04'), '0a:58:0a:6e:09:04');
  assert.equal(m.normalizeMacAddress('02-00-00-00-00-01'), '02:00:00:00:00:01');
  for (const bad of [null, undefined, '', '02:00:00:00:00', '0200.0000.0001', '03:00:00:00:00:01', '00:00:00:00:00:00', '02:00:00:00:00:0g']) {
    assert.equal(m.normalizeMacAddress(bad), null, String(bad));
  }
  assert.equal(m.proposeMacAddress(() => [0xab, 1, 2, 3, 0xff]), '02:ab:01:02:03:ff');
  const a = m.proposeMacAddress();
  const b = m.proposeMacAddress();
  for (const x of [a, b]) {
    assert.match(x, /^02(:[0-9a-f]{2}){5}$/);
    assert.equal(m.normalizeMacAddress(x), x);
  }
  assert.notEqual(a, b);
  // the panel's words in both catalogues, in display terms
  for (const l of ['en', 'fr']) {
    const t = tOf(load(l));
    for (const k of ['macAddress', 'macAddressHelp', 'macAddressInvalid', 'newMac', 'vmInterface', 'vmInterfaceHelp', 'networkDataHelp', 'networkFromConfigDrive', 'networkFromCloud']) {
      assert.notEqual(t(`ipv6.cloudInit.${k}`), `ipv6.cloudInit.${k}`, `${l}: ${k}`);
      assert.doesNotMatch(t(`ipv6.cloudInit.${k}`), KIND_NAME, `${l}: ${k}`);
    }
  }
});

test('cloud-init snippet: with resolvers the guest ignores the resolvers DHCP hands out', () => {
  const at = (subnets, translation) => ({ metadata: { name: 'p' }, spec: { network: 'n' }, status: { subnets, ipv6: { translation } } });
  const only = m.cloudInitSnippet(at([{ family: 6, prefix: '2001:db8:0:2::/64' }], { pref64: '64:ff9b::/96', resolvers: ['2001:db8::53', '2001:db8::54'] }));
  assert.equal(only.ignoreAutoDns, true);
  const ud = only.userData;
  // one write_files list: clatd.conf, the script, its unit; runcmd enables the unit before clatd
  assert.deepEqual(ud.match(/^ {2}- path: .*$/gm), [
    '  - path: /etc/clatd.conf',
    '  - path: /usr/local/sbin/ignore-auto-dns',
    '  - path: /etc/systemd/system/ignore-auto-dns.service',
  ]);
  assert.equal(ud.split('\n').filter((l) => /^\S/.test(l)).join('|'), '#cloud-config|packages: [clatd]|write_files:|runcmd:');
  assert.match(ud, /runcmd:\n {2}- \[systemctl, daemon-reload\]\n {2}- \[systemctl, enable, --now, ignore-auto-dns.service\]\n {2}- \[systemctl, enable, --now, clatd\]\n$/);
  // every line of a literal block is indented six spaces or empty (no tabs, no stray indentation)
  const script = m.ignoreAutoDnsScript('ipv6 ipv4');
  for (const l of script.replace(/\n$/, '').split('\n')) assert.ok(ud.includes(l ? `\n      ${l}\n` : '\n\n'), l);
  assert.match(script, /^#!\/bin\/bash\n/);
  assert.match(script, /^FAMILIES="ipv6 ipv4"$/m);
  assert.match(script, /nmcli connection modify "\$con" "\$@"/);
  // a full reactivation: before NetworkManager 1.58 "device reapply" keeps a running DHCPv6 lease's DNS
  assert.match(script, /\n {2}nmcli connection up "\$con" \|\| exit 1\n/);
  assert.doesNotMatch(script, /^[^#]*device reapply/m);
  assert.match(ud, /\n {6}StandardOutput=journal\+console\n/);
  assert.match(script, /\[DHCPv6\]\\nUseDNS=false/);
  // the IPv6 translation probe (a test tool) ships the same script
  const sample = fs.readFileSync(path.join(process.env.UI_ROOT, '..', 'tests/connectivity/ipv6-translation-probe/probe-user-data.yaml'), 'utf8');
  const from = sample.indexOf('  - path: /usr/local/sbin/ignore-auto-dns\n');
  assert.ok(from > 0);
  const lines = sample.slice(from).split('\n').slice(3);
  const body = [];
  for (const l of lines) {
    if (l && !l.startsWith('      ')) break;
    body.push(l.slice(6));
  }
  assert.equal(`${body.join('\n').replace(/\n+$/, '')}\n`, script);
  // an IPv6-only placement without translation resolvers: nothing to keep, no user data
  const bare = m.cloudInitSnippet(at([{ family: 6, prefix: '2001:db8:0:3::/64' }], null));
  assert.deepEqual([bare.userData, bare.clat, bare.ignoreAutoDns], [null, false, false]);
  // dual-stack with resolvers: only the IPv6 resolvers handed out are ignored, no CLAT
  const dual = m.cloudInitSnippet(at([{ family: 4, prefix: '10.60.1.0/24' }, { family: 6, prefix: '2001:db8:0:1::/64' }], { resolvers: ['2001:db8::53'] }));
  assert.deepEqual([dual.clat, dual.ignoreAutoDns], [false, true]);
  assert.match(dual.userData, /\n {6}FAMILIES="ipv6"\n/);
  assert.doesNotMatch(dual.userData, /clatd/);
  // the provider's path: its DHCP hands out the provider's own DNS64 resolver; only the CLAT
  const prov = m.cloudInitSnippet(at([{ family: 6, prefix: '2001:db8:0:4::/64' }], { path: 'Provider', pref64: '64:ff9b::/96', resolvers: ['fd00:db8::253'] }), {
    macAddress: '02:00:00:00:00:01',
  });
  assert.deepEqual([prov.clat, prov.ignoreAutoDns], [true, false]);
  assert.doesNotMatch(prov.userData, /ignore-auto-dns/);
  assert.match(prov.networkData, /addresses: \["fd00:db8::253"\]/);
  // the help reads in both catalogues
  for (const l of ['en', 'fr']) {
    const t = tOf(load(l));
    for (const k of ['ipv6.cloudInit.userData', 'ipv6.cloudInit.userDataDns', 'ipv6.cloudInit.userDataHelp']) assert.notEqual(t(k), k, `${l}: ${k}`);
    assert.doesNotMatch(t('ipv6.cloudInit.userDataDns'), KIND_NAME);
  }
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

test('the cloud-init snippet ships behind the site capability check (no site reports IPv6 today)', () => {
  const p = { metadata: { name: 'p' }, spec: { network: 'n' }, status: { subnets: [{ family: 6, prefix: '2001:db8:0:1::/64' }] } };
  assert.equal(m.showsCloudInitSnippet(p, m.NO_IPV6), false);
  assert.equal(m.showsCloudInitSnippet(p, undefined), false);
  assert.equal(m.showsCloudInitSnippet(p, { ipv6: true, ipv6Only: false, sources: [], translation: false }), true);
  assert.equal(m.showsCloudInitSnippet({ ...p, status: { prefixes: ['10.0.0.0/24'] } }, { ipv6: true, ipv6Only: false, sources: [], translation: false }), false);
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
