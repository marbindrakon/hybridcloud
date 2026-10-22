/**
 * What deleting an object does (GT-F8, OD41), from live data. The sections
 * follow what each kind's delete really does:
 *
 *   deleted -> removed with the object
 *   waits   -> the delete waits (the object stays Deleting) until these are gone
 *   kept    -> not deleted with it; delete them separately
 *   access  -> access its members or other groups lose
 *
 *   network      -> waits for its placements, the VM environments whose
 *                   namespaces use it and its attachment circuits
 *   environment  -> OpenStack, AWS, Google Cloud: waits for its placements and
 *                   its clusters; its cloud project and DNS zone are deleted
 *                   VMs: its site namespaces are removed once they hold no VMs,
 *                   its DNS zone is deleted; its placements are kept
 *   placement    -> the subnet it holds on its site; on a VM environment it
 *                   waits until no namespace of that environment uses the network
 *   access group -> the tenant roles, tool roles and access lists naming it
 *   team setup / app project -> the access its access lists grant
 *
 * An object that owns realized resources asks for its name to be typed; plain
 * objects keep the one-click confirm.
 */
import type { K8sResource } from '../types';

export type PreviewGroup = 'deleted' | 'waits' | 'kept' | 'access';

export type PreviewSectionKey =
  // deleted with it
  | 'namespaces'
  | 'cloudProject'
  | 'dnsZone'
  | 'subnet'
  // the delete waits until these are gone
  | 'waitPlacements'
  | 'waitVmEnvironments'
  | 'waitCircuits'
  | 'waitClusters'
  | 'waitSiteNamespace'
  // not deleted with it
  | 'keptPlacements'
  // access that ends
  | 'tenantRoles'
  | 'toolRoles'
  | 'accessLists'
  | 'scopeGrants';

export const SECTION_GROUP: Record<PreviewSectionKey, PreviewGroup> = {
  namespaces: 'deleted',
  cloudProject: 'deleted',
  dnsZone: 'deleted',
  subnet: 'deleted',
  waitPlacements: 'waits',
  waitVmEnvironments: 'waits',
  waitCircuits: 'waits',
  waitClusters: 'waits',
  waitSiteNamespace: 'waits',
  keptPlacements: 'kept',
  tenantRoles: 'access',
  toolRoles: 'access',
  accessLists: 'access',
  scopeGrants: 'access',
};

/** The order the dialog shows the groups in. */
export const PREVIEW_GROUPS: PreviewGroup[] = ['deleted', 'waits', 'kept', 'access'];

export interface PreviewEntry {
  /** Kind of a linked object (absent for plain text entries) */
  kind?: string;
  name: string;
  /** A field or role key the entry is shown with */
  detail?: string;
  /** The kind of the object `detail` names (shown in words) */
  detailKind?: string;
}

export interface PreviewSection {
  key: PreviewSectionKey;
  group: PreviewGroup;
  entries: PreviewEntry[];
}

export interface DeletePreview {
  sections: PreviewSection[];
  /** The object owns realized resources: the name must be typed to delete */
  realized: boolean;
}

/** Objects the preview reads, by kind, from the object's namespace (and the tenant). */
export interface PreviewContext {
  objects?: Record<string, K8sResource[] | undefined>;
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

/** One list read the preview of `kind` needs: a kind in a namespace. */
export interface PreviewRead {
  kind: string;
  namespace: string;
}

/** The lists the preview of `item` reads (all in the object's namespace). */
export function previewReads(kind: string, item: K8sResource): PreviewRead[] {
  const namespace = item.metadata.namespace ?? '';
  if (!namespace) return [];
  let kinds: string[] = [];
  if (kind === 'HybridNetwork') kinds = ['NetworkPlacement', 'CloudVirt', 'AttachmentCircuit'];
  else if (kind === 'CloudVirt') kinds = ['NetworkPlacement'];
  else if (ENVIRONMENT_KINDS.includes(kind)) kinds = ['NetworkPlacement', 'PlatformOpenshift'];
  else if (kind === 'NetworkPlacement') kinds = backendOf(item).kind === 'CloudVirt' ? ['CloudVirt'] : [];
  else if (kind === 'Rbac') kinds = ACCESS_GROUP_KINDS;
  return kinds.map((k) => ({ kind: k, namespace }));
}

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
  return entries.length ? [{ key, group: SECTION_GROUP[key], entries }] : [];
}

type Backend = { kind?: string; name?: string };
function backendOf(o: K8sResource): Backend {
  return (o.spec as { backend?: Backend } | undefined)?.backend ?? {};
}
function networkOf(o: K8sResource): string | undefined {
  return (o.spec as { network?: string } | undefined)?.network;
}
/** The namespaces of a VM environment that use `network` (spec, as the network and placement deletes read it). */
function vmNamespacesUsing(env: K8sResource, network: string): string[] {
  return ((env.spec as { namespaces?: { name?: string; network?: string }[] } | undefined)?.namespaces ?? [])
    .filter((n) => n.network === network)
    .map((n) => n.name ?? '')
    .filter(Boolean);
}

export function deletePreview(kind: string, item: K8sResource, ctx: PreviewContext = {}): DeletePreview {
  const name = item.metadata.name;
  const spec = (item.spec ?? {}) as Record<string, unknown>;
  const status = (item.status ?? {}) as Record<string, unknown>;
  const hasRealization = !!status.realization && typeof status.realization === 'object' && Object.keys(status.realization as object).length > 0;
  const list = (k: string): K8sResource[] => ctx.objects?.[k] ?? [];

  if (kind === 'HybridNetwork') {
    // The network's delete waits for its placements, the VM environments whose namespaces name it and its circuits.
    const placements = list('NetworkPlacement')
      .filter((p) => networkOf(p) === name)
      .map((p) => {
        const b = backendOf(p);
        return { kind: 'NetworkPlacement', name: p.metadata.name, detail: b.name, detailKind: b.kind };
      });
    const vmEnvs = list('CloudVirt')
      .filter((e) => vmNamespacesUsing(e, name).length > 0)
      .map((e) => ({ kind: 'CloudVirt', name: e.metadata.name, detail: vmNamespacesUsing(e, name).join(', ') }));
    const circuits = list('AttachmentCircuit')
      .filter((c) => networkOf(c) === name)
      .map((c) => ({ kind: 'AttachmentCircuit', name: c.metadata.name }));
    return {
      sections: [...section('waitPlacements', placements), ...section('waitVmEnvironments', vmEnvs), ...section('waitCircuits', circuits)],
      realized: hasRealization,
    };
  }

  if (ENVIRONMENT_KINDS.includes(kind)) {
    const placements = list('NetworkPlacement')
      .filter((p) => {
        const b = backendOf(p);
        return b.kind === kind && b.name === name;
      })
      .map((p) => ({ kind: 'NetworkPlacement', name: p.metadata.name, detail: networkOf(p) }));
    const zone = (status.dns as { zone?: string } | undefined)?.zone || (status.domain as string | undefined);
    const zoneRows: PreviewEntry[] = zone ? [{ name: zone }] : [];

    if (kind === 'CloudVirt') {
      // No guard: the site namespaces are removed only once they hold no VMs (the delete waits while any VM
      // remains). The placements on it are not deleted: they wait for the environment and are deleted separately.
      const nsRows: PreviewEntry[] = ((status.namespaces as { name?: string; namespace?: string }[] | undefined) ?? [])
        .map((n) => ({ name: n.namespace || n.name || '' }))
        .filter((n) => n.name);
      return {
        sections: [...section('namespaces', nsRows), ...section('dnsZone', zoneRows), ...section('keptPlacements', placements)],
        realized: hasRealization || nsRows.length > 0,
      };
    }

    // OpenStack, AWS, Google Cloud: the delete waits for the placements and the clusters on the environment.
    const envField = kind === 'CloudOSO' ? 'openstack' : kind === 'CloudAWS' ? 'aws' : 'gcp';
    const clusters = list('PlatformOpenshift')
      .filter((c) => at(c.spec, `${envField}.environment`) === name)
      .map((c) => ({ kind: 'PlatformOpenshift', name: c.metadata.name }));
    // The cloud side: the OpenStack or Google Cloud project, or the AWS resources (by region).
    const project =
      kind === 'CloudOSO'
        ? (status.projectName as string | undefined)
        : kind === 'CloudGCP'
          ? (status.project as string | undefined)
          : status.awsAccountId
            ? ((status.awsRegion as string | undefined) ?? '')
            : undefined;
    const projectRows: PreviewEntry[] = project !== undefined && (project || kind === 'CloudAWS') ? [{ name: project, detail: kind }] : [];
    return {
      sections: [
        ...section('cloudProject', projectRows),
        ...section('dnsZone', zoneRows),
        ...section('waitPlacements', placements),
        ...section('waitClusters', clusters),
      ],
      realized: hasRealization || projectRows.length > 0,
    };
  }

  if (kind === 'NetworkPlacement') {
    const prefixes = names(status.prefixes);
    const site = (status.site as string | undefined) ?? '';
    const rows: PreviewEntry[] = prefixes.map((p) => ({ name: p, detail: site }));
    // On a VM environment the placement's delete waits until none of the environment's namespaces uses the network.
    const b = backendOf(item);
    const network = networkOf(item) ?? '';
    const env = b.kind === 'CloudVirt' ? list('CloudVirt').find((e) => e.metadata.name === b.name) : undefined;
    const using = env && network ? vmNamespacesUsing(env, network) : [];
    const waits: PreviewEntry[] = env && using.length ? [{ kind: 'CloudVirt', name: env.metadata.name, detail: using.join(', ') }] : [];
    return { sections: [...section('subnet', rows), ...section('waitSiteNamespace', waits)], realized: hasRealization || rows.length > 0 };
  }

  if (kind === 'Rbac') {
    const roles = (at(ctx.tenant?.spec, 'namespaceRbac') ?? {}) as Record<string, unknown>;
    const tenantRows: PreviewEntry[] = Object.entries(roles)
      .filter(([, v]) => names(v).includes(name))
      .map(([role]) => ({ name: role }));
    const tool: PreviewEntry[] = [];
    const lists: PreviewEntry[] = [];
    for (const k of ACCESS_GROUP_KINDS) {
      for (const obj of list(k)) {
        for (const f of ACCESS_GROUP_FIELDS[k] ?? []) {
          if (names(at(obj.spec, f.path)).includes(name)) {
            (f.section === 'toolRoles' ? tool : lists).push({ kind: k, name: obj.metadata.name, detail: lastKey(f.path) });
          }
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
