/**
 * The OpenShift cluster create form as pure functions: the spec it sends, the
 * checks of its node counts and of its address settings.
 *
 * The builder sends what the form shows: a typed count is sent as typed (0
 * included where the API allows it), an empty one is left out (the
 * installation's default applies), and the region is never sent (the
 * environment's region applies). The address checks are the ones the cluster's
 * preflight makes, so the form refuses before submit what the API would refuse.
 */
import { parseIpv4Cidr, parseNetworkPrefix } from './hybridFabricDefaults';
import type { TenantReservedRange } from '../types';

export type ClusterProvider = 'openstack' | 'aws' | 'gcp';
export type ClusterPublish = 'External' | 'Internal';

/** Highest node count the form accepts (the dashboard server's limit). */
export const COUNT_MAX = 1000;
/** Lowest count per role: a cluster needs a control plane; workers may be 0 (a compact cluster). */
export const COUNT_MIN = { controlPlane: 1, worker: 0 } as const;

export type CountRole = keyof typeof COUNT_MIN;
export type CountProblem = 'integer' | 'min' | 'max';

/** What is wrong with a typed count, or null (an empty field is fine: the installation's default). */
export function countProblem(text: string, role: CountRole): CountProblem | null {
  const v = text.trim();
  if (!v) return null;
  if (!/^-?\d+$/.test(v)) return 'integer';
  const n = Number(v);
  if (n < COUNT_MIN[role]) return 'min';
  if (n > COUNT_MAX) return 'max';
  return null;
}

/** The count to send: the typed integer, undefined when empty or invalid. */
export function countValue(text: string, role: CountRole): number | undefined {
  if (!text.trim() || countProblem(text, role)) return undefined;
  return Number(text.trim());
}

/* ------------------------------------------------------------------ network choice */

/** The network picker's value: '' (the site's default), a network of the tenant, or a site's external network. */
export type NetworkChoice = { kind: 'none' } | { kind: 'network'; name: string } | { kind: 'external'; name: string };

export function choiceValue(choice: NetworkChoice): string {
  return choice.kind === 'none' ? '' : `${choice.kind}:${choice.name}`;
}

export function parseChoice(value: string): NetworkChoice {
  const i = value.indexOf(':');
  if (i > 0) {
    const kind = value.slice(0, i);
    const name = value.slice(i + 1);
    if (name && (kind === 'network' || kind === 'external')) return { kind, name };
  }
  return { kind: 'none' };
}

/* ------------------------------------------------------------------ address settings */

export type NetworkingField = 'clusterNetwork' | 'hostPrefix' | 'serviceNetwork' | 'machineNetwork';

export interface ClusterNetworkingInput {
  clusterNetwork: string;
  hostPrefix: string;
  serviceNetwork: string;
  machineNetwork: string;
}

export const EMPTY_NETWORKING: ClusterNetworkingInput = { clusterNetwork: '', hostPrefix: '', serviceNetwork: '', machineNetwork: '' };

/** Prefix lengths the API accepts per range, and the host prefix bounds. */
export const NETWORKING_LIMITS = {
  clusterNetwork: { min: 8, max: 22 },
  serviceNetwork: { min: 12, max: 24 },
  machineNetwork: { min: 16, max: 27 },
  hostPrefix: { min: 16, max: 28 },
} as const;

/** OpenShift's documented defaults, shown as examples and used for the hints on empty fields. */
export const OPENSHIFT_DEFAULTS = {
  clusterNetwork: '10.128.0.0/14',
  hostPrefix: 23,
  serviceNetwork: '172.30.0.0/16',
  machineNetwork: '10.0.0.0/16',
} as const;

export type NetworkingProblemCode =
  | 'cidr'
  | 'canonical'
  | 'length'
  | 'hostPrefixRange'
  | 'hostPrefixShort'
  | 'notWithNetwork'
  | 'AddressConflict'
  | 'PrefixReserved';

/**
 * One problem of an address setting. `other` names the other side of an overlap: a field
 * (`field:<name>`) or a prefix of the network (`network:<prefix>`); `range`/`purpose` the reserved range.
 */
export interface NetworkingProblem {
  field: NetworkingField;
  code: NetworkingProblemCode;
  cidr?: string;
  other?: string;
  range?: string;
  purpose?: string;
  min?: number;
  max?: number;
  canonical?: string;
}

export interface NetworkingContext {
  provider: ClusterProvider;
  /** The cluster names a network of the tenant */
  hasNetwork: boolean;
  /** That network's address space (both families; only IPv4 can overlap) */
  networkSpace: string[];
  /** The tenant profile's reserved ranges */
  reserved: TenantReservedRange[];
  /** The environment's site: site-specific reserved ranges of other sites do not apply */
  site?: string;
}

function overlaps(a: string, b: string): boolean {
  const x = parseNetworkPrefix(a);
  const y = parseNetworkPrefix(b);
  if (!x || !y || x.family !== y.family) return false;
  return !(x.end < y.start || y.end < x.start);
}

/** The reserved ranges that apply to a prefix used on a site (rows of other sites do not). */
export function applicableReserved(reserved: TenantReservedRange[] | undefined, site?: string): TenantReservedRange[] {
  return (reserved ?? []).filter((r) => r && typeof r.prefix === 'string' && (!r.site || r.site === site));
}

/** The first reserved range a prefix overlaps, or undefined. */
export function reservedOverlap(prefix: string, reserved: TenantReservedRange[]): TenantReservedRange | undefined {
  return reserved.find((r) => overlaps(prefix, r.prefix));
}

/** Whether the machine network field applies (OpenStack, or AWS/Google Cloud without a network). */
export function machineNetworkApplies(provider: ClusterProvider, hasNetwork: boolean): boolean {
  return provider === 'openstack' || !hasNetwork;
}

const RANGE_FIELDS = ['clusterNetwork', 'serviceNetwork', 'machineNetwork'] as const;
type RangeField = (typeof RANGE_FIELDS)[number];

/** The overlap partners of each range (the cluster preflight's table). */
const MUST_NOT_OVERLAP: Record<RangeField, RangeField[]> = {
  clusterNetwork: ['serviceNetwork', 'machineNetwork'],
  serviceNetwork: ['clusterNetwork', 'machineNetwork'],
  machineNetwork: [],
};

/** Every problem of the typed address settings (empty fields are fine: the installation's defaults). */
export function checkClusterNetworking(input: ClusterNetworkingInput, ctx: NetworkingContext): NetworkingProblem[] {
  const out: NetworkingProblem[] = [];
  const valid: Partial<Record<RangeField, string>> = {};
  const reserved = applicableReserved(ctx.reserved, ctx.site);
  const space4 = ctx.networkSpace.filter((p) => parseNetworkPrefix(p)?.family === 4);
  const machineApplies = machineNetworkApplies(ctx.provider, ctx.hasNetwork);

  for (const field of RANGE_FIELDS) {
    const text = input[field].trim();
    if (!text) continue;
    if (field === 'machineNetwork' && !machineApplies) {
      out.push({ field, code: 'notWithNetwork', cidr: text });
      continue;
    }
    const p = parseIpv4Cidr(text);
    if (!p) {
      out.push({ field, code: 'cidr', cidr: text });
      continue;
    }
    const canonical = parseNetworkPrefix(text)?.canonical ?? text;
    if (!p.masked || canonical !== text) {
      out.push({ field, code: 'canonical', cidr: text, canonical });
      continue;
    }
    const { min, max } = NETWORKING_LIMITS[field];
    if (p.length < min || p.length > max) {
      out.push({ field, code: 'length', cidr: text, min, max });
      continue;
    }
    valid[field] = text;
  }

  const host = input.hostPrefix.trim();
  if (host) {
    const { min, max } = NETWORKING_LIMITS.hostPrefix;
    const n = Number(host);
    if (!/^\d+$/.test(host) || n < min || n > max) {
      out.push({ field: 'hostPrefix', code: 'hostPrefixRange', min, max });
    } else {
      const pods = valid.clusterNetwork ?? (input.clusterNetwork.trim() ? undefined : OPENSHIFT_DEFAULTS.clusterNetwork);
      const podLength = pods ? parseIpv4Cidr(pods)?.length : undefined;
      if (podLength !== undefined && n <= podLength) {
        out.push({ field: 'hostPrefix', code: 'hostPrefixShort', cidr: pods, min: podLength + 1 });
      }
    }
  }

  for (const field of RANGE_FIELDS) {
    const cidr = valid[field];
    if (!cidr) continue;
    for (const otherField of MUST_NOT_OVERLAP[field]) {
      const other = valid[otherField];
      if (other && overlaps(cidr, other)) out.push({ field, code: 'AddressConflict', cidr, other: `field:${otherField}` });
    }
    // The cluster's own ranges stay off the network's space; on AWS and Google Cloud with a network the
    // machine network is the placement's subnet and is not typed here.
    const net = space4.find((s) => overlaps(cidr, s));
    if (net) out.push({ field, code: 'AddressConflict', cidr, other: `network:${net}` });
    if (!reservedApplies(field, ctx.provider)) continue;
    const r = reservedOverlap(cidr, reserved);
    if (r) out.push({ field, code: 'PrefixReserved', cidr, range: r.prefix, purpose: r.purpose ?? '' });
  }
  return out;
}

/**
 * Whether a range is checked against the reserved ranges, as the cluster job's preflight does: the pod and service
 * networks always, the machine network on OpenStack (on AWS and Google Cloud without a network it is the cluster's
 * own VPC; with a network it is the placement's subnet, which the network already keeps clear of them).
 */
export function reservedApplies(field: RangeField, provider: ClusterProvider): boolean {
  return field !== 'machineNetwork' || provider === 'openstack';
}

/**
 * Hints for empty fields: OpenShift's default of a range overlaps the network or a reserved range.
 * Not a refusal (the installation may use other defaults); it says which field to set.
 */
export function defaultOverlapHints(input: ClusterNetworkingInput, ctx: NetworkingContext): NetworkingProblem[] {
  const out: NetworkingProblem[] = [];
  const reserved = applicableReserved(ctx.reserved, ctx.site);
  const space4 = ctx.networkSpace.filter((p) => parseNetworkPrefix(p)?.family === 4);
  for (const field of RANGE_FIELDS) {
    if (input[field].trim()) continue;
    if (field === 'machineNetwork' && !machineNetworkApplies(ctx.provider, ctx.hasNetwork)) continue;
    const cidr = OPENSHIFT_DEFAULTS[field];
    const net = space4.find((s) => overlaps(cidr, s));
    if (net) {
      out.push({ field, code: 'AddressConflict', cidr, other: `network:${net}` });
      continue;
    }
    const r = reservedApplies(field, ctx.provider) ? reservedOverlap(cidr, reserved) : undefined;
    if (r) out.push({ field, code: 'PrefixReserved', cidr, range: r.prefix, purpose: r.purpose ?? '' });
  }
  return out;
}

/** The `spec.networking` to send: the typed fields only, or undefined when none is typed. */
export function networkingSpec(input: ClusterNetworkingInput, provider: ClusterProvider, hasNetwork: boolean): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {};
  if (input.clusterNetwork.trim()) out.clusterNetwork = input.clusterNetwork.trim();
  if (input.hostPrefix.trim()) out.hostPrefix = Number(input.hostPrefix.trim());
  if (input.serviceNetwork.trim()) out.serviceNetwork = input.serviceNetwork.trim();
  if (input.machineNetwork.trim() && machineNetworkApplies(provider, hasNetwork)) out.machineNetwork = input.machineNetwork.trim();
  return Object.keys(out).length ? out : undefined;
}

/* ------------------------------------------------------------------ the spec */

export interface ClusterFormInput {
  provider: ClusterProvider;
  environment: string;
  /** The picker's value (choiceValue) */
  network: string;
  /** AWS and Google Cloud; Internal needs a network */
  publish: ClusterPublish;
  controlPlaneCount: string;
  workerCount: string;
  networking: ClusterNetworkingInput;
  toolRbac?: Record<string, string[]>;
}

/** The PlatformOpenshift spec the form sends. */
export function buildClusterSpec(input: ClusterFormInput): Record<string, unknown> {
  const choice = parseChoice(input.network);
  const counts: Record<string, number> = {};
  const cp = countValue(input.controlPlaneCount, 'controlPlane');
  const workers = countValue(input.workerCount, 'worker');
  if (cp !== undefined) counts.controlPlaneCount = cp;
  if (workers !== undefined) counts.workerCount = workers;
  const toolRbac = input.toolRbac && Object.values(input.toolRbac).some((l) => l.length)
    ? Object.fromEntries(Object.entries(input.toolRbac).filter(([, l]) => l.length))
    : undefined;
  const hasNetwork = choice.kind === 'network';
  const networking = networkingSpec(input.networking, input.provider, hasNetwork);
  const tail = { ...(networking ? { networking } : {}), ...(toolRbac ? { toolRbac } : {}) };

  if (input.provider === 'openstack') {
    return {
      type: 'openstack',
      openstack: {
        environment: input.environment,
        ...(choice.kind === 'network' ? { network: choice.name } : {}),
        ...(choice.kind === 'external' ? { externalNetwork: choice.name } : {}),
        ...counts,
      },
      ...tail,
    };
  }
  const cloud = {
    environment: input.environment,
    ...(input.provider === 'aws' ? { clusterType: 'standalone' } : {}),
    ...(hasNetwork ? { network: choice.name, publish: input.publish } : {}),
    ...counts,
  };
  return { type: input.provider, [input.provider]: cloud, ...tail };
}

/** What stops the spec from being sent (beyond the name and the environment): count and address problems. */
export function clusterFormProblems(input: ClusterFormInput, ctx: NetworkingContext): {
  controlPlane: CountProblem | null;
  worker: CountProblem | null;
  networking: NetworkingProblem[];
  publish: 'internalNeedsNetwork' | null;
} {
  const choice = parseChoice(input.network);
  return {
    controlPlane: countProblem(input.controlPlaneCount, 'controlPlane'),
    worker: countProblem(input.workerCount, 'worker'),
    networking: checkClusterNetworking(input.networking, ctx),
    publish: input.provider !== 'openstack' && input.publish === 'Internal' && choice.kind !== 'network' ? 'internalNeedsNetwork' : null,
  };
}
