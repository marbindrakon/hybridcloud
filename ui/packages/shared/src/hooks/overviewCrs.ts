import { useCallback, useEffect, useState } from 'react';
import {
  API_VERSION_FULL,
  HybridSovereignKind,
  KIND_PLURALS,
  K8sResource,
} from '../types';
import { getK8sClientConfig } from './k8s';

export interface OverviewCRsResult {
  items: K8sResource[];
  loading: boolean;
  error: Error | null;
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

async function listKindInNamespace(
  kind: HybridSovereignKind,
  namespace: string,
  fetchFn: typeof fetch,
  baseUrl: string,
  headers: Record<string, string>,
): Promise<K8sResource[]> {
  const plural = KIND_PLURALS[kind];
  const url = `${baseUrl}/apis/${API_VERSION_FULL}/namespaces/${encodeURIComponent(namespace)}/${plural}`;
  const r = await fetchFn(url, { headers });
  if (!r.ok) {
    // Missing CRD / no access / empty ns — treat as empty so Overview stays usable
    if (r.status === 404 || r.status === 403) return [];
    throw new Error(`K8s API error ${r.status}: ${r.statusText} (${kind}@${namespace})`);
  }
  const data = (await r.json()) as { items?: K8sResource[] };
  return (data.items ?? []).map((item) => ({ ...item, kind: item.kind || kind }));
}

async function withRetry<T>(fn: () => Promise<T>): Promise<T> {
  let lastErr: unknown;
  for (let attempt = 0; attempt < RETRY_ATTEMPTS; attempt += 1) {
    try {
      return await fn();
    } catch (err) {
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
export async function fetchOverviewCRs(
  fetchFn: typeof fetch,
  baseUrl: string,
  headers: Record<string, string>,
): Promise<K8sResource[]> {
  const entities = await withRetry(() =>
    listKindInNamespace('Entity', PLATFORM_NS, fetchFn, baseUrl, headers),
  );

  const entityNamespaces = Array.from(
    new Set(entities.map(entityNamespaceFrom).filter(Boolean)),
  );

  const platformLists = PLATFORM_KINDS.filter((k) => k !== 'Entity').map((kind) =>
    withRetry(() => listKindInNamespace(kind, PLATFORM_NS, fetchFn, baseUrl, headers)).catch(
      () => [] as K8sResource[],
    ),
  );

  const pluginLists = PLUGIN_KINDS.map((kind) =>
    withRetry(() => listKindInNamespace(kind, PLUGINS_NS, fetchFn, baseUrl, headers)).catch(
      () => [] as K8sResource[],
    ),
  );

  const tenantLists = entityNamespaces.flatMap((ns) =>
    ENTITY_KINDS.map((kind) =>
      withRetry(() => listKindInNamespace(kind, ns, fetchFn, baseUrl, headers)).catch(
        () => [] as K8sResource[],
      ),
    ),
  );

  const lists = await Promise.all([...platformLists, ...pluginLists, ...tenantLists]);
  return [...entities, ...lists.flat()];
}

/**
 * Aggregated CR fetch for admin Overview.
 * - dashboard style: Express `/api/overview/crs`
 * - raw / console style: namespaced lists (never cluster-scoped plurals)
 */
export function useOverviewCRs(
  pollIntervalMs = 0,
  options: UseOverviewCRsOptions = {},
): OverviewCRsResult {
  const { enabled = true } = options;
  const [items, setItems] = useState<K8sResource[]>([]);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
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

    const run = async () => {
      if (cfg.apiStyle === 'dashboard') {
        const r = await withRetry(async () => {
          const resp = await fetchFn('/api/overview/crs', { headers });
          if (!resp.ok) throw new Error(`K8s API error ${resp.status}: ${resp.statusText}`);
          return resp;
        });
        const data: unknown = await r.json();
        return Array.isArray(data) ? (data as K8sResource[]) : [];
      }

      const base = cfg.baseUrl ?? '/api/kubernetes';
      return fetchOverviewCRs(fetchFn, base, headers);
    };

    run()
      .then((data) => {
        if (cancelled) return;
        setItems(data);
        setError(null);
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
  }, [tick, enabled]);

  useEffect(() => {
    if (!pollIntervalMs || !enabled) return;
    const id = setInterval(refresh, pollIntervalMs);
    return () => clearInterval(id);
  }, [pollIntervalMs, refresh, enabled]);

  return { items, loading, error, refresh };
}
