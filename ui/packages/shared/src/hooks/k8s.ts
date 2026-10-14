import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  API_VERSION_FULL,
  HybridSovereignKind,
  KIND_PLURALS,
  K8sResource,
} from '../types';
import { usePermissions } from './permissions';

export type K8sApiStyle = 'raw' | 'dashboard';

export interface K8sClientConfig {
  /** Base URL for raw proxy (e.g. /api/k8s) or unused when apiStyle=dashboard */
  baseUrl?: string;
  token?: string;
  fetchFn?: typeof fetch;
  /**
   * raw = Kubernetes API proxy (/api/k8s/apis/...)
   * dashboard = curated Express routes (/api/teams?namespace=...) returning item arrays
   */
  apiStyle?: K8sApiStyle;
}

export interface UseK8sResourceListOptions {
  namespace?: string;
  labelSelector?: string;
  fieldSelector?: string;
  pollIntervalMs?: number;
  enabled?: boolean;
}

export interface UseK8sResourceListResult<T extends K8sResource> {
  items: T[];
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

export interface UseK8sResourceOptions {
  namespace?: string;
  pollIntervalMs?: number;
  enabled?: boolean;
}

export interface UseK8sResourceResult<T extends K8sResource> {
  resource: T | null;
  loading: boolean;
  error: Error | null;
  refresh: () => void;
}

/** Curated dashboard list paths (tenant + admin Express servers) */
const DASHBOARD_LIST_PATH: Partial<Record<HybridSovereignKind, string>> = {
  Entity: '/api/entities',
  Team: '/api/teams',
  Project: '/api/projects',
  Assignment: '/api/assignments',
  PlatformOpenshift: '/api/platformopenshifts',
  CloudOSO: '/api/cloudosos',
  CloudAWS: '/api/cloudawss',
  CloudVirt: '/api/cloudvirts',
  OpenStackMigration: '/api/openstackmigrations',
  Persona: '/api/personas',
  Rbac: '/api/rbacs',
  Vault: '/api/vaults',
  VaultKV: '/api/vaultkvs',
  AAPOrg: '/api/aaporgs',
  QuayOrg: '/api/quayorgs',
  RbacConfig: '/api/rbacconfigs',
  AAPConfig: '/api/aapconfigs',
  QuayConfig: '/api/quayconfigs',
};

const DASHBOARD_CREATE_PATH: Partial<Record<HybridSovereignKind, string>> = {
  ...DASHBOARD_LIST_PATH,
};

let globalK8sConfig: K8sClientConfig = { baseUrl: '/api/k8s', apiStyle: 'raw' };

export function configureK8sClient(config: K8sClientConfig): void {
  globalK8sConfig = { ...globalK8sConfig, ...config };
}

export function getK8sClientConfig(): K8sClientConfig {
  return globalK8sConfig;
}

function buildRawListUrl(kind: HybridSovereignKind, options: UseK8sResourceListOptions): string {
  const plural = KIND_PLURALS[kind];
  const base = globalK8sConfig.baseUrl ?? '/api/k8s';
  const params = new URLSearchParams();
  if (options.labelSelector) params.set('labelSelector', options.labelSelector);
  if (options.fieldSelector) params.set('fieldSelector', options.fieldSelector);
  const query = params.toString() ? `?${params.toString()}` : '';

  // Entity (and other namespaced kinds) must use a namespace path when provided.
  // Cluster-scoped list (/apis/.../entities) also works for privileged users, but
  // OpenShift console proxies often 404 that form for namespaced CRDs.
  if (options.namespace) {
    return `${base}/apis/${API_VERSION_FULL}/namespaces/${options.namespace}/${plural}${query}`;
  }
  return `${base}/apis/${API_VERSION_FULL}/${plural}${query}`;
}

function buildDashboardListUrl(kind: HybridSovereignKind, options: UseK8sResourceListOptions): string {
  const path = DASHBOARD_LIST_PATH[kind];
  if (!path) {
    // Fallback to raw for kinds without curated routes (e.g. admin-only)
    return buildRawListUrl(kind, options);
  }
  if (kind === 'Entity' || kind === 'RbacConfig' || kind === 'AAPConfig' || kind === 'QuayConfig') {
    return path;
  }
  if (!options.namespace) {
    return path;
  }
  return `${path}?namespace=${encodeURIComponent(options.namespace)}`;
}

function buildListUrl(kind: HybridSovereignKind, options: UseK8sResourceListOptions): string {
  return globalK8sConfig.apiStyle === 'dashboard'
    ? buildDashboardListUrl(kind, options)
    : buildRawListUrl(kind, options);
}

function buildDashboardResourceUrl(
  kind: HybridSovereignKind,
  name: string,
  namespace?: string,
): string | null {
  const path = DASHBOARD_LIST_PATH[kind];
  if (!path) return null;
  const encoded = encodeURIComponent(name);
  if (kind === 'Entity' || kind === 'RbacConfig' || kind === 'AAPConfig' || kind === 'QuayConfig') {
    return `${path}/${encoded}`;
  }
  if (!namespace) return null;
  return `${path}/${encoded}?namespace=${encodeURIComponent(namespace)}`;
}

function buildResourceUrl(kind: HybridSovereignKind, name: string, namespace?: string): string {
  if (globalK8sConfig.apiStyle === 'dashboard') {
    const dash = buildDashboardResourceUrl(kind, name, namespace);
    if (dash) return dash;
  }
  const plural = KIND_PLURALS[kind];
  const base = globalK8sConfig.baseUrl ?? '/api/k8s';
  if (namespace) {
    return `${base}/apis/${API_VERSION_FULL}/namespaces/${namespace}/${plural}/${encodeURIComponent(name)}`;
  }
  return `${base}/apis/${API_VERSION_FULL}/${plural}/${encodeURIComponent(name)}`;
}

const UPDATE_METHOD: Partial<Record<HybridSovereignKind, 'PUT' | 'PATCH'>> = {
  Team: 'PUT',
  Assignment: 'PUT',
  Rbac: 'PUT',
  VaultKV: 'PUT',
  AAPOrg: 'PUT',
  QuayOrg: 'PUT',
  Project: 'PATCH',
  PlatformOpenshift: 'PATCH',
  CloudOSO: 'PATCH',
  CloudAWS: 'PATCH',
  CloudVirt: 'PATCH',
  Persona: 'PATCH',
};

const RECONCILE_ANNOTATION = 'ansible.sdk.operatorframework.io/reconcileNow';

/** Force reconcile by setting the operator reconcileNow annotation */
export async function forceReconcile(
  kind: HybridSovereignKind,
  name: string,
  namespace: string,
): Promise<unknown> {
  if (globalK8sConfig.apiStyle === 'dashboard') {
    return k8sFetchJson('/api/k8s/patch-annotation', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        group: 'hybridsovereign.redhat',
        version: 'v1alpha1',
        kind,
        name,
        namespace,
        annotation: RECONCILE_ANNOTATION,
        value: 'true',
      }),
    });
  }
  const url = buildResourceUrl(kind, name, namespace);
  const current = await k8sFetchJson<K8sResource>(url);
  return k8sFetchJson(url, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/merge-patch+json' },
    body: JSON.stringify({
      metadata: {
        annotations: {
          ...(current.metadata?.annotations ?? {}),
          [RECONCILE_ANNOTATION]: new Date().toISOString(),
        },
      },
    }),
  });
}

/** Delete a Hybrid Sovereign CR via curated dashboard API when available */
export async function deleteDashboardResource(
  kind: HybridSovereignKind,
  name: string,
  namespace?: string,
): Promise<unknown> {
  const path = DASHBOARD_LIST_PATH[kind];
  if (globalK8sConfig.apiStyle === 'dashboard' && path) {
    const url =
      namespace && kind !== 'Entity'
        ? `${path}/${encodeURIComponent(name)}?namespace=${encodeURIComponent(namespace)}`
        : `${path}/${encodeURIComponent(name)}`;
    return k8sFetchJson(url, { method: 'DELETE' });
  }
  return k8sFetchJson(buildResourceUrl(kind, name, namespace), { method: 'DELETE' });
}

/** Update a Hybrid Sovereign CR spec via curated dashboard API when available */
export async function updateDashboardResource(
  kind: HybridSovereignKind,
  name: string,
  namespace: string,
  body: { spec: unknown },
): Promise<unknown> {
  const path = DASHBOARD_LIST_PATH[kind];
  const method = UPDATE_METHOD[kind] ?? 'PUT';
  if (globalK8sConfig.apiStyle === 'dashboard' && path) {
    const url = `${path}/${encodeURIComponent(name)}?namespace=${encodeURIComponent(namespace)}`;
    return k8sFetchJson(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }
  return k8sFetchJson(buildResourceUrl(kind, name, namespace), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/merge-patch+json' },
    body: JSON.stringify(body),
  });
}

async function k8sFetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (globalK8sConfig.token) {
    headers.Authorization = `Bearer ${globalK8sConfig.token}`;
  }
  const fetchImpl = globalK8sConfig.fetchFn ?? fetch;
  const response = await fetchImpl(url, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { message?: string };
      if (body?.message) detail = body.message;
    } catch {
      /* ignore */
    }
    throw new Error(`K8s API error ${response.status}: ${detail}`);
  }
  // DELETE / some PATCHes return 200 with body, 202 Accepted, or 204 No Content.
  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return undefined as T;
  }
}

function normalizeListPayload<T>(data: unknown): T[] {
  let items: unknown[] = [];
  if (Array.isArray(data)) items = data;
  else if (data && typeof data === 'object' && Array.isArray((data as { items?: unknown[] }).items)) {
    items = (data as { items: unknown[] }).items;
  }
  // Drop holes / non-objects / entries without metadata.name (dashboard proxies sometimes emit sparse lists).
  return items.filter((item): item is T => {
    if (!item || typeof item !== 'object') return false;
    const name = (item as { metadata?: { name?: unknown } }).metadata?.name;
    return typeof name === 'string' && name.length > 0;
  });
}

/** Create a core/v1 Secret in a namespace (credentials for CloudAWS / CloudOSO). */
export async function createNamespaceSecret(
  namespace: string,
  opts: {
    name: string;
    stringData: Record<string, string>;
    labels?: Record<string, string>;
  },
): Promise<unknown> {
  const base = globalK8sConfig.baseUrl ?? '/api/k8s';
  const url = `${base}/api/v1/namespaces/${encodeURIComponent(namespace)}/secrets`;
  const body = {
    apiVersion: 'v1',
    kind: 'Secret',
    metadata: {
      name: opts.name,
      namespace,
      ...(opts.labels ? { labels: opts.labels } : {}),
    },
    type: 'Opaque',
    stringData: opts.stringData,
  };
  return k8sFetchJson(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Create a Hybrid Sovereign CR via curated dashboard API when available */
export async function createDashboardResource(
  kind: HybridSovereignKind,
  body: { name: string; namespace?: string; spec?: unknown } | unknown,
  namespace?: string,
): Promise<unknown> {
  const path = DASHBOARD_CREATE_PATH[kind];
  if (globalK8sConfig.apiStyle === 'dashboard' && path) {
    // Curated Express handlers expect { name, namespace, spec } — not a full K8s object
    let payload = body as { name?: string; namespace?: string; spec?: unknown; metadata?: { name?: string; namespace?: string } };
    if (payload && typeof payload === 'object' && 'metadata' in payload && payload.metadata?.name) {
      payload = {
        name: payload.metadata.name,
        namespace: payload.metadata.namespace ?? namespace,
        spec: (body as { spec?: unknown }).spec,
      };
    }
    const ns = payload.namespace ?? namespace;
    const url = ns && kind !== 'Entity' ? `${path}?namespace=${encodeURIComponent(ns)}` : path;
    return k8sFetchJson(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  }
  const plural = KIND_PLURALS[kind];
  const base = globalK8sConfig.baseUrl ?? '/api/k8s';
  const incoming = body as {
    apiVersion?: string;
    kind?: string;
    name?: string;
    namespace?: string;
    spec?: unknown;
    metadata?: { name?: string; namespace?: string };
  };
  const metaName = incoming.metadata?.name ?? incoming.name;
  const metaNs = incoming.metadata?.namespace ?? incoming.namespace ?? namespace;
  const payload =
    incoming.apiVersion && incoming.kind
      ? body
      : {
          apiVersion: API_VERSION_FULL,
          kind,
          metadata: {
            name: metaName,
            ...(metaNs ? { namespace: metaNs } : {}),
          },
          spec: incoming.spec ?? {},
        };
  const url =
    metaNs && kind !== 'Entity'
      ? `${base}/apis/${API_VERSION_FULL}/namespaces/${metaNs}/${plural}`
      : metaNs && kind === 'Entity'
        ? `${base}/apis/${API_VERSION_FULL}/namespaces/${metaNs}/${plural}`
        : `${base}/apis/${API_VERSION_FULL}/${plural}`;
  return k8sFetchJson(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** List Hybrid Sovereign CRs of a given kind */
export function useK8sResourceList<T extends K8sResource>(
  kind: HybridSovereignKind,
  options: UseK8sResourceListOptions = {},
): UseK8sResourceListResult<T> {
  const { namespace, pollIntervalMs, enabled = true, labelSelector, fieldSelector } = options;
  const [items, setItems] = useState<T[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
  const [tick, setTick] = useState(0);

  const url = useMemo(
    () => buildListUrl(kind, { namespace, labelSelector, fieldSelector }),
    [kind, namespace, labelSelector, fieldSelector],
  );

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    k8sFetchJson<unknown>(url)
      .then((data) => {
        if (!cancelled) {
          setItems(normalizeListPayload<T>(data));
          setError(null);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [url, enabled, tick]);

  useEffect(() => {
    if (!pollIntervalMs || !enabled) return;
    const id = setInterval(refresh, pollIntervalMs);
    return () => clearInterval(id);
  }, [pollIntervalMs, enabled, refresh]);

  return { items, loading, error, refresh };
}

/** Fetch a single Hybrid Sovereign CR by name */
export function useK8sResource<T extends K8sResource>(
  kind: HybridSovereignKind,
  name: string,
  options: UseK8sResourceOptions = {},
): UseK8sResourceResult<T> {
  const { namespace, pollIntervalMs, enabled = true } = options;
  const [resource, setResource] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
  const [tick, setTick] = useState(0);

  const url = useMemo(() => buildResourceUrl(kind, name, namespace), [kind, name, namespace]);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled || !name) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    k8sFetchJson<T>(url)
      .then((data) => {
        if (!cancelled) {
          setResource(data);
          setError(null);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [url, enabled, name, tick]);

  useEffect(() => {
    if (!pollIntervalMs || !enabled) return;
    const id = setInterval(refresh, pollIntervalMs);
    return () => clearInterval(id);
  }, [pollIntervalMs, enabled, refresh]);

  return { resource, loading, error, refresh };
}

/** Check if the current user can perform a verb on a resource */
export function useCanI(
  namespace: string,
  resource: string,
  verb: 'create' | 'get' | 'list' | 'update' | 'patch' | 'delete',
): { allowed: boolean; loading: boolean } {
  const { can, loading } = usePermissions(
    namespace || 'default',
    [{ resource, verb }],
    { enabled: !!namespace && !!resource },
  );
  return { allowed: can(resource, verb), loading };
}
