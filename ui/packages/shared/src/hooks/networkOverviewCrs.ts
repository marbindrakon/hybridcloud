import { useMemo } from 'react';
import { HybridSovereignKind, K8sResource } from '../types';
import { useK8sResourceList } from './k8s';
import { PLATFORM_NS } from './overviewCrs';

export interface NetworkOverviewCRsResult {
  items: K8sResource[];
  loading: boolean;
  /** Only set when the tenant's own networking kinds fail. */
  error: Error | null;
  refresh: () => void;
}

/**
 * Fetch exactly the kinds NetworkOverview draws, via per-kind namespaced lists.
 *
 * Deliberately not useOverviewCRs: that hook's `dashboard` path calls
 * `/api/overview/crs`, which only the ADMIN dashboard server implements. On the
 * tenant dashboard that URL falls through to the SPA and returns index.html,
 * so the overview died with `Unexpected token '<' ... is not valid JSON`.
 * `/api/k8s` is served by every host, so per-kind lists work everywhere.
 *
 * Platform kinds live in sovereign-cloud and a tenant may well be forbidden to
 * list them. That is not an error here — the fabric half simply renders empty
 * and the tenant still sees their own VRFs, so those failures are swallowed.
 */
export function useNetworkOverviewCRs(entityNamespace?: string): NetworkOverviewCRsResult {
  // A caller that scopes to an entity may pass '' for a tick while the entity
  // is still resolving. Listing with an empty namespace builds a cluster-scoped
  // URL, which the OpenShift console proxy 404s for namespaced CRDs, so hold
  // off until it is known.
  const scoped = entityNamespace !== undefined;
  const tenantReady = !scoped || !!entityNamespace;
  const opts = { enabled: true } as const;
  const tenantOpts = { namespace: entityNamespace || undefined, enabled: tenantReady };

  // Platform half — read-only context, best effort.
  const fabrics = useK8sResourceList<K8sResource>('HybridFabric', { namespace: PLATFORM_NS, ...opts });
  const gateways = useK8sResourceList<K8sResource>('CloudGateway', { namespace: PLATFORM_NS, ...opts });
  const infra = useK8sResourceList<K8sResource>('CloudInfrastructure', { namespace: PLATFORM_NS, ...opts });

  // Tenant half: networks, placements and the environments they can be placed on (CloudVirt,
  // CloudOSO). Undefined namespace means cluster-wide, which is what the admin views want.
  const networks = useK8sResourceList<K8sResource>('HybridNetwork', tenantOpts);
  const placements = useK8sResourceList<K8sResource>('NetworkPlacement', tenantOpts);
  const cloudOso = useK8sResourceList<K8sResource>('CloudOSO', tenantOpts);
  const cloudVirt = useK8sResourceList<K8sResource>('CloudVirt', tenantOpts);

  const tag = (list: K8sResource[], kind: HybridSovereignKind) =>
    list.map((i) => (i.kind ? i : { ...i, kind }));

  const items = useMemo(
    () => [
      ...tag(fabrics.items, 'HybridFabric'),
      ...tag(gateways.items, 'CloudGateway'),
      ...tag(infra.items, 'CloudInfrastructure'),
      ...tag(networks.items, 'HybridNetwork'),
      ...tag(placements.items, 'NetworkPlacement'),
      ...tag(cloudOso.items, 'CloudOSO'),
      ...tag(cloudVirt.items, 'CloudVirt'),
    ],
    [
      fabrics.items,
      gateways.items,
      infra.items,
      networks.items,
      placements.items,
      cloudOso.items,
      cloudVirt.items,
    ],
  );

  const all = [fabrics, gateways, infra, networks, placements, cloudOso, cloudVirt];

  return {
    items,
    loading: !tenantReady || all.some((r) => r.loading),
    // Only the tenant's own kinds are load-bearing; a 403 on sovereign-cloud
    // must not blank the page for a tenant.
    error: networks.error ?? placements.error ?? null,
    refresh: () => all.forEach((r) => r.refresh()),
  };
}
