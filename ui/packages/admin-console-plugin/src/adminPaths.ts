import { HybridSovereignKind, K8sResource, objectPath, pathFor } from '@hybridsovereign/shared';

const HOST = 'admin-console' as const;

/** Detail path of any object in the console (platform kinds under /platform, tenant kinds in their tenant). */
export function overviewItemHref(item: K8sResource): string | null {
  return objectPath(HOST, (item.kind || '') as HybridSovereignKind, item.metadata.namespace, item.metadata.name) ?? null;
}

export function objectHref(kind: HybridSovereignKind, item: K8sResource): string | null {
  return objectPath(HOST, kind, item.metadata.namespace, item.metadata.name) ?? null;
}

const ALL = { kind: 'all' as const };
const GLOBAL = { kind: 'global' as const };

/** List pages linked from the platform overview. */
export const ADMIN_LINKS = {
  tenants: pathFor(HOST, 'tenants', GLOBAL),
  sites: pathFor(HOST, 'sites', GLOBAL),
  teams: pathFor(HOST, 'teams', ALL),
  clusters: pathFor(HOST, 'clusters', ALL),
  assignments: pathFor(HOST, 'assignments', ALL),
  accessGroups: pathFor(HOST, 'access-groups', ALL),
  createHealthCheck: pathFor(HOST, 'platform-create', GLOBAL, { formType: 'uihealthchecker' }),
  healthCheck: (name: string) => pathFor(HOST, 'ui-health', GLOBAL, { name }),
};
