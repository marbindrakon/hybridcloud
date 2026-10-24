/**
 * Per-family addressing of networks and placements: the client-side mirror of the API's rules.
 *
 * - The IPv6 mode of a network follows from its fields (there is no mode field): IPv6 space in
 *   platformPrefixes = Platform, IPv6 entries in prefixes = Tenant, neither = None. Status reports
 *   the mode the platform derived (status.ipv6.mode); this module derives the same from the spec.
 * - checkNetworkAddressing mirrors the HybridNetwork CRD rules (canonical form, lengths, scope,
 *   duplicates, overlap, one IPv6 source, address space needed, platformPrefixes sizes).
 * - networkAddressingHints lists what only the platform's automation decides (reserved ranges,
 *   overlap with the tenant's other networks, offered block sizes, pinned blocks): hints, never blocks.
 * - Placement IPv6 inputs are offered only where the environment's site reports IPv6
 *   (CloudInfrastructure status.capabilities, or the tenant's view of it in TenantProfile).
 * - cloudInitNetworkConfig builds a guest network snippet from a placement's status.
 *
 * Nothing here describes a particular installation: sizes the platform offers come from the
 * platform's status, never from constants.
 */
import type {
  HybridNetwork,
  HybridNetworkSpec,
  Ipv6Mode,
  NetworkPlacement,
  NetworkPlacementBackendKind,
  PlacementIpv6Source,
  PlacementSubnetStatus,
  PlatformPrefixRequest,
  TenantProfile,
  TenantProfileSite,
} from '../types';
import { isUnicastScope, parseNetworkPrefix, prefixListOverlaps, type NetworkPrefix } from './hybridFabricDefaults';

/* ------------------------------------------------------------------ prefixes */

/** Whether an IPv6 prefix lies in 2000::/3 or fc00::/7 (IPv4 prefixes: always true). */
export function inIpv6Scope(p: NetworkPrefix): boolean {
  return isUnicastScope(p.canonical);
}

/** Prefix lengths a network prefix may have: IPv4 /8–/29, IPv6 /32–/64. */
export const SPACE_LENGTHS = { 4: { min: 8, max: 29 }, 6: { min: 32, max: 64 } } as const;
/** platformPrefixes sizes the API accepts: /48 to /64 in steps of 4 (the platform offers some of them). */
export const PLATFORM_BLOCK_LENGTHS = [48, 52, 56, 60, 64];
export const MAX_NETWORK_PREFIXES = 8;

export function familyOf(text: string): 4 | 6 | null {
  return parseNetworkPrefix(text)?.family ?? null;
}

/** IPv4 and IPv6 entries of a prefix list (unparseable entries count by the presence of ':'). */
export function splitFamilies(list: string[]): { v4: string[]; v6: string[] } {
  const v4: string[] = [];
  const v6: string[] = [];
  for (const p of list) ((familyOf(p) ?? (p.includes(':') ? 6 : 4)) === 6 ? v6 : v4).push(p);
  return { v4, v6 };
}

/* ------------------------------------------------------------------ modes */

/** The IPv6 mode a network spec asks for (the same rule the platform applies). */
export function deriveIpv6Mode(spec: Partial<HybridNetworkSpec> | undefined | null): Ipv6Mode {
  const platform = Array.isArray(spec?.platformPrefixes) ? spec?.platformPrefixes ?? [] : [];
  if (platform.some((q) => q && Number(q.family) === 6)) return 'Platform';
  const prefixes = Array.isArray(spec?.prefixes) ? spec?.prefixes ?? [] : [];
  if (prefixes.some((p) => familyOf(p) === 6)) return 'Tenant';
  return 'None';
}

/** The network's IPv6 mode: as the platform reports it, else derived from the spec. */
export function networkIpv6Mode(n: HybridNetwork | undefined | null): { mode: Ipv6Mode; reported: boolean } {
  const st = n?.status?.ipv6?.mode;
  if (st === 'Tenant' || st === 'Platform' || st === 'None') return { mode: st, reported: true };
  return { mode: deriveIpv6Mode(n?.spec), reported: false };
}

/** Whether the network has IPv4 address space (its own prefixes, or held space in status). */
export function networkHasIpv4(n: HybridNetwork | undefined | null): boolean {
  const held = (n?.status?.addressSpace ?? []).some((a) => Number(a.family) === 4);
  return held || (n?.spec?.prefixes ?? []).some((p) => familyOf(p) === 4);
}

/* ------------------------------------------------------------------ network rules (CRD mirror) */

export type AddressingErrorCode =
  | 'noSpace'
  | 'tooMany'
  | 'notCanonical'
  | 'length'
  | 'scope'
  | 'duplicate'
  | 'overlap'
  | 'twoIpv6Sources'
  | 'platformFamily'
  | 'platformDuplicateFamily'
  | 'platformLength'
  | 'platformPrefix'
  | 'platformPrefixLength'
  | 'platformMismatch';

export interface AddressingError {
  code: AddressingErrorCode;
  prefix?: string;
  family?: 4 | 6;
}

/** The HybridNetwork CRD's address rules, client side. An empty list = the API accepts the addressing. */
export function checkNetworkAddressing(prefixes: string[], platform: PlatformPrefixRequest[] = []): AddressingError[] {
  const errors: AddressingError[] = [];
  const list = prefixes.map((p) => p.trim()).filter(Boolean);
  if (list.length === 0 && platform.length === 0) errors.push({ code: 'noSpace' });
  if (list.length > MAX_NETWORK_PREFIXES) errors.push({ code: 'tooMany' });
  const seen = new Set<string>();
  for (const text of list) {
    if (seen.has(text)) {
      errors.push({ code: 'duplicate', prefix: text });
      continue;
    }
    seen.add(text);
    const p = parseNetworkPrefix(text);
    if (!p || !p.masked || p.canonical !== text) {
      errors.push({ code: 'notCanonical', prefix: text });
      continue;
    }
    const lim = SPACE_LENGTHS[p.family];
    if (p.length < lim.min || p.length > lim.max) errors.push({ code: 'length', prefix: text, family: p.family });
    else if (!inIpv6Scope(p)) errors.push({ code: 'scope', prefix: text });
  }
  if (prefixListOverlaps(Array.from(seen))) errors.push({ code: 'overlap' });
  const families = new Set<number>();
  for (const q of platform) {
    if (Number(q.family) !== 6) errors.push({ code: 'platformFamily' });
    if (families.has(Number(q.family))) errors.push({ code: 'platformDuplicateFamily' });
    families.add(Number(q.family));
    if (q.prefixLength !== undefined && (!Number.isInteger(q.prefixLength) || !PLATFORM_BLOCK_LENGTHS.includes(q.prefixLength))) {
      errors.push({ code: 'platformLength' });
    }
    if (q.prefix !== undefined) {
      const p = parseNetworkPrefix(q.prefix);
      if (!p || p.family !== 6 || !p.masked || p.canonical !== q.prefix.trim()) errors.push({ code: 'platformPrefix', prefix: q.prefix });
      else if (!PLATFORM_BLOCK_LENGTHS.includes(p.length)) errors.push({ code: 'platformPrefixLength', prefix: q.prefix });
      else if (q.prefixLength !== undefined && p.length !== q.prefixLength) errors.push({ code: 'platformMismatch', prefix: q.prefix });
    }
  }
  if (platform.some((q) => Number(q.family) === 6) && list.some((p) => familyOf(p) === 6)) errors.push({ code: 'twoIpv6Sources' });
  return errors;
}

/* ------------------------------------------------------------------ hints (what the automation decides) */

export type AddressingHintCode =
  /** A tenant IPv6 prefix: refused (PrefixReserved) if it overlaps space the platform keeps for itself */
  | 'reservedRanges'
  /** A tenant IPv6 prefix overlaps another network of the same tenant: PrefixInUse */
  | 'tenantOverlap'
  /** A platform block size the fabric does not offer: PrefixInvalid */
  | 'blockNotOffered'
  /** A pinned platform block: must be inside the platform's space and free (else refused or waits) */
  | 'pinnedBlock'
  /** Tenant-provided IPv6 has no internet access */
  | 'tenantNoInternet';

export interface AddressingHint {
  code: AddressingHintCode;
  prefix?: string;
  network?: string;
  lengths?: number[];
}

export function networkAddressingHints(opts: {
  prefixes: string[];
  platform?: PlatformPrefixRequest[];
  /** The tenant's other networks (same namespace) */
  others?: HybridNetwork[];
  /** What the fabric offers, when known */
  offer?: PlatformIpv6Offer | null;
  egressInternet?: boolean;
}): AddressingHint[] {
  const hints: AddressingHint[] = [];
  const v6 = opts.prefixes.map((p) => p.trim()).filter((p) => familyOf(p) === 6);
  if (v6.length) hints.push({ code: 'reservedRanges' });
  for (const prefix of v6) {
    const a = parseNetworkPrefix(prefix);
    if (!a) continue;
    for (const n of opts.others ?? []) {
      const theirs = [
        ...(n.spec?.prefixes ?? []),
        ...(n.status?.addressSpace ?? []).filter((s) => s.source !== 'Platform').map((s) => s.prefix),
      ];
      const hit = theirs.some((t) => {
        const b = parseNetworkPrefix(t);
        return !!b && b.family === 6 && !(a.end < b.start || b.end < a.start);
      });
      if (hit) hints.push({ code: 'tenantOverlap', prefix, network: n.metadata.name });
    }
  }
  if (v6.length && opts.egressInternet) hints.push({ code: 'tenantNoInternet' });
  for (const q of opts.platform ?? []) {
    const length = q.prefixLength ?? (q.prefix ? parseNetworkPrefix(q.prefix)?.length : undefined);
    if (length !== undefined && opts.offer?.blockLengths.length && !opts.offer.blockLengths.includes(length)) {
      hints.push({ code: 'blockNotOffered', lengths: opts.offer.blockLengths });
    }
    if (q.prefix) hints.push({ code: 'pinnedBlock', prefix: q.prefix });
  }
  return hints;
}

/* ------------------------------------------------------------------ the fabric's platform IPv6 offer */

export interface PlatformIpv6Offer {
  /** Block sizes the fabric offers (prefix lengths), ascending */
  blockLengths: number[];
  /** The size that applies when the network names none */
  defaultBlock?: number;
}

function numbers(v: unknown): number[] {
  return Array.isArray(v) ? v.map(Number).filter((n) => Number.isInteger(n) && n > 0 && n <= 128) : [];
}

function offerOf(blockLengths: unknown, defaultBlock: unknown): PlatformIpv6Offer | null {
  const lengths = Array.from(new Set(numbers(blockLengths))).sort((a, b) => a - b);
  const def = Number(defaultBlock);
  const hasDef = Number.isInteger(def) && def > 0;
  if (!lengths.length && !hasDef) return null;
  if (hasDef && !lengths.includes(def)) lengths.push(def);
  return { blockLengths: lengths.sort((a, b) => a - b), ...(hasDef ? { defaultBlock: def } : {}) };
}

/**
 * The platform IPv6 offer of a fabric, from its informational status.realization.ipam.pools (a list or a
 * map of pools with family 6, blockLengths and defaultBlock). Null when the fabric reports no IPv6 pool.
 */
export function fabricIpv6Offer(fabric: { status?: { realization?: Record<string, unknown> } } | undefined | null): PlatformIpv6Offer | null {
  const ipam = (fabric?.status?.realization as { ipam?: { pools?: unknown } } | undefined)?.ipam;
  const pools = ipam?.pools;
  const list: Array<Record<string, unknown>> = Array.isArray(pools)
    ? (pools as Array<Record<string, unknown>>)
    : pools && typeof pools === 'object'
      ? Object.values(pools as Record<string, Record<string, unknown>>)
      : [];
  const v6 = list.filter((p) => p && typeof p === 'object' && Number(p.family) === 6);
  if (!v6.length) return null;
  const merged = v6.map((p) => offerOf(p.blockLengths, p.defaultBlock ?? p.defaultBlockLength)).filter((o): o is PlatformIpv6Offer => !!o);
  if (!merged.length) return { blockLengths: [] };
  return {
    blockLengths: Array.from(new Set(merged.flatMap((o) => o.blockLengths))).sort((a, b) => a - b),
    ...(merged.find((o) => o.defaultBlock) ? { defaultBlock: merged.find((o) => o.defaultBlock)?.defaultBlock } : {}),
  };
}

/** The platform IPv6 offer as the tenant sees it (TenantProfile status.ipv6Platform). */
export function profileIpv6Offer(profile: TenantProfile | undefined | null): PlatformIpv6Offer | null {
  const o = profile?.status?.ipv6Platform;
  if (!o || typeof o !== 'object') return null;
  return offerOf(o.blockLengths, o.defaultBlockLength) ?? { blockLengths: [] };
}

/* ------------------------------------------------------------------ site capabilities */

export interface SiteIpv6Caps {
  /** IPv6 placement subnets can be carried */
  ipv6: boolean;
  /** IPv6-only placements are offered */
  ipv6Only: boolean;
  /** Subnet sources the site offers (public-cloud sites: Network and/or Provider) */
  sources: PlacementIpv6Source[];
  /** IPv6-only placements reach IPv4 by translation */
  translation: boolean;
}

export const NO_IPV6: SiteIpv6Caps = { ipv6: false, ipv6Only: false, sources: [], translation: false };

/** From a CloudInfrastructure's status.capabilities (booleans). */
export function capsFromInfrastructure(ci: { status?: { capabilities?: Record<string, unknown> } } | undefined | null): SiteIpv6Caps {
  const c = ci?.status?.capabilities ?? {};
  const on = (k: string) => c[k] === true;
  const ipv6 = on('ipv6');
  if (!ipv6) return NO_IPV6;
  const sources: PlacementIpv6Source[] = [];
  if (on('ipv6NetworkSource')) sources.push('Network');
  if (on('ipv6ProviderSource')) sources.push('Provider');
  return { ipv6, ipv6Only: on('ipv6Only'), sources, translation: on('ipv6Translation') };
}

/** From the tenant's view of a site (TenantProfile status.sites[].capabilities). */
export function capsFromProfileSite(site: TenantProfileSite | undefined | null): SiteIpv6Caps {
  const c = new Set<string>(Array.isArray(site?.capabilities) ? (site?.capabilities as string[]) : []);
  if (!c.has('IPv6')) return NO_IPV6;
  const sources: PlacementIpv6Source[] = [];
  if (c.has('IPv6NetworkSource')) sources.push('Network');
  if (c.has('IPv6ProviderSource')) sources.push('Provider');
  return { ipv6: true, ipv6Only: c.has('IPv6Only'), sources, translation: c.has('IPv6Translation') };
}

/** The site an environment lives on: spec.cloudRef, else what its status names. */
export function environmentSiteName(env: { spec?: unknown; status?: unknown } | undefined | null): string | undefined {
  const ref = (env?.spec as { cloudRef?: { name?: unknown } } | undefined)?.cloudRef?.name;
  if (typeof ref === 'string' && ref) return ref;
  const site = (env?.status as { site?: unknown } | undefined)?.site;
  if (typeof site === 'string' && site) return site;
  const named = (site as { name?: unknown } | undefined)?.name;
  return typeof named === 'string' && named ? named : undefined;
}

/* ------------------------------------------------------------------ placement IPv6 offer */

/** IPv6 subnet sizes per environment type (the API's limits; CloudAWS VPC blocks are /56 or /60). */
export function placementIpv6Lengths(kind: NetworkPlacementBackendKind | string): number[] {
  return kind === 'CloudAWS' ? [60, 56] : [64];
}

export const CLOUD_KINDS = ['CloudAWS', 'CloudGCP'];

export type PlacementIpv6Block = 'networkNoIpv6' | 'siteNoIpv6' | 'siteUnknown';

export interface PlacementIpv6Offer {
  /** Show the IPv6 section */
  show: boolean;
  /** Why it is hidden */
  block?: PlacementIpv6Block;
  /** IPv4 may be left out (IPv6-only) */
  ipv6Only: boolean;
  /** Explicit sources to offer beside the site default (public-cloud sites only) */
  sources: PlacementIpv6Source[];
  /** Sizes for the Network source */
  lengths: number[];
  /** A size (or pin) is required for the Network source (CloudVirt, CloudOSO) */
  sizeRequired: boolean;
}

/**
 * What the placement form may offer for IPv6. The API stays the authority (FamilyUnavailable): this only
 * hides inputs the platform would refuse.
 */
export function placementIpv6Offer(opts: {
  kind: NetworkPlacementBackendKind | string;
  caps: SiteIpv6Caps | null | undefined;
  networkMode: Ipv6Mode;
}): PlacementIpv6Offer {
  const cloud = CLOUD_KINDS.includes(opts.kind);
  const base = { ipv6Only: false, sources: [] as PlacementIpv6Source[], lengths: placementIpv6Lengths(opts.kind), sizeRequired: !cloud };
  if (opts.networkMode === 'None') return { ...base, show: false, block: 'networkNoIpv6' };
  if (!opts.caps) return { ...base, show: false, block: 'siteUnknown' };
  if (!opts.caps.ipv6) return { ...base, show: false, block: 'siteNoIpv6' };
  // Provider needs a network whose IPv6 the platform assigns.
  const sources = cloud ? opts.caps.sources.filter((s) => s !== 'Provider' || opts.networkMode === 'Platform') : [];
  return { ...base, show: true, ipv6Only: opts.caps.ipv6Only, sources };
}

export type PlacementFamilyBlock =
  /** The network has no IPv4 and the site offers no IPv6 */
  | 'noFamilyHere'
  /** The network has no IPv4 and the site does not offer IPv6-only placements */
  | 'needIpv6Only'
  /** Neither family chosen */
  | 'needFamily';

/**
 * Which families a placement asks for. IPv4 is left out only where the site offers IPv6-only
 * placements (or the network has no IPv4); IPv6 only where the offer shows it.
 */
export function placementFamilies(opts: {
  offer: PlacementIpv6Offer;
  networkHasIpv4: boolean;
  includeIpv4: boolean;
  includeIpv6: boolean;
}): { ipv4: boolean; ipv6: boolean; block?: PlacementFamilyBlock } {
  const { offer } = opts;
  if (!opts.networkHasIpv4) {
    if (!offer.show) return { ipv4: false, ipv6: false, block: 'noFamilyHere' };
    if (!offer.ipv6Only) return { ipv4: false, ipv6: false, block: 'needIpv6Only' };
    return { ipv4: false, ipv6: true };
  }
  const ipv6 = offer.show && opts.includeIpv6;
  const ipv4 = opts.includeIpv4 || !ipv6 || !offer.ipv6Only;
  if (!ipv4 && !ipv6) return { ipv4, ipv6, block: 'needFamily' };
  return { ipv4, ipv6 };
}

/** spec.ipv6 of a placement from the form's choices; null when the choice is incomplete. */
export function buildPlacementIpv6(opts: {
  kind: NetworkPlacementBackendKind | string;
  source: '' | PlacementIpv6Source;
  prefixLength: string;
  pinned: string;
}): { prefixLength?: number; prefix?: string; source?: PlacementIpv6Source } | null {
  const cloud = CLOUD_KINDS.includes(opts.kind);
  if (opts.source === 'Provider') return cloud ? { source: 'Provider' } : null;
  const out: { prefixLength?: number; prefix?: string; source?: PlacementIpv6Source } = {};
  const pin = opts.pinned.trim();
  if (pin) out.prefix = pin;
  else if (opts.prefixLength) out.prefixLength = Number(opts.prefixLength);
  if (opts.source === 'Network') out.source = 'Network';
  if (!cloud && out.prefix === undefined && out.prefixLength === undefined) return null;
  return out;
}

/** A pinned placement IPv6 subnet: canonical, of an allowed size, inside the network's IPv6 space when known. */
export function isPinnedIpv6Prefix(text: string, lengths: number[], space: string[] = []): boolean {
  const p = parseNetworkPrefix(text);
  if (!p || p.family !== 6 || !p.masked || p.canonical !== text.trim() || !lengths.includes(p.length)) return false;
  const v6 = space.map(parseNetworkPrefix).filter((s): s is NetworkPrefix => !!s && s.family === 6);
  return !v6.length || v6.some((s) => s.start <= p.start && p.end <= s.end);
}

/* ------------------------------------------------------------------ placement status */

/** A placement's subnets per family: status.subnets, else status.prefixes (objects written before subnets existed). */
export function placementSubnets(p: NetworkPlacement | undefined | null): PlacementSubnetStatus[] {
  const rows = p?.status?.subnets;
  if (Array.isArray(rows) && rows.length) return rows.filter((r) => r && typeof r === 'object');
  return (p?.status?.prefixes ?? []).map((prefix) => ({ family: familyOf(prefix) ?? (prefix.includes(':') ? 6 : 4), prefix }));
}

export function placementSubnet(p: NetworkPlacement | undefined | null, family: 4 | 6): PlacementSubnetStatus | undefined {
  return placementSubnets(p).find((s) => Number(s.family) === family && !!s.prefix);
}

/** The reason of the placement's Ready condition when it is FamilyUnavailable (with the platform's message). */
export function familyUnavailable(p: { status?: { conditions?: Array<{ type?: string; reason?: string; message?: string }> } } | undefined | null):
  | { message?: string }
  | null {
  const c = (p?.status?.conditions ?? []).find((x) => x?.reason === 'FamilyUnavailable');
  return c ? { message: c.message } : null;
}

/* ------------------------------------------------------------------ cloud-init */

export interface CloudInitSnippet {
  /** cloud-init network configuration, version 2 (the NoCloud / config drive networkData) */
  networkData: string;
  /** cloud-config user data for a CLAT in the guest; only for an IPv6-only placement with translation */
  userData: string | null;
}

/**
 * Guest network configuration for a VM on the placement's subnet, built from status only: DHCPv4 when the
 * placement has IPv4, DHCPv6 and router advertisements for IPv6, the resolvers the platform reports. For an
 * IPv6-only placement with translation it adds user data that configures a CLAT (clatd) with the
 * translation prefix. Null when the placement has no IPv6 subnet yet.
 */
export function cloudInitSnippet(p: NetworkPlacement | undefined | null, opts: { interfaceName?: string } = {}): CloudInitSnippet | null {
  const v6 = placementSubnet(p, 6);
  if (!v6?.prefix) return null;
  const v4 = placementSubnet(p, 4);
  const nic = opts.interfaceName || 'eth0';
  const tr = p?.status?.ipv6?.translation;
  const resolvers = Array.isArray(tr?.resolvers) ? (tr?.resolvers ?? []).filter((r) => typeof r === 'string' && !!r) : [];
  const net = [
    'version: 2',
    'ethernets:',
    `  ${nic}:`,
    `    dhcp4: ${v4?.prefix ? 'true' : 'false'}`,
    '    dhcp6: true',
    '    accept-ra: true',
  ];
  if (resolvers.length) net.push('    nameservers:', `      addresses: [${resolvers.join(', ')}]`);
  let userData: string | null = null;
  if (!v4?.prefix && tr?.pref64) {
    userData = [
      '#cloud-config',
      'packages: [clatd]',
      'write_files:',
      '  - path: /etc/clatd.conf',
      '    content: |',
      `      plat-prefix=${tr.pref64}`,
      'runcmd:',
      '  - [systemctl, enable, --now, clatd]',
      '',
    ].join('\n');
  }
  return { networkData: `${net.join('\n')}\n`, userData };
}

/** Whether the placement page shows the snippet: an IPv6 subnet in status and a site that reports IPv6. */
export function showsCloudInitSnippet(p: NetworkPlacement | undefined | null, caps: SiteIpv6Caps | undefined | null): boolean {
  return !!caps?.ipv6 && !!cloudInitSnippet(p);
}
