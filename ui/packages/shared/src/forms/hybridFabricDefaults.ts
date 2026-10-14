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

/** NetworkPlacement prefix: IPv4 network prefix with host bits zero, length 8 to 29. */
export function isPlacementPrefix(text: string): boolean {
  const c = parseIpv4Cidr(text);
  return !!c && c.masked && c.length >= 8 && c.length <= 29;
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
