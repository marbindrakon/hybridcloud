/**
 * What deleting an object does (GT-F8, OD41), from live data. The sections
 * follow what each kind's delete really does:
 *
 *   deleted -> removed with the object
 *   waits   -> the delete waits (the object stays Deleting) until these are gone
 *   kept    -> not deleted with it; delete them separately
 *   affected -> not deleted with it, but changed by the delete
 *   access  -> access its members or other groups lose
 *
 *   network      -> waits for its placements, the VM environments whose
 *                   namespaces use it and its attachment circuits
 *   environment  -> OpenStack, AWS, Google Cloud: waits for its placements and
 *                   its clusters; its cloud project and DNS zone are deleted
 *                   VMs: its site namespaces are removed once they hold no VMs
 *                   (the VMs each namespace holds are listed from the status),
 *                   its DNS zone is deleted; its placements are kept
 *   placement    -> the subnet it holds on its site; on a VM environment it
 *                   waits until no namespace of that environment uses the network
 *   access group -> its group in the identity provider; the tenant roles, tool
 *                   roles and access lists naming it
 *   team setup   -> its sign-in groups and the access its lists grant; the
 *                   cluster assignments naming it and their app projects stay
 *   app project  -> its sign-in groups and the access its lists grant; the
 *                   cluster assignments listing it stay, and the access their
 *                   team setups give on it ends
 *   tool objects -> what the teardown removes in the tool: the Quay or AAP
 *                   organization, the KV engine and its secrets, the Vault
 *                   instance, the cluster, what an assignment put on a cluster
 *                   (with the ACM policy it recorded)
 *   tenant       -> every object of the tenant, tier by tier, then its namespace
 *
 * An object that owns realized resources (any kind whose teardown removes
 * state outside the API) asks for its name to be typed; plain objects keep the
 * one-click confirm. A kind with a teardown always says what goes with it.
 */
import { KIND_PLURALS, type K8sResource } from '../types';

export type PreviewGroup = 'deleted' | 'waits' | 'kept' | 'affected' | 'access';

export type PreviewSectionKey =
  // deleted with it
  | 'namespaces'
  | 'cloudProject'
  | 'dnsZone'
  | 'subnet'
  | 'external'
  | 'tenantObjects'
  | 'tenantNamespace'
  // the delete waits until these are gone
  | 'waitPlacements'
  | 'waitVmEnvironments'
  | 'waitCircuits'
  | 'waitClusters'
  | 'waitSiteNamespace'
  | 'waitAssignments'
  | 'waitProtected'
  | 'waitVms'
  // not deleted with it
  | 'keptPlacements'
  | 'keptSecretEngines'
  // not deleted with it, but changed by the delete
  | 'teamAssignments'
  | 'teamProjects'
  | 'projectAssignments'
  // access that ends
  | 'tenantRoles'
  | 'toolRoles'
  | 'accessLists'
  | 'scopeGrants'
  | 'projectTeams';

export const SECTION_GROUP: Record<PreviewSectionKey, PreviewGroup> = {
  namespaces: 'deleted',
  cloudProject: 'deleted',
  dnsZone: 'deleted',
  subnet: 'deleted',
  external: 'deleted',
  tenantObjects: 'deleted',
  tenantNamespace: 'deleted',
  waitPlacements: 'waits',
  waitVmEnvironments: 'waits',
  waitCircuits: 'waits',
  waitClusters: 'waits',
  waitSiteNamespace: 'waits',
  waitAssignments: 'waits',
  waitProtected: 'waits',
  waitVms: 'waits',
  keptPlacements: 'kept',
  keptSecretEngines: 'kept',
  teamAssignments: 'affected',
  teamProjects: 'affected',
  projectAssignments: 'affected',
  tenantRoles: 'access',
  toolRoles: 'access',
  accessLists: 'access',
  scopeGrants: 'access',
  projectTeams: 'access',
};

/** The order the dialog shows the groups in. */
export const PREVIEW_GROUPS: PreviewGroup[] = ['deleted', 'waits', 'kept', 'affected', 'access'];

export interface PreviewEntry {
  /** Kind of a linked object (absent for plain text entries) */
  kind?: string;
  name: string;
  /** A field or role key the entry is shown with */
  detail?: string;
  /** The kind of the object `detail` names (shown in words) */
  detailKind?: string;
  /** Values the entry's sentence is filled with (external entries) */
  args?: Record<string, string>;
  /** The object is in another namespace than the deleted one: shown without a link */
  noLink?: boolean;
  /** Names listed after the entry (the VMs of a site namespace) */
  items?: string[];
  /** How many more there are than `items` lists (0 when `items` is complete) */
  more?: number;
}

export interface PreviewSection {
  key: PreviewSectionKey;
  group: PreviewGroup;
  entries: PreviewEntry[];
  /** When the listed entries were read on the site (the VMs of an environment's namespaces) */
  asOf?: string;
}

export interface DeletePreview {
  sections: PreviewSection[];
  /** The object owns realized resources: the name must be typed to delete */
  realized: boolean;
  /**
   * The line under the name field (deletePreview.typeHelp.<key>); absent = the
   * general one, null = none (the sections already say what goes).
   */
  typeHelp?: string | null;
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

/**
 * The order a tenant's delete removes its objects in (the tenant cascade's
 * tiers, the CRDs' teardown-tier label); each tier is gone before the next
 * starts, then the tenant namespace. VM environments go with the placements:
 * a placement on one waits until the environment's namespaces are gone.
 */
export const TENANT_TEARDOWN_TIERS: string[][] = [
  ['Assignment', 'AttachmentCircuit', 'NetworkPlacement', 'OpenStackMigration', 'CloudVirt'],
  ['PlatformOpenshift'],
  ['CloudOSO', 'CloudAWS', 'CloudGCP'],
  ['HybridNetwork'],
  ['Team', 'Project', 'Persona', 'AAPOrg', 'QuayOrg', 'VaultKV'],
  ['Vault'],
  ['Rbac'],
];

/**
 * Kinds whose teardown removes state outside the API by definition (the
 * external object is the realization): their delete asks for the name.
 */
export const REALIZED_KINDS = new Set([
  'QuayOrg',
  'VaultKV',
  'AAPOrg',
  'Vault',
  'PlatformOpenshift',
  'Assignment',
  'Entity',
  'Rbac',
  'Team',
  'Project',
  'AAPConfig',
  'QuayConfig',
  'RbacConfig',
  'CloudGateway',
  'HybridFabric',
  'CloudInfrastructure',
]);

/** What the teardown of a kind removes, when nothing more precise is known (deletePreview.external.<key>). */
const EXTERNAL_FALLBACK: Record<string, string> = {
  HybridNetwork: 'networkFabric',
  NetworkPlacement: 'placementSite',
  AttachmentCircuit: 'circuit',
  CloudVirt: 'environmentResources',
  CloudOSO: 'environmentResources',
  CloudAWS: 'environmentResources',
  CloudGCP: 'environmentResources',
  Team: 'signInGroups',
  Project: 'signInGroups',
  AAPConfig: 'signInClient',
  QuayConfig: 'signInClient',
  RbacConfig: 'signInClient',
  CloudGateway: 'gateway',
  HybridFabric: 'fabric',
  CloudInfrastructure: 'site',
};

/** One list read the preview of `kind` needs: a kind in a namespace. */
export interface PreviewRead {
  kind: string;
  namespace: string;
}

/**
 * A kind the UI can list: it has an API path (plural). A dependant kind without one (an edge
 * connection) is not read, so a preview never asks for `<namespace>/undefined` and never says
 * that objects of that kind could not be read (H2TG-F4).
 */
export function listableKind(kind: string): boolean {
  return Object.prototype.hasOwnProperty.call(KIND_PLURALS, kind);
}

/** The lists the preview of `item` reads (all in the object's namespace), for kinds the UI can list. */
export function previewReads(kind: string, item: K8sResource): PreviewRead[] {
  return previewReadsAll(kind, item).filter((r) => listableKind(r.kind));
}

function previewReadsAll(kind: string, item: K8sResource): PreviewRead[] {
  const namespace = item.metadata.namespace ?? '';
  if (!namespace) return [];
  let kinds: string[] = [];
  if (kind === 'HybridNetwork') kinds = ['NetworkPlacement', 'CloudVirt', 'AttachmentCircuit'];
  else if (kind === 'CloudVirt') kinds = ['NetworkPlacement'];
  else if (ENVIRONMENT_KINDS.includes(kind)) kinds = ['NetworkPlacement', 'PlatformOpenshift'];
  else if (kind === 'NetworkPlacement') kinds = backendOf(item).kind === 'CloudVirt' ? ['CloudVirt'] : [];
  else if (kind === 'Rbac') kinds = ACCESS_GROUP_KINDS;
  else if (kind === 'Team' || kind === 'Project') kinds = ['Assignment'];
  else if (kind === 'PlatformOpenshift') kinds = ['Assignment', 'NetworkPlacement'];
  else if (kind === 'Vault') kinds = ['VaultKV'];
  // a tenant's objects live in its own namespace
  else if (kind === 'Entity') return TENANT_TEARDOWN_TIERS.flat().map((k) => ({ kind: k, namespace: `entity-${item.metadata.name}` }));
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
/**
 * The VMs each site namespace of a VM environment holds, from status.virtualMachines.namespaces
 * (names sorted and capped, count complete). Namespaces without VMs, and an environment whose
 * status does not report them, give none.
 */
type NamespaceVms = { name?: unknown; namespace?: unknown; count?: unknown; names?: unknown };
export function vmsByNamespace(status: Record<string, unknown>): PreviewEntry[] {
  const vms = status.virtualMachines as { namespaces?: unknown } | undefined;
  const list = vms && Array.isArray(vms.namespaces) ? (vms.namespaces as NamespaceVms[]) : [];
  return list
    .filter((n) => n && typeof n === 'object')
    .map((n) => {
      const items = names(n.names);
      const counted = typeof n.count === 'number' && Number.isFinite(n.count) ? n.count : items.length;
      const ns = (typeof n.namespace === 'string' && n.namespace) || (typeof n.name === 'string' && n.name) || '';
      return { name: ns, items, more: Math.max(counted, items.length) - items.length };
    })
    .filter((e) => e.name && (e.items.length > 0 || e.more > 0));
}

/** The Assignments of `team` (spec.team), not being deleted. */
function assignmentsOfTeam(list: K8sResource[], team: string): K8sResource[] {
  return list.filter((a) => (a.spec as { team?: string } | undefined)?.team === team && !a.metadata.deletionTimestamp);
}

/** The namespaces of a VM environment that use `network` (spec, as the network and placement deletes read it). */
function vmNamespacesUsing(env: K8sResource, network: string): string[] {
  return ((env.spec as { namespaces?: { name?: string; network?: string }[] } | undefined)?.namespaces ?? [])
    .filter((n) => n.network === network)
    .map((n) => n.name ?? '')
    .filter(Boolean);
}

function tenantPrefixed(item: K8sResource, status: Record<string, unknown>): string {
  const ns = item.metadata.namespace ?? '';
  const tenant = ns.startsWith('entity-') ? ns.slice('entity-'.length) : '';
  return (status.orgName as string | undefined) || (tenant ? `${tenant}-${item.metadata.name}` : item.metadata.name);
}

function external(key: string, name = '', args?: Record<string, string>): PreviewSection[] {
  return section('external', [{ name, detail: key, ...(args ? { args } : {}) }]);
}

/** What the delete of `item` removes, waits for and keeps. */
export function deletePreview(kind: string, item: K8sResource, ctx: PreviewContext = {}): DeletePreview {
  const p = kindPreview(kind, item, ctx);
  const fallback = EXTERNAL_FALLBACK[kind];
  // A kind with a teardown never reads as "nothing else is deleted".
  const sections = fallback && !p.sections.some((s) => s.group === 'deleted') ? [...external(fallback), ...p.sections] : p.sections;
  const out: DeletePreview = { sections, realized: p.realized || REALIZED_KINDS.has(kind) };
  if (p.typeHelp !== undefined) out.typeHelp = p.typeHelp;
  return out;
}

function kindPreview(kind: string, item: K8sResource, ctx: PreviewContext): DeletePreview {
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
      // PGT2-F2: the VMs each site namespace holds, including a namespace that left the spec but still holds VMs.
      const vmRows = vmsByNamespace(status);
      // GUI O-2: the list is a snapshot of the site; the dialog says when it was taken.
      const asOf = (status.virtualMachines as { observedAt?: unknown } | undefined)?.observedAt;
      const vmSections = section('waitVms', vmRows).map((s) => (typeof asOf === 'string' && asOf ? { ...s, asOf } : s));
      return {
        sections: [
          ...section('namespaces', nsRows),
          ...section('dnsZone', zoneRows),
          ...vmSections,
          ...section('keptPlacements', placements),
        ],
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
      sections: [
        ...external('accessGroup', (status.group as string | undefined) || name),
        ...section('tenantRoles', tenantRows),
        ...section('toolRoles', tool),
        ...section('accessLists', lists),
      ],
      realized: true,
    };
  }

  if (kind === 'Team' || kind === 'Project') {
    // OD47: the same dialog as networks, environments and access groups. The teardown releases the sign-in groups and
    // removes the roles its lists grant (on itself and, for a team setup, on its app projects and cluster assignments);
    // the cluster assignments and app projects themselves are not deleted.
    const rows: PreviewEntry[] = [];
    for (const f of ACCESS_GROUP_FIELDS[kind]) {
      for (const g of names(at(spec, f.path))) rows.push({ kind: 'Rbac', name: g, detail: lastKey(f.path) });
    }
    const assignments = list('Assignment').filter((a) => !a.metadata.deletionTimestamp);
    if (kind === 'Team') {
      const mine = assignmentsOfTeam(assignments, name);
      const asgRows = mine.map((a) => ({ kind: 'Assignment', name: a.metadata.name }));
      const projectNames = Array.from(new Set(mine.flatMap((a) => names((a.spec as { projects?: unknown } | undefined)?.projects)))).sort();
      const projectRows = projectNames.map((p) => ({ kind: 'Project', name: p }));
      return {
        sections: [
          ...external('signInGroups'),
          ...section('teamAssignments', asgRows),
          ...section('teamProjects', projectRows),
          ...section('scopeGrants', rows),
        ],
        realized: true,
        typeHelp: 'teamAccess',
      };
    }
    const listing = assignments.filter((a) => names((a.spec as { projects?: unknown } | undefined)?.projects).includes(name));
    const asgRows = listing.map((a) => ({ kind: 'Assignment', name: a.metadata.name }));
    const teams = Array.from(new Set(listing.map((a) => (a.spec as { team?: string } | undefined)?.team ?? '').filter(Boolean))).sort();
    const teamRows = teams.map((tm) => ({ kind: 'Team', name: tm }));
    return {
      sections: [
        ...external('signInGroups'),
        ...section('projectAssignments', asgRows),
        ...section('scopeGrants', rows),
        ...section('projectTeams', teamRows),
      ],
      realized: true,
      typeHelp: 'teamAccess',
    };
  }

  if (kind === 'QuayOrg') return { sections: external('quayOrg', tenantPrefixed(item, status)), realized: true };
  if (kind === 'AAPOrg') return { sections: external('aapOrg', tenantPrefixed(item, status)), realized: true };
  if (kind === 'VaultKV') {
    return {
      sections: external('kvEngine', (status.kvPath as string | undefined) || name, { vault: (spec.vault as string | undefined) ?? '' }),
      realized: true,
    };
  }
  if (kind === 'Vault') {
    const engines = list('VaultKV')
      .filter((kv) => (kv.spec as { vault?: string } | undefined)?.vault === name)
      .map((kv) => ({ kind: 'VaultKV', name: kv.metadata.name }));
    return { sections: [...external('vault', name), ...section('keptSecretEngines', engines)], realized: true };
  }
  if (kind === 'PlatformOpenshift') {
    // The cluster's delete waits for its assignments and the placements on it.
    const assignments = list('Assignment')
      .filter((a) => (a.spec as { openshift?: string } | undefined)?.openshift === name)
      .map((a) => ({ kind: 'Assignment', name: a.metadata.name }));
    const placements = list('NetworkPlacement')
      .filter((pl) => {
        const b = backendOf(pl);
        return b.kind === 'PlatformOpenshift' && b.name === name;
      })
      .map((pl) => ({ kind: 'NetworkPlacement', name: pl.metadata.name, detail: networkOf(pl), detailKind: 'HybridNetwork' }));
    return {
      sections: [...external('cluster', name), ...section('waitAssignments', assignments), ...section('waitPlacements', placements)],
      realized: true,
    };
  }
  if (kind === 'Assignment') {
    const cluster = (status.clusterName as string | undefined) || ((spec.openshift as string | undefined) ?? '');
    const projects = names(spec.projects).join(', ');
    // Only an assignment that reached its cluster put anything there: the same record the delete job reads
    // (provisioned, or a policy name recorded), even while its cluster is not Ready now.
    const provisioned = status.assignmentProvisioned;
    const delivered =
      provisioned === true || String(provisioned ?? '').toLowerCase() === 'true' || !!(status.acmPolicyName as string | undefined);
    // P3R-O1: one sentence; the name is still typed (the record can lag behind the cluster).
    if (!delivered) return { sections: external('assignmentNotDelivered'), realized: true, typeHelp: null };
    // The policy that put it there, as the provision job recorded it (OD48: tenant-prefixed names).
    const policy = ((status.acmPolicyName as string | undefined) ?? '').trim();
    const policyRows: PreviewEntry[] = policy ? [{ name: policy, detail: 'acmPolicy' }] : [];
    const first: PreviewEntry = !cluster
      ? { name: '', detail: 'assignmentUnnamedCluster' }
      : { name: projects, detail: projects ? 'assignment' : 'assignmentAccess', args: { cluster } };
    return { sections: section('external', [first, ...policyRows]), realized: true };
  }
  if (kind === 'Entity') {
    // The tenant's delete removes its objects tier by tier (protected ones stop it), then its namespace.
    const objectsRows: PreviewEntry[] = [];
    const protectedRows: PreviewEntry[] = [];
    TENANT_TEARDOWN_TIERS.forEach((tier, i) => {
      for (const k of tier) {
        for (const o of list(k)) {
          const row = { kind: k, name: o.metadata.name, detail: String(i + 1), noLink: true };
          ((o.spec as { deletionProtection?: boolean } | undefined)?.deletionProtection ? protectedRows : objectsRows).push(row);
        }
      }
    });
    return {
      sections: [
        ...section('tenantObjects', objectsRows),
        ...section('tenantNamespace', [{ name: `entity-${name}` }]),
        ...external('tenantSignIn'),
        ...section('waitProtected', protectedRows),
      ],
      realized: true,
    };
  }

  if (kind === 'AttachmentCircuit') return { sections: [], realized: hasRealization || !!status.realized || status.vlan !== undefined };

  return { sections: [], realized: hasRealization };
}
