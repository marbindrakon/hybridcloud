/** Navigation registry types (C1). */
import type { HybridSovereignKind } from '../types';
import type { SelfServiceFormType } from '../components/CreateResourceForm';
import type { GateVerb } from '../access/permissionGate';

export type Host = 'tenant-dashboard' | 'admin-dashboard' | 'tenant-console' | 'admin-console';
export type Persona = 'tenant' | 'platform';
/** tenant = one tenant; all = All tenants (read-only); global = platform pages without a tenant. */
export type ScopeMode = 'tenant' | 'all' | 'global';

export type SectionId =
  | 'home'
  | 'environments'
  | 'networks'
  | 'clusters'
  | 'platform-services'
  | 'access'
  | 'platform'
  | 'sites'
  | 'integrations'
  | 'diagnostics';

/** Page components each host maps to its own implementation. */
export type PageId =
  | 'tenant-home'
  | 'list'
  | 'detail'
  | 'create'
  | 'network-map'
  | 'platform-overview'
  | 'fabric-map'
  | 'integrations'
  | 'ui-health';

export interface NavEntry {
  /** Stable kebab-case id; also the console flag SHC_NAV_<ID>. */
  id: string;
  /** i18n key of the label. */
  labelKey: string;
  section: SectionId;
  /** Relative to the scope root, no leading slash ('' = the scope root). */
  route: string;
  /** First kind = the list kind; the detail route is `${route}/:name`. */
  kinds?: HybridSovereignKind[];
  persona: Persona[];
  scopes: ScopeMode[];
  /** Any-of; the entry is hidden when the caller holds none of them in the scope. */
  requiredVerbs?: { kind: HybridSovereignKind; verb: GateVerb }[];
  /** Closed set, extended only here. */
  enabledWhen?: 'always' | 'platformReader' | 'platformAdmin';
  /** false = route and breadcrumbs only, no nav item. */
  nav?: boolean;
  /** Default: every host of the entry's persona(s). */
  hosts?: Host[];
  page: PageId;
  /** Create form of the list kind (route create/<form>). */
  createForm?: SelfServiceFormType;
  /** Old paths (relative, with :namespace / :name) redirected to this entry. */
  legacy?: string[];
}

export interface NavSection {
  id: SectionId;
  labelKey: string;
}
