import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  API_VERSION_FULL,
  HybridSovereignKind,
  KIND_PLURALS,
  K8sResource,
} from '../types';
import { usePermissions } from './permissions';
import { errorFromResponse, K8sApiErrorContext, networkError, parseWarningHeaders } from './errors';
import { applyFetchOutcome, FetchState, initialFetchState, pollDelayMs } from './fetchState';
import { isInFlight, phaseOf } from '../status/phase';

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
  /** Dashboard permission batch endpoint (default /api/permissions); unused by the console (ssar). */
  permissionsEndpoint?: string;
}

export interface UseK8sResourceListOptions {
  namespace?: string;
  labelSelector?: string;
  fieldSelector?: string;
  /** Fixed refresh interval; wins over pollWhileInFlight. */
  pollIntervalMs?: number;
  /** Refresh every 5 s while an item is in flight (Waiting, Creating, Updating, Deleting), else every 30 s. */
  pollWhileInFlight?: boolean;
  enabled?: boolean;
}

export interface UseK8sResourceListResult<T extends K8sResource> {
  /** The last good list for the current URL (kept when a refresh fails for a transient reason). */
  items: T[];
  loading: boolean;
  /** The list could not be loaded and there is nothing to show (full-page state). */
  error: Error | null;
  /** A refresh failed; `items` are from the last good answer (inline "retrying" state). */
  refreshError: Error | null;
  /** Epoch ms of the last good answer. */
  loadedAt?: number;
  refresh: () => void;
}

export interface UseK8sResourceOptions {
  namespace?: string;
  /** Fixed refresh interval; wins over pollWhileInFlight. */
  pollIntervalMs?: number;
  /** Refresh every 5 s while the object is in flight, else every 30 s. */
  pollWhileInFlight?: boolean;
  enabled?: boolean;
}

export interface UseK8sResourceResult<T extends K8sResource> {
  /** The last good object for the current URL (kept when a refresh fails for a transient reason). */
  resource: T | null;
  loading: boolean;
  error: Error | null;
  /** A refresh failed; `resource` is from the last good answer. */
  refreshError: Error | null;
  loadedAt?: number;
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
  CloudGCP: '/api/cloudgcps',
  CloudVirt: '/api/cloudvirts',
  HybridNetwork: '/api/hybridnetworks',
  NetworkPlacement: '/api/networkplacements',
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
  CloudGCP: 'PATCH',
  CloudVirt: 'PATCH',
  HybridNetwork: 'PATCH',
  Persona: 'PATCH',
};

const RECONCILE_ANNOTATION = 'ansible.sdk.operatorframework.io/reconcileNow';

/** Options of a create or update. */
export interface WriteOptions {
  /** Server dry run: the API validates and answers, nothing is persisted. */
  dryRun?: boolean;
  /** Called with the API's `Warning` texts of a successful answer. */
  onWarnings?: (warnings: string[]) => void;
}

/**
 * Query of a write: the raw API gets fieldValidation=Strict (unknown or
 * misspelled fields are refused with their path, never dropped) and dryRun=All
 * for a dry run; the curated dashboard routes add Strict themselves and only
 * need dryRun=All.
 */
function withWriteQuery(url: string, opts: WriteOptions | undefined, raw: boolean): string {
  const params: string[] = [];
  if (raw) params.push('fieldValidation=Strict');
  if (opts?.dryRun) params.push('dryRun=All');
  if (!params.length) return url;
  return `${url}${url.includes('?') ? '&' : '?'}${params.join('&')}`;
}

async function write<T>(url: string, init: RequestInit, opts: WriteOptions | undefined, ctx: K8sApiErrorContext): Promise<T> {
  const answer = await k8sFetchWithWarnings<T>(url, init, ctx);
  if (answer.warnings.length) opts?.onWarnings?.(answer.warnings);
  return answer.body;
}

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
        // Any new value is a request; the controller records the one it handled (Retry now).
        value: new Date().toISOString(),
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
  opts?: WriteOptions,
): Promise<unknown> {
  const path = DASHBOARD_LIST_PATH[kind];
  const method = UPDATE_METHOD[kind] ?? 'PUT';
  const ctx = { kind, verb: 'update', namespace, name };
  if (globalK8sConfig.apiStyle === 'dashboard' && path) {
    const url = `${path}/${encodeURIComponent(name)}?namespace=${encodeURIComponent(namespace)}`;
    return write(
      withWriteQuery(url, opts, false),
      { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
      opts,
      ctx,
    );
  }
  return write(
    withWriteQuery(buildResourceUrl(kind, name, namespace), opts, true),
    { method: 'PATCH', headers: { 'Content-Type': 'application/merge-patch+json' }, body: JSON.stringify(body) },
    opts,
    ctx,
  );
}

const forbiddenListeners = new Set<() => void>();

/** Called on every 403 answer (the permission gate drops its cache then). Returns the remover. */
export function addForbiddenListener(fn: () => void): () => void {
  forbiddenListeners.add(fn);
  return () => {
    forbiddenListeners.delete(fn);
  };
}

/** A successful answer with the Kubernetes `Warning` headers that came with it. */
export interface K8sAnswer<T> {
  body: T;
  warnings: string[];
}

/**
 * Fetch JSON from the configured API base and keep the answer's `Warning`
 * headers. A failed answer throws `K8sApiError` (status, reason, causes,
 * warnings); a request that never got an answer throws one with status 0.
 */
export async function k8sFetchWithWarnings<T>(
  url: string,
  init?: RequestInit,
  ctx: K8sApiErrorContext = {},
): Promise<K8sAnswer<T>> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (globalK8sConfig.token) {
    headers.Authorization = `Bearer ${globalK8sConfig.token}`;
  }
  const fetchImpl = globalK8sConfig.fetchFn ?? fetch;
  let response: Response;
  try {
    response = await fetchImpl(url, { ...init, headers });
  } catch (err) {
    throw networkError(err, ctx);
  }
  if (!response.ok) {
    if (response.status === 403) {
      forbiddenListeners.forEach((fn) => {
        try {
          fn();
        } catch {
          /* listener problems never mask the API answer */
        }
      });
    }
    throw await errorFromResponse(response, ctx);
  }
  const warnings = parseWarningHeaders(response.headers) ?? [];
  // DELETE / some PATCHes return 200 with body, 202 Accepted, or 204 No Content.
  if (response.status === 204 || response.status === 205) {
    return { body: undefined as T, warnings };
  }
  const text = await response.text();
  if (!text) return { body: undefined as T, warnings };
  try {
    return { body: JSON.parse(text) as T, warnings };
  } catch {
    return { body: undefined as T, warnings };
  }
}

/**
 * Fetch JSON from the configured API base. A failed answer throws `K8sApiError`
 * (status, reason, causes, warnings); a request that never got an answer throws
 * one with status 0.
 */
export async function k8sFetchJson<T>(
  url: string,
  init?: RequestInit,
  ctx: K8sApiErrorContext = {},
): Promise<T> {
  return (await k8sFetchWithWarnings<T>(url, init, ctx)).body;
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
  opts?: WriteOptions,
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
    return write(
      withWriteQuery(url, opts, false),
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
      opts,
      { kind, verb: 'create', namespace: ns, name: payload.name },
    );
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
  return write(
    withWriteQuery(url, opts, true),
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) },
    opts,
    { kind, verb: 'create', namespace: metaNs, name: metaName },
  );
}

/** One object by name (curated route or raw API, as configured); throws K8sApiError. */
export async function fetchResource<T extends K8sResource>(
  kind: HybridSovereignKind,
  name: string,
  namespace?: string,
): Promise<T> {
  return k8sFetchJson<T>(buildResourceUrl(kind, name, namespace), undefined, { kind, verb: 'get', namespace, name });
}

/** One list request (curated route or raw API, as configured); throws K8sApiError. */
export async function fetchResourceList<T extends K8sResource>(
  kind: HybridSovereignKind,
  options: UseK8sResourceListOptions = {},
): Promise<T[]> {
  const url = buildListUrl(kind, options);
  const data = await k8sFetchJson<unknown>(url, undefined, { kind, verb: 'list', namespace: options.namespace });
  return normalizeListPayload<T>(data);
}

const NO_ITEMS: never[] = [];

/** True while the browser tab is hidden (polling pauses then). */
function useDocumentHidden(): boolean {
  const [hidden, setHidden] = useState(() => typeof document !== 'undefined' && document.hidden === true);
  useEffect(() => {
    if (typeof document === 'undefined') return;
    const onChange = () => setHidden(document.hidden === true);
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);
  return hidden;
}

/**
 * Schedules the next refresh once the current request has finished, so
 * requests never overlap; a changed delay or a manual refresh reschedules.
 */
function useNextRefresh(delayMs: number, active: boolean, refresh: () => void): void {
  useEffect(() => {
    if (!active || !delayMs) return;
    const id = setTimeout(refresh, delayMs);
    return () => clearTimeout(id);
  }, [delayMs, active, refresh]);
}

/** List Hybrid Sovereign CRs of a given kind */
export function useK8sResourceList<T extends K8sResource>(
  kind: HybridSovereignKind,
  options: UseK8sResourceListOptions = {},
): UseK8sResourceListResult<T> {
  const { namespace, pollIntervalMs, pollWhileInFlight, enabled = true, labelSelector, fieldSelector } = options;
  const [loading, setLoading] = useState(enabled);
  const [tick, setTick] = useState(0);

  const url = useMemo(
    () => buildListUrl(kind, { namespace, labelSelector, fieldSelector }),
    [kind, namespace, labelSelector, fieldSelector],
  );
  const [state, setState] = useState<FetchState<T[]>>(() => initialFetchState<T[]>(url));

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    k8sFetchJson<unknown>(url, undefined, { kind, verb: 'list', namespace })
      .then((data) => {
        if (!cancelled) setState((prev) => applyFetchOutcome(prev, url, { ok: true, data: normalizeListPayload<T>(data) }));
      })
      .catch((err: Error) => {
        if (!cancelled) setState((prev) => applyFetchOutcome<T[]>(prev, url, { ok: false, error: err }));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [url, enabled, tick]);

  // A new URL (other kind, namespace or selector) never shows the previous
  // list's rows or error while it loads (UX-113).
  const current = state.key === url ? state : initialFetchState<T[]>(url);
  const items = (current.data ?? NO_ITEMS) as T[];
  const hidden = useDocumentHidden();
  const inFlight = !!pollWhileInFlight && items.some((i) => isInFlight(phaseOf(i)));
  const delay = pollDelayMs({ pollIntervalMs, pollWhileInFlight, inFlight, hidden });
  useNextRefresh(delay, enabled && !loading, refresh);

  return {
    items,
    loading,
    error: current.error,
    refreshError: current.refreshError,
    loadedAt: current.loadedAt,
    refresh,
  };
}

/** Fetch a single Hybrid Sovereign CR by name */
export function useK8sResource<T extends K8sResource>(
  kind: HybridSovereignKind,
  name: string,
  options: UseK8sResourceOptions = {},
): UseK8sResourceResult<T> {
  const { namespace, pollIntervalMs, pollWhileInFlight, enabled = true } = options;
  const [loading, setLoading] = useState(enabled);
  const [tick, setTick] = useState(0);

  const url = useMemo(() => buildResourceUrl(kind, name, namespace), [kind, name, namespace]);
  const [state, setState] = useState<FetchState<T>>(() => initialFetchState<T>(url));

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled || !name) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);

    k8sFetchJson<T>(url, undefined, { kind, verb: 'get', namespace, name })
      .then((data) => {
        if (!cancelled) setState((prev) => applyFetchOutcome(prev, url, { ok: true, data }));
      })
      .catch((err: Error) => {
        if (!cancelled) setState((prev) => applyFetchOutcome<T>(prev, url, { ok: false, error: err }));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [url, enabled, name, tick]);

  const current = state.key === url ? state : initialFetchState<T>(url);
  const resource = current.data ?? null;
  const hidden = useDocumentHidden();
  const inFlight = !!pollWhileInFlight && !!resource && isInFlight(phaseOf(resource));
  const delay = pollDelayMs({ pollIntervalMs, pollWhileInFlight, inFlight, hidden });
  useNextRefresh(delay, enabled && !!name && !loading, refresh);

  return {
    resource,
    loading,
    error: current.error,
    refreshError: current.refreshError,
    loadedAt: current.loadedAt,
    refresh,
  };
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
