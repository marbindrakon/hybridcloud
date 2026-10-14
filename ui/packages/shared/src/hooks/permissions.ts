import { useEffect, useMemo, useState } from 'react';
import { API_GROUP, HybridSovereignKind, KIND_PLURALS } from '../types';

export type K8sVerb = 'create' | 'get' | 'list' | 'update' | 'patch' | 'delete';

export interface PermissionCheck {
  resource: string;
  verb: K8sVerb;
}

export interface UsePermissionsResult {
  /** Map of `${resource}/${verb}` → allowed */
  permissions: Record<string, boolean>;
  loading: boolean;
  can: (resource: string, verb: K8sVerb) => boolean;
}

export type PermissionsClientStyle = 'can-i' | 'ssar';

let permissionsBaseUrl = '/api/k8s';
let permissionsStyle: PermissionsClientStyle = 'can-i';
let permissionsFetch: typeof fetch = fetch;

export function configurePermissionsClient(
  baseUrl: string,
  options?: { style?: PermissionsClientStyle; fetchFn?: typeof fetch },
): void {
  permissionsBaseUrl = baseUrl;
  if (options?.style) permissionsStyle = options.style;
  if (options?.fetchFn) permissionsFetch = options.fetchFn;
}

async function fetchCanIProxy(
  namespace: string,
  resource: string,
  verb: K8sVerb,
  token?: string,
): Promise<boolean> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const url = `${permissionsBaseUrl}/can-i/${encodeURIComponent(namespace)}/${resource}/${verb}`;
  try {
    const response = await permissionsFetch(url, { headers });
    if (!response.ok) return false;
    const data = (await response.json()) as { allowed?: boolean };
    return !!data.allowed;
  } catch {
    return false;
  }
}

async function fetchCanISSAR(
  namespace: string | undefined,
  resource: string,
  verb: K8sVerb,
  token?: string,
): Promise<boolean> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'Content-Type': 'application/json',
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  const body = {
    apiVersion: 'authorization.k8s.io/v1',
    kind: 'SelfSubjectAccessReview',
    spec: {
      resourceAttributes: {
        group: API_GROUP,
        resource,
        verb,
        ...(namespace && namespace !== '*' ? { namespace } : {}),
      },
    },
  };
  try {
    const response = await permissionsFetch(
      `${permissionsBaseUrl}/apis/authorization.k8s.io/v1/selfsubjectaccessreviews`,
      { method: 'POST', headers, body: JSON.stringify(body) },
    );
    if (!response.ok) return false;
    const data = (await response.json()) as { status?: { allowed?: boolean } };
    return !!data?.status?.allowed;
  } catch {
    return false;
  }
}

async function fetchCanI(
  namespace: string,
  resource: string,
  verb: K8sVerb,
  token?: string,
): Promise<boolean> {
  if (permissionsStyle === 'ssar') {
    return fetchCanISSAR(namespace, resource, verb, token);
  }
  return fetchCanIProxy(namespace, resource, verb, token);
}

/** Batch SubjectAccessReview checks for a namespace */
export function usePermissions(
  namespace: string,
  checks: PermissionCheck[],
  options: { token?: string; enabled?: boolean } = {},
): UsePermissionsResult {
  const { token, enabled = true } = options;
  const [permissions, setPermissions] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(enabled);

  const checkKey = useMemo(
    () => checks.map((c) => `${c.resource}/${c.verb}`).join(','),
    [checks],
  );

  // Callers usually pass a new array literal on every render, so the effect keys on the checks' content
  // (checkKey), never on the array itself: depending on `checks` re-ran it after each render, and its
  // setState calls re-rendered again (one can-i request per render, thousands per second).
  useEffect(() => {
    const current = checkKey
      ? checkKey.split(',').map((k) => {
          const [resource, verb] = k.split('/');
          return { resource, verb: verb as K8sVerb };
        })
      : [];
    if (!enabled || !namespace || current.length === 0) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    Promise.all(
      current.map(async (check) => {
        const allowed = await fetchCanI(namespace, check.resource, check.verb, token);
        return [`${check.resource}/${check.verb}`, allowed] as const;
      }),
    )
      .then((entries) => {
        if (!cancelled) {
          setPermissions(Object.fromEntries(entries));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [namespace, checkKey, enabled, token]);

  const can = (resource: string, verb: K8sVerb): boolean =>
    permissions[`${resource}/${verb}`] ?? false;

  return { permissions, loading, can };
}

/** Convenience: check list permission for a Hybrid Sovereign kind in a namespace */
export function useCanListKind(
  namespace: string,
  kind: HybridSovereignKind,
  options: { token?: string; enabled?: boolean } = {},
): { allowed: boolean; loading: boolean } {
  const plural = KIND_PLURALS[kind];
  const { can, loading } = usePermissions(
    namespace,
    [{ resource: plural, verb: 'list' }],
    options,
  );
  return { allowed: can(plural, 'list'), loading };
}

/** Cluster-scoped (or any-namespace) list check — uses SSAR without namespace when style=ssar */
export function useCanListKindCluster(
  kind: HybridSovereignKind,
  options: { token?: string; enabled?: boolean } = {},
): { allowed: boolean; loading: boolean } {
  const plural = KIND_PLURALS[kind];
  const { token, enabled = true } = options;
  const [allowed, setAllowed] = useState(false);
  const [loading, setLoading] = useState(enabled);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const run =
      permissionsStyle === 'ssar'
        ? fetchCanISSAR(undefined, plural, 'list', token)
        : fetchCanIProxy('*', plural, 'list', token);
    run
      .then((ok) => {
        if (!cancelled) setAllowed(ok);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [plural, enabled, token]);

  return { allowed, loading };
}
