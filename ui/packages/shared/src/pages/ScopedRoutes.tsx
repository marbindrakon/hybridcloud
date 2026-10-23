import React from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { Card, CardBody, CardTitle, Gallery } from '@patternfly/react-core';
import { Link } from 'react-router-dom';
import { useTranslation } from '../i18n';
import { PageHeader } from '../components/PageHeader';
import { entriesForHost } from '../nav/registry';
import { legacyTarget, matchLegacy, pathFor } from '../nav/paths';
import { NotFoundPage, useBreadcrumbs, useDocumentTitle, useNav, useNavHost } from '../nav/useNav';
import type { Host, NavEntry, PageId } from '../nav/types';
import { landingScope, useOptionalScope } from '../scope/ScopeProvider';
import { ScopeGate } from '../scope/NoScopeStates';
import { tenantLabel } from '../scope/resolveIdentity';
import { ScopedListPage } from './ScopedListPage';
import { ScopedDetailPage } from './ScopedDetailPage';
import { ScopedCreatePage } from './ScopedCreatePage';
import { isPlatformEntry } from './usePageEntry';

/** Host-specific page components; list, detail and create default to the shared ones. */
export type PageMap = Partial<Record<PageId, React.ComponentType<{ entry: NavEntry }>>>;

function DefaultPage({ entry, pages, detail }: { entry: NavEntry; pages: PageMap; detail?: boolean }): React.ReactElement {
  if (detail) {
    const D = pages.detail;
    return D ? <D entry={entry} /> : <ScopedDetailPage entryId={entry.id} />;
  }
  const P = pages[entry.page];
  if (P) return <P entry={entry} />;
  if (entry.page === 'list') return <ScopedListPage entryId={entry.id} />;
  if (entry.page === 'create') return <ScopedCreatePage persona={isPlatformEntry(entry) ? 'platform' : 'tenant'} />;
  return <NotFoundPage />;
}

/**
 * Routes of one persona below a scope prefix (/t/<tenant>, /all or /platform),
 * generated from the registry; unknown paths get the not-found page.
 */
export function ScopedRoutes({ host, persona, pages }: { host: Host; persona: 'tenant' | 'platform'; pages: PageMap }): React.ReactElement {
  const entries = entriesForHost(host).filter((e) => (persona === 'platform' ? isPlatformEntry(e) : !isPlatformEntry(e)));
  const nav = useNav(host);
  const home = nav.pathFor(persona === 'platform' ? 'platform-overview' : 'home');
  return (
    <ScopeGate>
      <Routes>
        {entries.map((e) =>
          e.route === '' ? (
            <Route key={e.id} index element={<DefaultPage entry={e} pages={pages} />} />
          ) : (
            <Route key={e.id} path={e.route} element={<DefaultPage entry={e} pages={pages} />} />
          ),
        )}
        {entries
          .filter((e) => e.kinds?.length && !e.route.includes(':'))
          .map((e) => (
            <Route key={`${e.id}-detail`} path={`${e.route}/:name`} element={<DefaultPage entry={e} pages={pages} detail />} />
          ))}
        <Route path="*" element={<NotFoundPage homeHref={home} />} />
      </Routes>
    </ScopeGate>
  );
}

/** "/" of a dashboard: the user's tenant (or platform home), or the picker / no-role state. */
export function RootRedirect({ host, persona = 'tenant' }: { host: Host; persona?: 'tenant' | 'platform' }): React.ReactElement {
  const ctx = useOptionalScope();
  if (!ctx || ctx.loading) return <ScopeGate />;
  const target = landingScope(ctx.identity, persona);
  if (target) {
    const entry = target.kind === 'global' ? 'platform-overview' : 'home';
    return <Navigate replace to={pathFor(host, entry, target)} />;
  }
  return <ScopeGate />;
}

/** Old, unscoped bookmarks: redirect to their page in the registry, or not found. */
export function LegacyRedirect({ host }: { host: Host }): React.ReactElement {
  const location = useLocation();
  const ctx = useOptionalScope();
  const rest = location.pathname.replace(/^\/+/, '');
  const m = matchLegacy(host, rest);
  if (m && (ctx?.loading ?? false) && m.route.scope === 'current') return <ScopeGate />;
  if (m) {
    const to = legacyTarget(host, m, landingScope(ctx?.identity));
    if (to) return <Navigate replace to={`${to}${location.search}`} />;
  }
  return <NotFoundPage homeHref="/" />;
}

/** Home of the All tenants scope: the tenants the user may open. */
export function AllTenantsHome(): React.ReactElement {
  const { t } = useTranslation();
  const host = (useNavHost() ?? 'tenant-dashboard') as Host;
  const ctx = useOptionalScope();
  const crumbs = useBreadcrumbs('home');
  useDocumentTitle('home');
  return (
    <>
      <PageHeader title={t('scope.allTenants')} breadcrumbs={crumbs} />
      <Gallery hasGutter minWidths={{ default: '260px' }}>
        {(ctx?.tenants ?? []).map((tn) => (
          <Card key={tn.name} isCompact>
            <CardTitle>
              <Link to={pathFor(host, 'home', { kind: 'tenant', name: tn.name })}>{tenantLabel(tn)}</Link>
            </CardTitle>
            <CardBody className="sc-muted">{tn.namespace}</CardBody>
          </Card>
        ))}
      </Gallery>
    </>
  );
}
