/**
 * The one navigation registry (C1, OD1, OD25). Every host (tenant dashboard,
 * admin dashboard, tenant console plugin, admin console plugin) renders its
 * nav, routes, breadcrumbs, titles and not-found handling from this list, so
 * the hosts cannot drift apart. Labels are i18n keys only.
 */
import type { Host, NavEntry, NavSection, Persona } from './types';

export const NAV_SECTIONS: NavSection[] = [
  { id: 'home', labelKey: 'nav.section.home' },
  { id: 'environments', labelKey: 'nav.section.environments' },
  { id: 'networks', labelKey: 'nav.section.networks' },
  { id: 'clusters', labelKey: 'nav.section.clusters' },
  { id: 'platform-services', labelKey: 'nav.section.platformServices' },
  { id: 'access', labelKey: 'nav.section.access' },
  { id: 'platform', labelKey: 'nav.section.platform' },
  { id: 'sites', labelKey: 'nav.section.sites' },
  { id: 'integrations', labelKey: 'nav.section.integrations' },
  { id: 'diagnostics', labelKey: 'nav.section.diagnostics' },
];

const TENANT = { persona: ['tenant'] as Persona[], scopes: ['tenant', 'all'] as NavEntry['scopes'] };
const PLATFORM = {
  persona: ['platform'] as Persona[],
  scopes: ['global'] as NavEntry['scopes'],
  enabledWhen: 'platformAdmin' as const,
};

export const NAV_REGISTRY: NavEntry[] = [
  // ---- tenant persona (tenant and All tenants scope) ----
  { id: 'home', labelKey: 'nav.home', section: 'home', route: '', page: 'tenant-home', ...TENANT },
  {
    id: 'env-vm', labelKey: 'nav.envVm', section: 'environments', route: 'environments/vm', kinds: ['CloudVirt'],
    page: 'list', createForm: 'cloudvirt', requiredVerbs: [{ kind: 'CloudVirt', verb: 'list' }], ...TENANT,
  },
  {
    id: 'env-openstack', labelKey: 'nav.envOpenstack', section: 'environments', route: 'environments/openstack', kinds: ['CloudOSO'],
    page: 'list', createForm: 'cloudoso', requiredVerbs: [{ kind: 'CloudOSO', verb: 'list' }], ...TENANT,
  },
  {
    id: 'env-aws', labelKey: 'nav.envAws', section: 'environments', route: 'environments/aws', kinds: ['CloudAWS'],
    page: 'list', createForm: 'cloudaws', requiredVerbs: [{ kind: 'CloudAWS', verb: 'list' }], ...TENANT,
  },
  {
    id: 'env-gcp', labelKey: 'nav.envGcp', section: 'environments', route: 'environments/gcp', kinds: ['CloudGCP'],
    page: 'list', createForm: 'cloudgcp', requiredVerbs: [{ kind: 'CloudGCP', verb: 'list' }], ...TENANT,
  },
  {
    id: 'env-migrations', labelKey: 'nav.envMigrations', section: 'environments', route: 'environments/migrations',
    kinds: ['OpenStackMigration'], page: 'list', createForm: 'migration', nav: false, ...TENANT,
  },
  {
    id: 'networks', labelKey: 'nav.networks', section: 'networks', route: 'networks', kinds: ['HybridNetwork'],
    page: 'list', createForm: 'hybridnetwork', requiredVerbs: [{ kind: 'HybridNetwork', verb: 'list' }], ...TENANT,
  },
  {
    id: 'network-map', labelKey: 'nav.networkMap', section: 'networks', route: 'networks/map', page: 'network-map',
    requiredVerbs: [{ kind: 'HybridNetwork', verb: 'list' }], ...TENANT,
  },
  {
    // No nav item: placements are made with the Attach dialog of the environment (owner brief, design 5.6).
    id: 'placements', labelKey: 'nav.placements', section: 'networks', route: 'networks/placements',
    kinds: ['NetworkPlacement'], page: 'list', createForm: 'networkplacement', nav: false, ...TENANT,
  },
  {
    id: 'clusters', labelKey: 'nav.clusters', section: 'clusters', route: 'clusters', kinds: ['PlatformOpenshift'],
    page: 'list', createForm: 'platformopenshift', requiredVerbs: [{ kind: 'PlatformOpenshift', verb: 'list' }], ...TENANT,
  },
  {
    id: 'teams', labelKey: 'nav.teams', section: 'clusters', route: 'clusters/teams', kinds: ['Team'],
    page: 'list', createForm: 'team', requiredVerbs: [{ kind: 'Team', verb: 'list' }], ...TENANT,
  },
  {
    id: 'projects', labelKey: 'nav.projects', section: 'clusters', route: 'clusters/projects', kinds: ['Project'],
    page: 'list', createForm: 'project', requiredVerbs: [{ kind: 'Project', verb: 'list' }], ...TENANT,
  },
  {
    id: 'assignments', labelKey: 'nav.assignments', section: 'clusters', route: 'clusters/assignments', kinds: ['Assignment'],
    page: 'list', createForm: 'assignment', requiredVerbs: [{ kind: 'Assignment', verb: 'list' }], ...TENANT,
  },
  {
    id: 'svc-vaults', labelKey: 'nav.svcVaults', section: 'platform-services', route: 'services/vaults', kinds: ['Vault'],
    page: 'list', createForm: 'vault', requiredVerbs: [{ kind: 'Vault', verb: 'list' }], ...TENANT,
  },
  {
    id: 'svc-vault-kvs', labelKey: 'nav.svcVaultKvs', section: 'platform-services', route: 'services/kv', kinds: ['VaultKV'],
    page: 'list', createForm: 'vaultkv', requiredVerbs: [{ kind: 'VaultKV', verb: 'list' }], ...TENANT,
  },
  {
    id: 'svc-aap-orgs', labelKey: 'nav.svcAapOrgs', section: 'platform-services', route: 'services/aap', kinds: ['AAPOrg'],
    page: 'list', createForm: 'aaporg', requiredVerbs: [{ kind: 'AAPOrg', verb: 'list' }], ...TENANT,
  },
  {
    id: 'svc-quay-orgs', labelKey: 'nav.svcQuayOrgs', section: 'platform-services', route: 'services/quay', kinds: ['QuayOrg'],
    page: 'list', createForm: 'quayorg', requiredVerbs: [{ kind: 'QuayOrg', verb: 'list' }], ...TENANT,
  },
  {
    id: 'access-groups', labelKey: 'nav.accessGroups', section: 'access', route: 'access/groups', kinds: ['Rbac'],
    page: 'list', createForm: 'rbac', requiredVerbs: [{ kind: 'Rbac', verb: 'list' }], ...TENANT,
  },
  { id: 'create', labelKey: 'nav.create', section: 'home', route: 'create/:formType', page: 'create', nav: false, ...TENANT },

  // ---- platform persona (platform scope, platform administrators only) ----
  { id: 'platform-overview', labelKey: 'nav.platformOverview', section: 'platform', route: '', page: 'platform-overview', ...PLATFORM },
  {
    id: 'tenants', labelKey: 'nav.tenants', section: 'platform', route: 'tenants', kinds: ['Entity'],
    page: 'list', createForm: 'entity', ...PLATFORM,
  },
  {
    id: 'sites', labelKey: 'nav.sites', section: 'sites', route: 'sites', kinds: ['CloudInfrastructure'],
    page: 'list', createForm: 'cloudinfrastructure', ...PLATFORM,
  },
  {
    id: 'fabric', labelKey: 'nav.fabric', section: 'sites', route: 'fabric', kinds: ['HybridFabric'],
    page: 'list', createForm: 'hybridfabric', ...PLATFORM,
  },
  {
    id: 'gateways', labelKey: 'nav.gateways', section: 'sites', route: 'gateways', kinds: ['CloudGateway'],
    page: 'list', createForm: 'cloudgateway', ...PLATFORM,
  },
  { id: 'fabric-map', labelKey: 'nav.fabricMap', section: 'sites', route: 'fabric/map', page: 'fabric-map', ...PLATFORM },
  { id: 'integrations', labelKey: 'nav.integrations', section: 'integrations', route: 'integrations', page: 'integrations', ...PLATFORM },
  {
    id: 'integration-rbac', labelKey: 'kinds.RbacConfig', section: 'integrations', route: 'integrations/rbac',
    kinds: ['RbacConfig'], page: 'list', nav: false, ...PLATFORM,
  },
  {
    id: 'integration-aap', labelKey: 'kinds.AAPConfig', section: 'integrations', route: 'integrations/aap',
    kinds: ['AAPConfig'], page: 'list', nav: false, ...PLATFORM,
  },
  {
    id: 'integration-quay', labelKey: 'kinds.QuayConfig', section: 'integrations', route: 'integrations/quay',
    kinds: ['QuayConfig'], page: 'list', nav: false, ...PLATFORM,
  },
  {
    id: 'ui-health', labelKey: 'nav.uiHealth', section: 'diagnostics', route: 'diagnostics/ui-health',
    kinds: ['UIHealthChecker'], page: 'ui-health', createForm: 'uihealthchecker', ...PLATFORM,
  },
  { id: 'platform-create', labelKey: 'nav.create', section: 'platform', route: 'create/:formType', page: 'create', nav: false, ...PLATFORM },
];

const DEFAULT_HOSTS: Record<Persona, Host[]> = {
  tenant: ['tenant-dashboard', 'admin-dashboard', 'tenant-console'],
  platform: ['admin-dashboard', 'admin-console'],
};

export function hostsOf(entry: NavEntry): Host[] {
  if (entry.hosts) return entry.hosts;
  return Array.from(new Set(entry.persona.flatMap((p) => DEFAULT_HOSTS[p])));
}

export function entriesForHost(host: Host): NavEntry[] {
  return NAV_REGISTRY.filter((e) => hostsOf(e).includes(host));
}

export function entryById(id: string): NavEntry | undefined {
  return NAV_REGISTRY.find((e) => e.id === id);
}

/** The entry whose list kind is `kind`, preferring the given persona. */
export function entryForKind(kind: string, persona?: Persona): NavEntry | undefined {
  const matches = NAV_REGISTRY.filter((e) => e.kinds?.[0] === kind && e.page === 'list');
  return (persona && matches.find((e) => e.persona.includes(persona))) || matches[0];
}

/** The entry of a create form. */
export function entryForCreateForm(form: string): NavEntry | undefined {
  return NAV_REGISTRY.find((e) => e.createForm === form);
}

export function sectionOf(id: string): NavSection | undefined {
  return NAV_SECTIONS.find((s) => s.id === id);
}

/** Kinds that have no page any more (reachable with oc only). */
export const KINDS_WITHOUT_PAGES = ['Persona', 'TransportLink', 'Iaac'] as const;
