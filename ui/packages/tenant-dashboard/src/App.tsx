import React from 'react';
import { Route, Routes } from 'react-router-dom';
import {
  SovereignThemeProvider,
  SovereignI18nProvider,
  useTranslation,
  NavHostProvider,
  ScopeProvider,
  PermissionProvider,
  DashboardShell,
  ScopedRoutes,
  RootRedirect,
  LegacyRedirect,
  AllTenantsHome,
  useRouterScopeAdapter,
  useOptionalScope,
  pathFor,
  type PageMap,
} from '@hybridsovereign/shared';
import { TenantOverviewPage } from './pages/TenantOverviewPage';
import { TenantNetworkOverviewPage } from './pages/TenantNetworkOverviewPage';

const HOST = 'tenant-dashboard' as const;

/** Home: the tenant overview, or the tenant list in All tenants scope. */
function HomePage(): React.ReactElement {
  const ctx = useOptionalScope();
  return ctx?.scope.kind === 'all' ? <AllTenantsHome /> : <TenantOverviewPage />;
}

const PAGES: PageMap = {
  'tenant-home': HomePage,
  'network-map': TenantNetworkOverviewPage,
};

function TenantShell(): React.ReactElement {
  const { t } = useTranslation();
  const ctx = useOptionalScope();
  const scope = ctx?.scope;
  const home =
    scope?.kind === 'tenant' || scope?.kind === 'all' ? pathFor(HOST, 'home', scope) : '/';
  return (
    <DashboardShell host={HOST} brand={t('nav.sovereignCloud')} homeHref={home}>
      <Routes>
        <Route path="/" element={<RootRedirect host={HOST} />} />
        <Route path="/t/:tenant/*" element={<ScopedRoutes host={HOST} persona="tenant" pages={PAGES} />} />
        <Route path="/all/*" element={<ScopedRoutes host={HOST} persona="tenant" pages={PAGES} />} />
        <Route path="*" element={<LegacyRedirect host={HOST} />} />
      </Routes>
    </DashboardShell>
  );
}

function ScopedApp(): React.ReactElement {
  const adapter = useRouterScopeAdapter();
  return (
    <ScopeProvider adapter={adapter}>
      <PermissionProvider>
        <TenantShell />
      </PermissionProvider>
    </ScopeProvider>
  );
}

export function App(): React.ReactElement {
  return (
    <SovereignI18nProvider>
      <SovereignThemeProvider>
        <NavHostProvider host={HOST}>
          <ScopedApp />
        </NavHostProvider>
      </SovereignThemeProvider>
    </SovereignI18nProvider>
  );
}
