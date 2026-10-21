/**
 * Permission gate (C3): which verbs the caller holds on which kinds, per
 * namespace ("-" = cluster scope, used for All tenants).
 *
 * Dashboards ask their server's `/api/permissions` (one bounded batch of
 * SelfSubjectAccessReviews on the caller's token); the console runs the same
 * reviews in parallel through `/api/kubernetes`. Answers are cached for 30 s
 * and dropped on demand (window focus, any 403). The gate only hides or
 * disables actions; the API stays authoritative.
 */
import { getPermissionsClientStyle } from '../hooks/permissions';
import { getK8sClientConfig, k8sFetchJson } from '../hooks/k8s';
import { HybridSovereignKind, KIND_PLURALS } from '../types';
import { selfSubjectAccessReview } from './ssar';

export type GateVerb = 'get' | 'list' | 'create' | 'update' | 'patch' | 'delete';
export const GATE_VERBS: GateVerb[] = ['get', 'list', 'create', 'update', 'patch', 'delete'];

export interface PermissionMatrix {
  namespace: string;
  allowed: Record<string, Partial<Record<GateVerb, boolean>>>;
  evaluatedAt?: string;
}

export const PLATFORM_KINDS: HybridSovereignKind[] = [
  'Entity',
  'CloudInfrastructure',
  'HybridFabric',
  'CloudGateway',
  'UIHealthChecker',
];
export const PLUGIN_KINDS: HybridSovereignKind[] = ['RbacConfig', 'AAPConfig', 'QuayConfig'];
export const TENANT_KINDS: HybridSovereignKind[] = [
  'TenantProfile',
  'CloudVirt',
  'CloudOSO',
  'CloudAWS',
  'CloudGCP',
  'OpenStackMigration',
  'HybridNetwork',
  'NetworkPlacement',
  'PlatformOpenshift',
  'Team',
  'Project',
  'Assignment',
  'Vault',
  'VaultKV',
  'AAPOrg',
  'QuayOrg',
  'Rbac',
];

/** The kinds worth evaluating in a namespace. */
export function kindsForNamespace(namespace: string): HybridSovereignKind[] {
  if (namespace === 'sovereign-cloud') return PLATFORM_KINDS;
  if (namespace === 'sovereign-cloud-plugins') return PLUGIN_KINDS;
  if (namespace === '-') return [...TENANT_KINDS, ...PLATFORM_KINDS];
  return TENANT_KINDS;
}

/** Which namespace a kind is evaluated in for a scope namespace. */
export function gateNamespaceFor(kind: HybridSovereignKind, scopeNamespace: string): string {
  if (scopeNamespace === '-') return '-';
  if (PLUGIN_KINDS.includes(kind)) return 'sovereign-cloud-plugins';
  if (PLATFORM_KINDS.includes(kind)) return 'sovereign-cloud';
  return scopeNamespace;
}

const TTL_MS = 30000;
const cache = new Map<string, { at: number; promise: Promise<PermissionMatrix> }>();
const listeners = new Set<() => void>();
let version = 0;

export function permissionsVersion(): number {
  return version;
}

export function subscribePermissions(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** Drop every cached answer (after a 403, on window focus, after sign-in changes). */
export function invalidatePermissions(): void {
  cache.clear();
  version += 1;
  listeners.forEach((fn) => fn());
}

async function fetchMatrix(namespace: string, kinds: HybridSovereignKind[]): Promise<PermissionMatrix> {
  const plurals = Array.from(new Set(kinds.map((k) => KIND_PLURALS[k])));
  if (getPermissionsClientStyle() === 'ssar') {
    const pairs = plurals.flatMap((resource) => GATE_VERBS.map((verb) => ({ resource, verb })));
    const results = await Promise.all(
      pairs.map(async (p) => ({ ...p, ok: await selfSubjectAccessReview({ namespace, resource: p.resource, verb: p.verb }) })),
    );
    const allowed: PermissionMatrix['allowed'] = {};
    for (const r of results) {
      allowed[r.resource] = allowed[r.resource] ?? {};
      allowed[r.resource][r.verb] = r.ok;
    }
    return { namespace, allowed, evaluatedAt: new Date().toISOString() };
  }
  const params = new URLSearchParams({ namespace, resources: plurals.join(','), verbs: GATE_VERBS.join(',') });
  const endpoint = getK8sClientConfig().permissionsEndpoint ?? '/api/permissions';
  return k8sFetchJson<PermissionMatrix>(`${endpoint}?${params.toString()}`, undefined, {
    kind: 'permissions',
    verb: 'get',
    namespace,
  });
}

/** The cached matrix for a namespace (tenant namespace, platform namespace or "-"). */
export function getPermissionMatrix(namespace: string): Promise<PermissionMatrix> {
  const now = Date.now();
  const hit = cache.get(namespace);
  if (hit && now - hit.at < TTL_MS) return hit.promise;
  const promise = fetchMatrix(namespace, kindsForNamespace(namespace));
  cache.set(namespace, { at: now, promise });
  promise.catch(() => {
    if (cache.get(namespace)?.promise === promise) cache.delete(namespace);
  });
  return promise;
}

export function matrixAllows(m: PermissionMatrix | undefined, kind: HybridSovereignKind, verb: GateVerb): boolean | undefined {
  if (!m) return undefined;
  const v = m.allowed?.[KIND_PLURALS[kind]]?.[verb];
  return typeof v === 'boolean' ? v : undefined;
}
