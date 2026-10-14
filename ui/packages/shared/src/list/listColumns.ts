import type { ReactNode } from 'react';
import type { TFunction } from 'i18next';
import type { HybridSovereignKind, K8sResource } from '../types/crds';

export type ListLinkMode = 'router' | 'anchor';

/** Marker objects returned by column renderers — interpreted by ResourceListTable. */
export type CellMarker =
  | { __kind: 'externalLink'; href: string }
  | { __kind: 'truncate'; text: string; max?: number }
  | { __kind: 'chips'; items: Array<{ label: string; color?: string }>; overflow?: number }
  | { __kind: 'chip'; label: string; color?: string }
  | { __kind: 'countChip'; count: number; tip?: string; label?: string }
  | { __kind: 'mono'; text: string }
  | { __kind: 'relativeTime'; label: string; title: string };

export type ListCellValue = ReactNode | CellMarker;

export interface ListColumnContext {
  kind: HybridSovereignKind;
  detailHref: (item: K8sResource) => string;
  showNamespace: boolean;
  t: TFunction;
  linkMode: ListLinkMode;
}

export interface ListColumnDef {
  id: string;
  /** i18n key under list.* or common.* / fields.* */
  labelKey: string;
  sortable?: boolean;
  /** Hide on tenant namespaced views when redundant */
  adminOnly?: boolean;
  getSortValue: (item: K8sResource) => string | number;
  getSearchText?: (item: K8sResource) => string;
  render: (item: K8sResource, ctx: ListColumnContext) => ListCellValue;
}

function specOf(item: K8sResource): Record<string, unknown> {
  return (item.spec ?? {}) as Record<string, unknown>;
}

function statusOf(item: K8sResource): Record<string, unknown> {
  return (item.status ?? {}) as Record<string, unknown>;
}

function asString(v: unknown): string {
  if (v == null || v === '') return '';
  return String(v);
}

function asStringArray(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String).filter(Boolean) : [];
}

function boolFlag(v: unknown): boolean {
  return v === true;
}

/** spec.cloudRef.name (CloudInfrastructure reference) */
function cloudRefName(item: K8sResource): string {
  const ref = specOf(item).cloudRef;
  if (ref && typeof ref === 'object') return asString((ref as Record<string, unknown>).name);
  return '';
}

function cloudRefColumn(): ListColumnDef {
  return {
    id: 'cloudRef',
    labelKey: 'list.cloudInfrastructure',
    sortable: true,
    getSortValue: (i) => cloudRefName(i),
    getSearchText: (i) => cloudRefName(i),
    render: (i) => cloudRefName(i) || '—',
  };
}

function chip(label: string, color = 'grey'): CellMarker {
  return { __kind: 'chip', label: label || '—', color };
}

function chips(
  items: Array<{ label: string; color?: string } | null | false>,
  overflow = 0,
): CellMarker {
  return {
    __kind: 'chips',
    items: items.filter(Boolean) as Array<{ label: string; color?: string }>,
    overflow,
  };
}

function countChip(count: number, tip: string, label: string): CellMarker {
  return { __kind: 'countChip', count, tip, label };
}

function truncate(text: string): CellMarker {
  return { __kind: 'truncate', text };
}

function mono(text: string): CellMarker {
  return { __kind: 'mono', text };
}

function externalLink(href: string): CellMarker {
  return { __kind: 'externalLink', href };
}

/** Relative age for interactive “Updated” cells (title keeps absolute). */
export function formatRelativeTime(iso?: string | null): { label: string; title: string } {
  if (!iso) return { label: '—', title: '' };
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return { label: iso, title: iso };
  const sec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  let label: string;
  if (sec < 60) label = `${sec}s ago`;
  else if (sec < 3600) label = `${Math.floor(sec / 60)}m ago`;
  else if (sec < 86400) label = `${Math.floor(sec / 3600)}h ago`;
  else label = `${Math.floor(sec / 86400)}d ago`;
  return { label, title: iso };
}

export function resourceSearchBlob(item: K8sResource, columns: ListColumnDef[]): string {
  const parts = [
    item.metadata.name,
    item.metadata.namespace ?? '',
    item.status?.status ?? '',
    item.status?.message ?? '',
    JSON.stringify(item.spec ?? {}),
  ];
  for (const col of columns) {
    if (col.getSearchText) parts.push(col.getSearchText(item));
  }
  return parts.join(' ').toLowerCase();
}

const BASE_NAME = 'name';
const BASE_NS = 'namespace';
const BASE_STATUS = 'status';
const BASE_UPDATED = 'updated';

/** Kind-specific columns inserted between name(/ns) and status. */
const KIND_EXTRA_COLUMNS: Partial<Record<HybridSovereignKind, ListColumnDef[]>> = {
  Entity: [
    {
      id: 'billing',
      labelKey: 'list.billingId',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).billingID),
      getSearchText: (i) => asString(specOf(i).billingID),
      render: (i) => asString(specOf(i).billingID) || '—',
    },
    {
      id: 'website',
      labelKey: 'list.website',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).websiteLink),
      getSearchText: (i) => asString(specOf(i).websiteLink),
      render: (i) => externalLink(asString(specOf(i).websiteLink)),
    },
    {
      id: 'description',
      labelKey: 'list.description',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).description),
      getSearchText: (i) => asString(specOf(i).description),
      render: (i) => truncate(asString(specOf(i).description)),
    },
  ],
  Team: [
    {
      id: 'features',
      labelKey: 'list.features',
      sortable: true,
      getSortValue: (i) => {
        const f = (specOf(i).features ?? {}) as Record<string, unknown>;
        return `${boolFlag(f.argo) ? 'argo' : ''}${boolFlag(f.istio) ? 'istio' : ''}`;
      },
      getSearchText: (i) => {
        const f = (specOf(i).features ?? {}) as Record<string, unknown>;
        return [boolFlag(f.argo) && 'argo', boolFlag(f.istio) && 'istio'].filter(Boolean).join(' ');
      },
      render: (i) => {
        const f = (specOf(i).features ?? {}) as Record<string, unknown>;
        return chips([
          boolFlag(f.argo) && { label: 'Argo', color: 'blue' },
          boolFlag(f.istio) && { label: 'Istio', color: 'purple' },
        ]);
      },
    },
    {
      id: 'admins',
      labelKey: 'list.admins',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).teamAdmin).length,
      getSearchText: (i) => asStringArray(specOf(i).teamAdmin).join(' '),
      render: (i) =>
        countChip(asStringArray(specOf(i).teamAdmin).length, asStringArray(specOf(i).teamAdmin).join(', '), 'admins'),
    },
    {
      id: 'rbacConfig',
      labelKey: 'list.rbacConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).rbacConfig),
      getSearchText: (i) => asString(specOf(i).rbacConfig),
      render: (i) => asString(specOf(i).rbacConfig) || '—',
    },
  ],
  Project: [
    {
      id: 'displayName',
      labelKey: 'list.displayName',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).displayName) || i.metadata.name,
      getSearchText: (i) => `${asString(specOf(i).displayName)} ${asString(specOf(i).description)}`,
      render: (i) => asString(specOf(i).displayName) || '—',
    },
    {
      id: 'description',
      labelKey: 'list.description',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).description),
      render: (i) => truncate(asString(specOf(i).description)),
    },
  ],
  Assignment: [
    {
      id: 'team',
      labelKey: 'list.team',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).team),
      getSearchText: (i) => asString(specOf(i).team),
      render: (i) => chip(asString(specOf(i).team) || '—', 'cyan'),
    },
    {
      id: 'projects',
      labelKey: 'list.projects',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).projects).length,
      getSearchText: (i) => asStringArray(specOf(i).projects).join(' '),
      render: (i) => {
        const projects = asStringArray(specOf(i).projects);
        return chips(
          projects.slice(0, 4).map((p) => ({ label: p, color: 'grey' })),
          Math.max(0, projects.length - 4),
        );
      },
    },
    {
      id: 'platform',
      labelKey: 'list.platform',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).openshift),
      getSearchText: (i) => asString(specOf(i).openshift),
      render: (i) => asString(specOf(i).openshift) || '—',
    },
  ],
  PlatformOpenshift: [
    {
      id: 'type',
      labelKey: 'list.provider',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).type),
      getSearchText: (i) => asString(specOf(i).type),
      render: (i) => chip(asString(specOf(i).type) || '—', 'blue'),
    },
    {
      id: 'environment',
      labelKey: 'list.environment',
      sortable: true,
      getSortValue: (i) => {
        const sec = (specOf(i)[asString(specOf(i).type)] ?? {}) as Record<string, unknown>;
        return asString(sec.environment);
      },
      getSearchText: (i) => {
        const sec = (specOf(i)[asString(specOf(i).type)] ?? {}) as Record<string, unknown>;
        return asString(sec.environment);
      },
      render: (i) => {
        const sec = (specOf(i)[asString(specOf(i).type)] ?? {}) as Record<string, unknown>;
        return asString(sec.environment) || '—';
      },
    },
    {
      id: 'topology',
      labelKey: 'list.nodes',
      sortable: true,
      getSortValue: (i) => {
        const os = (specOf(i).openstack ?? {}) as Record<string, unknown>;
        return Number(os.workerCount ?? 0) + Number(os.controlPlaneCount ?? 0);
      },
      getSearchText: (i) => {
        const os = (specOf(i).openstack ?? {}) as Record<string, unknown>;
        return `${asString(os.environment)} ${asString(os.workerFlavor)}`;
      },
      render: (i) => {
        const os = (specOf(i).openstack ?? {}) as Record<string, unknown>;
        if (!os.environment && os.workerCount == null) return '—';
        return truncate(
          [
            asString(os.environment),
            os.controlPlaneCount != null && `${os.controlPlaneCount} CP`,
            os.workerCount != null && `${os.workerCount} workers`,
          ]
            .filter(Boolean)
            .join(' · '),
        );
      },
    },
  ],
  CloudOSO: [
    cloudRefColumn(),
    {
      id: 'baseDomain',
      labelKey: 'list.baseDomain',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).baseDomain),
      getSearchText: (i) => asString(specOf(i).baseDomain),
      render: (i) => asString(specOf(i).baseDomain) || '—',
    },
    {
      id: 'landingzone',
      labelKey: 'list.landingZone',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).landingzone),
      render: (i) => asString(specOf(i).landingzone) || '—',
    },
    {
      id: 'extNet',
      labelKey: 'list.externalNetwork',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).externalNetwork),
      render: (i) => asString(specOf(i).externalNetwork) || '—',
    },
  ],
  CloudAWS: [
    cloudRefColumn(),
    {
      id: 'account',
      labelKey: 'list.account',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).account),
      getSearchText: (i) => asString(specOf(i).account),
      render: (i) => mono(asString(specOf(i).account)),
    },
    {
      id: 'baseDomain',
      labelKey: 'list.baseDomain',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).baseDomain),
      render: (i) => asString(specOf(i).baseDomain) || '—',
    },
    {
      id: 'landingzone',
      labelKey: 'list.landingZone',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).landingzone),
      render: (i) => asString(specOf(i).landingzone) || '—',
    },
  ],
  CloudVirt: [
    cloudRefColumn(),
    {
      id: 'baseDomain',
      labelKey: 'list.baseDomain',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).baseDomain),
      getSearchText: (i) => asString(specOf(i).baseDomain),
      render: (i) => asString(specOf(i).baseDomain) || '—',
    },
    {
      id: 'storageClass',
      labelKey: 'list.storageClass',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).storageClass),
      render: (i) => asString(specOf(i).storageClass) || '—',
    },
  ],
  OpenStackMigration: [
    {
      id: 'source',
      labelKey: 'list.source',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).source) || asString(statusOf(i).source),
      render: (i) => asString(specOf(i).source) || asString(statusOf(i).source) || '—',
    },
    {
      id: 'vm',
      labelKey: 'list.vmName',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).vmName) || asString(statusOf(i).vmName),
      render: (i) => asString(specOf(i).vmName) || asString(statusOf(i).vmName) || '—',
    },
    {
      id: 'cloudoso',
      labelKey: 'list.cloudoso',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).cloudoso) || asString(statusOf(i).cloudoso),
      render: (i) => asString(specOf(i).cloudoso) || asString(statusOf(i).cloudoso) || '—',
    },
  ],
  Persona: [
    {
      id: 'type',
      labelKey: 'list.personaType',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).type),
      render: (i) => chip(asString(specOf(i).type) || '—', 'orange'),
    },
    {
      id: 'rbac',
      labelKey: 'list.rbac',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).rbac),
      render: (i) => asString(specOf(i).rbac) || '—',
    },
  ],
  Rbac: [
    {
      id: 'config',
      labelKey: 'list.rbacConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).config),
      render: (i) => asString(specOf(i).config) || '—',
    },
    {
      id: 'group',
      labelKey: 'list.group',
      sortable: true,
      getSortValue: (i) => asString(statusOf(i).group),
      render: (i) => asString(statusOf(i).group) || '—',
    },
    {
      id: 'description',
      labelKey: 'list.description',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).description),
      render: (i) => truncate(asString(specOf(i).description)),
    },
  ],
  RbacConfig: [
    {
      id: 'type',
      labelKey: 'list.provider',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).type),
      render: (i) => asString(specOf(i).type) || '—',
    },
    {
      id: 'secret',
      labelKey: 'list.secret',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).secret),
      render: (i) => mono(asString(specOf(i).secret)),
    },
  ],
  Vault: [
    {
      id: 'ha',
      labelKey: 'list.ha',
      sortable: true,
      getSortValue: (i) => (boolFlag(specOf(i).ha) ? 1 : 0),
      render: (i) => chip(boolFlag(specOf(i).ha) ? 'HA' : 'standalone', boolFlag(specOf(i).ha) ? 'green' : 'grey'),
    },
    {
      id: 'rbacConfig',
      labelKey: 'list.rbacConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).rbacConfig),
      render: (i) => asString(specOf(i).rbacConfig) || '—',
    },
  ],
  VaultKV: [
    {
      id: 'vault',
      labelKey: 'list.vault',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).vault),
      render: (i) => asString(specOf(i).vault) || '—',
    },
    {
      id: 'admins',
      labelKey: 'list.admins',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).vaultAdminRbac).length,
      render: (i) =>
        countChip(
          asStringArray(specOf(i).vaultAdminRbac).length,
          asStringArray(specOf(i).vaultAdminRbac).join(', '),
          'admins',
        ),
    },
    {
      id: 'readers',
      labelKey: 'list.readers',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).vaultReaderRbac).length,
      render: (i) =>
        countChip(
          asStringArray(specOf(i).vaultReaderRbac).length,
          asStringArray(specOf(i).vaultReaderRbac).join(', '),
          'readers',
        ),
    },
  ],
  AAPOrg: [
    {
      id: 'config',
      labelKey: 'list.aapConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).aapConfig),
      render: (i) => asString(specOf(i).aapConfig) || '—',
    },
    {
      id: 'org',
      labelKey: 'list.orgName',
      sortable: true,
      getSortValue: (i) => asString(statusOf(i).orgName),
      render: (i) => asString(statusOf(i).orgName) || '—',
    },
    {
      id: 'admins',
      labelKey: 'list.admins',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).aapAdminRbac).length,
      render: (i) =>
        countChip(
          asStringArray(specOf(i).aapAdminRbac).length,
          asStringArray(specOf(i).aapAdminRbac).join(', '),
          'admins',
        ),
    },
  ],
  AAPConfig: [
    {
      id: 'secret',
      labelKey: 'list.secret',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).secret),
      render: (i) => mono(asString(specOf(i).secret)),
    },
    {
      id: 'rbacConfig',
      labelKey: 'list.rbacConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).rbacConfig),
      render: (i) => asString(specOf(i).rbacConfig) || '—',
    },
  ],
  QuayOrg: [
    {
      id: 'config',
      labelKey: 'list.quayConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).quayConfig),
      render: (i) => asString(specOf(i).quayConfig) || '—',
    },
    {
      id: 'org',
      labelKey: 'list.orgName',
      sortable: true,
      getSortValue: (i) => asString(statusOf(i).orgName),
      render: (i) => asString(statusOf(i).orgName) || '—',
    },
  ],
  QuayConfig: [
    {
      id: 'secret',
      labelKey: 'list.secret',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).secret),
      render: (i) => mono(asString(specOf(i).secret)),
    },
    {
      id: 'rbacConfig',
      labelKey: 'list.rbacConfig',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).rbacConfig),
      render: (i) => asString(specOf(i).rbacConfig) || '—',
    },
  ],
  CloudInfrastructure: [
    {
      id: 'type',
      labelKey: 'list.provider',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).type),
      getSearchText: (i) => asString(specOf(i).type),
      render: (i) => chip(asString(specOf(i).type) || '—', 'blue'),
    },
    {
      id: 'displayName',
      labelKey: 'list.displayName',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).displayName),
      getSearchText: (i) => asString(specOf(i).displayName),
      render: (i) => truncate(asString(specOf(i).displayName)),
    },
    {
      id: 'entities',
      labelKey: 'list.entities',
      sortable: true,
      getSortValue: (i) => (Array.isArray(specOf(i).entityRefs) ? (specOf(i).entityRefs as unknown[]).length : 0),
      getSearchText: (i) =>
        (Array.isArray(specOf(i).entityRefs) ? (specOf(i).entityRefs as Array<{ name?: string }>) : [])
          .map((r) => asString(r?.name))
          .join(' '),
      render: (i) => {
        const refs = (Array.isArray(specOf(i).entityRefs) ? (specOf(i).entityRefs as Array<{ name?: string }>) : [])
          .map((r) => asString(r?.name))
          .filter(Boolean);
        return refs.length ? countChip(refs.length, refs.join(', '), 'entities') : chip('all', 'grey');
      },
    },
    {
      id: 'capabilities',
      labelKey: 'list.capabilities',
      getSortValue: (i) => Object.values((statusOf(i).capabilities ?? {}) as Record<string, unknown>).filter(boolFlag).length,
      getSearchText: (i) => Object.keys((statusOf(i).capabilities ?? {}) as Record<string, unknown>).join(' '),
      render: (i) => {
        const caps = Object.entries((statusOf(i).capabilities ?? {}) as Record<string, unknown>)
          .filter(([, v]) => boolFlag(v))
          .map(([k]) => k);
        if (!caps.length) return '—';
        return chips(caps.slice(0, 3).map((c) => ({ label: c, color: 'green' })), Math.max(0, caps.length - 3));
      },
    },
  ],
  HybridFabric: [
    {
      id: 'enabled',
      labelKey: 'list.enabled',
      sortable: true,
      getSortValue: (i) => (boolFlag(specOf(i).enabled) ? 1 : 0),
      render: (i) =>
        chip(boolFlag(specOf(i).enabled) ? 'enabled' : 'disabled', boolFlag(specOf(i).enabled) ? 'green' : 'grey'),
    },
    {
      id: 'asn',
      labelKey: 'list.asn',
      sortable: true,
      getSortValue: (i) => Number(specOf(i).domainAsn ?? 0),
      render: (i) => asString(specOf(i).domainAsn) || '—',
    },
    {
      id: 'vni',
      labelKey: 'list.vniPool',
      sortable: true,
      getSortValue: (i) => {
        const p = (specOf(i).vniPool ?? {}) as Record<string, unknown>;
        return Number(p.start ?? 0);
      },
      render: (i) => {
        const p = (specOf(i).vniPool ?? {}) as Record<string, unknown>;
        if (p.start == null) return '—';
        return `${p.start}–${p.end ?? '?'}`;
      },
    },
    {
      id: 'underlay',
      labelKey: 'list.underlay',
      sortable: true,
      getSortValue: (i) => asString(((specOf(i).underlay ?? {}) as Record<string, unknown>).cidr),
      getSearchText: (i) => {
        const u = (specOf(i).underlay ?? {}) as Record<string, unknown>;
        return `${asString(u.type)} ${asString(u.cidr)}`;
      },
      render: (i) => {
        const u = (specOf(i).underlay ?? {}) as Record<string, unknown>;
        if (!u.cidr && !u.type) return '—';
        return chips([
          { label: asString(u.type) || 'ovn-layer2', color: 'purple' },
          u.cidr ? { label: asString(u.cidr), color: 'grey' } : null,
        ]);
      },
    },
  ],
  CloudGateway: [
    cloudRefColumn(),
    {
      id: 'transport',
      labelKey: 'list.tunnel',
      sortable: true,
      getSortValue: (i) => asString(((specOf(i).transport ?? {}) as Record<string, unknown>).type),
      render: (i) =>
        chip(asString(((specOf(i).transport ?? {}) as Record<string, unknown>).type) || 'default', 'purple'),
    },
    {
      id: 'fabric',
      labelKey: 'list.fabric',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).fabricRef),
      render: (i) => asString(specOf(i).fabricRef) || '—',
    },
  ],
  TransportLink: [
    {
      id: 'tunnel',
      labelKey: 'list.tunnel',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).tunnelType),
      render: (i) => chip(asString(specOf(i).tunnelType) || '—', 'purple'),
    },
    {
      id: 'fabric',
      labelKey: 'list.fabric',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).fabricRef),
      render: (i) => asString(specOf(i).fabricRef) || '—',
    },
    {
      id: 'gateway',
      labelKey: 'list.gateway',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).cloudGatewayRef),
      render: (i) => asString(specOf(i).cloudGatewayRef) || '—',
    },
  ],
  HybridNetwork: [
    {
      id: 'description',
      labelKey: 'list.description',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).description),
      render: (i) => truncate(asString(specOf(i).description)),
    },
    {
      id: 'viewers',
      labelKey: 'list.viewers',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).networkViewerRbac).length,
      render: (i) =>
        countChip(
          asStringArray(specOf(i).networkViewerRbac).length,
          asStringArray(specOf(i).networkViewerRbac).join(', '),
          'viewers',
        ),
    },
    {
      id: 'overlayMtu',
      labelKey: 'list.overlayMtu',
      sortable: true,
      getSortValue: (i) => Number(statusOf(i).overlayMtu ?? specOf(i).overlayMtu ?? 0),
      render: (i) => asString(statusOf(i).overlayMtu ?? specOf(i).overlayMtu) || '—',
    },
    {
      id: 'networkId',
      labelKey: 'list.networkId',
      sortable: true,
      getSortValue: (i) => asString(statusOf(i).networkId),
      render: (i) => mono(asString(statusOf(i).networkId)),
    },
  ],
  NetworkPlacement: [
    {
      id: 'network',
      labelKey: 'list.network',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).network),
      render: (i) => asString(specOf(i).network) || '—',
    },
    {
      id: 'backend',
      labelKey: 'list.backend',
      sortable: true,
      getSortValue: (i) => {
        const b = (specOf(i).backend ?? {}) as Record<string, unknown>;
        return `${asString(b.kind)}/${asString(b.name)}`;
      },
      getSearchText: (i) => {
        const b = (specOf(i).backend ?? {}) as Record<string, unknown>;
        return `${asString(b.kind)} ${asString(b.name)}`;
      },
      render: (i) => {
        const b = (specOf(i).backend ?? {}) as Record<string, unknown>;
        if (!b.kind) return '—';
        return chips([
          { label: asString(b.kind), color: 'blue' },
          { label: asString(b.name), color: 'grey' },
        ]);
      },
    },
    {
      id: 'prefixes',
      labelKey: 'list.prefixes',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).prefixes).length,
      getSearchText: (i) => asStringArray(specOf(i).prefixes).join(' '),
      render: (i) =>
        countChip(asStringArray(specOf(i).prefixes).length, asStringArray(specOf(i).prefixes).join(', '), 'CIDRs'),
    },
    {
      id: 'vmNamespaces',
      labelKey: 'list.vmNamespaces',
      sortable: true,
      getSortValue: (i) => asStringArray(specOf(i).vmNamespaces).length,
      getSearchText: (i) => asStringArray(specOf(i).vmNamespaces).join(' '),
      render: (i) => {
        const b = (specOf(i).backend ?? {}) as Record<string, unknown>;
        if (asString(b.kind) !== 'CloudVirt') return '—';
        const ns = asStringArray(specOf(i).vmNamespaces);
        return ns.length ? countChip(ns.length, ns.join(', '), 'namespaces') : chip('default', 'grey');
      },
    },
  ],
  UIHealthChecker: [
    {
      id: 'displayName',
      labelKey: 'list.displayName',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).displayName) || i.metadata.name,
      render: (i) => asString(specOf(i).displayName) || '—',
    },
    {
      id: 'group',
      labelKey: 'list.group',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).group),
      render: (i) => chip(asString(specOf(i).group) || '—', 'cyan'),
    },
    {
      id: 'url',
      labelKey: 'list.url',
      sortable: true,
      getSortValue: (i) => asString(specOf(i).url),
      getSearchText: (i) => asString(specOf(i).url),
      render: (i) => externalLink(asString(specOf(i).url)),
    },
  ],
};

function nameColumn(): ListColumnDef {
  return {
    id: BASE_NAME,
    labelKey: 'common.name',
    sortable: true,
    getSortValue: (i) => i.metadata.name,
    getSearchText: (i) => i.metadata.name,
    render: () => null,
  };
}

function namespaceColumn(): ListColumnDef {
  return {
    id: BASE_NS,
    labelKey: 'common.namespace',
    sortable: true,
    adminOnly: true,
    getSortValue: (i) => i.metadata.namespace ?? '',
    getSearchText: (i) => i.metadata.namespace ?? '',
    render: (i) => i.metadata.namespace ?? '—',
  };
}

function statusColumn(): ListColumnDef {
  return {
    id: BASE_STATUS,
    labelKey: 'common.status',
    sortable: true,
    getSortValue: (i) => i.status?.status ?? (i.status?.ready ? 'ready' : 'pending'),
    getSearchText: (i) => `${i.status?.status ?? ''} ${i.status?.message ?? ''}`,
    render: () => null,
  };
}

function updatedColumn(): ListColumnDef {
  return {
    id: BASE_UPDATED,
    labelKey: 'list.updated',
    sortable: true,
    getSortValue: (i) => Date.parse(i.status?.lastReconciledAt ?? i.metadata.creationTimestamp ?? '') || 0,
    getSearchText: (i) => i.status?.lastReconciledAt ?? '',
    render: (i): CellMarker => {
      const rel = formatRelativeTime(i.status?.lastReconciledAt ?? i.metadata.creationTimestamp);
      return { __kind: 'relativeTime', label: rel.label, title: rel.title };
    },
  };
}

export function getListColumns(
  kind: HybridSovereignKind,
  opts: { showNamespace: boolean },
): ListColumnDef[] {
  const extras = KIND_EXTRA_COLUMNS[kind] ?? [];
  const cols: ListColumnDef[] = [nameColumn()];
  if (opts.showNamespace) cols.push(namespaceColumn());
  cols.push(...extras, statusColumn(), updatedColumn());
  return cols.filter((c) => !c.adminOnly || opts.showNamespace);
}

/** Expandable detail rows — key facts beyond the table. */
export function getExpandRows(item: K8sResource): Array<{ label: string; value: string }> {
  const rows: Array<{ label: string; value: string }> = [];
  const msg = item.status?.message;
  if (msg) rows.push({ label: 'Message', value: msg });
  const conds = item.status?.conditions;
  if (Array.isArray(conds) && conds.length) {
    for (const c of conds.slice(0, 4)) {
      const cond = c as { type?: string; reason?: string; message?: string; status?: string };
      rows.push({
        label: cond.type || 'Condition',
        value: [cond.status, cond.reason, cond.message].filter(Boolean).join(' · '),
      });
    }
  }
  const created = item.metadata.creationTimestamp;
  if (created) rows.push({ label: 'Created', value: created });
  const uid = item.metadata.uid;
  if (uid) rows.push({ label: 'UID', value: uid });
  return rows;
}
