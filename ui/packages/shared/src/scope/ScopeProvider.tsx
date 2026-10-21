import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { resolveIdentity } from './resolveIdentity';
import { isEntityName, namespaceOfTenant, tenantOfNamespace } from './parseGroups';
import type { Identity, RequestedScope, Scope, ScopeAdapter, TenantRef } from './types';

const PREF_KEY = 'sovereign-ui-tenant';
const RECENT_KEY = 'sovereign-ui-tenant-recent';
const IDENTITY_TTL_MS = 5 * 60 * 1000;

function readStorage(key: string): string | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage.getItem(key);
  } catch {
    return null;
  }
}
function writeStorage(key: string, value: string): void {
  try {
    if (typeof window !== 'undefined') window.localStorage.setItem(key, value);
  } catch {
    /* private mode: preferences are a convenience only */
  }
}

/** Remembered tenant: only a preference among the tenants the user may open. */
export function preferredTenant(tenants: TenantRef[]): TenantRef | undefined {
  const pref = readStorage(PREF_KEY);
  return pref ? tenants.find((t) => t.name === pref) : undefined;
}

export function recentTenants(tenants: TenantRef[]): TenantRef[] {
  let names: string[] = [];
  try {
    names = JSON.parse(readStorage(RECENT_KEY) || '[]');
  } catch {
    names = [];
  }
  return names.map((n) => tenants.find((t) => t.name === n)).filter((t): t is TenantRef => !!t);
}

function rememberTenant(name: string): void {
  writeStorage(PREF_KEY, name);
  let names: string[] = [];
  try {
    names = JSON.parse(readStorage(RECENT_KEY) || '[]');
  } catch {
    names = [];
  }
  writeStorage(RECENT_KEY, JSON.stringify([name, ...names.filter((n) => n !== name)].slice(0, 5)));
}

// One identity resolution per page load (console pages each mount a provider).
let identityCache: { at: number; promise: Promise<Identity> } | null = null;

export function loadIdentity(force = false): Promise<Identity> {
  const now = Date.now();
  if (!force && identityCache && now - identityCache.at < IDENTITY_TTL_MS) return identityCache.promise;
  const promise = resolveIdentity();
  identityCache = { at: now, promise };
  promise.catch(() => {
    if (identityCache?.promise === promise) identityCache = null;
  });
  return promise;
}

/** Scope from what was requested and who the user is. */
export function computeScope(identity: Identity | undefined, loading: boolean, requested: RequestedScope): Scope {
  if (!identity) return { kind: 'none', reason: loading ? 'loading' : 'no-role' };
  switch (requested.kind) {
    case 'tenant': {
      const tenant = identity.tenants.find((t) => t.name === requested.name);
      return tenant ? { kind: 'tenant', tenant } : { kind: 'none', reason: 'no-access', requested: requested.name };
    }
    case 'all':
      return identity.platform.reader ? { kind: 'all' } : { kind: 'none', reason: 'no-access', requested: 'all' };
    case 'global':
      return identity.platform.admin ? { kind: 'global' } : { kind: 'none', reason: 'no-access', requested: 'platform' };
    default:
      if (!identity.tenants.length && !identity.platform.reader && !identity.platform.admin) {
        return { kind: 'none', reason: 'no-role' };
      }
      return { kind: 'none', reason: 'unselected' };
  }
}

/**
 * Where a user without a scope in the URL lands: their only tenant, the
 * remembered one, All tenants for platform readers without tenant groups, or
 * nothing (the host shows the tenant picker or the no-role state).
 */
export function landingScope(identity: Identity | undefined, persona: 'tenant' | 'platform' = 'tenant'): RequestedScope | null {
  if (!identity) return null;
  if (persona === 'platform' && identity.platform.admin) return { kind: 'global' };
  const own = identity.tenants.filter((t) => t.via === 'group');
  if (own.length === 1) return { kind: 'tenant', name: own[0].name };
  const pref = preferredTenant(identity.tenants);
  if (pref) return { kind: 'tenant', name: pref.name };
  if (!own.length && identity.platform.reader) return { kind: 'all' };
  if (identity.tenants.length === 1) return { kind: 'tenant', name: identity.tenants[0].name };
  return null;
}

export interface ScopeContextValue {
  identity?: Identity;
  loading: boolean;
  error: Error | null;
  scope: Scope;
  requested: RequestedScope;
  tenants: TenantRef[];
  /** The tenant namespace of the scope; undefined for All tenants / platform. */
  namespace?: string;
  select: (target: string | 'all' | 'platform') => void;
  refresh: () => void;
}

const ScopeContext = createContext<ScopeContextValue | null>(null);

export interface ScopeProviderProps {
  adapter: ScopeAdapter;
  children?: React.ReactNode;
}

export function ScopeProvider({ adapter, children }: ScopeProviderProps): React.ReactElement {
  const [identity, setIdentity] = useState<Identity | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loadIdentity(tick > 0)
      .then((id) => {
        if (cancelled) return;
        setIdentity(id);
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
  }, [tick]);

  const { requested, go } = adapter;
  const scope = useMemo(() => computeScope(identity, loading, requested), [identity, loading, requested]);

  useEffect(() => {
    if (scope.kind === 'tenant') rememberTenant(scope.tenant.name);
  }, [scope]);

  const select = useCallback(
    (target: string) => {
      if (target === 'all') go({ kind: 'all' });
      else if (target === 'platform') go({ kind: 'global' });
      else go({ kind: 'tenant', name: target });
    },
    [go],
  );
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  const value = useMemo<ScopeContextValue>(
    () => ({
      identity,
      loading,
      error,
      scope,
      requested,
      tenants: identity?.tenants ?? [],
      namespace: scope.kind === 'tenant' ? scope.tenant.namespace : undefined,
      select,
      refresh,
    }),
    [identity, loading, error, scope, requested, select, refresh],
  );

  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

/** The scope context, or undefined when no ScopeProvider is mounted. */
export function useOptionalScope(): ScopeContextValue | undefined {
  return useContext(ScopeContext) ?? undefined;
}

function useScopeContext(): ScopeContextValue {
  const ctx = useContext(ScopeContext);
  if (!ctx) throw new Error('ScopeProvider is not mounted');
  return ctx;
}

export function useIdentity(): { identity?: Identity; loading: boolean; error: Error | null } {
  const { identity, loading, error } = useScopeContext();
  return { identity, loading, error };
}

export function useTenantScope(): {
  scope: Scope;
  tenants: TenantRef[];
  namespace?: string;
  select: (target: string | 'all' | 'platform') => void;
} {
  const { scope, tenants, namespace, select } = useScopeContext();
  return { scope, tenants, namespace, select };
}

// ---- host adapters --------------------------------------------------------

/** Parse a dashboard path: /t/<tenant>/..., /all/..., /platform/... */
export function parseScopedPath(pathname: string): { requested: RequestedScope; rest: string } {
  const parts = pathname.split('/').filter(Boolean);
  if (parts[0] === 't' && parts[1] && isEntityName(parts[1])) {
    return { requested: { kind: 'tenant', name: parts[1] }, rest: parts.slice(2).join('/') };
  }
  if (parts[0] === 'all') return { requested: { kind: 'all' }, rest: parts.slice(1).join('/') };
  if (parts[0] === 'platform') return { requested: { kind: 'global' }, rest: parts.slice(1).join('/') };
  return { requested: { kind: 'unset' }, rest: parts.join('/') };
}

/** URL prefix of a scope on the dashboards. */
export function scopePrefix(requested: RequestedScope): string {
  switch (requested.kind) {
    case 'tenant':
      return `/t/${requested.name}`;
    case 'all':
      return '/all';
    case 'global':
      return '/platform';
    default:
      return '';
  }
}

/** Sections whose pages sit one level deeper (environments/vm, clusters/teams, ...). */
const NESTED_SECTIONS = new Set(['environments', 'networks', 'clusters', 'services', 'access', 'diagnostics', 'fabric']);

/** The page part of a scoped path without an object name: "networks/core" -> "networks". */
export function pageOf(rest: string): string {
  const parts = rest.split('/').filter(Boolean);
  if (!parts.length || parts[0] === 'create') return '';
  if (NESTED_SECTIONS.has(parts[0]) && parts[1] && ['vm', 'openstack', 'aws', 'gcp', 'migrations', 'map', 'placements', 'teams', 'projects', 'assignments', 'vaults', 'kv', 'aap', 'quay', 'groups', 'ui-health'].includes(parts[1])) {
    return `${parts[0]}/${parts[1]}`;
  }
  return parts[0];
}

/**
 * Dashboard adapter: the scope lives in the URL. Switching tenant keeps the
 * page (`/t/a/networks` -> `/t/b/networks`) but drops an object name, which
 * belongs to the old tenant; switching to or from the platform scope goes to
 * that scope's home.
 */
export function useRouterScopeAdapter(): ScopeAdapter {
  const location = useLocation();
  const navigate = useNavigate();
  const parsed = useMemo(() => parseScopedPath(location.pathname), [location.pathname]);
  const requestedKey = JSON.stringify(parsed.requested);
  // Stable object identity per requested scope (avoids recomputing the scope on every navigation).
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const requested = useMemo(() => parsed.requested, [requestedKey]);
  const go = useCallback(
    (next: RequestedScope) => {
      const samePersona = (next.kind === 'global') === (parsed.requested.kind === 'global');
      const tail = samePersona && parsed.requested.kind !== 'unset' ? pageOf(parsed.rest) : '';
      navigate(`${scopePrefix(next)}${tail ? `/${tail}` : ''}` || '/');
    },
    [navigate, parsed],
  );
  return useMemo(() => ({ requested, go }), [requested, go]);
}

/** The console's "all namespaces" value of useActiveNamespace. */
export const CONSOLE_ALL_NAMESPACES = '#ALL_NS#';

/**
 * Console adapter: the scope is the console's active namespace (the project
 * selector, labelled "Tenant namespace"). The plugin passes the SDK's
 * useActiveNamespace() pair so shared code does not import the SDK.
 */
export function useConsoleScopeAdapter(
  activeNamespace: string | undefined,
  setActiveNamespace: (ns: string) => void,
  persona: 'tenant' | 'platform' = 'tenant',
): ScopeAdapter {
  const requested = useMemo<RequestedScope>(() => {
    if (persona === 'platform') return { kind: 'global' };
    if (!activeNamespace) return { kind: 'unset' };
    if (activeNamespace === CONSOLE_ALL_NAMESPACES) return { kind: 'all' };
    const t = tenantOfNamespace(activeNamespace);
    return t ? { kind: 'tenant', name: t } : { kind: 'unset' };
  }, [activeNamespace, persona]);
  const go = useCallback(
    (next: RequestedScope) => {
      if (next.kind === 'tenant') setActiveNamespace(namespaceOfTenant(next.name));
      else if (next.kind === 'all') setActiveNamespace(CONSOLE_ALL_NAMESPACES);
    },
    [setActiveNamespace],
  );
  return useMemo(() => ({ requested, go }), [requested, go]);
}
