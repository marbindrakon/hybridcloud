/** Identity and tenant scope (C2). */

export interface TenantContactRef {
  name?: string;
  email?: string;
  url?: string;
}

export interface TenantRef {
  /** Entity name, e.g. acme-corp */
  name: string;
  /** Tenant namespace, e.g. entity-acme-corp */
  namespace: string;
  displayName?: string;
  contacts?: TenantContactRef[];
  /** The user's groups that point at this tenant (`<entity>.<group>`). */
  groups: string[];
  /** How the tenant became visible: through a group, or as a platform reader. */
  via: 'group' | 'platform';
}

export interface Identity {
  username: string;
  displayName?: string;
  groups: string[];
  tenants: TenantRef[];
  platform: { reader: boolean; admin: boolean };
}

export type Scope =
  | { kind: 'tenant'; tenant: TenantRef }
  | { kind: 'all' }
  | { kind: 'global' }
  | { kind: 'none'; reason: 'no-role' | 'no-access' | 'loading' | 'unselected'; requested?: string };

/** What the URL (dashboards) or the active namespace (console) asks for. */
export type RequestedScope =
  | { kind: 'tenant'; name: string }
  | { kind: 'all' }
  | { kind: 'global' }
  | { kind: 'unset' };

export interface ScopeAdapter {
  requested: RequestedScope;
  /** Switch scope (navigate, or set the console's active namespace). */
  go: (next: RequestedScope) => void;
}
