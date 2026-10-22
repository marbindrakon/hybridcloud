/**
 * "Shared with you" (NC-13): objects a user may open by name although they
 * may not list the kind, as with the resource-scoped grants of Team and
 * Project access fields. A SelfSubjectRulesReview in the tenant namespace
 * gives the resourceNames the user may `get`; each is then read by name.
 *
 * The review is made once per namespace and cached like the permission
 * matrix: the navigation, Home and the list pages share one answer (GT-F4).
 */
import { useEffect, useMemo, useState } from 'react';
import { fetchResource, k8sFetchJson } from '../hooks/k8s';
import { API_GROUP, KIND_PLURALS, type HybridSovereignKind, type K8sResource } from '../types';
import { rawApiBase } from './ssar';
import { subscribePermissions } from './permissionGate';

interface ResourceRule {
  apiGroups?: string[];
  resources?: string[];
  resourceNames?: string[];
  verbs?: string[];
}

interface RulesReview {
  status?: { resourceRules?: ResourceRule[] };
}

/** plural -> names the caller may `get` by name (rules with resourceNames only). */
export type SharedNames = Record<string, string[]>;

/** The rules of a review folded into names per plural of the API group. */
export function sharedNamesFromRules(rules: ResourceRule[] | undefined): SharedNames {
  const out: Record<string, Set<string>> = {};
  for (const r of rules ?? []) {
    const groups = r.apiGroups ?? [];
    const verbs = r.verbs ?? [];
    if (!(groups.includes(API_GROUP) || groups.includes('*'))) continue;
    if (!(verbs.includes('get') || verbs.includes('*'))) continue;
    const names = (r.resourceNames ?? []).filter(Boolean);
    if (!names.length) continue;
    for (const res of r.resources ?? []) {
      if (!res || res.includes('/')) continue; // subresources (status) do not open a page
      out[res] = out[res] ?? new Set<string>();
      for (const n of names) out[res].add(n);
    }
  }
  return Object.fromEntries(Object.entries(out).map(([k, v]) => [k, Array.from(v).sort()]));
}

const TTL_MS = 30000;
const cache = new Map<string, { at: number; promise: Promise<SharedNames> }>();
subscribePermissions(() => cache.clear());

/** Names per plural the caller may `get` in `namespace` (one cached review). */
export function sharedNamesIn(namespace: string): Promise<SharedNames> {
  const now = Date.now();
  const hit = cache.get(namespace);
  if (hit && now - hit.at < TTL_MS) return hit.promise;
  const promise = k8sFetchJson<RulesReview>(
    `${rawApiBase()}/apis/authorization.k8s.io/v1/selfsubjectrulesreviews`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiVersion: 'authorization.k8s.io/v1', kind: 'SelfSubjectRulesReview', spec: { namespace } }),
    },
    { kind: 'SelfSubjectRulesReview', verb: 'create', namespace },
  ).then((review) => sharedNamesFromRules(review?.status?.resourceRules));
  cache.set(namespace, { at: now, promise });
  promise.catch(() => {
    if (cache.get(namespace)?.promise === promise) cache.delete(namespace);
  });
  return promise;
}

/** Names of `kind` objects the caller may `get` by name in `namespace`. */
export async function sharedNames(kind: HybridSovereignKind, namespace: string): Promise<string[]> {
  return (await sharedNamesIn(namespace))[KIND_PLURALS[kind]] ?? [];
}

/**
 * Kinds of which the caller may open at least one object by name in
 * `namespace` (undefined while unknown). The navigation shows their entries.
 */
export function useSharedKinds(namespace: string | undefined, enabled: boolean): Set<string> | undefined {
  const [kinds, setKinds] = useState<Set<string> | undefined>(undefined);
  useEffect(() => {
    if (!enabled || !namespace) {
      setKinds(undefined);
      return;
    }
    let cancelled = false;
    sharedNamesIn(namespace)
      .then((names) => {
        if (cancelled) return;
        const out = new Set<string>();
        for (const [kind, plural] of Object.entries(KIND_PLURALS)) if ((names[plural] ?? []).length) out.add(kind);
        setKinds(out);
      })
      .catch(() => {
        if (!cancelled) setKinds(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, [namespace, enabled]);
  return kinds;
}

/** The objects of each kind in `kinds` the caller may open by name, read one by one. */
export function useSharedObjects(
  kinds: HybridSovereignKind[],
  namespace: string | undefined,
  enabled: boolean,
): { byKind: Partial<Record<HybridSovereignKind, K8sResource[]>>; loading: boolean } {
  const key = kinds.join(',');
  const [state, setState] = useState<{ byKind: Partial<Record<HybridSovereignKind, K8sResource[]>>; loading: boolean }>({
    byKind: {},
    loading: false,
  });
  useEffect(() => {
    const ks = key ? (key.split(',') as HybridSovereignKind[]) : [];
    if (!enabled || !namespace || !ks.length) {
      setState({ byKind: {}, loading: false });
      return;
    }
    let cancelled = false;
    setState((s) => ({ ...s, loading: true }));
    sharedNamesIn(namespace)
      .then((names) =>
        Promise.all(
          ks.map(async (kind) => {
            const objs = await Promise.all(
              (names[KIND_PLURALS[kind]] ?? []).map((n) =>
                fetchResource<K8sResource>(kind, n, namespace)
                  .then((o) => (o ? { ...o, kind: o.kind || kind } : null))
                  .catch(() => null),
              ),
            );
            return [kind, objs.filter((o): o is K8sResource => !!o)] as const;
          }),
        ),
      )
      .then((pairs) => {
        if (!cancelled) setState({ byKind: Object.fromEntries(pairs), loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ byKind: {}, loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [key, namespace, enabled]);
  return state;
}

export function useSharedWithYou(
  kind: HybridSovereignKind,
  namespace: string | undefined,
  enabled: boolean,
): { items: K8sResource[]; loading: boolean } {
  const kinds = useMemo(() => [kind], [kind]);
  const { byKind, loading } = useSharedObjects(kinds, namespace, enabled);
  return { items: byKind[kind] ?? [], loading };
}
