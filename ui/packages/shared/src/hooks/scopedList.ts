/**
 * A list for the current scope: one tenant namespace, or All tenants.
 *
 * All tenants first tries one cluster-wide list (platform readers usually may)
 * and, when that is refused (or the host only serves namespaced routes), lists
 * every tenant namespace the user can open and merges them: the per-namespace
 * rule of design 5.1. Namespaces the user may not read are skipped and
 * reported in `skipped`.
 */
import { useCallback, useEffect, useState } from 'react';
import type { HybridSovereignKind, K8sResource } from '../types';
import { fetchResourceList, getK8sClientConfig, useK8sResourceList } from './k8s';
import { classifyError } from './errors';
import { applyFetchOutcome, initialFetchState, pollDelayMs, type FetchState } from './fetchState';
import { isInFlight, phaseOf } from '../status/phase';
import { useOptionalScope } from '../scope/ScopeProvider';

export interface ScopedListResult<T extends K8sResource> {
  items: T[];
  loading: boolean;
  error: Error | null;
  /** A refresh failed; `items` are from the last good answer. */
  refreshError: Error | null;
  /** Tenant namespaces that answered 403 in All tenants scope. */
  skipped: string[];
  refresh: () => void;
}

const EMPTY: never[] = [];

export function useScopedResourceList<T extends K8sResource>(
  kind: HybridSovereignKind,
  options: { namespace?: string; pollIntervalMs?: number; pollWhileInFlight?: boolean; enabled?: boolean } = {},
): ScopedListResult<T> {
  const ctx = useOptionalScope();
  const scope = ctx?.scope;
  const all = !options.namespace && scope?.kind === 'all';
  const namespace = options.namespace ?? (scope?.kind === 'tenant' ? scope.tenant.namespace : undefined);
  const enabled = options.enabled ?? true;

  const single = useK8sResourceList<T>(kind, {
    namespace,
    pollIntervalMs: options.pollIntervalMs,
    pollWhileInFlight: options.pollWhileInFlight,
    enabled: enabled && !all && (!!namespace || !scope || scope.kind === 'global'),
  });

  const tenants = ctx?.tenants ?? [];
  const tenantKey = tenants.map((t) => t.namespace).join(',');
  const key = `all:${kind}:${tenantKey}`;
  const [state, setState] = useState<FetchState<{ items: T[]; skipped: string[] }>>(() => initialFetchState(key));
  const [loading, setLoading] = useState(all);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!all || !enabled) return;
    let cancelled = false;
    setLoading(true);
    const run = async () => {
      if (getK8sClientConfig().apiStyle !== 'dashboard') {
        try {
          return { items: await fetchResourceList<T>(kind, {}), skipped: [] as string[] };
        } catch (err) {
          if (classifyError(err) !== 'forbidden' && classifyError(err) !== 'notFound') throw err;
        }
      }
      const namespaces = tenantKey ? tenantKey.split(',') : [];
      const results = await Promise.all(
        namespaces.map(async (ns) => {
          try {
            return { ns, items: await fetchResourceList<T>(kind, { namespace: ns }) };
          } catch (err) {
            const cls = classifyError(err);
            if (cls === 'forbidden' || cls === 'notFound') return { ns, items: [] as T[], skipped: true };
            throw err;
          }
        }),
      );
      return {
        items: results.flatMap((r) => r.items),
        skipped: results.filter((r) => 'skipped' in r && r.skipped).map((r) => r.ns),
      };
    };
    run()
      .then((r) => {
        if (!cancelled) setState((prev) => applyFetchOutcome(prev, key, { ok: true, data: r }));
      })
      .catch((err: Error) => {
        if (!cancelled) setState((prev) => applyFetchOutcome<{ items: T[]; skipped: string[] }>(prev, key, { ok: false, error: err }));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [all, enabled, kind, tenantKey, key, tick]);

  const current = state.key === key ? state : initialFetchState<{ items: T[]; skipped: string[] }>(key);
  const allItems = current.data?.items ?? (EMPTY as T[]);
  const inFlight = !!options.pollWhileInFlight && allItems.some((i) => isInFlight(phaseOf(i)));
  const delay = pollDelayMs({ pollIntervalMs: options.pollIntervalMs, pollWhileInFlight: options.pollWhileInFlight, inFlight });

  useEffect(() => {
    if (!all || !enabled || loading || !delay) return;
    const id = setTimeout(() => setTick((t) => t + 1), delay);
    return () => clearTimeout(id);
  }, [all, enabled, loading, delay]);

  const refreshAll = useCallback(() => setTick((t) => t + 1), []);

  if (!all) return { ...single, skipped: [] };
  return {
    items: allItems,
    loading,
    error: current.error,
    refreshError: current.refreshError,
    skipped: current.data?.skipped ?? [],
    refresh: refreshAll,
  };
}
