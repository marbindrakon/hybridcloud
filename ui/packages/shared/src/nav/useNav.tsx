import React, { createContext, useContext, useEffect, useMemo } from 'react';
import { useLocation } from 'react-router-dom';
import { EmptyState, EmptyStateActions, EmptyStateBody, EmptyStateFooter, Button } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { useOptionalScope } from '../scope/ScopeProvider';
import { tenantLabel } from '../scope/resolveIdentity';
import type { Scope } from '../scope/types';
import { gateNamespaceFor, matrixAllows } from '../access/permissionGate';
import { useGateMatrix, useScopeGateNamespace } from '../access/PermissionProvider';
import { useSharedKinds } from '../access/sharedWithYou';
import { entriesForHost, entryById, NAV_SECTIONS } from './registry';
import { createPathFor, pathFor, routeFor as routeForPath, type PathParams } from './paths';
import type { Host, NavEntry, ScopeMode, SectionId } from './types';

const NavHostContext = createContext<Host | null>(null);

/** Tells shared components which host renders them (links, routes, titles). */
export function NavHostProvider({ host, children }: { host: Host; children?: React.ReactNode }): React.ReactElement {
  return <NavHostContext.Provider value={host}>{children}</NavHostContext.Provider>;
}

export function useNavHost(): Host | null {
  return useContext(NavHostContext);
}

function scopeModeOf(scope: Scope | undefined): ScopeMode | null {
  if (!scope) return null;
  if (scope.kind === 'tenant') return 'tenant';
  if (scope.kind === 'all') return 'all';
  if (scope.kind === 'global') return 'global';
  return null;
}

export interface NavItemView {
  entry: NavEntry;
  href: string;
  active: boolean;
}

export interface NavGroupView {
  section: SectionId;
  labelKey: string;
  items: NavItemView[];
}

/**
 * Registry entries visible on this host for the current user and scope:
 * persona and scope mode match, `enabledWhen` holds, and at least one of
 * `requiredVerbs` is allowed by the permission gate. Sections with no visible
 * entry do not render. Tenant entries are listed in tenant and All tenants
 * scope; platform entries need a platform administrator.
 */
export function useNav(hostArg?: Host): {
  host: Host;
  groups: NavGroupView[];
  pathFor: (entryId: string, params?: PathParams) => string;
  routeFor: (kind: string, name?: string) => string | undefined;
  createPathFor: (form: string) => string | undefined;
} {
  const ctxHost = useNavHost();
  const host = (hostArg ?? ctxHost ?? 'tenant-dashboard') as Host;
  const scopeCtx = useOptionalScope();
  const location = useLocation();
  const scope = scopeCtx?.scope;
  const identity = scopeCtx?.identity;
  const mode = scopeModeOf(scope);
  const gateNs = useScopeGateNamespace();
  const { matrix } = useGateMatrix(gateNs);
  // Tenant entries link into the current tenant or All tenants. In the platform scope
  // (admin hosts) they point at All tenants for platform readers.
  const tenantScopeForPaths = useMemo(() => {
    if (scope?.kind === 'tenant' || scope?.kind === 'all') return scope;
    if (scope?.kind === 'global' && identity?.platform.reader) return { kind: 'all' as const };
    return { kind: 'unset' as const };
  }, [scope, identity]);
  const tenantMode: ScopeMode | null =
    mode === 'tenant' || mode === 'all' ? mode : mode === 'global' && identity?.platform.reader ? 'all' : null;
  // A user who may not list a kind can still hold objects of it by name (team setup and app
  // project grants): their entries stay in the navigation (GT-F4). One rules review, only when
  // the matrix denies a read the navigation asks for.
  const readDenied = useMemo(
    () =>
      !!matrix &&
      !!gateNs &&
      tenantMode === 'tenant' &&
      entriesForHost(host).some((e) =>
        (e.requiredVerbs ?? []).some(
          (rv) => (rv.verb === 'list' || rv.verb === 'get') && gateNamespaceFor(rv.kind, gateNs) === gateNs && matrixAllows(matrix, rv.kind, rv.verb) === false,
        ),
      ),
    [matrix, gateNs, tenantMode, host],
  );
  const sharedKinds = useSharedKinds(gateNs, readDenied);

  const groups = useMemo<NavGroupView[]>(() => {
    const entries = entriesForHost(host).filter((e) => e.nav !== false);
    const visible = entries.filter((e) => {
      const platformEntry = e.persona.includes('platform') && !e.persona.includes('tenant');
      if (platformEntry) {
        if (e.enabledWhen === 'platformAdmin' && !identity?.platform.admin) return false;
        if (e.enabledWhen === 'platformReader' && !identity?.platform.reader) return false;
        return true;
      }
      if (!tenantMode || !e.scopes.includes(tenantMode)) return false;
      if (mode === 'global') return true; // platform reader looking at All tenants
      if (e.requiredVerbs?.length) {
        // Unknown (still loading) counts as visible; a known denial hides the entry.
        if (!matrix) return true;
        return e.requiredVerbs.some((rv) => {
          if (!gateNs) return false;
          const ns = gateNamespaceFor(rv.kind, gateNs);
          if (ns !== gateNs) return true;
          if (matrixAllows(matrix, rv.kind, rv.verb) !== false) return true;
          return (rv.verb === 'list' || rv.verb === 'get') && !!sharedKinds?.has(rv.kind);
        });
      }
      return true;
    });
    const path = location.pathname.replace(/\/+$/, '') || '/';
    const out: NavGroupView[] = [];
    for (const section of NAV_SECTIONS) {
      const items = visible
        .filter((e) => e.section === section.id)
        .map((entry) => {
          const platformEntry = entry.persona.includes('platform') && !entry.persona.includes('tenant');
          const href = pathFor(host, entry, platformEntry ? { kind: 'global' } : tenantScopeForPaths);
          const clean = href.replace(/\/+$/, '') || '/';
          const isRoot = entry.route === '';
          const active = isRoot ? path === clean : path === clean || path.startsWith(`${clean}/`);
          return { entry, href, active };
        });
      // Only the most specific active entry is active (networks vs networks/map).
      const best = items.filter((i) => i.active).sort((a, b) => b.href.length - a.href.length)[0];
      items.forEach((i) => {
        i.active = i === best;
      });
      if (items.length) out.push({ section: section.id, labelKey: section.labelKey, items });
    }
    return out;
  }, [host, identity, mode, tenantMode, matrix, gateNs, sharedKinds, location.pathname, tenantScopeForPaths]);

  return {
    host,
    groups,
    pathFor: (entryId: string, params?: PathParams) => {
      const e = entryById(entryId);
      const platformEntry = !!e && e.persona.includes('platform') && !e.persona.includes('tenant');
      return pathFor(host, entryId, platformEntry ? { kind: 'global' } : tenantScopeForPaths, params);
    },
    routeFor: (kind: string, name?: string) => routeForPath(host, kind, tenantScopeForPaths, name),
    createPathFor: (form: string) => createPathFor(host, form, tenantScopeForPaths),
  };
}

/**
 * `routeFor` for components that may render outside a shell (no provider):
 * undefined means "render the name as text".
 */
export function useRouteFor(): (kind: string, name?: string) => string | undefined {
  const host = useNavHost();
  const scopeCtx = useOptionalScope();
  return (kind: string, name?: string) => {
    if (!host || !scopeCtx || scopeCtx.scope.kind === 'none') return undefined;
    return routeForPath(host, kind, scopeCtx.scope, name);
  };
}

export interface Crumb {
  label: string;
  to?: string;
}

function scopeCrumbLabel(t: (k: string) => string, scope: Scope | undefined, platformEntry: boolean): string | undefined {
  if (platformEntry || scope?.kind === 'global') return t('scope.platform');
  if (scope?.kind === 'all') return t('scope.allTenants');
  if (scope?.kind === 'tenant') return tenantLabel(scope.tenant);
  return undefined;
}

/** Sovereign Cloud > tenant (or All tenants, Platform) > section > page > object. */
export function useBreadcrumbs(entryId: string, objectName?: string): Crumb[] {
  const { t } = useTranslation();
  const nav = useNav();
  const scopeCtx = useOptionalScope();
  const entry = entryById(entryId);
  if (!entry) return [{ label: t('nav.sovereignCloud') }];
  const platformEntry = entry.persona.includes('platform') && !entry.persona.includes('tenant');
  const crumbs: Crumb[] = [{ label: t('nav.sovereignCloud') }];
  const scopeLabel = scopeCrumbLabel(t, scopeCtx?.scope, platformEntry);
  const home = platformEntry ? 'platform-overview' : 'home';
  if (scopeLabel) crumbs.push({ label: scopeLabel, to: nav.pathFor(home) });
  const section = NAV_SECTIONS.find((s) => s.id === entry.section);
  if (section && section.id !== 'home' && section.id !== 'platform') crumbs.push({ label: t(section.labelKey) });
  if (entry.route !== '' || objectName) {
    crumbs.push({ label: t(entry.labelKey), to: objectName ? nav.pathFor(entry.id) : undefined });
  }
  if (objectName) crumbs.push({ label: objectName });
  return crumbs;
}

/** "<Page> · <tenant display name> · Sovereign Cloud" (UX-114). */
export function useDocumentTitle(entryId: string, objectName?: string): string {
  const { t } = useTranslation();
  const scopeCtx = useOptionalScope();
  const entry = entryById(entryId);
  const platformEntry = !!entry && entry.persona.includes('platform') && !entry.persona.includes('tenant');
  const page = objectName ?? (entry ? t(entry.labelKey) : '');
  const scope = scopeCrumbLabel(t, scopeCtx?.scope, platformEntry);
  const title = [page, scope, t('nav.sovereignCloud')].filter(Boolean).join(' · ');
  useEffect(() => {
    if (typeof document !== 'undefined' && !(window as { SERVER_FLAGS?: unknown }).SERVER_FLAGS) document.title = title;
  }, [title]);
  return title;
}

/** Catch-all route: a clear not-found page with a way home. */
export function NotFoundPage({ homeHref, onHome }: { homeHref?: string; onHome?: () => void }): React.ReactElement {
  const { t } = useTranslation();
  useEffect(() => {
    if (typeof document !== 'undefined' && !(window as { SERVER_FLAGS?: unknown }).SERVER_FLAGS) {
      document.title = [t('common.pageNotFound'), t('nav.sovereignCloud')].join(' · ');
    }
  }, [t]);
  return (
    <EmptyState titleText={t('common.pageNotFound')} headingLevel="h1">
      <EmptyStateBody>{t('shell.notFoundBody')}</EmptyStateBody>
      {(homeHref || onHome) && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button variant="primary" component={homeHref && !onHome ? 'a' : undefined} href={onHome ? undefined : homeHref} onClick={onHome}>
              {t('shell.goHome')}
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
}
