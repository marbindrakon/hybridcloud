/**
 * Which networks an OpenShift cluster can be put on, and why a network cannot (the cluster's
 * preflight refusals, checked in the form before submit).
 *
 * OpenStack: the tenant's networks placed on the environment with router attachments on, and the
 * external networks of the environment's site (from the tenant profile). AWS and Google Cloud: the
 * tenant's networks placed on the environment; publishing Internal needs the platform's access to
 * the network.
 *
 * Every refusal is a reason of the API (the same word as the cluster's Preflight condition or the
 * network's PlatformReachable condition) with the values its message needs.
 */
import { findCondition, phaseOf } from '../status/phase';
import { parseNetworkPrefix } from './hybridFabricDefaults';
import {
  applicableReserved,
  NETWORKING_LIMITS,
  OPENSHIFT_DEFAULTS,
  reservedOverlap,
  type ClusterProvider,
  type ClusterPublish,
  type NetworkingField,
  type NetworkingProblem,
} from './clusterSpec';
import type {
  HybridNetwork,
  NetworkPlacement,
  NetworkPlacementBackendKind,
  TenantProfile,
  TenantReservedRange,
  TenantSiteExternalNetwork,
} from '../types';

/** A refusal: the API's reason and the values of its message. */
export interface ChoiceBlock {
  reason: string;
  params?: Record<string, string>;
  /** The API's own message, shown when the values the UI text needs are not known */
  message?: string;
}

export interface HybridNetworkOption {
  name: string;
  /** The placement of the network on the environment */
  placement?: string;
  /** The placement's IPv4 subnet */
  subnet4?: string;
  block: ChoiceBlock | null;
}

export interface ExternalNetworkOption {
  name: string;
  default: boolean;
  block: ChoiceBlock | null;
}

export const PROVIDER_ENV_KIND: Record<ClusterProvider, NetworkPlacementBackendKind> = {
  openstack: 'CloudOSO',
  aws: 'CloudAWS',
  gcp: 'CloudGCP',
};

/** Reasons of PlatformReachable False that the tenant can act on with the platform access switch. */
export const PLATFORM_ACCESS_OFF_REASONS = ['NotRequested', 'PlatformAccessOff'];

function family(prefix: string): 4 | 6 | null {
  return parseNetworkPrefix(prefix)?.family ?? null;
}

function inRanges(prefix: string, ranges: string[]): boolean {
  const p = parseNetworkPrefix(prefix);
  if (!p) return false;
  return ranges
    .map(parseNetworkPrefix)
    .some((r) => !!r && r.family === p.family && r.start <= p.start && p.end <= r.end);
}

/** The placement's allocated IPv4 subnet (status), or undefined. */
export function placementSubnet4(p: NetworkPlacement | undefined): string | undefined {
  return (p?.status?.prefixes ?? []).find((x) => family(x) === 4);
}

/** The IPv4 prefixes of a network's address space. */
export function networkSpace4(n: HybridNetwork | undefined): string[] {
  const space = (n?.status?.addressSpace ?? []).filter((a) => Number(a.family) === 4).map((a) => a.prefix);
  return space.length ? space : (n?.spec?.prefixes ?? []).filter((p) => family(p) === 4);
}

/** Both families of a network's address space (status, else spec). */
export function networkSpace(n: HybridNetwork | undefined): string[] {
  const space = (n?.status?.addressSpace ?? []).map((a) => a.prefix);
  return space.length ? space : n?.spec?.prefixes ?? [];
}

/**
 * Why the platform cannot reach the network, from its PlatformReachable condition (null = it can).
 * The values for the messages are filled from the tenant profile where the condition carries none.
 */
export function platformReachBlock(
  n: HybridNetwork,
  ctx: { managedRanges?: string[]; reserved?: TenantReservedRange[] },
): ChoiceBlock | null {
  const cond = findCondition(n, 'PlatformReachable');
  if (!cond) return { reason: 'PlatformAccessUnknown' };
  if (cond.status === 'True') return null;
  const reason = cond.reason || 'PlatformAccessUnknown';
  // the platform is still connecting the network: the cluster job accepts it and waits, so the form does too
  if (reason === 'AttachPending') return null;
  const message = cond.message || undefined;
  if (reason === 'OutsideAttachableRanges') return { reason, message, params: { ranges: (ctx.managedRanges ?? []).join(', ') } };
  if (reason === 'InfrastructureOverlap') {
    const hit = networkSpace(n)
      .map((p) => reservedOverlap(p, applicableReserved(ctx.reserved)))
      .find((r): r is TenantReservedRange => !!r);
    return { reason, message, params: { range: hit?.prefix ?? '', purpose: hit?.purpose ?? '' } };
  }
  return { reason, message };
}

/** The tenant networks a cluster on this environment can use, each with the reason it cannot. */
export function hybridNetworkOptions(opts: {
  provider: ClusterProvider;
  environment: string;
  publish?: ClusterPublish;
  networks: HybridNetwork[];
  placements: NetworkPlacement[];
  profile?: TenantProfile | null;
}): HybridNetworkOption[] {
  const kind = PROVIDER_ENV_KIND[opts.provider];
  if (!opts.environment) return [];
  const managedRanges = opts.profile?.status?.managedClusterRanges ?? [];
  const reserved = opts.profile?.status?.reservedRanges ?? [];
  const out: HybridNetworkOption[] = [];
  for (const n of opts.networks) {
    const name = n.metadata?.name;
    if (!name) continue;
    const placement = opts.placements.find(
      (p) => p.spec?.network === name && p.spec?.backend?.kind === kind && p.spec?.backend?.name === opts.environment,
    );
    // only networks placed on the environment are listed
    if (!placement) continue;
    const subnet4 = placementSubnet4(placement);
    const option: HybridNetworkOption = { name, placement: placement.metadata?.name, subnet4, block: null };
    out.push(option);
    const needsPlatform = opts.provider === 'openstack' || opts.publish === 'Internal';
    if (phaseOf(placement) !== 'Ready') {
      option.block = { reason: 'WaitingForPlacement' };
      continue;
    }
    if (!subnet4) {
      option.block = { reason: 'PlacementHasNoIPv4' };
      continue;
    }
    if (opts.provider === 'openstack') {
      const wanted = placement.spec?.use?.routerAttachments === true;
      const state = placement.status?.use?.routerAttachments?.state;
      if (!wanted) {
        option.block = { reason: 'RouterAttachmentsOff', params: { placement: placement.metadata?.name ?? '' } };
        continue;
      }
      if (state !== 'ready') {
        option.block = { reason: 'WaitingForPlacement' };
        continue;
      }
    }
    if (!needsPlatform) continue;
    const reach = platformReachBlock(n, { managedRanges, reserved });
    if (reach) {
      option.block = reach;
      continue;
    }
    if (managedRanges.length && !inRanges(subnet4, managedRanges)) {
      option.block = { reason: 'OutsideAttachableRanges', params: { ranges: managedRanges.join(', ') } };
    }
  }
  return out.sort((a, b) => Number(!!a.block) - Number(!!b.block) || a.name.localeCompare(b.name));
}

/** The external networks of the environment's site (tenant profile), the site's default first. */
export function externalNetworkOptions(profile: TenantProfile | null | undefined, site: string | undefined): ExternalNetworkOption[] | null {
  if (!site) return null;
  const s = (profile?.status?.sites ?? []).find((x) => x?.name === site);
  const list: TenantSiteExternalNetwork[] | undefined = s?.externalNetworks;
  // absent: the profile does not say (the site's default applies when the field is left empty)
  if (!Array.isArray(list)) return null;
  return list
    .filter((x) => x && typeof x.name === 'string' && x.name)
    .map((x) => ({
      name: x.name,
      default: x.default === true,
      block: Array.isArray(x.families) && x.families.length && !x.families.map(Number).includes(4) ? { reason: 'NoIPv4' } : null,
    }))
    .sort((a, b) => Number(b.default) - Number(a.default) || a.name.localeCompare(b.name));
}

/** The picker's preselection: the site's default external network when usable, else nothing. */
export function defaultExternalChoice(options: ExternalNetworkOption[] | null): string | undefined {
  return options?.find((o) => o.default && !o.block)?.name;
}

/** A refusal when the OpenStack cluster has no network chosen and the site has no default to fall back to. */
export function emptyChoiceBlock(provider: ClusterProvider, external: ExternalNetworkOption[] | null): ChoiceBlock | null {
  if (provider !== 'openstack') return null;
  // the profile does not list the site's networks: the API decides (the site's default, if any)
  if (external === null) return null;
  return external.some((o) => o.default && !o.block) ? null : { reason: 'NetworkChoiceRequired' };
}

/** Message key of a reason (shared by the cluster form, the network page and the placement page). */
export function reasonKey(reason: string): string {
  switch (reason) {
    case 'PlacementHasNoIPv4':
    case 'NoIPv4':
      return 'clusterNet.reason.NoIPv4';
    case 'PlatformAccessOff':
    case 'NotRequested':
      return 'clusterNet.reason.NotRequested';
    case 'ServiceNotOffered':
    case 'CircuitNotDeployed':
    case 'HubEndpointNotAdmitted':
      return 'clusterNet.reason.ServiceNotOffered';
    case 'WaitingForPlatformAccess':
    case 'AttachPending':
      return 'clusterNet.reason.AttachPending';
    case 'RouterAttachmentsOff':
    case 'WaitingForPlacement':
    case 'OutsideAttachableRanges':
    case 'InfrastructureOverlap':
    case 'AddressConflict':
    case 'PrefixReserved':
    case 'ServicePrefixInUse':
    case 'NetworkChoiceRequired':
    case 'ExternalNetworkNotListed':
    case 'InUse':
    case 'PlatformAccessUnknown':
      return `clusterNet.reason.${reason}`;
    default:
      return '';
  }
}

/** The values each text needs; without them the API's message is shown instead. */
const NEEDED_PARAMS: Record<string, string[]> = {
  OutsideAttachableRanges: ['ranges'],
  InfrastructureOverlap: ['range'],
  AddressConflict: ['field', 'cidr', 'other'],
  PrefixReserved: ['subject', 'range'],
  ExternalNetworkNotListed: ['name'],
  InUse: ['ports'],
};

/** The text of a reason, or the API's own message (or the reason word) when the UI has no text for it. */
export function reasonText(
  t: (k: string, o?: Record<string, unknown>) => string,
  block: { reason?: string; message?: string; params?: Record<string, string> },
): string {
  const reason = block.reason ?? '';
  const key = reasonKey(reason);
  const params = block.params ?? {};
  const missing = (NEEDED_PARAMS[reason] ?? []).some((p) => !params[p]);
  if (!key || (missing && block.message)) return block.message || reason;
  return t(key, { ...params });
}

/** Label key of an address setting. */
export const NETWORKING_LABEL: Record<NetworkingField, string> = {
  clusterNetwork: 'clusterNet.clusterNetwork',
  hostPrefix: 'clusterNet.hostPrefix',
  serviceNetwork: 'clusterNet.serviceNetwork',
  machineNetwork: 'clusterNet.machineNetwork',
};

/** The text of an address setting problem (the job's wording for the overlaps). */
export function networkingProblemText(t: (k: string, o?: Record<string, unknown>) => string, p: NetworkingProblem): string {
  const label = t(NETWORKING_LABEL[p.field]);
  const otherText = (other: string | undefined): string => {
    if (!other) return '';
    const [kind, value] = [other.slice(0, other.indexOf(':')), other.slice(other.indexOf(':') + 1)];
    if (kind === 'field') return t(NETWORKING_LABEL[value as NetworkingField] ?? value);
    return t('clusterNet.networkOf', { prefix: value });
  };
  switch (p.code) {
    case 'AddressConflict':
      return reasonText(t, { reason: 'AddressConflict', params: { field: label, cidr: p.cidr ?? '', other: otherText(p.other) } });
    case 'PrefixReserved':
      return reasonText(t, {
        reason: 'PrefixReserved',
        params: { subject: `${label} ${p.cidr ?? ''}`.trim(), range: p.range ?? '', purpose: p.purpose ?? '' },
      });
    case 'cidr':
      return t('clusterNet.problem.cidr', { example: p.field === 'serviceNetwork' ? OPENSHIFT_DEFAULTS.serviceNetwork : OPENSHIFT_DEFAULTS.clusterNetwork });
    case 'canonical':
      return t('clusterNet.problem.canonical', { canonical: p.canonical });
    case 'length':
      return t('clusterNet.problem.length', { min: p.min, max: p.max });
    case 'hostPrefixRange':
      return t('clusterNet.problem.hostPrefixRange', { min: NETWORKING_LIMITS.hostPrefix.min, max: NETWORKING_LIMITS.hostPrefix.max });
    case 'hostPrefixShort':
      return t('clusterNet.problem.hostPrefixShort', { min: p.min, cidr: p.cidr });
    case 'notWithNetwork':
      return t('clusterNet.problem.notWithNetwork');
    default:
      return '';
  }
}

/** A typed network prefix that overlaps a refused reserved range: the PrefixReserved text, or null. */
export function refusedPrefixText(
  t: (k: string, o?: Record<string, unknown>) => string,
  prefix: string,
  reserved: TenantReservedRange[] | undefined,
): string | null {
  const refused = (reserved ?? []).filter((r) => r && r.rule === 'Refused' && !r.site);
  const hit = reservedOverlap(prefix, refused);
  if (!hit) return null;
  return reasonText(t, { reason: 'PrefixReserved', params: { subject: prefix, range: hit.prefix, purpose: hit.purpose ?? '' } });
}
