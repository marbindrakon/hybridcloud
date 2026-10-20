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
  LayerGroupIcon,
  NetworkIcon,
  AwsIcon,
  GoogleIcon,
  ServerIcon,
  TopologyIcon,
  OutlinedBellIcon,
  QuestionCircleIcon,
  UserIcon,
  ProjectDiagramIcon,
  BarsIcon,
  KeyIcon,
  ProcessAutomationIcon,
  SecurityIcon,
  LockIcon,
  BundleIcon,
} from '@patternfly/react-icons';
import { NavLink, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import {
  SovereignThemeProvider,
  SovereignI18nProvider,
  useTheme,
  useTranslation,
  LanguageToggle,
  useEntityNamespace,
  NamespaceContextBar,
  HybridSovereignKind,
} from '@hybridsovereign/shared';
import { TenantOverviewPage } from './pages/TenantOverviewPage';
import { TenantNetworkOverviewPage } from './pages/TenantNetworkOverviewPage';
import { TenantResourcePage } from './pages/TenantResourcePage';
import { TenantResourceDetailPage } from './pages/TenantResourceDetailPage';
import { SelfServiceFormPage } from './pages/SelfServiceFormPage';

const DEV_USER_GROUPS = ['acme-corp-platform-engineering-admins'];

type NavEntry =
  | {
      type: 'link';
      path: string;
      labelKey: string;
      icon: React.ComponentType;
      end?: boolean;
      kind?: HybridSovereignKind;
      form?: string;
    }
  | { type: 'sep'; labelKey: string };

/** Static sidebar — all links always visible; pages fetch K8s only on navigate/refresh */
const NAV: NavEntry[] = [
  { type: 'link', path: '/', labelKey: 'nav.overview', icon: TachometerAltIcon, end: true },
  { type: 'sep', labelKey: 'nav.team' },
  { type: 'link', path: '/teams', labelKey: 'nav.teams', icon: UsersIcon, kind: 'Team', form: 'team' },
  { type: 'link', path: '/projects', labelKey: 'nav.projects', icon: FolderOpenIcon, kind: 'Project', form: 'project' },
  {
    type: 'link',
    path: '/assignments',
    labelKey: 'nav.assignments',
    icon: ProjectDiagramIcon,
    kind: 'Assignment',
    form: 'assignment',
  },
  { type: 'sep', labelKey: 'nav.iaasPlatform' },
  { type: 'link', path: '/cloudoso', labelKey: 'nav.cloudOso', icon: LayerGroupIcon, kind: 'CloudOSO', form: 'cloudoso' },
  { type: 'link', path: '/cloudaws', labelKey: 'nav.cloudAws', icon: AwsIcon, kind: 'CloudAWS', form: 'cloudaws' },
  { type: 'link', path: '/cloudgcp', labelKey: 'nav.cloudGcp', icon: GoogleIcon, kind: 'CloudGCP', form: 'cloudgcp' },
  { type: 'link', path: '/cloudvirt', labelKey: 'nav.cloudVirt', icon: ServerIcon, kind: 'CloudVirt', form: 'cloudvirt' },
  { type: 'sep', labelKey: 'nav.iaasNetworking' },
  {
    type: 'link',
    path: '/network-overview',
    labelKey: 'nav.networkOverview',
    icon: NetworkIcon,
  },
  {
    type: 'link',
    path: '/networks',
    labelKey: 'nav.hybridNetworks',
    icon: TopologyIcon,
    kind: 'HybridNetwork',
    form: 'hybridnetwork',
  },
  {
    type: 'link',
    path: '/placements',
    labelKey: 'nav.networkPlacements',
    icon: ProjectDiagramIcon,
    kind: 'NetworkPlacement',
    form: 'networkplacement',
  },
  { type: 'sep', labelKey: 'nav.appPlatform' },
  {
    type: 'link',
    path: '/platforms',
    labelKey: 'nav.platformOpenshift',
    icon: ClusterIcon,
    kind: 'PlatformOpenshift',
    form: 'platformopenshift',
  },
  { type: 'sep', labelKey: 'nav.services' },
  { type: 'link', path: '/vaults', labelKey: 'nav.vaults', icon: SecurityIcon, kind: 'Vault', form: 'vault' },
  { type: 'link', path: '/vaultkvs', labelKey: 'nav.vaultKvs', icon: KeyIcon, kind: 'VaultKV', form: 'vaultkv' },
  { type: 'link', path: '/aaporgs', labelKey: 'nav.aapOrgs', icon: ProcessAutomationIcon, kind: 'AAPOrg', form: 'aaporg' },
  { type: 'link', path: '/quayorgs', labelKey: 'nav.quayOrgs', icon: BundleIcon, kind: 'QuayOrg', form: 'quayorg' },
  { type: 'sep', labelKey: 'nav.accessControl' },
  { type: 'link', path: '/rbac', labelKey: 'nav.rbac', icon: LockIcon, kind: 'Rbac', form: 'rbac' },
];

function ThemeToggle(): React.ReactElement {
  const { mode, toggleTheme } = useTheme();
  const { t } = useTranslation();
  return (
    <Button
      variant="plain"
      size="sm"
      aria-label={t('common.toggleTheme')}
      onClick={toggleTheme}
      icon={mode === 'dark' ? <SunIcon /> : <MoonIcon />}
    />
  );
}

function staticNavGroups(): { titleKey?: string; items: Extract<NavEntry, { type: 'link' }>[] }[] {
  const groups: { titleKey?: string; items: Extract<NavEntry, { type: 'link' }>[] }[] = [];
  let current: { titleKey?: string; items: Extract<NavEntry, { type: 'link' }>[] } = { items: [] };
  for (const entry of NAV) {
    if (entry.type === 'sep') {
      if (current.items.length) groups.push(current);
      current = { titleKey: entry.labelKey, items: [] };
    } else {
      current.items.push(entry);
    }
  }
  if (current.items.length) groups.push(current);
  return groups;
}

function TenantLayout(): React.ReactElement {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();
  const { mode } = useTheme();
  const [isSidebarOpen, setSidebarOpen] = React.useState(true);
  const {
    namespace: tenantNamespace,
    entity,
    entities,
    selectEntity,
  } = useEntityNamespace({
    userGroups: DEV_USER_GROUPS,
  });

  const navGroups = React.useMemo(() => staticNavGroups(), []);

  const header = (
    <Masthead className="sc-pf-masthead">
      <MastheadToggle>
        <PageToggleButton
          variant="plain"
          aria-label={t('common.globalNav')}
          isSidebarOpen={isSidebarOpen}
          onSidebarToggle={() => setSidebarOpen((o) => !o)}
          id="tenant-nav-toggle"
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
            <span>{t('nav.sovereignCloud')}</span>
          </NavLink>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar id="tenant-masthead-toolbar" isFullHeight isStatic>
          <ToolbarContent>
            <ToolbarGroup align={{ default: 'alignEnd' }}>
              <ToolbarItem>
                <LanguageToggle isCompact />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" size="sm" aria-label={t('common.notifications')} icon={<OutlinedBellIcon />} />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" size="sm" aria-label={t('common.help')} icon={<QuestionCircleIcon />} />
              </ToolbarItem>
              <ToolbarItem>
                <ThemeToggle />
              </ToolbarItem>
              <ToolbarItem>
                <Button variant="plain" size="sm" aria-label={t('common.user')} icon={<UserIcon />}>
                  {t('common.tenant')}
                </Button>
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );

  const sidebar = (
    <PageSidebar theme={mode} isSidebarOpen={isSidebarOpen} id="tenant-sidebar">
      <PageSidebarBody>
        <div className="sc-sidebar-title">{t('nav.sovereignCloud')}</div>
        <Nav theme={mode} aria-label={t('nav.tenantNav')}>
          {navGroups.map((group, gi) => (
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
      </PageSidebarBody>
    </PageSidebar>
  );

  const resourceRoutes = React.useMemo(
    () => NAV.filter((i): i is Extract<NavEntry, { type: 'link' }> => i.type === 'link' && !!i.kind),
    [],
  );

  return (
    <Page className="sc-standalone-shell" header={header} sidebar={sidebar} isManagedSidebar>
      <PageSection padding={{ default: 'noPadding' }} className="sc-ns-section">
        <div className="sc-page">
          <NamespaceContextBar
            namespace={tenantNamespace}
            entityName={entity?.metadata.name}
            billingId={(entity?.spec as { billingID?: string } | undefined)?.billingID}
            entities={entities}
            onSelectEntity={selectEntity}
            onTopologyClick={() => navigate('/')}
          />
        </div>
      </PageSection>
      <PageSection isFilled className="sc-page-section sc-page-section--dashboard">
        <div className="sc-page">
          <Routes>
            <Route path="/" element={<TenantOverviewPage namespace={tenantNamespace} />} />
            <Route
              path="/network-overview"
              element={<TenantNetworkOverviewPage namespace={tenantNamespace} />}
            />
            {resourceRoutes.flatMap((item) => [
              <Route
                key={`${item.path}-detail`}
                path={`${item.path}/:name`}
                element={
                  <TenantResourceDetailPage
                    kind={item.kind as 'Team'}
                    title={t(item.labelKey)}
                    listPath={item.path}
                    namespace={tenantNamespace}
                  />
                }
              />,
              <Route
                key={item.path}
                path={item.path}
                element={
                  <TenantResourcePage
                    kind={item.kind as 'Team'}
                    title={t(item.labelKey)}
                    namespace={tenantNamespace}
                    listPath={item.path}
                    formType={item.form as 'team' | undefined}
                  />
                }
              />,
            ])}
            <Route path="/create/:formType" element={<SelfServiceFormPage namespace={tenantNamespace} />} />
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
        <TenantLayout />
      </SovereignThemeProvider>
    </SovereignI18nProvider>
  );
}

export { DEV_USER_GROUPS as TENANT_NAMESPACE };
