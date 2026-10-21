import { useCallback, useEffect, useState } from 'react';
import {
  API_VERSION_FULL,
  HybridSovereignKind,
  KIND_PLURALS,
  K8sResource,
} from '../types';
import { getK8sClientConfig } from './k8s';
import { errorFromFetchFailure, errorFromResponse } from './errors';

export interface OverviewCRsResult {
  items: K8sResource[];
  loading: boolean;
  error: Error | null;
  /**
   * Kinds the user may not list in at least one namespace that was read. Counts
   * for these kinds are incomplete: show a lock, never a zero (UX-057).
   */
  forbiddenKinds: HybridSovereignKind[];
  refresh: () => void;
}

export interface UseOverviewCRsOptions {
  enabled?: boolean;
}

/** Platform CRs live in sovereign-cloud (Entity + networking/health). */
export const PLATFORM_NS = 'sovereign-cloud';
/** Plugin config CRs live in sovereign-cloud-plugins. */
export const PLUGINS_NS = 'sovereign-cloud-plugins';

const PLATFORM_KINDS: HybridSovereignKind[] = [
  'Entity',
  'CloudInfrastructure',
  'HybridFabric',
  'CloudGateway',
  'TransportLink',
  'UIHealthChecker',
];

const PLUGIN_KINDS: HybridSovereignKind[] = ['RbacConfig', 'AAPConfig', 'QuayConfig'];

/** Tenant CRs live in entity-* namespaces. */
const ENTITY_KINDS: HybridSovereignKind[] = [
  'Team',
  'Project',
  'Assignment',
  'Persona',
  'PlatformOpenshift',
  'CloudOSO',
  'CloudAWS',
  'CloudGCP',
  'CloudVirt',
  'Vault',
  // Tenant VRFs and their per-cloud placements. Needed by NetworkOverview to
  // draw networks across clouds; the fabric/gateway half is in PLATFORM_KINDS.
  'HybridNetwork',
  'NetworkPlacement',
];

const RETRY_ATTEMPTS = 4;
const RETRY_BASE_MS = 400;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function entityNamespaceFrom(item: K8sResource): string {
  const fromStatus = item.status?.entity;
  if (typeof fromStatus === 'string' && fromStatus.length > 0) {
    return fromStatus.startsWith('entity-') ? fromStatus : `entity-${fromStatus}`;
  }
  return `entity-${item.metadata.name}`;
}

interface KindList {
  kind: HybridSovereignKind;
  items: K8sResource[];
  /** 403: the user may not list this kind here. */
  forbidden: boolean;
}

/** Thrown inside withRetry for answers that will not change on a retry. */
class FinalAnswer extends Error {
  constructor(readonly result: KindList) {
    super('final');
  }
}

async function listKindInNamespace(
  kind: HybridSovereignKind,
  namespace: string,
  fetchFn: typeof fetch,
  baseUrl: string,
  headers: Record<string, string>,
): Promise<KindList> {
  const plural = KIND_PLURALS[kind];
  const url = `${baseUrl}/apis/${API_VERSION_FULL}/namespaces/${encodeURIComponent(namespace)}/${plural}`;
  let r: Response;
  try {
    r = await fetchFn(url, { headers });
  } catch (err) {
    // consoleFetch throws non-2xx answers; 403 and 404 mean the same as below.
    const error = errorFromFetchFailure(err, { kind, verb: 'list', namespace });
    if (error.status === 403) throw new FinalAnswer({ kind, items: [], forbidden: true });
    if (error.status === 404) throw new FinalAnswer({ kind, items: [], forbidden: false });
    throw error;
  }
  if (!r.ok) {
    // 403 is an access limit (reported, never shown as zero); 404 is a kind
    // that is not installed or a namespace that is gone: nothing to count.
    if (r.status === 403) throw new FinalAnswer({ kind, items: [], forbidden: true });
    if (r.status === 404) throw new FinalAnswer({ kind, items: [], forbidden: false });
    throw await errorFromResponse(r, { kind, verb: 'list', namespace });
  }
  const data = (await r.json()) as { items?: K8sResource[] };
  return {
    kind,
    items: (data.items ?? []).map((item) => ({ ...item, kind: item.kind || kind })),
    forbidden: false,
  };
}

/** listKindInNamespace with retries for transient failures only. */
async function listKind(
  kind: HybridSovereignKind,
  namespace: string,
  fetchFn: typeof fetch,
  baseUrl: string,
  headers: Record<string, string>,
): Promise<KindList> {
  try {
    return await withRetry(() => listKindInNamespace(kind, namespace, fetchFn, baseUrl, headers));
  } catch (err) {
    if (err instanceof FinalAnswer) return err.result;
    throw err;
  }
}

async function withRetry<T>(fn: () => Promise<T>): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < RETRY_ATTEMPTS; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
      if (err instanceof FinalAnswer) throw err;
      lastErr = err;
      if (attempt < RETRY_ATTEMPTS - 1) {
        await sleep(RETRY_BASE_MS * 2 ** attempt);
      }
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

/**
 * List Overview CRs via namespaced paths only.
 *
 * OpenShift console's `/api/kubernetes` proxy returns 404 for cluster-scoped
 * list URLs on namespaced CRDs (`/apis/.../teams`). That showed as zero KPIs
 * and "Unable to load topology" on first login.
 */
export interface OverviewFetchResult {
  items: K8sResource[];
  forbiddenKinds: HybridSovereignKind[];
}

export async function fetchOverviewCRs(
  fetchFn: typeof fetch,
  baseUrl: string,
  headers: Record<string, string>,
): Promise<OverviewFetchResult> {
  const entityList = await listKind('Entity', PLATFORM_NS, fetchFn, baseUrl, headers);
  const entities = entityList.items;

  const entityNamespaces = Array.from(
    new Set(entities.map(entityNamespaceFrom).filter(Boolean)),
  );

  // A transient failure of one list is not fatal for the overview; it is
  // counted as unreadable so the counters show a lock instead of a zero.
  const safe = (kind: HybridSovereignKind, ns: string) =>
    listKind(kind, ns, fetchFn, baseUrl, headers).catch(
      (): KindList => ({ kind, items: [], forbidden: true }),
    );

  const platformLists = PLATFORM_KINDS.filter((k) => k !== 'Entity').map((kind) =>
    safe(kind, PLATFORM_NS),
  );
  const pluginLists = PLUGIN_KINDS.map((kind) => safe(kind, PLUGINS_NS));
  const tenantLists = entityNamespaces.flatMap((ns) => ENTITY_KINDS.map((kind) => safe(kind, ns)));

  const lists = [entityList, ...(await Promise.all([...platformLists, ...pluginLists, ...tenantLists]))];
  const forbiddenKinds = Array.from(new Set(lists.filter((l) => l.forbidden).map((l) => l.kind)));
  return { items: lists.flatMap((l) => l.items), forbiddenKinds };
}

/**
 * Aggregated CR fetch for the admin overviews: namespaced lists with the
 * caller's token through the host's raw API proxy (never cluster-scoped
 * plurals, never a service-account aggregator).
 */
export function useOverviewCRs(
  pollIntervalMs = 0,
  options: UseOverviewCRsOptions = {},
): OverviewCRsResult {
  const { enabled = true } = options;
  const [items, setItems] = useState<K8sResource[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
  const [forbiddenKinds, setForbiddenKinds] = useState<HybridSovereignKind[]>([]);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    const cfg = getK8sClientConfig();
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
    const fetchFn = cfg.fetchFn ?? fetch;

    const base = cfg.baseUrl ?? '/api/kubernetes';
    fetchOverviewCRs(fetchFn, base, headers)
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setForbiddenKinds(data.forbiddenKinds);
        setError(null);
      })
      .catch((err: Error) => {
        if (cancelled) return;
        setItems([]);
        setForbiddenKinds([]);
        setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [tick, enabled]);

  useEffect(() => {
    if (!pollIntervalMs || !enabled) return;
    const id = setInterval(refresh, pollIntervalMs);
    return () => clearInterval(id);
  }, [pollIntervalMs, refresh, enabled]);

  return { items, loading, error, forbiddenKinds, refresh };
}
