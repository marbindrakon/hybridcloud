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
  TenantOverviewPage,
  TenantNetworkOverviewPage,
  IntegrationsPage,
  useRouterScopeAdapter,
  useOptionalScope,
  type PageMap,
} from '@hybridsovereign/shared';
import { OverviewPage } from './pages/OverviewPage';
import { UIHealthPage } from './pages/UIHealthPage';
import NetworkOverviewPage from './pages/NetworkOverviewPage';

const HOST = 'admin-dashboard' as const;

/** Home: the tenant overview, or the tenant list in All tenants scope. */
function TenantHome(): React.ReactElement {
  const ctx = useOptionalScope();
  return ctx?.scope.kind === 'all' ? <AllTenantsHome /> : <TenantOverviewPage />;
}

const TENANT_PAGES: PageMap = {
  'tenant-home': TenantHome,
  'network-map': TenantNetworkOverviewPage,
};

const PLATFORM_PAGES: PageMap = {
  'platform-overview': OverviewPage,
  'fabric-map': NetworkOverviewPage,
  integrations: IntegrationsPage,
  'ui-health': UIHealthPage,
};

function AdminShell(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <DashboardShell host={HOST} brand={t('nav.sovereignAdminConsole')} homeHref="/platform" includePlatform>
      <Routes>
        <Route path="/" element={<RootRedirect host={HOST} persona="platform" />} />
        <Route path="/platform/*" element={<ScopedRoutes host={HOST} persona="platform" pages={PLATFORM_PAGES} />} />
        <Route path="/t/:tenant/*" element={<ScopedRoutes host={HOST} persona="tenant" pages={TENANT_PAGES} />} />
        <Route path="/all/*" element={<ScopedRoutes host={HOST} persona="tenant" pages={TENANT_PAGES} />} />
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
        <AdminShell />
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
