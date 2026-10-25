/**
 * What the platform offers for IPv6, as far as the caller can read it:
 * - site capabilities: the tenant's view of its sites (TenantProfile status.sites[].capabilities), else
 *   CloudInfrastructure status.capabilities for platform readers;
 * - the platform IPv6 blocks of a fabric: HybridFabric status.realization for platform readers, else
 *   TenantProfile status.ipv6Platform.
 * Unknown stays unknown (undefined): forms then offer no IPv6 placement inputs and preselect no
 * platform block, and the API remains the authority.
 */
import { useMemo } from 'react';
import type { CloudInfrastructure, HybridFabric } from '../types';
import { useK8sResourceList } from './k8s';
import { useTenantProfile } from './tenantProfile';
import {
  capsFromInfrastructure,
  capsFromProfileSite,
  fabricIpv6Offer,
  profileIpv6Offer,
  type PlatformIpv6Offer,
  type SiteIpv6Caps,
} from '../forms/ipv6';

const PLATFORM_NS = 'sovereign-cloud';

export interface UseSiteIpv6CapsResult {
  /** Capabilities of a site by name; undefined when the caller cannot tell */
  capsFor: (site: string | undefined) => SiteIpv6Caps | undefined;
  loading: boolean;
}

export function useSiteIpv6Caps(namespace: string | undefined, opts: { enabled?: boolean } = {}): UseSiteIpv6CapsResult {
  const enabled = (opts.enabled ?? true) && !!namespace;
  const profile = useTenantProfile(namespace, { enabled });
  // The platform's sites are read only when the tenant's view is not there (platform readers without a profile).
  const infra = useK8sResourceList<CloudInfrastructure>('CloudInfrastructure', {
    namespace: PLATFORM_NS,
    enabled: enabled && !profile.loading && !profile.available,
  });
  return useMemo(() => {
    const capsFor = (site: string | undefined): SiteIpv6Caps | undefined => {
      if (!site) return undefined;
      const entry = profile.sites().find((s) => s.name === site);
      if (entry) return capsFromProfileSite(entry);
      const ci = infra.error ? undefined : infra.items.find((i) => i.metadata.name === site);
      return ci ? capsFromInfrastructure(ci as { status?: { capabilities?: Record<string, unknown> } }) : undefined;
    };
    return { capsFor, loading: profile.loading || (enabled && infra.loading && !infra.error) };
  }, [infra.items, infra.error, infra.loading, profile, enabled]);
}

export interface UsePlatformIpv6OfferResult {
  /** The offer; null = the fabric reports no platform IPv6 space; undefined = the caller cannot tell */
  offer: PlatformIpv6Offer | null | undefined;
  loading: boolean;
}

/** The platform IPv6 blocks the network's fabric offers (fabric = the one named, else the only eligible one). */
export function usePlatformIpv6Offer(
  namespace: string | undefined,
  fabric: HybridFabric | undefined,
  opts: { fabricsReadable: boolean; enabled?: boolean },
): UsePlatformIpv6OfferResult {
  const enabled = (opts.enabled ?? true) && !!namespace;
  const profile = useTenantProfile(namespace, { enabled: enabled && !opts.fabricsReadable });
  return useMemo(() => {
    if (opts.fabricsReadable && fabric) return { offer: fabricIpv6Offer(fabric), loading: false };
    if (profile.available) {
      const o = profileIpv6Offer(profile.profile);
      // The profile names no offer: it may be an older profile, so the caller cannot tell.
      return { offer: o ?? undefined, loading: false };
    }
    return { offer: undefined, loading: profile.loading };
  }, [opts.fabricsReadable, fabric, profile]);
}
