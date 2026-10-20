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
  openstack: 'CloudOSO',
  aws: 'CloudAWS',
  gcp: 'CloudGCP',
};

/** Cluster-scoped-ish kinds that genuinely live in one shared namespace. */
const PLATFORM_SCOPED_KINDS = new Set<string>(['HybridFabric', 'CloudInfrastructure']);

/**
 * Identity of a node in the graph. Tenant CRs live in `entity-<tenant>`
 * namespaces and their names are NOT globally unique (e.g. `local-virt` exists
 * in both `entity-acme-corp` and `entity-chad`), so every lookup has to be
 * keyed on namespace + kind + name — never on the bare name, and never on
 * array position.
 */
function nodeKey(kind: string, namespace: string | undefined, name: string): string {
  return `${kind}|${namespace ?? ''}|${name}`;
}

/** The entity that owns a tenant CR: its `.status.entity`, else `entity-<x>` namespace. */
function entityOf(item: K8sResource): string {
  const fromStatus = (item.status as { entity?: unknown } | undefined)?.entity;
  if (typeof fromStatus === 'string' && fromStatus) return fromStatus;
  const ns = item.metadata.namespace ?? '';
  return ns.startsWith('entity-') ? ns.slice('entity-'.length) : '';
}

const NODE_W = 150;
const NODE_H = 46;
const COL_GAP = 22; // horizontal gap between sibling nodes in a layer
const LAYER_GAP = 62; // vertical gap between layers
const PAD_X = 20;
const PAD_Y = 16;
const LAYER_LABEL_H = 16;

function statusFromReady(ready?: boolean, status?: string): TopologyNode['status'] {
  return normalizeHealth(ready, status);
}

function statusColor(status: TopologyNode['status']): string {
  if (status === 'ready') return 'var(--sc-success, #3e8635)';
  if (status === 'failed') return 'var(--sc-danger, #c9190b)';
  if (status === 'pending' || status === 'reconciling') return 'var(--sc-warning, #f0ab00)';
  return 'var(--sc-text-muted, #6a6e73)';
}

export interface TopologyLayer {
  title: string;
  y: number;
}

/**
 * Top-down layered layout: one layer per row, flowing downward, matching the
 * layered look of the hybrid network topology diagram. Each layer is centred on
 * a common content width so parents sit above their children.
 */
function layoutLayers(layers: Array<{ title: string; nodes: TopologyNode[] }>): {
  nodes: TopologyNode[];
  rows: TopologyLayer[];
  width: number;
  height: number;
} {
  const widest = Math.max(1, ...layers.map((l) => l.nodes.length));
  const contentW = widest * NODE_W + (widest - 1) * COL_GAP;

  const placed: TopologyNode[] = [];
  const rows: TopologyLayer[] = [];
  let y = PAD_Y;

  for (const layer of layers) {
    const n = layer.nodes.length;
    const rowW = n * NODE_W + Math.max(n - 1, 0) * COL_GAP;
    const startX = PAD_X + (contentW - rowW) / 2;
    rows.push({ title: layer.title, y });
    const nodeY = y + LAYER_LABEL_H;
    layer.nodes.forEach((node, i) => {
      placed.push({ ...node, x: startX + i * (NODE_W + COL_GAP), y: nodeY });
    });
    y = nodeY + NODE_H + LAYER_GAP;
  }

  return {
    nodes: placed,
    rows,
    width: PAD_X * 2 + contentW,
    height: y - LAYER_GAP + PAD_Y,
  };
}

/** Vertical cubic connector: leaves the bottom of `from`, enters the top of `to`. */
function edgePath(from: TopologyNode, to: TopologyNode): string {
  const x1 = (from.x ?? 0) + NODE_W / 2;
  const y1 = (from.y ?? 0) + NODE_H;
  const x2 = (to.x ?? 0) + NODE_W / 2;
  const y2 = to.y ?? 0;
  const my = (y1 + y2) / 2;
  return `M ${x1} ${y1} C ${x1} ${my}, ${x2} ${my}, ${x2} ${y2}`;
}

/**
 * Status as a drawn shape as well as a colour, so the legend survives
 * greyscale, print and forced-colors mode.
 */
function StatusGlyph({ status, x, y }: { status: TopologyNode['status']; x: number; y: number }) {
  return (
    <g transform={`translate(${x}, ${y})`} className="sc-topology-node__glyph">
      <circle cx={0} cy={0} r={5.5} fill={statusColor(status)} />
      {status === 'ready' && <path d="M -2.6 0 L -0.8 2 L 2.8 -2" className="sc-topology-node__mark" />}
      {status === 'failed' && (
        <path d="M -2.2 -2.2 L 2.2 2.2 M 2.2 -2.2 L -2.2 2.2" className="sc-topology-node__mark" />
      )}
      {(status === 'pending' || status === 'reconciling') && (
        <path d="M 0 -2.8 L 0 0 L 2.2 1.4" className="sc-topology-node__mark" />
      )}
      {status === 'unknown' && <path d="M -2.6 0 L 2.6 0" className="sc-topology-node__mark" />}
    </g>
  );
}

/** The same glyph at legend size, as a standalone inline SVG. */
function LegendGlyph({ status }: { status: TopologyNode['status'] }): React.ReactElement {
  return (
    <svg className="sc-topo-legend__glyph" width={14} height={14} viewBox="-7 -7 14 14" aria-hidden="true">
      <StatusGlyph status={status} x={0} y={0} />
    </svg>
  );
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
      id: nodeKey(kind, item.metadata.namespace, item.metadata.name),
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
            id: nodeKey('Entity', entityNamespace, entityNamespace.replace(/^entity-/, '')),
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
        id: nodeKey('CloudInfrastructure', 'sovereign-cloud', infraName),
        label: infraName,
        kind: 'CloudInfrastructure',
        namespace: 'sovereign-cloud',
        status: 'unknown',
      });
    }

    // ---------------------------------------------------------------- edges
    // Every node is identified by namespace|kind|name. Tenant CR names are not
    // globally unique (`local-virt` exists in entity-acme-corp AND entity-chad),
    // so an edge is only ever built between two resolved node keys — never by
    // bare name and never by array position.
    const allNodes: TopologyNode[] = [
      ...entityNodes,
      ...teamNodes,
      ...projectNodes,
      ...assignmentNodes,
      ...platformNodes,
      ...cloudNodes,
      ...fabricNodes,
      ...infraNodes,
    ];
    const byKey = new Map(allNodes.map((n) => [n.id, n]));

    /**
     * Resolve a CR reference to a node key. A reference inside a tenant CR is
     * namespace-local, so it must resolve inside `ns`. Only the two
     * platform-scoped kinds (HybridFabric, CloudInfrastructure) may fall back
     * to the shared `sovereign-cloud` namespace.
     */
    const resolve = (kind: string, ns: string | undefined, name: string): string | undefined => {
      if (!name) return undefined;
      const local = nodeKey(kind, ns, name);
      if (byKey.has(local)) return local;
      if (PLATFORM_SCOPED_KINDS.has(kind)) {
        const shared = nodeKey(kind, 'sovereign-cloud', name);
        if (byKey.has(shared)) return shared;
      }
      return undefined;
    };

    const edgePairs: Array<{ from: string; to: string; failed?: boolean }> = [];
    const link = (from: string | undefined, to: string | undefined, failed?: boolean) => {
      if (!from || !to || from === to) return;
      edgePairs.push({ from, to, failed });
    };

    // Assignment ← Team / Project / PlatformOpenshift / CloudAWS / CloudOSO.
    // All of these refs are plain names inside the Assignment's own namespace.
    if (allow(assignmentPerm.allowed)) {
      for (const a of assignmentItems) {
        const ans = a.metadata.namespace;
        const spec = (a.spec || {}) as AssignSpec;
        const self = resolve('Assignment', ans, a.metadata.name);
        const failed = statusFromReady(a.status?.ready, a.status?.status) === 'failed';
        if (spec.team) link(resolve('Team', ans, spec.team), self, failed);
        for (const pName of spec.projects ?? []) link(resolve('Project', ans, pName), self, failed);
        if (spec.openshift) link(resolve('PlatformOpenshift', ans, spec.openshift), self, failed);
        if (spec.aws) link(resolve('CloudAWS', ans, spec.aws), self, failed);
        if (spec.cloudoso) link(resolve('CloudOSO', ans, spec.cloudoso), self, failed);
      }
    }

    // HybridFabric → CloudInfrastructure through each CloudGateway (fabricRef + cloudRef).
    for (const g of gatewayItems) {
      const gns = g.metadata.namespace;
      const fabricName = (g.spec as { fabricRef?: { name?: string } } | undefined)?.fabricRef?.name;
      const infraName = cloudRefOf(g);
      if (!fabricName || !infraName) continue;
      link(
        resolve('HybridFabric', gns, fabricName),
        resolve('CloudInfrastructure', gns, infraName),
        statusFromReady(g.status?.ready, g.status?.status) === 'failed',
      );
    }

    // CloudInfrastructure → tenant cloud project (spec.cloudRef → sovereign-cloud).
    for (const { item, kind } of visibleClouds) {
      const infraName = cloudRefOf(item);
      if (!infraName) continue;
      link(
        resolve('CloudInfrastructure', item.metadata.namespace, infraName),
        nodeKey(kind, item.metadata.namespace, item.metadata.name),
        statusFromReady(item.status?.ready, item.status?.status) === 'failed',
      );
    }

    // Cloud project → PlatformOpenshift (spec.<type>.environment, same namespace).
    const platformHasCloudParent = new Set<string>();
    if (allow(platformPerm.allowed)) {
      for (const p of platformItems) {
        const pns = p.metadata.namespace;
        const spec = (p.spec ?? {}) as { type?: string } & Record<string, unknown>;
        const cloudKind = PLATFORM_ENV_KIND[spec.type ?? ''];
        const env = (spec[spec.type ?? ''] as { environment?: string } | undefined)?.environment;
        if (!cloudKind || !env) continue;
        const cloud = resolve(cloudKind, pns, env);
        const self = nodeKey('PlatformOpenshift', pns, p.metadata.name);
        if (!cloud) continue;
        platformHasCloudParent.add(self);
        link(cloud, self, statusFromReady(p.status?.ready, p.status?.status) === 'failed');
      }
    }

    // Entity → everything it owns. Ownership comes from the resource's OWN
    // namespace (`entity-<name>`) or its `.status.entity`, never from name
    // similarity: that is what mis-attached `onix-aws` to entity `bulbasaur`.
    const entityNodeByName = new Map(entityNodes.map((e) => [e.label, e]));
    const ownerNode = (item: K8sResource): TopologyNode | undefined => {
      const name = entityOf(item);
      if (!name) return undefined;
      const e = entityNodeByName.get(name);
      // In scoped mode there is exactly one Entity node; only claim resources
      // that really sit in that entity's namespace.
      if (!e) return undefined;
      if (entityNamespace && item.metadata.namespace !== entityNamespace) return undefined;
      return e;
    };

    for (const { item, kind } of visibleClouds) {
      const e = ownerNode(item);
      if (!e) continue;
      link(
        e.id,
        nodeKey(kind, item.metadata.namespace, item.metadata.name),
        statusFromReady(item.status?.ready, item.status?.status) === 'failed',
      );
    }

    if (allow(teamPerm.allowed)) {
      for (const t of teamItems) {
        const e = ownerNode(t);
        link(e?.id, nodeKey('Team', t.metadata.namespace, t.metadata.name));
      }
    }

    // A PlatformOpenshift hangs off its cloud project when it has one; only
    // orphans attach straight to the entity, so the chain stays a single spine.
    if (allow(platformPerm.allowed)) {
      for (const p of platformItems) {
        const self = nodeKey('PlatformOpenshift', p.metadata.namespace, p.metadata.name);
        if (platformHasCloudParent.has(self)) continue;
        const e = ownerNode(p);
        link(e?.id, self, statusFromReady(p.status?.ready, p.status?.status) === 'failed');
      }
    }

    // Likewise, an Assignment only attaches to its entity if nothing upstream claimed it.
    if (allow(assignmentPerm.allowed)) {
      const claimed = new Set(edgePairs.map((ep) => ep.to));
      for (const a of assignmentItems) {
        const self = nodeKey('Assignment', a.metadata.namespace, a.metadata.name);
        if (claimed.has(self)) continue;
        const e = ownerNode(a);
        link(e?.id, self, statusFromReady(a.status?.ready, a.status?.status) === 'failed');
      }
    }

    // --------------------------------------------------------------- pruning
    const referenced = new Set<string>();
    for (const ep of edgePairs) {
      referenced.add(ep.from);
      referenced.add(ep.to);
    }
    const keep = (n: TopologyNode) =>
      n.kind === 'Entity' ||
      (!entityNamespace && (n.kind === 'HybridFabric' || n.kind === 'CloudInfrastructure')) ||
      referenced.has(n.id);

    // -------------------------------------------------------------- ordering
    // Sort each layer by owning entity, then kind, then name, so a tenant's
    // resources sit under its Entity and edges run mostly straight down.
    const entityOrder = new Map(entityNodes.map((e, i) => [e.label, i]));
    const ownerRank = (n: TopologyNode): number => {
      const name = (n.namespace ?? '').startsWith('entity-')
        ? (n.namespace ?? '').slice('entity-'.length)
        : '';
      return entityOrder.get(name) ?? Number.MAX_SAFE_INTEGER;
    };
    const byOwner = (a: TopologyNode, b: TopologyNode) =>
      ownerRank(a) - ownerRank(b) ||
      a.kind.localeCompare(b.kind) ||
      a.label.localeCompare(b.label);

    const sorted = (ns2: TopologyNode[]) => ns2.filter(keep).sort(byOwner);

    const filteredTeams = sorted(teamNodes);
    const filteredProjects = sorted(projectNodes);
    const filteredAssignments = sorted(assignmentNodes);
    const filteredPlatforms = sorted(platformNodes);
    const filteredClouds = sorted(cloudNodes);
    const filteredFabrics = fabricNodes.filter(keep);
    const filteredInfra = infraNodes.filter(keep).sort((a, b) => a.label.localeCompare(b.label));

    // --------------------------------------------------------- top-down layers
    const layers = (
      entityNamespace
        ? [
            { title: 'Entity', nodes: entityNodes },
            { title: 'Teams', nodes: filteredTeams },
            { title: 'Projects', nodes: filteredProjects },
            { title: 'Sites', nodes: filteredInfra },
            { title: 'Cloud projects', nodes: filteredClouds },
            { title: 'Clusters', nodes: filteredPlatforms },
            { title: 'Assignments', nodes: filteredAssignments },
          ]
        : [
            { title: 'Fabric', nodes: filteredFabrics },
            { title: 'Sites', nodes: filteredInfra },
            { title: 'Entities', nodes: entityNodes },
            { title: 'Cloud projects', nodes: filteredClouds },
            { title: 'Clusters', nodes: filteredPlatforms },
            { title: 'Assignments', nodes: filteredAssignments },
          ]
    ).filter((l) => l.nodes.length > 0);

    const { nodes, rows, width, height } = layoutLayers(layers);
    const placedByKey = new Map(nodes.map((n) => [n.id, n]));

    const edges: TopologyEdge[] = [];
    const seen = new Set<string>();
    for (const ep of edgePairs) {
      const from = placedByKey.get(ep.from);
      const to = placedByKey.get(ep.to);
      if (!from || !to) continue;
      const id = `${from.id}->${to.id}`;
      if (seen.has(id)) continue;
      seen.add(id);
      edges.push({ id, from: from.id, to: to.id, failed: ep.failed });
    }

    return { nodes, edges, rows, width: Math.max(width, 480), height: Math.max(height, 200) };
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

  const nodeById = useMemo(
    () => new Map(graph.nodes.map((n) => [n.id, n])),
    [graph.nodes],
  );

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

  // The graph now flows top-down, so "fit" has to respect the viewport height
  // as well as its width.
  const fit = () => {
    const el = wrapRef.current;
    if (!el) return;
    const sw = (el.clientWidth - 16) / graph.width;
    const sh = el.clientHeight > 0 ? (el.clientHeight - 16) / graph.height : Infinity;
    setZoom(Math.max(0.4, Math.min(1, sw, sh)));
  };

  return (
    <div className="sc-topology-graph" aria-label="Entity topology">
      <div className="sc-topology-graph__toolbar">
        {/* Status is carried by the glyph shape and the text label as well as
            the colour, so it survives greyscale and forced-colors. */}
        <div className="sc-topo-legend">
          <span>
            <LegendGlyph status="ready" /> Healthy
          </span>
          <span>
            <LegendGlyph status="failed" /> Failed
          </span>
          <span>
            <LegendGlyph status="pending" /> Pending
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
          {/* Layer captions — the row labels of the top-down flow. */}
          {graph.rows.map((r) => (
            <text key={r.title} x={PAD_X} y={r.y + 10} className="sc-topology-layer__title">
              {r.title}
            </text>
          ))}
          {graph.edges.map((e) => {
            const from = nodeById.get(e.from);
            const to = nodeById.get(e.to);
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
              <title>{`${n.kind} ${n.label}${n.namespace ? ` (${n.namespace})` : ''} — ${n.status}`}</title>
              <rect width={NODE_W} height={NODE_H} rx={3} ry={3} />
              <StatusGlyph status={n.status} x={14} y={NODE_H / 2} />
              <text x={26} y={19} className="sc-topology-node__kind">
                {n.kind}
              </text>
              <text x={26} y={35} className="sc-topology-node__name">
                {n.label.length > 16 ? `${n.label.slice(0, 15)}…` : n.label}
              </text>
            </g>
          ))}
        </svg>
      </div>
    </div>
  );
}
