/**
 * Paths of registry entries per host and scope, and the legacy redirects.
 *
 * Dashboards:  /t/<tenant>/<route>, /all/<route>, /platform/<route>
 * Console:     /hybridsovereign/<route>/ns/<namespace>, /hybridsovereign/<route>/all-namespaces,
 *              /hybridsovereign/platform/<route>
 */
import type { RequestedScope, Scope } from '../scope/types';
import { namespaceOfTenant, tenantOfNamespace } from '../scope/parseGroups';
import { entryById, entryForKind, entryForCreateForm } from './registry';
import type { Host, NavEntry } from './types';

export type AnyScope = RequestedScope | Scope;

export interface PathParams {
  name?: string;
  formType?: string;
}

function scopeKind(scope: AnyScope): 'tenant' | 'all' | 'global' | 'none' {
  if (scope.kind === 'tenant' || scope.kind === 'all' || scope.kind === 'global') return scope.kind;
  return 'none';
}

function tenantName(scope: AnyScope): string | undefined {
  if (scope.kind !== 'tenant') return undefined;
  return 'tenant' in scope ? scope.tenant.name : scope.name;
}

function fill(route: string, params: PathParams): string {
  let r = route;
  if (params.formType) r = r.replace(':formType', encodeURIComponent(params.formType));
  if (params.name) r = r ? `${r}/${encodeURIComponent(params.name)}` : encodeURIComponent(params.name);
  return r;
}

export function isConsoleHost(host: Host): boolean {
  return host === 'tenant-console' || host === 'admin-console';
}

/** Path of an entry for a host and scope. Platform entries ignore the tenant. */
export function pathFor(host: Host, entry: NavEntry | string, scope: AnyScope, params: PathParams = {}): string {
  const e = typeof entry === 'string' ? entryById(entry) : entry;
  if (!e) return isConsoleHost(host) ? '/hybridsovereign' : '/';
  const route = fill(e.route, params);
  const platform = e.persona.includes('platform') && !e.persona.includes('tenant');
  if (isConsoleHost(host)) {
    if (platform) return `/hybridsovereign/platform${route ? `/${route}` : ''}`;
    const base = `/hybridsovereign/${route || 'home'}`;
    const k = scopeKind(scope);
    if (k === 'all') return `${base}/all-namespaces`;
    const t = tenantName(scope);
    return t ? `${base}/ns/${namespaceOfTenant(t)}` : base;
  }
  if (platform) return `/platform${route ? `/${route}` : ''}`;
  const k = scopeKind(scope);
  if (k === 'all') return `/all${route ? `/${route}` : ''}`;
  const t = tenantName(scope);
  if (t) return `/t/${t}${route ? `/${route}` : ''}`;
  return route ? `/${route}` : '/';
}

/** Detail path of an object of `kind`, in the entry that lists that kind. */
export function routeFor(host: Host, kind: string, scope: AnyScope, name?: string): string | undefined {
  const persona = scope.kind === 'global' ? 'platform' : 'tenant';
  const e = entryForKind(kind, persona);
  if (!e) return undefined;
  return pathFor(host, e, scope, name ? { name } : {});
}

/** Path of the create form of a kind's entry. */
export function createPathFor(host: Host, form: string, scope: AnyScope): string | undefined {
  const e = entryForCreateForm(form);
  if (!e) return undefined;
  const create = e.persona.includes('platform') && !e.persona.includes('tenant') ? 'platform-create' : 'create';
  return pathFor(host, create, scope, { formType: form });
}

/**
 * Old paths per host -> registry entry. `scope`: 'namespace' takes the tenant
 * from a :namespace segment, 'current' uses the user's tenant (landing),
 * 'all' / 'global' fixed scopes. A `to` of null means the page was removed.
 */
export interface LegacyRoute {
  host: Host;
  from: string;
  to: string | null;
  scope: 'namespace' | 'current' | 'all' | 'global';
}

const TENANT_LEGACY: [string, string][] = [
  ['teams', 'teams'],
  ['projects', 'projects'],
  ['assignments', 'assignments'],
  ['cloudoso', 'env-openstack'],
  ['cloudaws', 'env-aws'],
  ['cloudgcp', 'env-gcp'],
  ['cloudvirt', 'env-vm'],
  ['migrations', 'env-migrations'],
  ['network-overview', 'network-map'],
  ['networks', 'networks'],
  ['placements', 'placements'],
  ['platforms', 'clusters'],
  ['vaults', 'svc-vaults'],
  ['vaultkvs', 'svc-vault-kvs'],
  ['aaporgs', 'svc-aap-orgs'],
  ['quayorgs', 'svc-quay-orgs'],
  ['rbac', 'access-groups'],
];

export const LEGACY_ROUTES: LegacyRoute[] = [
  // tenant dashboard: unscoped paths of the old static sidebar
  ...TENANT_LEGACY.flatMap(([from, to]): LegacyRoute[] => [
    { host: 'tenant-dashboard', from, to, scope: 'current' },
    ...(to === 'network-map' ? [] : [{ host: 'tenant-dashboard' as Host, from: `${from}/:name`, to, scope: 'current' as const }]),
  ]),
  { host: 'tenant-dashboard', from: 'personas', to: null, scope: 'current' },
  { host: 'tenant-dashboard', from: 'personas/:name', to: null, scope: 'current' },
  { host: 'tenant-dashboard', from: 'create/:formType', to: 'create', scope: 'current' },
  // admin dashboard
  { host: 'admin-dashboard', from: 'entities', to: 'tenants', scope: 'global' },
  { host: 'admin-dashboard', from: 'entities/:name', to: 'tenants', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/overview', to: 'fabric-map', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/fabrics', to: 'fabric', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/fabrics/:name', to: 'fabric', scope: 'global' },
  { host: 'admin-dashboard', from: 'clouds/infrastructure', to: 'sites', scope: 'global' },
  { host: 'admin-dashboard', from: 'clouds/infrastructure/:name', to: 'sites', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/gateways', to: 'gateways', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/gateways/:name', to: 'gateways', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/uihealth', to: 'ui-health', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/uihealth/:name', to: 'ui-health', scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/networks', to: 'networks', scope: 'all' },
  { host: 'admin-dashboard', from: 'networking/networks/:namespace/:name', to: 'networks', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'networking/placements', to: 'placements', scope: 'all' },
  { host: 'admin-dashboard', from: 'networking/placements/:namespace/:name', to: 'placements', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'networking/transport', to: null, scope: 'global' },
  { host: 'admin-dashboard', from: 'networking/transport/:name', to: null, scope: 'global' },
  { host: 'admin-dashboard', from: 'personas', to: null, scope: 'global' },
  { host: 'admin-dashboard', from: 'personas/:namespace/:name', to: null, scope: 'global' },
  { host: 'admin-dashboard', from: 'clouds', to: 'env-vm', scope: 'all' },
  { host: 'admin-dashboard', from: 'clouds/cloudvirt/:namespace/:name', to: 'env-vm', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'clouds/cloudoso/:namespace/:name', to: 'env-openstack', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'clouds/cloudaws/:namespace/:name', to: 'env-aws', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'clouds/cloudgcp/:namespace/:name', to: 'env-gcp', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'operators', to: 'access-groups', scope: 'all' },
  { host: 'admin-dashboard', from: 'operators/rbacs/:namespace/:name', to: 'access-groups', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'operators/rbacconfigs/:namespace/:name', to: 'integration-rbac', scope: 'global' },
  { host: 'admin-dashboard', from: 'services', to: 'integrations', scope: 'global' },
  { host: 'admin-dashboard', from: 'services/aapconfigs/:namespace/:name', to: 'integration-aap', scope: 'global' },
  { host: 'admin-dashboard', from: 'services/quayconfigs/:namespace/:name', to: 'integration-quay', scope: 'global' },
  { host: 'admin-dashboard', from: 'services/aaporgs/:namespace/:name', to: 'svc-aap-orgs', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'services/quayorgs/:namespace/:name', to: 'svc-quay-orgs', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'services/vaults/:namespace/:name', to: 'svc-vaults', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'services/vaultkvs/:namespace/:name', to: 'svc-vault-kvs', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'teams', to: 'teams', scope: 'all' },
  { host: 'admin-dashboard', from: 'teams/:namespace/:name', to: 'teams', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'projects', to: 'projects', scope: 'all' },
  { host: 'admin-dashboard', from: 'projects/:namespace/:name', to: 'projects', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'assignments', to: 'assignments', scope: 'all' },
  { host: 'admin-dashboard', from: 'assignments/:namespace/:name', to: 'assignments', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'platforms', to: 'clusters', scope: 'all' },
  { host: 'admin-dashboard', from: 'platforms/:namespace/:name', to: 'clusters', scope: 'namespace' },
  { host: 'admin-dashboard', from: 'create/:formType', to: 'platform-create', scope: 'global' },
];

export interface LegacyMatch {
  route: LegacyRoute;
  params: { namespace?: string; name?: string; formType?: string };
}

/** Match a path (without leading slash, no scope prefix) against the host's legacy table. */
export function matchLegacy(host: Host, rest: string): LegacyMatch | undefined {
  const parts = rest.split('/').filter(Boolean);
  for (const route of LEGACY_ROUTES) {
    if (route.host !== host) continue;
    const pat = route.from.split('/');
    if (pat.length !== parts.length) continue;
    const params: LegacyMatch['params'] = {};
    let ok = true;
    for (let i = 0; i < pat.length; i += 1) {
      if (pat[i].startsWith(':')) (params as Record<string, string>)[pat[i].slice(1)] = decodeURIComponent(parts[i]);
      else if (pat[i] !== parts[i]) {
        ok = false;
        break;
      }
    }
    if (ok) return { route, params };
  }
  return undefined;
}

/**
 * Where a legacy path goes now. `current` is the scope the user would land in
 * (for unscoped tenant paths). Returns undefined when the page was removed or
 * no scope can be derived.
 */
export function legacyTarget(host: Host, m: LegacyMatch, current: RequestedScope | null): string | undefined {
  if (!m.route.to) return undefined;
  let scope: RequestedScope | null;
  switch (m.route.scope) {
    case 'namespace': {
      const t = tenantOfNamespace(m.params.namespace);
      scope = t ? { kind: 'tenant', name: t } : null;
      break;
    }
    case 'all':
      scope = { kind: 'all' };
      break;
    case 'global':
      scope = { kind: 'global' };
      break;
    default:
      scope = current;
  }
  if (!scope) return undefined;
  return pathFor(host, m.route.to, scope, { name: m.params.name, formType: m.params.formType });
}
