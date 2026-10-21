import React, { useEffect, useMemo, useState } from 'react';
import { setForbiddenListener } from '../hooks/k8s';
import { useOptionalScope } from '../scope/ScopeProvider';
import type { HybridSovereignKind } from '../types';
import { KIND_PLURALS } from '../types';
import {
  gateNamespaceFor,
  getPermissionMatrix,
  invalidatePermissions,
  matrixAllows,
  permissionsVersion,
  subscribePermissions,
  type GateVerb,
  type PermissionMatrix,
} from './permissionGate';
import { hintRoles } from './roleHints';
import type { TenantRoleKey } from './roleHints.generated';

const FOCUS_REFRESH_MIN_MS = 10000;
const FORBIDDEN_REFRESH_MIN_MS = 5000;

/**
 * Keeps the permission gate fresh: answers are dropped when the window gets
 * focus again and after any 403 (both throttled), so a role granted or removed
 * meanwhile shows without a reload.
 */
export function PermissionProvider({ children }: { children?: React.ReactNode }): React.ReactElement {
  useEffect(() => {
    let lastFocus = Date.now();
    let lastForbidden = 0;
    const onFocus = () => {
      const now = Date.now();
      if (now - lastFocus < FOCUS_REFRESH_MIN_MS) return;
      lastFocus = now;
      invalidatePermissions();
    };
    window.addEventListener('focus', onFocus);
    setForbiddenListener(() => {
      const now = Date.now();
      if (now - lastForbidden < FORBIDDEN_REFRESH_MIN_MS) return;
      lastForbidden = now;
      // Defer: the failing request's own render must not race the refresh.
      setTimeout(invalidatePermissions, 0);
    });
    return () => {
      window.removeEventListener('focus', onFocus);
      setForbiddenListener(null);
    };
  }, []);
  return <>{children}</>;
}

/** The gate namespace for the current scope: tenant namespace, "-" for All tenants, the platform namespace. */
export function useScopeGateNamespace(): string | undefined {
  const ctx = useOptionalScope();
  if (!ctx) return undefined;
  const s = ctx.scope;
  if (s.kind === 'tenant') return s.tenant.namespace;
  if (s.kind === 'all') return '-';
  if (s.kind === 'global') return 'sovereign-cloud';
  return undefined;
}

export interface PermissionReason {
  roles: TenantRoleKey[];
  contacts?: { name?: string; email?: string; url?: string }[];
  tenantLabel?: string;
}

export interface UsePermissionResult {
  allowed: boolean;
  loading: boolean;
  /** Set when denied: the roles that would grant it and the tenant contacts. */
  reason?: PermissionReason;
}

function useMatrix(namespace: string | undefined): { matrix?: PermissionMatrix; loading: boolean } {
  const [v, setV] = useState(permissionsVersion());
  const [state, setState] = useState<{ ns?: string; matrix?: PermissionMatrix; loading: boolean }>({ loading: !!namespace });
  useEffect(() => subscribePermissions(() => setV(permissionsVersion())), []);
  useEffect(() => {
    if (!namespace) {
      setState({ loading: false });
      return;
    }
    let cancelled = false;
    setState((s) => ({ ns: namespace, matrix: s.ns === namespace ? s.matrix : undefined, loading: true }));
    getPermissionMatrix(namespace)
      .then((matrix) => {
        if (!cancelled) setState({ ns: namespace, matrix, loading: false });
      })
      .catch(() => {
        if (!cancelled) setState({ ns: namespace, matrix: undefined, loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [namespace, v]);
  return { matrix: state.ns === namespace ? state.matrix : undefined, loading: state.loading };
}

/**
 * May the caller `verb` objects of `kind` in the current scope (or in
 * `options.namespace`)? Denied while loading; fails closed.
 */
export function usePermission(
  kind: HybridSovereignKind,
  verb: GateVerb,
  options: { namespace?: string } = {},
): UsePermissionResult {
  const ctx = useOptionalScope();
  const scopeNs = useScopeGateNamespace();
  const base = options.namespace ?? scopeNs;
  const ns = base ? gateNamespaceFor(kind, base) : undefined;
  const { matrix, loading } = useMatrix(ns);
  const allowed = matrixAllows(matrix, kind, verb) ?? false;
  const reason = useMemo<PermissionReason | undefined>(() => {
    if (allowed || loading) return undefined;
    const tenant = ctx?.scope.kind === 'tenant' ? ctx.scope.tenant : undefined;
    return {
      roles: hintRoles(KIND_PLURALS[kind], verb),
      contacts: tenant?.contacts,
      tenantLabel: tenant ? tenant.displayName || tenant.name : undefined,
    };
  }, [allowed, loading, ctx, kind, verb]);
  return { allowed, loading, reason };
}

/** Several verbs of one kind at once (one matrix fetch). */
export function usePermissionsFor(
  kind: HybridSovereignKind,
  options: { namespace?: string } = {},
): { can: (verb: GateVerb) => boolean; loading: boolean; known: boolean } {
  const scopeNs = useScopeGateNamespace();
  const base = options.namespace ?? scopeNs;
  const ns = base ? gateNamespaceFor(kind, base) : undefined;
  const { matrix, loading } = useMatrix(ns);
  return {
    can: (verb: GateVerb) => matrixAllows(matrix, kind, verb) ?? false,
    loading,
    known: !!matrix,
  };
}
