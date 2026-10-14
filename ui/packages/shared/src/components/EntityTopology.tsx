import React, { useMemo, useRef, useState } from 'react';
import { Spinner, Alert, Button } from '@patternfly/react-core';
import {
  ExpandIcon,
  SearchPlusIcon,
  SearchMinusIcon,
  CompressAltIcon,
} from '@patternfly/react-icons';
import { HybridSovereignKind, K8sResource } from '../types';
import { useK8sResourceList } from '../hooks/k8s';
import { useOverviewCRs } from '../hooks/overviewCrs';
import { useCanListKind } from '../hooks/permissions';
import { normalizeHealth } from './StatusBadge';

export interface TopologyNode {
  id: string;
  label: string;
  kind: HybridSovereignKind | 'Group';
  status: 'ready' | 'pending' | 'failed' | 'reconciling' | 'unknown';
  namespace?: string;
  x?: number;
  y?: number;
}

export interface TopologyEdge {
  id: string;
  from: string;
  to: string;
  failed?: boolean;
}

export interface EntityTopologyProps {
  entityNamespace?: string;
  filterByPermissions?: boolean;
  /** Pre-fetched Overview CRs (avoids a second aggregate fetch on admin Overview). */
  resources?: K8sResource[];
  resourcesLoading?: boolean;
  resourcesError?: Error | null;
}

function itemsOfKind(items: K8sResource[], kind: HybridSovereignKind): K8sResource[] {
  return items.filter((i) => (i.kind || '') === kind);
}

/** spec.cloudRef.name — the CloudInfrastructure a cloud project or gateway sits on. */
function cloudRefOf(item: K8sResource): string {
  const ref = (item.spec as { cloudRef?: { name?: string } } | undefined)?.cloudRef;
  return typeof ref?.name === 'string' ? ref.name : '';
}

/** PlatformOpenshift type → kind of the cloud project named by spec.<type>.environment */
const PLATFORM_ENV_KIND: Record<string, HybridSovereignKind> = {
  hosted: 'CloudVirt',
  openstack: 'CloudOSO',
  aws: 'CloudAWS',
};

const COL_GAP = 200;
const ROW_GAP = 64;
const NODE_W = 148;
const NODE_H = 44;
const PAD_X = 24;
const PAD_Y = 36;

function statusFromReady(ready?: boolean, status?: string): TopologyNode['status'] {
  return normalizeHealth(ready, status);
}

function statusColor(status: TopologyNode['status']): string {
  if (status === 'ready') return 'var(--sc-success, #3e8635)';
  if (status === 'failed') return 'var(--sc-danger, #c9190b)';
  if (status === 'pending' || status === 'reconciling') return 'var(--sc-warning, #f0ab00)';
  return 'var(--sc-text-muted, #6a6e73)';
}

function layoutColumns(columns: TopologyNode[][]): {
  nodes: TopologyNode[];
  width: number;
  height: number;
} {
  const placed: TopologyNode[] = [];
  let maxH = 0;
  columns.forEach((col, ci) => {
    col.forEach((node, ri) => {
      placed.push({
        ...node,
        x: PAD_X + ci * COL_GAP,
        y: PAD_Y + ri * ROW_GAP,
      });
    });
    maxH = Math.max(maxH, col.length);
  });
  return {
    nodes: placed,
    width: PAD_X * 2 + Math.max(columns.length - 1, 0) * COL_GAP + NODE_W,
    height: PAD_Y * 2 + Math.max(maxH - 1, 0) * ROW_GAP + NODE_H,
  };
}

function edgePath(
  from: TopologyNode,
  to: TopologyNode,
): string {
  const x1 = (from.x ?? 0) + NODE_W;
  const y1 = (from.y ?? 0) + NODE_H / 2;
  const x2 = to.x ?? 0;
  const y2 = (to.y ?? 0) + NODE_H / 2;
  const mx = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
}

/** Live entity topology — layered SVG graph matching design-assets */
export function EntityTopology({
  entityNamespace,
  filterByPermissions = false,
  resources,
  resourcesLoading,
  resourcesError,
}: EntityTopologyProps): React.ReactElement {
  const ns = entityNamespace ?? '';
  const scoped = !!entityNamespace;
  // Admin platform view: never hit cluster-scoped list URLs (console proxy 404s).
  const useParentResources = !scoped && resources !== undefined;
  const overview = useOverviewCRs(0, { enabled: !scoped && !useParentResources });

  const teams = useK8sResourceList<K8sResource>('Team', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const projects = useK8sResourceList<K8sResource>('Project', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const assignments = useK8sResourceList<K8sResource>('Assignment', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const platforms = useK8sResourceList<K8sResource>('PlatformOpenshift', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const cloudOso = useK8sResourceList<K8sResource>('CloudOSO', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const cloudAws = useK8sResourceList<K8sResource>('CloudAWS', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  const cloudVirt = useK8sResourceList<K8sResource>('CloudVirt', {
    namespace: entityNamespace,
    enabled: scoped,
  });
  // Tenants usually cannot list sovereign-cloud; infra nodes then come from cloudRef names alone.
  const cloudInfra = useK8sResourceList<K8sResource>('CloudInfrastructure', {
    namespace: 'sovereign-cloud',
    enabled: scoped,
  });

  const aggregateItems = useParentResources ? resources ?? [] : overview.items;
  const entityItems = scoped ? [] : itemsOfKind(aggregateItems, 'Entity');
  const teamItems = scoped ? teams.items : itemsOfKind(aggregateItems, 'Team');
  const projectItems = scoped ? projects.items : itemsOfKind(aggregateItems, 'Project');
  const assignmentItems = scoped ? assignments.items : itemsOfKind(aggregateItems, 'Assignment');
  const platformItems = scoped
    ? platforms.items
    : itemsOfKind(aggregateItems, 'PlatformOpenshift');
  const cloudOsoItems = scoped ? cloudOso.items : itemsOfKind(aggregateItems, 'CloudOSO');
  const cloudAwsItems = scoped ? cloudAws.items : itemsOfKind(aggregateItems, 'CloudAWS');
  const cloudVirtItems = scoped ? cloudVirt.items : itemsOfKind(aggregateItems, 'CloudVirt');
  const cloudInfraItems = scoped
    ? cloudInfra.error
      ? []
      : cloudInfra.items
    : itemsOfKind(aggregateItems, 'CloudInfrastructure');
  const fabricItems = scoped ? [] : itemsOfKind(aggregateItems, 'HybridFabric');
  const gatewayItems = scoped ? [] : itemsOfKind(aggregateItems, 'CloudGateway');

  const permNs = ns || 'sovereign-cloud';
  const teamPerm = useCanListKind(permNs, 'Team', { enabled: filterByPermissions });
  const projectPerm = useCanListKind(permNs, 'Project', { enabled: filterByPermissions });
  const assignmentPerm = useCanListKind(permNs, 'Assignment', { enabled: filterByPermissions });
  const platformPerm = useCanListKind(permNs, 'PlatformOpenshift', { enabled: filterByPermissions });
  const cloudOsoPerm = useCanListKind(permNs, 'CloudOSO', { enabled: filterByPermissions });
  const cloudAwsPerm = useCanListKind(permNs, 'CloudAWS', { enabled: filterByPermissions });
  const cloudVirtPerm = useCanListKind(permNs, 'CloudVirt', { enabled: filterByPermissions });

  const [zoom, setZoom] = useState(1);
  const wrapRef = useRef<HTMLDivElement>(null);

  const graph = useMemo(() => {
    const allow = (ok: boolean) => !filterByPermissions || ok;
    const toNode = (item: K8sResource, kind: HybridSovereignKind): TopologyNode => ({
      id: `${kind}/${item.metadata.namespace ?? ''}/${item.metadata.name}`,
      label: item.metadata.name,
      kind,
      namespace: item.metadata.namespace,
      status: statusFromReady(item.status?.ready, item.status?.status),
    });

    type AssignSpec = {
      team?: string;
      projects?: string[];
      openshift?: string;
      aws?: string;
      cloudoso?: string;
    };

    const entityNodes: TopologyNode[] = entityNamespace
      ? [
          {
            id: `Entity/${entityNamespace}`,
            label: entityNamespace.replace(/^entity-/, ''),
            kind: 'Entity',
            status: 'ready',
            namespace: entityNamespace,
          },
        ]
      : entityItems.map((e) => toNode(e, 'Entity'));

    const teamNodes = allow(teamPerm.allowed) ? teamItems.map((i) => toNode(i, 'Team')) : [];
    const projectNodes = allow(projectPerm.allowed)
      ? projectItems.map((i) => toNode(i, 'Project'))
      : [];
    const assignmentNodes = allow(assignmentPerm.allowed)
      ? assignmentItems.map((i) => toNode(i, 'Assignment'))
      : [];
    const platformNodes = allow(platformPerm.allowed)
      ? platformItems.map((i) => toNode(i, 'PlatformOpenshift'))
      : [];
    const visibleClouds: Array<{ item: K8sResource; kind: HybridSovereignKind }> = [
      ...(allow(cloudOsoPerm.allowed) ? cloudOsoItems.map((item) => ({ item, kind: 'CloudOSO' as const })) : []),
      ...(allow(cloudAwsPerm.allowed) ? cloudAwsItems.map((item) => ({ item, kind: 'CloudAWS' as const })) : []),
      ...(allow(cloudVirtPerm.allowed) ? cloudVirtItems.map((item) => ({ item, kind: 'CloudVirt' as const })) : []),
    ];
    const cloudNodes = visibleClouds.map(({ item, kind }) => toNode(item, kind));
    const fabricNodes = fabricItems.map((i) => toNode(i, 'HybridFabric'));

    // Site nodes: CloudInfrastructure objects, plus any cloudRef name we cannot read (tenant view).
    const infraNodes = cloudInfraItems.map((i) => toNode(i, 'CloudInfrastructure'));
    const infraNames = new Set(infraNodes.map((n) => n.label));
    const referencedInfra = [
      ...visibleClouds.map(({ item }) => cloudRefOf(item)),
      ...gatewayItems.map(cloudRefOf),
    ].filter(Boolean);
    for (const infraName of referencedInfra) {
      if (infraNames.has(infraName)) continue;
      infraNames.add(infraName);
      infraNodes.push({
        id: `CloudInfrastructure/sovereign-cloud/${infraName}`,
        label: infraName,
        kind: 'CloudInfrastructure',
        namespace: 'sovereign-cloud',
        status: 'unknown',
      });
    }

    // Build edges ONLY from real CR references (no mesh heuristics)
    const edgePairs: Array<{ fromKind: string; fromName: string; toKind: string; toName: string; failed?: boolean }> =
      [];

    for (const a of assignmentItems) {
      if (!allow(assignmentPerm.allowed)) break;
      const spec = (a.spec || {}) as AssignSpec;
      const failed = statusFromReady(a.status?.ready, a.status?.status) === 'failed';
      if (spec.team) {
        edgePairs.push({ fromKind: 'Team', fromName: spec.team, toKind: 'Assignment', toName: a.metadata.name, failed });
      }
      for (const pName of spec.projects ?? []) {
        edgePairs.push({
          fromKind: 'Project',
          fromName: pName,
          toKind: 'Assignment',
          toName: a.metadata.name,
          failed,
        });
      }
      if (spec.openshift) {
        edgePairs.push({
          fromKind: 'PlatformOpenshift',
          fromName: spec.openshift,
          toKind: 'Assignment',
          toName: a.metadata.name,
          failed,
        });
      }
      if (spec.aws) {
        edgePairs.push({
          fromKind: 'CloudAWS',
          fromName: spec.aws,
          toKind: 'Assignment',
          toName: a.metadata.name,
          failed,
        });
      }
      if (spec.cloudoso) {
        edgePairs.push({
          fromKind: 'CloudOSO',
          fromName: spec.cloudoso,
          toKind: 'Assignment',
          toName: a.metadata.name,
          failed,
        });
      }
    }

    // HybridFabric → CloudInfrastructure through each CloudGateway (fabricRef + cloudRef)
    for (const g of gatewayItems) {
      const fabricName = (g.spec as { fabricRef?: string } | undefined)?.fabricRef;
      const infraName = cloudRefOf(g);
      if (!fabricName || !infraName) continue;
      edgePairs.push({
        fromKind: 'HybridFabric',
        fromName: fabricName,
        toKind: 'CloudInfrastructure',
        toName: infraName,
        failed: statusFromReady(g.status?.ready, g.status?.status) === 'failed',
      });
    }

    // CloudInfrastructure → tenant cloud project (spec.cloudRef)
    for (const { item, kind } of visibleClouds) {
      const infraName = cloudRefOf(item);
      if (!infraName) continue;
      edgePairs.push({
        fromKind: 'CloudInfrastructure',
        fromName: infraName,
        toKind: kind,
        toName: item.metadata.name,
        failed: statusFromReady(item.status?.ready, item.status?.status) === 'failed',
      });
    }

    // Cloud project → PlatformOpenshift (spec.<type>.environment)
    for (const p of platformItems) {
      const spec = (p.spec ?? {}) as { type?: string } & Record<string, unknown>;
      const cloudKind = PLATFORM_ENV_KIND[spec.type ?? ''];
      const env = (spec[spec.type ?? ''] as { environment?: string } | undefined)?.environment;
      if (!cloudKind || !env) continue;
      if (!visibleClouds.some((c) => c.kind === cloudKind && c.item.metadata.name === env)) continue;
      edgePairs.push({
        fromKind: cloudKind,
        fromName: env,
        toKind: 'PlatformOpenshift',
        toName: p.metadata.name,
        failed: statusFromReady(p.status?.ready, p.status?.status) === 'failed',
      });
    }

    // Entity → Team / Entity → Platform only for scoped ownership (namespace membership)
    if (entityNamespace && entityNodes[0]) {
      for (const t of teamNodes) {
        edgePairs.push({
          fromKind: 'Entity',
          fromName: entityNodes[0].label,
          toKind: 'Team',
          toName: t.label,
          failed: t.status === 'failed',
        });
      }
    } else {
      for (const e of entityNodes) {
        for (const p of platformNodes) {
          if (p.namespace === `entity-${e.label}`) {
            edgePairs.push({
              fromKind: 'Entity',
              fromName: e.label,
              toKind: 'PlatformOpenshift',
              toName: p.label,
              failed: p.status === 'failed',
            });
          }
        }
        for (const a of assignmentNodes) {
          if (a.namespace === `entity-${e.label}`) {
            // Prefer linking entity → assignment only when no team edge exists
            const hasTeam = edgePairs.some(
              (ep) => ep.toKind === 'Assignment' && ep.toName === a.label && ep.fromKind === 'Team',
            );
            if (!hasTeam) {
              edgePairs.push({
                fromKind: 'Entity',
                fromName: e.label,
                toKind: 'Assignment',
                toName: a.label,
                failed: a.status === 'failed',
              });
            }
          }
        }
      }
    }

    // Only keep nodes that are entities/teams/projects OR appear in an edge
    const referenced = new Set<string>();
    for (const ep of edgePairs) {
      referenced.add(`${ep.fromKind}:${ep.fromName}`);
      referenced.add(`${ep.toKind}:${ep.toName}`);
    }
    const keep = (n: TopologyNode) =>
      n.kind === 'Entity' ||
      (!entityNamespace && (n.kind === 'HybridFabric' || n.kind === 'CloudInfrastructure')) ||
      referenced.has(`${n.kind}:${n.label}`);

    const filteredTeams = teamNodes.filter(keep);
    const filteredProjects = projectNodes.filter(keep);
    const filteredAssignments = assignmentNodes.filter(keep);
    const filteredPlatforms = platformNodes.filter(keep);
    const filteredClouds = cloudNodes.filter(keep);
    const filteredFabrics = fabricNodes.filter(keep);
    const filteredInfra = infraNodes.filter(keep);

    // Order columns by dependency flow; sort rows to reduce crossings
    const teamOrder = new Map(filteredTeams.map((t, i) => [t.label, i]));
    filteredAssignments.sort((a, b) => {
      const aTeam = edgePairs.find((e) => e.toName === a.label && e.fromKind === 'Team')?.fromName;
      const bTeam = edgePairs.find((e) => e.toName === b.label && e.fromKind === 'Team')?.fromName;
      return (teamOrder.get(aTeam ?? '') ?? 99) - (teamOrder.get(bTeam ?? '') ?? 99);
    });
    filteredProjects.sort((a, b) => {
      const aTeam = edgePairs.find(
        (e) => e.fromName === a.label && e.fromKind === 'Project',
      );
      const bTeam = edgePairs.find(
        (e) => e.fromName === b.label && e.fromKind === 'Project',
      );
      const aAssign = aTeam?.toName;
      const bAssign = bTeam?.toName;
      return (aAssign ?? '').localeCompare(bAssign ?? '');
    });

    const columns: TopologyNode[][] = (
      entityNamespace
        ? [
            entityNodes,
            filteredTeams,
            filteredProjects,
            filteredInfra,
            filteredClouds,
            filteredPlatforms,
            filteredAssignments,
          ]
        : [filteredFabrics, filteredInfra, entityNodes, filteredClouds, filteredPlatforms, filteredAssignments]
    ).filter((c) => c.length > 0);

    const { nodes, width, height } = layoutColumns(columns);
    const byKindName = new Map(nodes.map((n) => [`${n.kind}:${n.label}`, n]));

    const edges: TopologyEdge[] = [];
    const seen = new Set<string>();
    for (const ep of edgePairs) {
      const from = byKindName.get(`${ep.fromKind}:${ep.fromName}`);
      const to = byKindName.get(`${ep.toKind}:${ep.toName}`);
      if (!from || !to) continue;
      const id = `${from.id}->${to.id}`;
      if (seen.has(id)) continue;
      seen.add(id);
      edges.push({ id, from: from.id, to: to.id, failed: ep.failed });
    }

    return { nodes, edges, width: Math.max(width, 640), height: Math.max(height, 200) };
  }, [
    entityNamespace,
    entityItems,
    teamItems,
    projectItems,
    assignmentItems,
    platformItems,
    cloudOsoItems,
    cloudAwsItems,
    cloudVirtItems,
    cloudInfraItems,
    fabricItems,
    gatewayItems,
    filterByPermissions,
    teamPerm.allowed,
    projectPerm.allowed,
    assignmentPerm.allowed,
    platformPerm.allowed,
    cloudOsoPerm.allowed,
    cloudAwsPerm.allowed,
    cloudVirtPerm.allowed,
  ]);

  const loading = scoped
    ? teams.loading ||
      projects.loading ||
      assignments.loading ||
      platforms.loading ||
      cloudOso.loading ||
      cloudAws.loading ||
      cloudVirt.loading
    : useParentResources
      ? !!resourcesLoading
      : overview.loading;

  const listError = scoped
    ? teams.error ||
      projects.error ||
      assignments.error ||
      platforms.error ||
      cloudOso.error ||
      cloudAws.error ||
      cloudVirt.error
    : useParentResources
      ? resourcesError ?? null
      : overview.error;

  if (loading && graph.nodes.length === 0) {
    return <Spinner aria-label="Loading topology" />;
  }

  if (listError && graph.nodes.length === 0) {
    return (
      <Alert variant="warning" title="Unable to load topology" isInline>
        {listError.message || 'Failed to list Hybrid Sovereign resources.'}
      </Alert>
    );
  }

  if (graph.nodes.length === 0) {
    return (
      <Alert variant="info" title="No topology data" isInline>
        {filterByPermissions
          ? `No resources visible for your access in ${
              entityNamespace ? `namespace ${entityNamespace}` : 'the platform'
            }.`
          : `No Hybrid Sovereign resources found for ${
              entityNamespace ? `namespace ${entityNamespace}` : 'the platform'
            }.`}
      </Alert>
    );
  }

  const fit = () => {
    const el = wrapRef.current;
    if (!el) return;
    const scale = Math.min(1, (el.clientWidth - 16) / graph.width);
    setZoom(Math.max(0.5, scale));
  };

  return (
    <div className="sc-topology-graph" aria-label="Entity topology">
      <div className="sc-topology-graph__toolbar">
        <div className="sc-topo-legend">
          <span>
            <i className="sc-dot sc-dot--ready" /> Healthy
          </span>
          <span>
            <i className="sc-dot sc-dot--failed" /> Failed
          </span>
          <span>
            <i className="sc-dot sc-dot--pending" /> Pending
          </span>
        </div>
        <div className="sc-topology-graph__controls">
          <Button variant="plain" size="sm" aria-label="Zoom in" onClick={() => setZoom((z) => Math.min(1.5, z + 0.1))}>
            <SearchPlusIcon />
          </Button>
          <Button variant="plain" size="sm" aria-label="Zoom out" onClick={() => setZoom((z) => Math.max(0.5, z - 0.1))}>
            <SearchMinusIcon />
          </Button>
          <Button variant="secondary" size="sm" icon={<CompressAltIcon />} onClick={fit}>
            Fit to view
          </Button>
          <Button
            variant="plain"
            size="sm"
            aria-label="Fullscreen"
            onClick={() => wrapRef.current?.requestFullscreen?.()}
          >
            <ExpandIcon />
          </Button>
        </div>
      </div>
      <div className="sc-topology-graph__viewport" ref={wrapRef}>
        <svg
          width={graph.width * zoom}
          height={graph.height * zoom}
          viewBox={`0 0 ${graph.width} ${graph.height}`}
          className="sc-topology-svg"
        >
          <defs>
            <marker
              id="sc-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#6a6e73" />
            </marker>
            <marker
              id="sc-arrow-failed"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="6"
              markerHeight="6"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#c9190b" />
            </marker>
          </defs>
          {graph.edges.map((e) => {
            const from = graph.nodes.find((n) => n.id === e.from);
            const to = graph.nodes.find((n) => n.id === e.to);
            if (!from || !to) return null;
            return (
              <path
                key={e.id}
                d={edgePath(from, to)}
                className={e.failed ? 'sc-topology-edge sc-topology-edge--failed' : 'sc-topology-edge'}
                markerEnd={e.failed ? 'url(#sc-arrow-failed)' : 'url(#sc-arrow)'}
              />
            );
          })}
          {graph.nodes.map((n) => (
            <g key={n.id} transform={`translate(${n.x}, ${n.y})`} className={`sc-topology-node sc-topology-node--${n.status}`}>
              <rect width={NODE_W} height={NODE_H} rx={3} ry={3} />
              <circle cx={12} cy={NODE_H / 2} r={4} fill={statusColor(n.status)} />
              <text x={22} y={18} className="sc-topology-node__kind">
                {n.kind}
              </text>
              <text x={22} y={34} className="sc-topology-node__name">
                {n.label.length > 16 ? `${n.label.slice(0, 15)}…` : n.label}
              </text>
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}
