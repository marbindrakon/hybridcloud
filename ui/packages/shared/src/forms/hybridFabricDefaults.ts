/**
 * Network form constants: only the CRDs' own `default:` values, enums and validation rules.
 * Nothing here describes a particular installation.
 */
import type { CloudInfrastructureType, GatewayTransport, NetworkPlacementBackendKind } from '../types';

/** CloudGateway.spec.transport (CRD default none) */
export const GATEWAY_TRANSPORTS: GatewayTransport[] = ['none', 'wireguard'];
export const DEFAULT_GATEWAY_TRANSPORT: GatewayTransport = 'none';
/** CloudGateway.spec.bfd (CRD default true) */
export const DEFAULT_GATEWAY_BFD = true;

/** CloudInfrastructure.spec.type */
export const CLOUD_INFRASTRUCTURE_TYPES: CloudInfrastructureType[] = ['openstack', 'openshift', 'aws', 'external'];
/** CloudInfrastructure.spec.openshift.clusterRef (CRD default local) */
export const DEFAULT_OPENSHIFT_CLUSTER_REF = 'local';
/** CloudInfrastructure.spec.dns defaults */
export const DEFAULT_SITE_DNS = { mode: 'platform' as const, createProject: false, zoneQuota: 1000 };
/** CloudInfrastructure.spec.openstack.createProjectDomain (CRD default false) */
export const DEFAULT_CREATE_PROJECT_DOMAIN = false;
/** CloudInfrastructure.spec.external.evpn (CRD default true) */
export const DEFAULT_EXTERNAL_EVPN = true;

/** NetworkPlacement.spec.backend.kind */
export const PLACEMENT_BACKEND_KINDS: NetworkPlacementBackendKind[] = ['CloudVirt', 'CloudOSO'];

/** CloudVirt.spec.namespaces (CRD default [{name: vms}]) */
export const DEFAULT_CLOUDVIRT_NAMESPACES = [{ name: 'vms' }];

/** CloudOSO / CloudAWS spec.landingzone (CRD default) */
export const DEFAULT_LANDING_ZONE = 'default';

/** MTU range shared by HybridFabric.overlayMtu, HybridNetwork.mtu and CloudGateway.siteUnderlay.mtu */
export const MTU_MIN = 1280;
export const MTU_MAX = 9216;
export const VNI_MIN = 1;
export const VNI_MAX = 16777215;
export const ASN_MIN = 1;
export const ASN_MAX = 4294967295;

/**
 * Whether spec.credentialsRef is needed: openstack and aws always, openshift unless the cluster is the
 * services cluster itself (clusterRef local), external never.
 */
export function cloudInfrastructureNeedsCredentials(
  type: CloudInfrastructureType | string,
  clusterRef?: string,
): boolean {
  if (type === 'openstack' || type === 'aws') return true;
  if (type === 'openshift') return !!clusterRef && clusterRef !== DEFAULT_OPENSHIFT_CLUSTER_REF;
  return false;
}

/** Split a comma- or newline-separated list. */
export function parseCidrList(text: string): string[] {
  return text
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Kubernetes DNS label (namespace names, node names). */
export const DNS_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/;

/** WireGuard public key (base64 of 32 bytes), as the CRD pattern requires. */
export const WIREGUARD_KEY = /^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$/;

function ipv4ToInt(ip: string): number | null {
  const parts = ip.split('.');
  if (parts.length !== 4) return null;
  let n = 0;
  for (const p of parts) {
    if (!/^\d{1,3}$/.test(p)) return null;
    const v = Number(p);
    if (v > 255) return null;
    n = n * 256 + v;
  }
  return n;
}

export function isIpv4(text: string): boolean {
  return ipv4ToInt(text.trim()) !== null;
}

/** Parse an IPv4 prefix: network address, prefix length, and whether host bits are zero. */
export function parseIpv4Cidr(text: string): { base: number; length: number; masked: boolean } | null {
  const m = /^([0-9.]+)\/(\d{1,2})$/.exec(text.trim());
  if (!m) return null;
  const base = ipv4ToInt(m[1]);
  const length = Number(m[2]);
  if (base === null || length > 32) return null;
  const size = 2 ** (32 - length);
  return { base, length, masked: base % size === 0 };
}

export function isIpv4Cidr(text: string): boolean {
  return parseIpv4Cidr(text) !== null;
}

/** Parse an IPv6 address (no zone, no embedded IPv4) to its 128-bit value. */
function ipv6ToBigInt(ip: string): bigint | null {
  if (!/^[0-9a-fA-F:]+$/.test(ip) || (ip.match(/::/g) ?? []).length > 1) return null;
  const [head, tail] = ip.includes('::') ? ip.split('::') : [ip, undefined];
  const left = head ? head.split(':') : [];
  const right = tail ? tail.split(':') : [];
  const missing = 8 - left.length - right.length;
  if ((tail === undefined && missing !== 0) || (tail !== undefined && missing < 1)) return null;
  const groups = [...left, ...Array(tail === undefined ? 0 : missing).fill('0'), ...right];
  let n = BigInt(0);
  for (const g of groups) {
    if (!/^[0-9a-fA-F]{1,4}$/.test(g)) return null;
    n = (n << BigInt(16)) + BigInt(parseInt(g, 16));
  }
  return n;
}

/** RFC 5952 text of a 128-bit IPv6 value (lowercase, no leading zeros, the longest run of 2+ zero groups as ::). */
function ipv6ToString(n: bigint): string {
  const groups: number[] = [];
  for (let i = 7; i >= 0; i -= 1) groups.push(Number((n >> BigInt(16 * i)) & BigInt(0xffff)));
  let best = -1;
  let bestLen = 1;
  for (let i = 0; i < 8; ) {
    if (groups[i] !== 0) {
      i += 1;
      continue;
    }
    let j = i;
    while (j < 8 && groups[j] === 0) j += 1;
    if (j - i > bestLen) {
      best = i;
      bestLen = j - i;
    }
    i = j;
  }
  const hex = groups.map((g) => g.toString(16));
  if (best < 0) return hex.join(':');
  return `${hex.slice(0, best).join(':')}::${hex.slice(best + bestLen).join(':')}`;
}

/** An IPv4 or IPv6 prefix: family, first and last address, length, canonical text, whether host bits are zero. */
export interface NetworkPrefix {
  family: 4 | 6;
  start: bigint;
  end: bigint;
  length: number;
  canonical: string;
  masked: boolean;
}

export function parseNetworkPrefix(text: string): NetworkPrefix | null {
  const m = /^([^/\s]+)\/(\d{1,3})$/.exec(text.trim());
  if (!m) return null;
  const length = Number(m[2]);
  const v4 = ipv4ToInt(m[1]);
  const family: 4 | 6 = v4 !== null ? 4 : 6;
  const value = v4 !== null ? BigInt(v4) : ipv6ToBigInt(m[1]);
  const bits = family === 4 ? 32 : 128;
  if (value === null || length > bits) return null;
  const size = BigInt(1) << BigInt(bits - length);
  const start = value - (value % size);
  const canonical =
    family === 4
      ? `${[24, 16, 8, 0].map((s) => Number((start >> BigInt(s)) & BigInt(255))).join('.')}/${length}`
      : `${ipv6ToString(start)}/${length}`;
  return { family, start, end: start + size - BigInt(1), length, canonical, masked: start === value };
}

/** A prefix of a HybridNetwork's address space: canonical, host bits zero, IPv4 /8–/29 or IPv6 /32–/64. */
export function isSpacePrefix(text: string): boolean {
  const p = parseNetworkPrefix(text);
  if (!p || !p.masked || p.canonical !== text.trim()) return false;
  return p.family === 4 ? p.length >= 8 && p.length <= 29 : p.length >= 32 && p.length <= 64;
}

/** Whether two prefixes of a list (same family) overlap. */
export function prefixListOverlaps(list: string[]): boolean {
  const parsed = list.map(parseNetworkPrefix).filter((p): p is NetworkPrefix => !!p);
  for (let i = 0; i < parsed.length; i += 1) {
    for (let j = i + 1; j < parsed.length; j += 1) {
      const a = parsed[i];
      const b = parsed[j];
      if (a.family === b.family && !(a.end < b.start || b.end < a.start)) return true;
    }
  }
  return false;
}

/** A HybridNetwork's address space as the CRD accepts it: 1–8 distinct, disjoint prefixes, at least one IPv4. */
export function isAddressSpace(list: string[]): boolean {
  return (
    list.length >= 1 &&
    list.length <= 8 &&
    list.every(isSpacePrefix) &&
    list.some((p) => parseNetworkPrefix(p)?.family === 4) &&
    new Set(list).size === list.length &&
    !prefixListOverlaps(list)
  );
}

/** NetworkPlacement IPv4 subnet sizes (CloudAWS: up to a /24) */
export const PLACEMENT_IPV4_MIN = 16;
export const PLACEMENT_IPV4_MAX = 29;
export const PLACEMENT_IPV4_MAX_AWS = 24;
export const PLACEMENT_IPV4_DEFAULT = 24;

/** A pinned NetworkPlacement IPv4 prefix: canonical, host bits zero, /16 to maxLength. */
export function isPinnedIpv4Prefix(text: string, maxLength: number = PLACEMENT_IPV4_MAX): boolean {
  const p = parseNetworkPrefix(text);
  return (
    !!p && p.family === 4 && p.masked && p.canonical === text.trim() && p.length >= PLACEMENT_IPV4_MIN && p.length <= maxLength
  );
}

/** Whether a prefix lies inside one of a list of prefixes (same family). */
export function prefixWithin(text: string, space: string[]): boolean {
  const p = parseNetworkPrefix(text);
  if (!p) return false;
  return space
    .map(parseNetworkPrefix)
    .some((s) => !!s && s.family === p.family && s.start <= p.start && p.end <= s.end);
}

/** Whether an IPv4 address lies inside an IPv4 prefix. */
export function cidrContains(cidr: string, ip: string): boolean {
  const c = parseIpv4Cidr(cidr);
  const a = ipv4ToInt(ip.trim());
  if (!c || a === null) return false;
  const size = 2 ** (32 - c.length);
  const start = c.base - (c.base % size);
  return a >= start && a < start + size;
}

/** Integer inside [min, max]. */
export function isIntInRange(text: string, min: number, max: number): boolean {
  if (!/^\d+$/.test(text.trim())) return false;
  const n = Number(text);
  return n >= min && n <= max;
}
