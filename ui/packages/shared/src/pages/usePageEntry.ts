import { useParams } from 'react-router-dom';
import { useOptionalScope } from '../scope/ScopeProvider';
import { tenantOfNamespace } from '../scope/parseGroups';
import { gateNamespaceFor } from '../access/permissionGate';
import { entryById } from '../nav/registry';
import { useNav, useNavHost } from '../nav/useNav';
import { pathFor } from '../nav/paths';
import type { Host, NavEntry } from '../nav/types';
import type { HybridSovereignKind, K8sResource } from '../types';

export function isPlatformEntry(e: NavEntry | undefined): boolean {
  return !!e && e.persona.includes('platform') && !e.persona.includes('tenant');
}

/** Everything a scoped page needs about its registry entry and the scope. */
export function usePageEntry(entryId: string): {
  host: Host;
  entry?: NavEntry;
  kind?: HybridSovereignKind;
  /** Namespace to read from; undefined in All tenants scope. */
  namespace?: string;
  allTenants: boolean;
  platform: boolean;
  listHref: string;
  detailHref: (item: K8sResource) => string;
} {
  const host = (useNavHost() ?? 'tenant-dashboard') as Host;
  const nav = useNav(host);
  const ctx = useOptionalScope();
  const entry = entryById(entryId);
  const kind = entry?.kinds?.[0];
  const platform = isPlatformEntry(entry);
  const scope = ctx?.scope;
  const allTenants = !platform && scope?.kind === 'all';
  const namespace = platform
    ? kind
      ? gateNamespaceFor(kind, 'sovereign-cloud')
      : 'sovereign-cloud'
    : scope?.kind === 'tenant'
      ? scope.tenant.namespace
      : undefined;
  const listHref = nav.pathFor(entryId);
  const detailHref = (item: K8sResource): string => {
    if (!entry) return '#';
    if (allTenants) {
      const t = tenantOfNamespace(item.metadata.namespace);
      if (t) return pathFor(host, entry, { kind: 'tenant', name: t }, { name: item.metadata.name });
    }
    return nav.pathFor(entryId, { name: item.metadata.name });
  };
  return { host, entry, kind, namespace, allTenants, platform, listHref, detailHref };
}

export function useObjectName(): string {
  const { name } = useParams<{ name?: string }>();
  return name ? decodeURIComponent(name) : '';
}
