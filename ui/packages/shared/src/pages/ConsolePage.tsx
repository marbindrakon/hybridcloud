import React, { useCallback, useEffect, useMemo } from 'react';
import { Navigate, useLocation, useNavigate } from 'react-router-dom';
import { PageSection } from '@patternfly/react-core';
import { useTranslation } from '../i18n';
import { PermissionProvider } from '../access/PermissionProvider';
import { ScopeProvider, landingScope, useOptionalScope, CONSOLE_ALL_NAMESPACES } from '../scope/ScopeProvider';
import { ScopeGate } from '../scope/NoScopeStates';
import { TenantScopeSelector } from '../scope/TenantScopeSelector';
import { namespaceOfTenant } from '../scope/parseGroups';
import type { RequestedScope, ScopeAdapter } from '../scope/types';
import { NavHostProvider, NotFoundPage } from '../nav/useNav';
import { legacyTarget, matchLegacy, parseConsolePath, pathFor, type ConsoleMatch } from '../nav/paths';
import type { Host, NavEntry, PageId } from '../nav/types';
import { ScopedListPage } from './ScopedListPage';
import { ScopedDetailPage } from './ScopedDetailPage';
import { ScopedCreatePage } from './ScopedCreatePage';
import { AllTenantsHome } from './ScopedRoutes';
import { TenantOverviewPage } from './TenantOverviewPage';
import { TenantNetworkOverviewPage } from './TenantNetworkOverviewPage';
import { isPlatformEntry } from './usePageEntry';

export type ConsolePageMap = Partial<Record<PageId, React.ComponentType>>;

export interface ConsolePageProps {
  host: Extract<Host, 'tenant-console' | 'admin-console'>;
  /** The console's active namespace pair (SDK useActiveNamespace), kept in step with the URL. */
  activeNamespace?: string;
  setActiveNamespace?: (ns: string) => void;
  /** Host pages for platform page ids (overview, fabric map, integrations, endpoint health). */
  pages?: ConsolePageMap;
}

function EntryPage({ match, pages }: { match: ConsoleMatch; pages: ConsolePageMap }): React.ReactElement {
  const ctx = useOptionalScope();
  const e: NavEntry = match.entry;
  if (match.name) return <ScopedDetailPage entryId={e.id} />;
  const P = pages[e.page];
  if (P) return <P />;
  switch (e.page) {
    case 'list':
      return <ScopedListPage entryId={e.id} />;
    case 'create':
      return <ScopedCreatePage persona={isPlatformEntry(e) ? 'platform' : 'tenant'} />;
    case 'tenant-home':
      return ctx?.scope.kind === 'all' ? <AllTenantsHome /> : <TenantOverviewPage />;
    case 'network-map':
      return <TenantNetworkOverviewPage />;
    default:
      return <NotFoundPage />;
  }
}

/** Old console paths and the bare landing paths: redirect into the registry. */
function ConsoleResolver({ host }: { host: ConsolePageProps['host'] }): React.ReactElement {
  const location = useLocation();
  const ctx = useOptionalScope();
  const rest = location.pathname.replace(/^\/hybridsovereign\/?/, '');
  const m = matchLegacy(host, rest);
  if (!m) return <NotFoundPage homeHref={host === 'admin-console' ? '/hybridsovereign/platform' : '/hybridsovereign/home'} />;
  if (m.route.scope === 'current' && (!ctx || ctx.loading)) return <ScopeGate />;
  const target = legacyTarget(host, m, landingScope(ctx?.identity));
  if (target) return <Navigate replace to={target} />;
  return <ScopeGate />;
}

/**
 * One console page for every Sovereign Cloud route of a plugin. The URL
 * carries the entry and the scope (/hybridsovereign/<route>/ns/<namespace>,
 * /all-namespaces, /platform/<route>); the console's project selector
 * ("Tenant namespace") follows it. Old paths redirect.
 */
export function ConsolePage({ host, activeNamespace, setActiveNamespace, pages = {} }: ConsolePageProps): React.ReactElement {
  const location = useLocation();
  const navigate = useNavigate();
  const { t } = useTranslation();
  const match = useMemo(() => parseConsolePath(host, location.pathname), [host, location.pathname]);
  const requestedKey = JSON.stringify(match?.requested ?? { kind: 'unset' });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const requested = useMemo<RequestedScope>(() => match?.requested ?? { kind: 'unset' }, [requestedKey]);

  // Keep the console's active namespace (project selector, namespaced nav hrefs) on the URL's tenant.
  useEffect(() => {
    if (!setActiveNamespace) return;
    if (requested.kind === 'tenant') {
      const ns = namespaceOfTenant(requested.name);
      if (activeNamespace !== ns) setActiveNamespace(ns);
    } else if (requested.kind === 'all' && activeNamespace !== CONSOLE_ALL_NAMESPACES) {
      setActiveNamespace(CONSOLE_ALL_NAMESPACES);
    }
  }, [requested, activeNamespace, setActiveNamespace]);

  const go = useCallback(
    (next: RequestedScope) => {
      if (!match) return;
      const sameKind = next.kind !== 'global' && !isPlatformEntry(match.entry);
      const entry = sameKind ? match.entry : next.kind === 'global' ? 'platform-overview' : 'home';
      navigate(pathFor(host, entry, next, sameKind && match.formType ? { formType: match.formType } : {}));
    },
    [host, match, navigate],
  );
  const adapter = useMemo<ScopeAdapter>(() => ({ requested, go }), [requested, go]);

  return (
    <NavHostProvider host={host}>
      <ScopeProvider adapter={adapter}>
        <PermissionProvider>
          <PageSection className="sc-console-page">
            <div className="sc-page">
              {match && !isPlatformEntry(match.entry) && (
                <div className="sc-console-scope" aria-label={t('scope.tenant')}>
                  <TenantScopeSelector showNamespace />
                </div>
              )}
              {match ? (
                match.requested.kind === 'unset' ? (
                  <UnsetScope host={host} match={match} />
                ) : (
                  <ScopeGate>
                    <EntryPage match={match} pages={pages} />
                  </ScopeGate>
                )
              ) : (
                <ConsoleResolver host={host} />
              )}
            </div>
          </PageSection>
        </PermissionProvider>
      </ScopeProvider>
    </NavHostProvider>
  );
}

/** A tenant page opened with a namespace that is not a tenant: go to the user's tenant. */
function UnsetScope({ host, match }: { host: ConsolePageProps['host']; match: ConsoleMatch }): React.ReactElement {
  const ctx = useOptionalScope();
  if (!ctx || ctx.loading) return <ScopeGate />;
  const target = landingScope(ctx.identity);
  if (target && target.kind !== 'global') {
    return <Navigate replace to={pathFor(host, match.entry, target, match.formType ? { formType: match.formType } : {})} />;
  }
  return <ScopeGate />;
}
