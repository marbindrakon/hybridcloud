import React from 'react';
import {
  Page,
  PageSidebar,
  PageSidebarBody,
  Nav,
  NavList,
  NavItem,
  PageSection,
  Button,
  EmptyState,
  EmptyStateBody,
  Title,
  Masthead,
  MastheadMain,
  MastheadBrand,
  MastheadContent,
  MastheadToggle,
  PageToggleButton,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
  ToolbarGroup,
} from '@patternfly/react-core';
import {
  MoonIcon,
  SunIcon,
  TachometerAltIcon,
  FolderOpenIcon,
  UsersIcon,
  ClusterIcon,
  TopologyIcon,
  CogIcon,
  OutlinedBellIcon,
  QuestionCircleIcon,
  UserIcon,
  ProjectDiagramIcon,
  BuildingIcon,
  BarsIcon,
  GlobeIcon,
  UserEditIcon,
  InfrastructureIcon,
  NetworkIcon,
} from '@patternfly/react-icons';
import { NavLink, Routes, Route, useLocation } from 'react-router-dom';
import {
  SovereignThemeProvider,
  SovereignI18nProvider,
  useTheme,
  useTranslation,
  LanguageToggle,
  HybridSovereignKind,
} from '@hybridsovereign/shared';
import { OverviewPage } from './pages/OverviewPage';
import { ResourceListPage } from './pages/ResourceListPage';
import { ServicesPage } from './pages/ServicesPage';
import { CreateResourcePage } from './pages/CreateResourcePage';
import { AdminResourceDetailPage } from './pages/AdminResourceDetailPage';
import { UIHealthPage } from './pages/UIHealthPage';
import NetworkOverviewPage from './pages/NetworkOverviewPage';

type NavEntry =
  | {
      type: 'link';
      path: string;
      labelKey: string;
      icon: React.ComponentType;
      end?: boolean;
      kind?: HybridSovereignKind;
    }
  | { type: 'sep'; labelKey: string };

/** Static sidebar — all links always visible; pages fetch K8s only on navigate/refresh */
const NAV: NavEntry[] = [
  { type: 'sep', labelKey: 'nav.admin' },
  { type: 'link', path: '/', labelKey: 'nav.overview', icon: TachometerAltIcon, end: true },
  { type: 'link', path: '/entities', labelKey: 'nav.entities', icon: BuildingIcon, kind: 'Entity' },
  { type: 'link', path: '/personas', labelKey: 'nav.personas', icon: UserEditIcon, kind: 'Persona' },
  { type: 'sep', labelKey: 'nav.adminNetworks' },
  {
    type: 'link',
    path: '/networking/overview',
    labelKey: 'nav.networkOverview',
    icon: NetworkIcon,
  },
  {
    type: 'link',
    path: '/networking/fabrics',
    labelKey: 'nav.hybridFabrics',
    icon: TopologyIcon,
    kind: 'HybridFabric',
  },
  {
    type: 'link',
    path: '/clouds/infrastructure',
    labelKey: 'nav.cloudInfrastructure',
    icon: InfrastructureIcon,
    kind: 'CloudInfrastructure',
  },
  {
    type: 'link',
    path: '/networking/gateways',
    labelKey: 'nav.cloudGateways',
    icon: GlobeIcon,
    kind: 'CloudGateway',
  },
  {
    type: 'link',
    path: '/networking/transport',
    labelKey: 'nav.transportLinks',
    icon: ProjectDiagramIcon,
    kind: 'TransportLink',
  },
  { type: 'sep', labelKey: 'nav.adminMonitor' },
  { type: 'link', path: '/services', labelKey: 'nav.serviceUrls', icon: GlobeIcon, kind: 'AAPConfig' },
  { type: 'link', path: '/operators', labelKey: 'nav.operators', icon: CogIcon, kind: 'RbacConfig' },
  { type: 'sep', labelKey: 'nav.team' },
  { type: 'link', path: '/teams', labelKey: 'nav.teams', icon: UsersIcon, kind: 'Team' },
  { type: 'link', path: '/projects', labelKey: 'nav.projects', icon: FolderOpenIcon, kind: 'Project' },
  {
    type: 'link',
    path: '/assignments',
    labelKey: 'nav.assignments',
    icon: ProjectDiagramIcon,
    kind: 'Assignment',
  },
  { type: 'sep', labelKey: 'nav.appPlatform' },
  {
    type: 'link',
    path: '/platforms',
    labelKey: 'nav.platformOpenshift',
    icon: ClusterIcon,
    kind: 'PlatformOpenshift',
  },
];

function ThemeToggle(): React.ReactElement {
  const { mode, toggleTheme } = useTheme();
  const { t } = useTranslation();
  return (
    <Button
      variant="plain"
      aria-label={t('common.toggleTheme')}
      onClick={toggleTheme}
      icon={mode === 'dark' ? <SunIcon /> : <MoonIcon />}
    />
  );
}

function AdminNav(): React.ReactElement {
  const location = useLocation();
  const { t } = useTranslation();
  const { mode } = useTheme();
  const groups = React.useMemo(() => {
    const result: { titleKey?: string; items: Extract<NavEntry, { type: 'link' }>[] }[] = [];
    let current: { titleKey?: string; items: Extract<NavEntry, { type: 'link' }>[] } = { items: [] };
    for (const entry of NAV) {
      if (entry.type === 'sep') {
        if (current.items.length) result.push(current);
        current = { titleKey: entry.labelKey, items: [] };
      } else {
        current.items.push(entry);
      }
    }
    if (current.items.length) result.push(current);
    return result;
  }, []);

  return (
    <Nav theme={mode} aria-label={t('nav.adminNav')}>
      {groups.map((group, gi) => (
        <NavList key={group.titleKey ?? `group-${gi}`}>
          {group.titleKey ? (
            <li className="sc-nav-separator" role="presentation">
              {t(group.titleKey)}
            </li>
          ) : null}
          {group.items.map((item) => {
            const Icon = item.icon;
            const active = item.end
              ? location.pathname === item.path
              : location.pathname === item.path || location.pathname.startsWith(`${item.path}/`);
            return (
              <NavItem key={item.path} isActive={active}>
                <NavLink to={item.path} end={item.end} className="sc-nav-link">
                  <span className="sc-nav-link__icon">
                    <Icon />
                  </span>
                  {t(item.labelKey)}
                </NavLink>
              </NavItem>
            );
          })}
        </NavList>
      ))}
    </Nav>
  );
}

function AdminLayout(): React.ReactElement {
  const [isSidebarOpen, setSidebarOpen] = React.useState(true);
  const { t } = useTranslation();
  const { mode } = useTheme();

  const header = (
    <Masthead className="sc-pf-masthead">
      <MastheadToggle>
        <PageToggleButton
          variant="plain"
          aria-label={t('common.globalNav')}
          isSidebarOpen={isSidebarOpen}
          onSidebarToggle={() => setSidebarOpen((o) => !o)}
          id="admin-nav-toggle"
        >
          <BarsIcon />
        </PageToggleButton>
      </MastheadToggle>
      <MastheadMain>
        <MastheadBrand>
          <NavLink to="/" className="sc-masthead-brand">
            <span className="sc-masthead-brand__mark" aria-hidden>
              <TopologyIcon />
            </span>
            <span>{t('nav.sovereignAdminConsole')}</span>
          </NavLink>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar id="admin-masthead-toolbar" isFullHeight isStatic>
          <ToolbarContent>
            <ToolbarGroup align={{ default: 'alignEnd' }}>
              <ToolbarItem>
                <LanguageToggle isCompact />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" aria-label={t('common.notifications')} icon={<OutlinedBellIcon />} />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" aria-label={t('common.help')} icon={<QuestionCircleIcon />} />
              </ToolbarItem>
              <ToolbarItem>
                <ThemeToggle />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" aria-label={t('common.user')} icon={<UserIcon />}>
                  {t('common.admin')}
                </Button>
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );

  const sidebar = (
    <PageSidebar theme={mode} isSidebarOpen={isSidebarOpen} id="admin-sidebar">
      <PageSidebarBody>
        <div className="sc-sidebar-title">{t('nav.sovereignAdmin')}</div>
        <AdminNav />
      </PageSidebarBody>
    </PageSidebar>
  );

  return (
    <Page className="sc-standalone-shell" header={header} sidebar={sidebar} isManagedSidebar>
      <PageSection isFilled className="sc-page-section sc-page-section--dashboard">
        <div className="sc-page">
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route
              path="/entities"
              element={
                <ResourceListPage kind="Entity" title={t('nav.entities')} listPath="/entities" createPath="/create/entity" />
              }
            />
            <Route
              path="/entities/:name"
              element={
                <AdminResourceDetailPage
                  kind="Entity"
                  title={t('nav.entities')}
                  listPath="/entities"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route path="/teams" element={<ResourceListPage kind="Team" title={t('nav.teams')} listPath="/teams" />} />
            <Route
              path="/teams/:namespace/:name"
              element={<AdminResourceDetailPage kind="Team" title={t('nav.teams')} listPath="/teams" />}
            />
            <Route
              path="/assignments"
              element={<ResourceListPage kind="Assignment" title={t('nav.assignments')} listPath="/assignments" />}
            />
            <Route
              path="/assignments/:namespace/:name"
              element={<AdminResourceDetailPage kind="Assignment" title={t('nav.assignments')} listPath="/assignments" />}
            />
            <Route
              path="/projects"
              element={<ResourceListPage kind="Project" title={t('nav.projects')} listPath="/projects" />}
            />
            <Route
              path="/projects/:namespace/:name"
              element={<AdminResourceDetailPage kind="Project" title={t('nav.projects')} listPath="/projects" />}
            />
            <Route
              path="/personas"
              element={
                <ResourceListPage
                  kind="Persona"
                  title={t('nav.personas')}
                  listPath="/personas"
                  createPath="/create/persona"
                />
              }
            />
            <Route
              path="/personas/:namespace/:name"
              element={<AdminResourceDetailPage kind="Persona" title={t('nav.personas')} listPath="/personas" />}
            />
            <Route
              path="/platforms"
              element={
                <ResourceListPage kind="PlatformOpenshift" title={t('nav.platformOpenshift')} listPath="/platforms" />
              }
            />
            <Route
              path="/platforms/:namespace/:name"
              element={
                <AdminResourceDetailPage
                  kind="PlatformOpenshift"
                  title={t('nav.platformOpenshift')}
                  listPath="/platforms"
                />
              }
            />
            <Route
              path="/clouds"
              element={
                <>
                  <ResourceListPage
                    kind="CloudInfrastructure"
                    title={t('pages.cloudInfrastructureSection')}
                    subtitle={t('pages.cloudInfrastructureSectionSubtitle')}
                    listPath="/clouds/infrastructure"
                    createPath="/create/cloudinfrastructure"
                    namespace="sovereign-cloud"
                  />
                  <div style={{ marginTop: '1.5rem' }}>
                    <ResourceListPage
                      kind="CloudOSO"
                      title={t('pages.cloudProjectsSection')}
                      subtitle={t('pages.cloudProjectsSectionSubtitle')}
                      secondaryKind="CloudAWS"
                      tertiaryKind="CloudVirt"
                      listPath="/clouds/cloudoso"
                      secondaryListPath="/clouds/cloudaws"
                      tertiaryListPath="/clouds/cloudvirt"
                    />
                  </div>
                </>
              }
            />
            <Route
              path="/clouds/infrastructure"
              element={
                <ResourceListPage
                  kind="CloudInfrastructure"
                  title={t('nav.cloudInfrastructure')}
                  subtitle={t('pages.cloudInfrastructureSectionSubtitle')}
                  listPath="/clouds/infrastructure"
                  createPath="/create/cloudinfrastructure"
                  namespace="sovereign-cloud"
                />
              }
            />
            <Route
              path="/clouds/infrastructure/:name"
              element={
                <AdminResourceDetailPage
                  kind="CloudInfrastructure"
                  title={t('nav.cloudInfrastructure')}
                  listPath="/clouds/infrastructure"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route
              path="/clouds/cloudoso/:namespace/:name"
              element={
                <AdminResourceDetailPage kind="CloudOSO" title={t('nav.cloudEnvironments')} listPath="/clouds" />
              }
            />
            <Route
              path="/clouds/cloudaws/:namespace/:name"
              element={
                <AdminResourceDetailPage kind="CloudAWS" title={t('nav.cloudEnvironments')} listPath="/clouds" />
              }
            />
            <Route
              path="/clouds/cloudvirt/:namespace/:name"
              element={
                <AdminResourceDetailPage kind="CloudVirt" title={t('nav.cloudEnvironments')} listPath="/clouds" />
              }
            />
            <Route
              path="/operators"
              element={
                <ResourceListPage
                  kind="Rbac"
                  title={t('nav.operators')}
                  subtitle={t('pages.operatorsSubtitle')}
                  secondaryKind="RbacConfig"
                  listPath="/operators/rbacs"
                  secondaryListPath="/operators/rbacconfigs"
                />
              }
            />
            <Route
              path="/operators/rbacs/:namespace/:name"
              element={<AdminResourceDetailPage kind="Rbac" title={t('nav.operators')} listPath="/operators" />}
            />
            <Route
              path="/operators/rbacconfigs/:namespace/:name"
              element={
                <AdminResourceDetailPage kind="RbacConfig" title={t('nav.operators')} listPath="/operators" />
              }
            />
            <Route path="/services" element={<ServicesPage />} />
            <Route
              path="/services/aaporgs/:namespace/:name"
              element={<AdminResourceDetailPage kind="AAPOrg" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route
              path="/services/aapconfigs/:namespace/:name"
              element={<AdminResourceDetailPage kind="AAPConfig" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route
              path="/services/quayorgs/:namespace/:name"
              element={<AdminResourceDetailPage kind="QuayOrg" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route
              path="/services/quayconfigs/:namespace/:name"
              element={<AdminResourceDetailPage kind="QuayConfig" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route
              path="/services/vaults/:namespace/:name"
              element={<AdminResourceDetailPage kind="Vault" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route
              path="/services/vaultkvs/:namespace/:name"
              element={<AdminResourceDetailPage kind="VaultKV" title={t('nav.serviceUrls')} listPath="/services" />}
            />
            <Route path="/networking/overview" element={<NetworkOverviewPage />} />
            <Route
              path="/networking/fabrics"
              element={
                <ResourceListPage
                  kind="HybridFabric"
                  title={t('nav.hybridFabrics')}
                  listPath="/networking/fabrics"
                  createPath="/create/hybridfabric"
                />
              }
            />
            <Route
              path="/networking/fabrics/:name"
              element={
                <AdminResourceDetailPage
                  kind="HybridFabric"
                  title={t('nav.hybridFabrics')}
                  listPath="/networking/fabrics"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route
              path="/networking/gateways"
              element={
                <ResourceListPage
                  kind="CloudGateway"
                  title={t('nav.cloudGateways')}
                  listPath="/networking/gateways"
                  createPath="/create/cloudgateway"
                />
              }
            />
            <Route
              path="/networking/gateways/:name"
              element={
                <AdminResourceDetailPage
                  kind="CloudGateway"
                  title={t('nav.cloudGateways')}
                  listPath="/networking/gateways"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route
              path="/networking/transport"
              element={
                <ResourceListPage
                  kind="TransportLink"
                  title={t('nav.transportLinks')}
                  listPath="/networking/transport"
                  createPath="/create/transportlink"
                />
              }
            />
            <Route
              path="/networking/transport/:name"
              element={
                <AdminResourceDetailPage
                  kind="TransportLink"
                  title={t('nav.transportLinks')}
                  listPath="/networking/transport"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route path="/networking/uihealth" element={<UIHealthPage />} />
            <Route
              path="/networking/uihealth/:name"
              element={
                <AdminResourceDetailPage
                  kind="UIHealthChecker"
                  title={t('nav.uiHealth')}
                  listPath="/networking/uihealth"
                  fixedNamespace="sovereign-cloud"
                />
              }
            />
            <Route path="/create/:kind" element={<CreateResourcePage />} />
            <Route
              path="*"
              element={
                <EmptyState>
                  <Title headingLevel="h4" size="lg">
                    {t('common.pageNotFound')}
                  </Title>
                  <EmptyStateBody>{t('nav.pageNotFoundHint')}</EmptyStateBody>
                </EmptyState>
              }
            />
          </Routes>
        </div>
      </PageSection>
    </Page>
  );
}

export function App(): React.ReactElement {
  return (
    <SovereignI18nProvider>
      <SovereignThemeProvider>
        <AdminLayout />
      </SovereignThemeProvider>
    </SovereignI18nProvider>
  );
}
