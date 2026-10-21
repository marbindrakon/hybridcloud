/**
 * "Shared with you" (NC-13): objects a user may open by name although they
 * may not list the kind, as with the resource-scoped grants of Team and
 * Project access fields. A SelfSubjectRulesReview in the tenant namespace
 * gives the resourceNames the user may `get`; each is then read by name.
 */
import { useEffect, useState } from 'react';
import { fetchResource, k8sFetchJson } from '../hooks/k8s';
import { API_GROUP, KIND_PLURALS, type HybridSovereignKind, type K8sResource } from '../types';
import { rawApiBase } from './ssar';

interface RulesReview {
  status?: { resourceRules?: { apiGroups?: string[]; resources?: string[]; resourceNames?: string[]; verbs?: string[] }[] };
}

/** Names of `kind` objects the caller may `get` by name in `namespace` (rules with resourceNames only). */
export async function sharedNames(kind: HybridSovereignKind, namespace: string): Promise<string[]> {
  const plural = KIND_PLURALS[kind];
  const review = await k8sFetchJson<RulesReview>(
    `${rawApiBase()}/apis/authorization.k8s.io/v1/selfsubjectrulesreviews`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiVersion: 'authorization.k8s.io/v1', kind: 'SelfSubjectRulesReview', spec: { namespace } }),
    },
    { kind: 'SelfSubjectRulesReview', verb: 'create', namespace },
  );
  const names = new Set<string>();
  for (const r of review?.status?.resourceRules ?? []) {
    const groups = r.apiGroups ?? [];
    const resources = r.resources ?? [];
    const verbs = r.verbs ?? [];
    if (!(groups.includes(API_GROUP) || groups.includes('*'))) continue;
    if (!(resources.includes(plural) || resources.includes('*'))) continue;
    if (!(verbs.includes('get') || verbs.includes('*'))) continue;
    for (const n of r.resourceNames ?? []) if (n) names.add(n);
  }
  return Array.from(names).sort();
}

export function useSharedWithYou(
  kind: HybridSovereignKind,
  namespace: string | undefined,
  enabled: boolean,
): { items: K8sResource[]; loading: boolean } {
  const [state, setState] = useState<{ items: K8sResource[]; loading: boolean }>({ items: [], loading: false });
  useEffect(() => {
    if (!enabled || !namespace) {
      setState({ items: [], loading: false });
      return;
    }
    let cancelled = false;
    setState({ items: [], loading: true });
    sharedNames(kind, namespace)
      .then((names) =>
        Promise.all(
          names.map((n) =>
            fetchResource<K8sResource>(kind, n, namespace)
              .then((o) => (o ? { ...o, kind: o.kind || kind } : null))
              .catch(() => null),
          ),
        ),
      )
      .then((objs) => {
        if (!cancelled) setState({ items: objs.filter((o): o is K8sResource => !!o), loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ items: [], loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [kind, namespace, enabled]);
  return state;
}
