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
import { useOptionalScope } from '../scope/ScopeProvider';

export interface ScopedListResult<T extends K8sResource> {
  items: T[];
  loading: boolean;
  error: Error | null;
  /** Tenant namespaces that answered 403 in All tenants scope. */
  skipped: string[];
  refresh: () => void;
}

export function useScopedResourceList<T extends K8sResource>(
  kind: HybridSovereignKind,
  options: { namespace?: string; pollIntervalMs?: number; enabled?: boolean } = {},
): ScopedListResult<T> {
  const ctx = useOptionalScope();
  const scope = ctx?.scope;
  const all = !options.namespace && scope?.kind === 'all';
  const namespace = options.namespace ?? (scope?.kind === 'tenant' ? scope.tenant.namespace : undefined);
  const enabled = options.enabled ?? true;

  const single = useK8sResourceList<T>(kind, {
    namespace,
    pollIntervalMs: options.pollIntervalMs,
    enabled: enabled && !all && (!!namespace || !scope || scope.kind === 'global'),
  });

  const [state, setState] = useState<{ items: T[]; loading: boolean; error: Error | null; skipped: string[] }>({
    items: [],
    loading: all,
    error: null,
    skipped: [],
  });
  const [tick, setTick] = useState(0);
  const tenants = ctx?.tenants ?? [];
  const tenantKey = tenants.map((t) => t.namespace).join(',');

  useEffect(() => {
    if (!all || !enabled) return;
    let cancelled = false;
    setState((s) => ({ ...s, loading: true }));
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
        if (!cancelled) setState({ items: r.items, loading: false, error: null, skipped: r.skipped });
      })
      .catch((err: Error) => {
        if (!cancelled) setState({ items: [], loading: false, error: err, skipped: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [all, enabled, kind, tenantKey, tick]);

  useEffect(() => {
    if (!all || !enabled || !options.pollIntervalMs) return;
    const id = setInterval(() => setTick((t) => t + 1), options.pollIntervalMs);
    return () => clearInterval(id);
  }, [all, enabled, options.pollIntervalMs]);

  const refreshAll = useCallback(() => setTick((t) => t + 1), []);

  if (!all) return { ...single, skipped: [] };
  return { ...state, refresh: refreshAll };
}
