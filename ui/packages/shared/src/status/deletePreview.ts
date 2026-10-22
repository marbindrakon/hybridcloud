/**
 * What deleting an object takes with it (GT-F8, OD41), from live data:
 *
 *   network      -> its network placements
 *   environment  -> its network placements, its site namespaces (and the VMs
 *                   in them) or its cloud project, its DNS zone
 *   placement    -> the subnet it holds on its site
 *   access group -> the tenant roles, tool roles and access lists naming it
 *   team setup / app project -> the access its access lists grant
 *
 * An object that owns realized resources (status.realization, site
 * namespaces, a cloud project or network placements) asks for its name to be
 * typed; plain objects keep the one-click confirm.
 */
import type { K8sResource } from '../types';

export type PreviewSectionKey =
  | 'placements'
  | 'namespaces'
  | 'cloudProject'
  | 'dnsZone'
  | 'subnet'
  | 'tenantRoles'
  | 'toolRoles'
  | 'accessLists'
  | 'scopeGrants';

export interface PreviewEntry {
  /** Kind of a linked object (absent for plain text entries) */
  kind?: string;
  name: string;
  /** A field or role key the entry is shown with */
  detail?: string;
}

export interface PreviewSection {
  key: PreviewSectionKey;
  entries: PreviewEntry[];
}

export interface DeletePreview {
  sections: PreviewSection[];
  /** The object owns realized resources: the name must be typed to delete */
  realized: boolean;
}

/** Objects of the tenant namespace (and the tenant) the preview reads. */
export interface PreviewContext {
  placements?: K8sResource[];
  /** Objects that may name an access group, any kinds, with their `kind` set */
  related?: K8sResource[];
  /** The tenant (Entity), when readable */
  tenant?: K8sResource | null;
}

export const ENVIRONMENT_KINDS = ['CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP'];

/** Fields of each kind that name access groups (spec paths, dot separated). */
export const ACCESS_GROUP_FIELDS: Record<string, { path: string; section: 'toolRoles' | 'accessLists' }[]> = {
  AAPOrg: ['aapAdminRbac', 'aapJobExecutorRbac', 'aapViewerRbac'].map((path) => ({ path, section: 'toolRoles' as const })),
  QuayOrg: ['quayAdminRbac', 'quayCreatorRbac', 'quayMemberRbac'].map((path) => ({ path, section: 'toolRoles' as const })),
  VaultKV: ['vaultAdminRbac', 'vaultOpsRbac', 'vaultDeveloperRbac', 'vaultReaderRbac'].map((path) => ({ path, section: 'toolRoles' as const })),
  CloudVirt: ['toolRbac.editRbac', 'toolRbac.viewRbac'].map((path) => ({ path, section: 'toolRoles' as const })),
  PlatformOpenshift: [
    'toolRbac.clusterAdminRbac',
    'toolRbac.clusterOperatorRbac',
    'toolRbac.clusterDeveloperRbac',
    'toolRbac.clusterViewerRbac',
    'clusterViewerRbac',
  ].map((path) => ({ path, section: 'toolRoles' as const })),
  Assignment: ['toolRbac.assignmentAdmin', 'toolRbac.assignmentDeveloper', 'toolRbac.assignmentViewer', 'toolRbac.assignmentOps'].map(
    (path) => ({ path, section: 'toolRoles' as const }),
  ),
  Team: ['teamAdmin', 'teamMemberRbac', 'teamViewerRbac'].map((path) => ({ path, section: 'accessLists' as const })),
  Project: ['projectAdmin', 'projectDeveloperRbac', 'projectViewerRbac'].map((path) => ({ path, section: 'accessLists' as const })),
  HybridNetwork: [{ path: 'networkViewerRbac', section: 'accessLists' as const }],
};

/** Kinds the preview of an access group reads. */
export const ACCESS_GROUP_KINDS = Object.keys(ACCESS_GROUP_FIELDS);

function at(obj: unknown, path: string): unknown {
  let cur: unknown = obj;
  for (const p of path.split('.')) {
    if (!cur || typeof cur !== 'object') return undefined;
    cur = (cur as Record<string, unknown>)[p];
  }
  return cur;
}

function names(v: unknown): string[] {
  if (typeof v === 'string') return v ? [v] : [];
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string' && !!x);
  return [];
}

function lastKey(path: string): string {
  const parts = path.split('.');
  return parts[parts.length - 1];
}

function section(key: PreviewSectionKey, entries: PreviewEntry[]): PreviewSection[] {
  return entries.length ? [{ key, entries }] : [];
}

export function deletePreview(kind: string, item: K8sResource, ctx: PreviewContext = {}): DeletePreview {
  const name = item.metadata.name;
  const spec = (item.spec ?? {}) as Record<string, unknown>;
  const status = (item.status ?? {}) as Record<string, unknown>;
  const hasRealization = !!status.realization && typeof status.realization === 'object' && Object.keys(status.realization as object).length > 0;
  const placements = ctx.placements ?? [];

  if (kind === 'HybridNetwork') {
    const rows = placements
      .filter((p) => (p.spec as { network?: string } | undefined)?.network === name)
      .map((p) => {
        const b = (p.spec as { backend?: { kind?: string; name?: string } }).backend ?? {};
        return { kind: 'NetworkPlacement', name: p.metadata.name, detail: [b.kind, b.name].filter(Boolean).join(' ') };
      });
    return { sections: section('placements', rows), realized: hasRealization || rows.length > 0 };
  }

  if (ENVIRONMENT_KINDS.includes(kind)) {
    const rows = placements
      .filter((p) => {
        const b = (p.spec as { backend?: { kind?: string; name?: string } } | undefined)?.backend;
        return b?.kind === kind && b?.name === name;
      })
      .map((p) => ({ kind: 'NetworkPlacement', name: p.metadata.name, detail: (p.spec as { network?: string }).network }));
    const nsRows: PreviewEntry[] =
      kind === 'CloudVirt'
        ? ((status.namespaces as { name?: string; namespace?: string }[] | undefined) ?? [])
            .map((n) => ({ name: n.namespace || n.name || '' }))
            .filter((n) => n.name)
        : [];
    // The cloud side: the OpenStack or Google Cloud project, or the AWS resources (by region).
    const project =
      kind === 'CloudOSO'
        ? (status.projectName as string | undefined)
        : kind === 'CloudGCP'
          ? (status.project as string | undefined)
          : kind === 'CloudAWS' && status.awsAccountId
            ? ((status.awsRegion as string | undefined) ?? '')
            : undefined;
    const projectRows: PreviewEntry[] = project !== undefined && (project || kind === 'CloudAWS') ? [{ name: project, detail: kind }] : [];
    const zone = (status.dns as { zone?: string } | undefined)?.zone || (status.domain as string | undefined);
    const zoneRows: PreviewEntry[] = zone ? [{ name: zone }] : [];
    return {
      sections: [
        ...section('placements', rows),
        ...section('namespaces', nsRows),
        ...section('cloudProject', projectRows),
        ...section('dnsZone', zoneRows),
      ],
      realized: hasRealization || rows.length > 0 || nsRows.length > 0 || projectRows.length > 0,
    };
  }

  if (kind === 'NetworkPlacement') {
    const prefixes = names(status.prefixes);
    const site = (status.site as string | undefined) ?? '';
    const rows: PreviewEntry[] = prefixes.map((p) => ({ name: p, detail: site }));
    return { sections: section('subnet', rows), realized: hasRealization || rows.length > 0 };
  }

  if (kind === 'Rbac') {
    const roles = (at(ctx.tenant?.spec, 'namespaceRbac') ?? {}) as Record<string, unknown>;
    const tenantRows: PreviewEntry[] = Object.entries(roles)
      .filter(([, v]) => names(v).includes(name))
      .map(([role]) => ({ name: role }));
    const tool: PreviewEntry[] = [];
    const lists: PreviewEntry[] = [];
    for (const obj of ctx.related ?? []) {
      const k = obj.kind ?? '';
      for (const f of ACCESS_GROUP_FIELDS[k] ?? []) {
        if (names(at(obj.spec, f.path)).includes(name)) {
          (f.section === 'toolRoles' ? tool : lists).push({ kind: k, name: obj.metadata.name, detail: lastKey(f.path) });
        }
      }
    }
    return {
      sections: [...section('tenantRoles', tenantRows), ...section('toolRoles', tool), ...section('accessLists', lists)],
      realized: hasRealization,
    };
  }

  if (kind === 'Team' || kind === 'Project') {
    const rows: PreviewEntry[] = [];
    for (const f of ACCESS_GROUP_FIELDS[kind]) {
      for (const g of names(at(spec, f.path))) rows.push({ kind: 'Rbac', name: g, detail: lastKey(f.path) });
    }
    return { sections: section('scopeGrants', rows), realized: hasRealization };
  }

  return { sections: [], realized: hasRealization };
}
