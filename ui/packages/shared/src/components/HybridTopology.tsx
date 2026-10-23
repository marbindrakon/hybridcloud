import React, { useMemo, useState } from 'react';
import { Alert, Button } from '@patternfly/react-core';
import { TableIcon, TopologyIcon } from '@patternfly/react-icons';
import type {
  CloudGateway,
  CloudInfrastructure,
  HybridFabric,
  HybridNetwork,
  HybridSovereignKind,
  K8sResource,
  NetworkPlacement,
  VniPool,
} from '../types';
import { useTranslation } from '../i18n';
import { displayText, kindTerm, objectLabel } from '../i18n/vocabulary';
import { normalizeHealth } from './StatusBadge';
import { egressText, placementVnis } from './KindStatusDetails';

/**
 * HybridTopology — the hybrid network drawn as the layers it is made of (adapted from
 * hybridcloud-demojam).
 *
 * Five bands, top to bottom, in dependency order:
 *
 *   1. Fabric      the HybridFabric: domain ASN, overlay MTU, border gateway nodes ready, and the
 *                  use of its layer 3 and layer 2 VNI pools.
 *   2. Gateways    one CloudGateway per site. The line up to the fabric carries the transport:
 *                  WireGuard gets a halo and the tunnel state across the nodes; none (the site's
 *                  underlay VLAN trunked to the nodes) a plain line. Each node's sessions and tunnel
 *                  state are in the tooltip and the table view.
 *   3. Sites       the CloudInfrastructure each gateway attaches, with its type and readiness.
 *   4. Networks    HybridNetworks grouped by Entity. Across the core a network is identified by its
 *                  route target (status.realization.vpnTarget), so that is the badge on the card,
 *                  next to the internet egress state and the placements ready.
 *   5. Placements  each NetworkPlacement under its network: backend, prefixes, the layer 3 and
 *                  layer 2 VNIs the fabric allocated it, and the site namespaces it serves. A line
 *                  runs from the placement through the routing lane to the site it lands on.
 *
 * A tenant may not be able to read the platform kinds (HybridFabric, CloudGateway,
 * CloudInfrastructure). Without any of them bands 1 and 2 are left out and the sites come from
 * the placements (status.site, else the backend's spec.cloudRef).
 *
 * Accessibility notes:
 *  - State is never colour alone. Every node carries a drawn glyph (tick / clock / cross / dash)
 *    AND a text state label, every fabric line is labelled, and there is a full table view of
 *    the same model.
 *  - Text wears text tokens; colour lives on strokes and glyphs, so contrast holds in both
 *    themes. Dark mode rides PatternFly's own status tokens.
 *  - Sites are also named in text on each placement chip, so the connector is reinforcement
 *    rather than the only way to read the mapping.
 *
 * Robustness: every field below can be absent on a partially-reconciled object. Nothing here
 * indexes without a guard and nothing throws on a missing spec, status, ref or array.
 */

export type NetState = 'ready' | 'pending' | 'failed' | 'absent';

export interface HybridTopologyProps {
  /** Networking CRs: HybridFabric, CloudGateway, CloudInfrastructure, HybridNetwork,
   *  NetworkPlacement and the placement backends (CloudVirt, CloudOSO, CloudAWS, CloudGCP). */
  items: K8sResource[];
  /** Scope to one tenant: only that entity's HybridNetworks are drawn. */
  entityNamespace?: string;
  /** Draw only this fabric's gateways and networks (an installation with several fabrics). */
  fabricName?: string;
  onSelect?: (kind: HybridSovereignKind, item: K8sResource) => void;
}

/* ------------------------------------------------------------------ model */

export interface GatewayNodeView {
  name: string;
  address: string;
  ready?: boolean;
  site?: number;
  core?: number;
  upstream?: number;
  /** transport wireguard only: up / down */
  tunnel: string;
}

export interface GatewayView {
  transport: string;
  /** wireguard only: up when every node's tunnel is up, down when none is, partial otherwise */
  tunnel: '' | 'up' | 'down' | 'partial';
  nodes: GatewayNodeView[];
  /** The gateway's state; a WireGuard gateway is pending until its tunnel is up on every node. */
  state: NetState;
}

export interface TopoPlacement {
  key: string;
  cr: NetworkPlacement;
  name: string;
  backendKind: string;
  backendName: string;
  siteName: string;
  cloudGateway: string;
  prefixes: string[];
  l3Vni?: number;
  l2Vni?: number;
  namespaces: string[];
  state: NetState;
  message: string;
}

export interface TopoNetwork {
  key: string;
  cr: HybridNetwork;
  name: string;
  namespace: string;
  entity: string;
  vpnTarget?: number;
  /** The full route target (ASN:number) when known, else the bare number */
  routeTarget: string;
  mtu?: number;
  description: string;
  state: NetState;
  duplicateTarget: boolean;
  placementsReady: number;
  placements: TopoPlacement[];
  message: string;
}

export interface TopoGroup {
  entity: string;
  networks: TopoNetwork[];
}

export interface TopoGateway extends GatewayView {
  cr: CloudGateway;
  name: string;
  siteName: string;
  message: string;
}

export interface TopoSite {
  cr?: CloudInfrastructure;
  name: string;
  type: string;
  displayName: string;
  /** absent: no CloudInfrastructure is readable for this site */
  state: NetState;
  gateways: TopoGateway[];
}

export interface TopoPool {
  /** status.allocated / status.available key */
  key: 'vni' | 'l2vni';
  start?: number;
  end?: number;
  size?: number;
  allocated: number;
}

export interface TopoFabricNode {
  name: string;
  address: string;
  ready?: boolean;
}

export interface TopoFabric {
  cr?: HybridFabric;
  name: string;
  asn?: number;
  overlayMtu?: number;
  nodes: TopoFabricNode[];
  nodesReady: number;
  nodesTotal: number;
  pools: TopoPool[];
  /** absent: no HybridFabric is readable */
  state: NetState;
}

export interface TopologyModel {
  /** False when none of HybridFabric, CloudGateway, CloudInfrastructure is readable. */
  platformVisible: boolean;
  fabric: TopoFabric;
  sites: TopoSite[];
  groups: TopoGroup[];
  duplicateTargets: Array<{ target: number; networks: string[] }>;
  networkCount: number;
  placementCount: number;
}

/* ---------------------------------------------------------------- helpers */

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

function strs(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : [];
}

function trunc(s: string, n: number): string {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, Math.max(n - 1, 1))}…` : s;
}

function ofKind<T extends K8sResource>(items: K8sResource[], kind: HybridSovereignKind): T[] {
  return items.filter((i) => (i?.kind || '') === kind) as T[];
}

function byName(a: K8sResource, b: K8sResource): number {
  return a.metadata.name.localeCompare(b.metadata.name);
}

export function stateOf(item: K8sResource | undefined): NetState {
  if (!item) return 'pending';
  const health = normalizeHealth(item);
  if (health === 'ready') return 'ready';
  if (health === 'failed') return 'failed';
  return 'pending';
}

function entityOf(net: HybridNetwork): string {
  const fromStatus = str(net.status?.entity);
  if (fromStatus) return fromStatus;
  const ns = net.metadata?.namespace ?? '';
  return ns.startsWith('entity-') ? ns.slice('entity-'.length) : ns || '—';
}

function fabricOfNetwork(net: HybridNetwork): string {
  return str(net.status?.fabric) || str(net.spec?.fabricRef?.name);
}

/** Site type a backend kind implies, for a site whose CloudInfrastructure isn't readable. */
const BACKEND_SITE_TYPE: Record<string, string> = { CloudVirt: 'openshift', CloudOSO: 'openstack', CloudAWS: 'aws', CloudGCP: 'gcp' };

const BACKEND_KINDS: HybridSovereignKind[] = ['CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP'];

/** Transport, per-node sessions and tunnel state of one CloudGateway. */
export function describeGateway(gw: CloudGateway): GatewayView {
  const transport = str(gw.spec?.transport) || 'none';
  const raw = gw.status?.nodes;
  const nodes: GatewayNodeView[] = (Array.isArray(raw) ? raw : []).map((n) => ({
    name: str(n?.name),
    address: str(n?.address),
    ready: typeof n?.ready === 'boolean' ? n.ready : undefined,
    site: num(n?.siteSessions),
    core: num(n?.coreSessions),
    upstream: num(n?.upstreamSessions),
    tunnel: str(n?.tunnel),
  }));
  let tunnel: GatewayView['tunnel'] = '';
  if (transport === 'wireguard') {
    const states = nodes.map((n) => n.tunnel).filter(Boolean);
    if (states.length === 0) tunnel = 'down';
    else if (states.every((s) => s === 'up')) tunnel = 'up';
    else if (states.every((s) => s !== 'up')) tunnel = 'down';
    else tunnel = 'partial';
  }
  const base = stateOf(gw);
  return {
    transport,
    tunnel,
    nodes,
    // The line is ready only when the gateway is ready and, for a tunnel, every node's tunnel is up.
    state: base === 'ready' && transport === 'wireguard' && tunnel !== 'up' ? 'pending' : base,
  };
}

function poolOf(fabric: HybridFabric, key: TopoPool['key'], range: VniPool | undefined): TopoPool | undefined {
  const allocated = num(fabric.status?.allocated?.[key]);
  const available = num(fabric.status?.available?.[key]);
  const start = num(range?.start);
  const end = num(range?.end);
  let size: number | undefined;
  if (start !== undefined && end !== undefined && end >= start) size = end - start + 1;
  else if (allocated !== undefined && available !== undefined) size = allocated + available;
  if (size === undefined && allocated === undefined) return undefined;
  return { key, start, end, size, allocated: allocated ?? 0 };
}

export function buildTopologyModel(
  items: K8sResource[],
  entityNamespace?: string,
  fabricName?: string,
): TopologyModel {
  const safe = Array.isArray(items) ? items.filter((i) => i && i.metadata && i.metadata.name) : [];

  const fabrics = ofKind<HybridFabric>(safe, 'HybridFabric').sort(byName);
  const infra = ofKind<CloudInfrastructure>(safe, 'CloudInfrastructure');
  let gateways = ofKind<CloudGateway>(safe, 'CloudGateway');
  let networks = ofKind<HybridNetwork>(safe, 'HybridNetwork');
  const placements = ofKind<NetworkPlacement>(safe, 'NetworkPlacement');
  const platformVisible = fabrics.length > 0 || gateways.length > 0 || infra.length > 0;

  if (entityNamespace) networks = networks.filter((n) => n.metadata.namespace === entityNamespace);
  if (fabricName) {
    gateways = gateways.filter((g) => str(g.spec?.fabricRef?.name) === fabricName);
    networks = networks.filter((n) => fabricOfNetwork(n) === fabricName);
  }

  /* ---- fabric */
  const fabricCr = fabricName ? fabrics.find((f) => f.metadata.name === fabricName) : fabrics[0];
  const fspec = fabricCr?.spec;
  const specNodes = Array.isArray(fspec?.borderGateway?.nodes) ? fspec?.borderGateway?.nodes ?? [] : [];
  const statusNodes = Array.isArray(fabricCr?.status?.nodes) ? fabricCr?.status?.nodes ?? [] : [];
  const nodeNames = Array.from(
    new Set([...specNodes.map((n) => str(n?.name)), ...statusNodes.map((n) => str(n?.name))]),
  ).filter(Boolean);
  const pools = fabricCr
    ? [poolOf(fabricCr, 'vni', fspec?.vniPool), poolOf(fabricCr, 'l2vni', fspec?.l2vniPool)].filter(
        (p): p is TopoPool => !!p,
      )
    : [];
  const fabric: TopoFabric = {
    cr: fabricCr,
    name:
      fabricCr?.metadata.name ||
      fabricName ||
      gateways.map((g) => str(g.spec?.fabricRef?.name)).find(Boolean) ||
      networks.map(fabricOfNetwork).find(Boolean) ||
      '—',
    asn: num(fspec?.domainAsn),
    overlayMtu: num(fspec?.overlayMtu),
    nodes: nodeNames.map((name) => {
      const s = statusNodes.find((n) => n?.name === name);
      return {
        name,
        address: str(specNodes.find((n) => n?.name === name)?.address),
        ready: typeof s?.ready === 'boolean' ? s.ready : undefined,
      };
    }),
    nodesReady: statusNodes.filter((n) => n?.ready === true).length,
    nodesTotal: Math.max(specNodes.length, statusNodes.length),
    pools,
    state: fabricCr ? stateOf(fabricCr) : 'absent',
  };

  /* ---- gateways, keyed to the site they attach */
  const topoGateways: TopoGateway[] = gateways.sort(byName).map((gw) => ({
    ...describeGateway(gw),
    cr: gw,
    name: gw.metadata.name,
    siteName: str(gw.spec?.cloudRef?.name) || str(gw.status?.site?.name),
    message: str(gw.status?.message),
  }));

  /* ---- backend -> site (backend names are only unique per namespace) */
  const backendSite = new Map<string, string>();
  for (const kind of BACKEND_KINDS) {
    for (const b of ofKind<K8sResource<{ cloudRef?: { name?: string } }>>(safe, kind)) {
      const site = str(b.spec?.cloudRef?.name);
      if (site) backendSite.set(`${b.metadata.namespace ?? ''}|${kind}|${b.metadata.name}`, site);
    }
  }

  /* ---- placements, bucketed under their network */
  const placementsByNetwork = new Map<string, TopoPlacement[]>();
  for (const p of placements) {
    const netName = str(p.spec?.network);
    const ns = p.metadata.namespace ?? '';
    if (!netName) continue;
    const kind = str(p.spec?.backend?.kind);
    const name = str(p.spec?.backend?.name);
    const vnis = placementVnis(p);
    const entry: TopoPlacement = {
      key: `${ns}/${p.metadata.name}`,
      cr: p,
      name: p.metadata.name,
      backendKind: kind,
      backendName: name,
      siteName: str(p.status?.site) || backendSite.get(`${ns}|${kind}|${name}`) || '',
      cloudGateway: str(p.status?.cloudGateway),
      prefixes: strs(p.status?.prefixes),
      l3Vni: num(vnis.l3),
      l2Vni: num(vnis.l2),
      namespaces: strs(p.status?.namespaces),
      state: stateOf(p),
      message: str(p.status?.message),
    };
    const k = `${ns}|${netName}`;
    const list = placementsByNetwork.get(k);
    if (list) list.push(entry);
    else placementsByNetwork.set(k, [entry]);
  }

  /* ---- duplicate route targets: the fabric ledger hands out one per network, so two networks
   * sharing one would leak into each other; checked defensively all the same */
  const targetOwners = new Map<number, string[]>();
  for (const n of networks) {
    const v = num(n.status?.realization?.vpnTarget);
    if (v === undefined) continue;
    const label = `${n.metadata.namespace}/${n.metadata.name}`;
    const owners = targetOwners.get(v);
    if (owners) owners.push(label);
    else targetOwners.set(v, [label]);
  }
  const duplicateTargets = Array.from(targetOwners.entries())
    .filter(([, owners]) => owners.length > 1)
    .map(([target, owners]) => ({ target, networks: owners }))
    .sort((a, b) => a.target - b.target);
  const dupSet = new Set(duplicateTargets.map((d) => d.target));

  /* ---- networks */
  const topoNetworks: TopoNetwork[] = networks.map((n) => {
    const ns = n.metadata.namespace ?? '';
    const ps = (placementsByNetwork.get(`${ns}|${n.metadata.name}`) ?? []).sort((a, b) =>
      a.name.localeCompare(b.name),
    );
    const vpnTarget = num(n.status?.realization?.vpnTarget);
    const base = stateOf(n);
    return {
      key: `${ns}/${n.metadata.name}`,
      cr: n,
      name: n.metadata.name,
      namespace: ns,
      entity: entityOf(n),
      vpnTarget,
      routeTarget:
        str(n.status?.realization?.routeTarget) ||
        (vpnTarget !== undefined ? (fabric.asn !== undefined ? `${fabric.asn}:${vpnTarget}` : String(vpnTarget)) : ''),
      mtu: num(n.status?.mtu) ?? num(n.spec?.mtu),
      description: str(n.spec?.description),
      // A network with no placement is a real, common state: call it out rather than letting it
      // read as pending. A failure still shows as one.
      state: ps.length === 0 && base !== 'failed' ? 'absent' : base,
      duplicateTarget: vpnTarget !== undefined && dupSet.has(vpnTarget),
      placementsReady: ps.filter((p) => p.state === 'ready').length,
      placements: ps,
      message: str(n.status?.message),
    };
  });

  /* ---- sites: the CloudInfrastructures, plus any site only a gateway or a placement names */
  const drawnPlacements = topoNetworks.flatMap((n) => n.placements);
  const wanted = fabricName
    ? new Set([...topoGateways.map((g) => g.siteName), ...drawnPlacements.map((p) => p.siteName)])
    : undefined;
  const siteMap = new Map<string, TopoSite>();
  for (const i of infra) {
    if (wanted && !wanted.has(i.metadata.name)) continue;
    siteMap.set(i.metadata.name, {
      cr: i,
      name: i.metadata.name,
      type: str(i.spec?.type),
      displayName: str(i.spec?.displayName),
      state: stateOf(i),
      gateways: [],
    });
  }
  for (const gw of topoGateways) {
    if (!gw.siteName) continue;
    let site = siteMap.get(gw.siteName);
    if (!site) {
      site = {
        name: gw.siteName,
        type: str(gw.cr.status?.site?.type),
        displayName: '',
        state: 'pending',
        gateways: [],
      };
      siteMap.set(gw.siteName, site);
    }
    if (!site.type) site.type = str(gw.cr.status?.site?.type);
    site.gateways.push(gw);
  }
  for (const p of drawnPlacements) {
    if (!p.siteName || siteMap.has(p.siteName)) continue;
    siteMap.set(p.siteName, {
      name: p.siteName,
      type: BACKEND_SITE_TYPE[p.backendKind] ?? '',
      displayName: '',
      state: 'absent',
      gateways: [],
    });
  }
  const sites = Array.from(siteMap.values()).sort((a, b) => a.name.localeCompare(b.name));

  /* ---- networks grouped by entity */
  const groupMap = new Map<string, TopoGroup>();
  for (const n of topoNetworks) {
    const g = groupMap.get(n.entity);
    if (g) g.networks.push(n);
    else groupMap.set(n.entity, { entity: n.entity, networks: [n] });
  }
  const groups = Array.from(groupMap.values()).sort((a, b) => a.entity.localeCompare(b.entity));
  for (const g of groups) g.networks.sort((a, b) => a.name.localeCompare(b.name));

  return {
    platformVisible,
    fabric,
    sites,
    groups,
    duplicateTargets,
    networkCount: topoNetworks.length,
    placementCount: drawnPlacements.length,
  };
}

/* -------------------------------------------------------------- geometry */

const PAD = 16;
const CARD_W = 206;
const GUTTER = 22; // left strip of each column, reserved for connector runs
const COL_W = CARD_W + GUTTER;
const COL_GAP = 12;
const GROUP_PAD = 10;
const GROUP_GAP = 22;
const GROUP_HEAD = 22;

const FAB_H = 92;
const GW_Y = 142;
const GW_H = 64;
const SITE_Y = 252;
const SITE_H = 66;
const LANE_H = 78;
const GROUPS_Y = SITE_Y + SITE_H + LANE_H;

const NET_H = 80;
const CHIP_H = 86;
const CHIP_GAP = 8;

const SITE_W = 186;
const SITE_MIN_GAP = 36;
/** Transport label on a fabric line; fits the narrowest site slot (SITE_W) */
const PILL_W = 172;

interface ColLayout {
  net: TopoNetwork;
  x: number; // left edge of the column (gutter starts here)
  cardX: number;
  y: number;
  height: number;
  chips: Array<{ p: TopoPlacement; y: number; gutterX: number }>;
}

interface GroupLayout {
  group: TopoGroup;
  x: number;
  width: number;
  height: number;
  cols: ColLayout[];
}

interface SiteLayout {
  site: TopoSite;
  x: number;
  cx: number;
  gws: Array<{ gw: TopoGateway; x: number; w: number; cx: number }>;
}

function layout(model: TopologyModel): {
  width: number;
  height: number;
  /** Vertical shift that drops the fabric and gateway bands when the platform isn't readable */
  offsetY: number;
  contentW: number;
  groups: GroupLayout[];
  sites: SiteLayout[];
  siteCx: Map<string, number>;
} {
  // Groups first — they set the natural content width.
  let gx = PAD;
  let maxGroupH = 0;
  const groups: GroupLayout[] = model.groups.map((g) => {
    const cols: ColLayout[] = g.networks.map((net, i) => {
      const x = gx + GROUP_PAD + i * (COL_W + COL_GAP);
      const y = GROUPS_Y + GROUP_HEAD;
      const chips = net.placements.map((p, j) => ({
        p,
        y: y + NET_H + CHIP_GAP + j * (CHIP_H + CHIP_GAP),
        // Each placement gets its own lane inside the gutter so connectors
        // never overlap a card or each other.
        gutterX: x + 6 + Math.min(j, 2) * 6,
      }));
      return {
        net,
        x,
        cardX: x + GUTTER,
        y,
        height: NET_H + net.placements.length * (CHIP_H + CHIP_GAP),
        chips,
      };
    });
    const width = Math.max(
      g.networks.length * COL_W + Math.max(g.networks.length - 1, 0) * COL_GAP + GROUP_PAD * 2,
      COL_W + GROUP_PAD * 2,
    );
    const height = GROUP_HEAD + Math.max(...cols.map((c) => c.height), NET_H) + GROUP_PAD;
    maxGroupH = Math.max(maxGroupH, height);
    const out: GroupLayout = { group: g, x: gx, width, height, cols };
    gx += width + GROUP_GAP;
    return out;
  });

  const groupsW = groups.length > 0 ? gx - GROUP_GAP - PAD : 0;
  const siteCount = Math.max(model.sites.length, 1);
  const sitesW = siteCount * SITE_W + (siteCount - 1) * SITE_MIN_GAP;
  const contentW = Math.max(groupsW, sitesW, 640);

  const slot = contentW / siteCount;
  const siteCx = new Map<string, number>();
  const sites: SiteLayout[] = model.sites.map((s, i) => {
    const cx = PAD + slot * (i + 0.5);
    siteCx.set(s.name, cx);
    const n = Math.max(s.gateways.length, 1);
    const gwW = Math.max(Math.min(SITE_W, slot / n - 12), 104);
    const span = n * gwW + (n - 1) * 10;
    const start = cx - span / 2;
    return {
      site: s,
      x: cx - SITE_W / 2,
      cx,
      gws: s.gateways.map((gw, j) => {
        const x = start + j * (gwW + 10);
        return { gw, x, w: gwW, cx: x + gwW / 2 };
      }),
    };
  });

  // Without the platform kinds the fabric and gateway bands are left out, and without sites the site band too.
  const offsetY = model.platformVisible ? 0 : sites.length > 0 ? SITE_Y - PAD : GROUPS_Y - PAD;
  const height = GROUPS_Y + (groups.length > 0 ? maxGroupH : 0) + PAD - offsetY;
  return { width: contentW + PAD * 2, height, offsetY, contentW, groups, sites, siteCx };
}

/* ----------------------------------------------------------------- glyphs */

/**
 * State as a drawn shape, so state survives greyscale, print and
 * forced-colors. Colour is reinforcement only; the shape and the adjacent
 * text label carry the meaning.
 */
function StateGlyph({ state, x, y }: { state: NetState; x: number; y: number }): React.ReactElement {
  const r = 6;
  return (
    <g transform={`translate(${x}, ${y})`} className={`sc-hstopo__glyph sc-hstopo__glyph--${state}`}>
      <circle cx={0} cy={0} r={r} />
      {state === 'ready' && <path d="M -3 0 L -1 2.4 L 3.2 -2.4" className="sc-hstopo__glyph-mark" />}
      {state === 'pending' && <path d="M 0 -3.2 L 0 0 L 2.6 1.6" className="sc-hstopo__glyph-mark" />}
      {state === 'failed' && (
        <path d="M -2.6 -2.6 L 2.6 2.6 M 2.6 -2.6 L -2.6 2.6" className="sc-hstopo__glyph-mark" />
      )}
      {state === 'absent' && <path d="M -3 0 L 3 0" className="sc-hstopo__glyph-mark" />}
    </g>
  );
}

interface NodeProps {
  onActivate?: () => void;
}

function nodeHandlers({ onActivate }: NodeProps): Record<string, unknown> {
  if (!onActivate) return {};
  return {
    role: 'button',
    tabIndex: 0,
    onClick: onActivate,
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onActivate();
      }
    },
  };
}

/* ------------------------------------------------------------ text helpers */

type TFn = (key: string, opts?: Record<string, unknown>) => string;

/** State label; `absent` reads "not placed" on a network and "no details" on a fabric or site. */
function stateLabel(t: TFn, state: NetState, absentKey = 'networkOverview.state_absent'): string {
  return state === 'absent' ? t(absentKey) : t(`networkOverview.state_${state}`);
}

/** "node (address): site 3 · core 1 · tunnel Up" for one gateway node. */
function nodeLine(t: TFn, n: GatewayNodeView): string {
  const parts: string[] = [];
  if (n.ready === false) parts.push(t('detail.notReady'));
  parts.push(t('networkOverview.sessionsSite', { count: n.site ?? 0 }));
  parts.push(t('networkOverview.sessionsCore', { count: n.core ?? 0 }));
  if (n.upstream) parts.push(t('networkOverview.sessionsUpstream', { count: n.upstream }));
  if (n.tunnel) {
    parts.push(t('networkOverview.tunnelState', { state: t(`detail.${n.tunnel === 'up' ? 'up' : 'down'}`) }));
  }
  return `${n.name || '—'}${n.address ? ` (${n.address})` : ''}: ${parts.join(' · ')}`;
}

/** The label on the line from the fabric down to a gateway. */
function transportLabel(t: TFn, gw: GatewayView): string {
  if (gw.transport === 'wireguard') {
    return `${gw.transport} · ${t(`networkOverview.tunnel_${gw.tunnel || 'down'}`)}`;
  }
  return gw.transport === 'none' ? t('fields.transportNone') : gw.transport;
}

function vniText(t: TFn, p: TopoPlacement): string {
  if (p.l3Vni !== undefined && p.l2Vni !== undefined) return t('hybridTopology.vnis', { l3: p.l3Vni, l2: p.l2Vni });
  if (p.l3Vni !== undefined) return t('hybridTopology.vniL3', { l3: p.l3Vni });
  return t('hybridTopology.noVni');
}

function siteText(t: TFn, p: TopoPlacement): string {
  const site = p.siteName || t('hybridTopology.unresolved');
  return p.namespaces.length
    ? t('hybridTopology.nsToSite', { namespaces: p.namespaces.join(', '), site })
    : t('hybridTopology.toSite', { site });
}

/** Short egress state for a network card: "egress ready" / "no egress". */
function egressShort(t: TFn, n: HybridNetwork): string {
  const state = str(n.status?.egress?.internet);
  if (state) return t('hybridTopology.egressShort', { state: t(`detail.egress_${state}`, { defaultValue: state }) });
  if (n.spec?.egress?.internet === true) {
    return t('hybridTopology.egressShort', { state: t('networkOverview.egressRequested') });
  }
  return t('hybridTopology.egressOffShort');
}

/* -------------------------------------------------------------- component */

export function HybridTopology({
  items,
  entityNamespace,
  fabricName,
  onSelect,
}: HybridTopologyProps): React.ReactElement {
  const { t } = useTranslation();
  const [asTable, setAsTable] = useState(false);
  const model = useMemo(
    () => buildTopologyModel(items, entityNamespace, fabricName),
    [items, entityNamespace, fabricName],
  );
  const geo = useMemo(() => layout(model), [model]);

  const { fabric, sites, groups, duplicateTargets, platformVisible } = model;
  const hasAnything = sites.length > 0 || groups.length > 0 || !!fabric.cr;

  if (!hasAnything) {
    return (
      <Alert variant="info" title={t('hybridTopology.emptyTitle')} isInline>
        {t('hybridTopology.emptyBody')}
      </Alert>
    );
  }

  const counts = { sites: sites.length, networks: model.networkCount, placements: model.placementCount };
  const summary = platformVisible
    ? t('hybridTopology.summary', { fabric: fabric.name, ...counts })
    : t('hybridTopology.summaryTenant', counts);

  const fabricTitle = [
    `HybridFabric ${fabric.name} — ${stateLabel(t, fabric.state, 'hybridTopology.noDetails')}`,
    fabric.asn !== undefined ? `${t('fields.domainAsn')}: ${fabric.asn}` : '',
    fabric.overlayMtu !== undefined ? `${t('fields.overlayMtu')}: ${fabric.overlayMtu}` : '',
    ...fabric.nodes.map(
      (n) =>
        `${n.name}${n.address ? ` (${n.address})` : ''}: ${
          n.ready ? t('detail.ready') : t('detail.notReady')
        }`,
    ),
  ]
    .filter(Boolean)
    .join('\n');

  return (
    <div className="sc-hstopo">
      <div className="sc-hstopo__head">
        <div>
          <h3 className="sc-netov__title">
            {t('hybridTopology.title')} {fabricName && <code>{fabricName}</code>}
          </h3>
          <p className="sc-netov__hint">
            {platformVisible ? t('hybridTopology.hint') : t('hybridTopology.hintTenant')}
          </p>
        </div>
        <Button
          variant="secondary"
          size="sm"
          icon={asTable ? <TopologyIcon /> : <TableIcon />}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? t('hybridTopology.diagramView') : t('networkOverview.tableView')}
        </Button>
      </div>

      {duplicateTargets.length > 0 && (
        <Alert
          variant="danger"
          isInline
          title={t('hybridTopology.duplicateTitle', { count: duplicateTargets.length })}
          className="sc-hstopo__alert"
        >
          <ul className="sc-hstopo__dup-list">
            {duplicateTargets.map((d) => (
              <li key={d.target}>
                {t('hybridTopology.duplicateItem', { target: d.target, networks: d.networks.join(', ') })}
              </li>
            ))}
          </ul>
        </Alert>
      )}

      {asTable ? (
        <TopologyTable model={model} showEntity={!entityNamespace} />
      ) : (
        <>
          <div className="sc-hstopo__canvas-wrap">
            <svg
              className="sc-hstopo__canvas"
              viewBox={`0 0 ${geo.width} ${geo.height}`}
              width={geo.width}
              height={geo.height}
              role="img"
              aria-label={summary}
            >
              <g transform={geo.offsetY ? `translate(0, ${-geo.offsetY})` : undefined}>
                {/* band 1 — fabric */}
                {platformVisible && (
                  <g
                    className={`sc-hstopo__fabric sc-hstopo__node--${fabric.state}`}
                    {...nodeHandlers({
                      onActivate:
                        onSelect && fabric.cr ? () => onSelect('HybridFabric', fabric.cr as K8sResource) : undefined,
                    })}
                  >
                    <title>{fabricTitle}</title>
                    <rect x={PAD} y={0} width={geo.contentW} height={FAB_H} rx={4} />
                    <text x={PAD + 14} y={22} className="sc-hstopo__band-kind">
                      {t('hybridTopology.fabricKind')}
                    </text>
                    <text x={PAD + 14} y={44} className="sc-hstopo__band-name">
                      {trunc(fabric.name, 26)}
                    </text>
                    <StateGlyph state={fabric.state} x={PAD + 22} y={62} />
                    <text x={PAD + 34} y={66} className="sc-hstopo__band-state">
                      {stateLabel(t, fabric.state, 'hybridTopology.noDetails')}
                    </text>

                    <FabricFacts x={PAD + 250} width={Math.max(geo.contentW - 266, 160)} fabric={fabric} />
                  </g>
                )}

                {/* band 2 lines — fabric to gateway, carrying the transport */}
                {platformVisible &&
                  geo.sites.flatMap((s) =>
                    s.gws.map((g) => {
                      const x1 = g.cx;
                      const y1 = FAB_H;
                      const y2 = GW_Y;
                      const my = (y1 + y2) / 2;
                      const d = `M ${x1} ${y1} C ${x1} ${my}, ${x1} ${my}, ${x1} ${y2}`;
                      const tunnelled = g.gw.transport === 'wireguard';
                      return (
                        <g key={`e-${g.gw.name}`}>
                          {/* A tunnelled transport gets a halo: encapsulation, drawn. */}
                          {tunnelled && (
                            <path d={d} className={`sc-hstopo__tunnel sc-hstopo__tunnel--${g.gw.state}`} />
                          )}
                          <path d={d} className={`sc-hstopo__edge sc-hstopo__edge--${g.gw.state}`} />
                          <rect
                            x={x1 - PILL_W / 2}
                            y={my - 10}
                            width={PILL_W}
                            height={18}
                            rx={9}
                            className="sc-hstopo__edge-pill"
                          />
                          <text x={x1} y={my + 3} textAnchor="middle" className="sc-hstopo__edge-label">
                            {trunc(transportLabel(t, g.gw), 30)}
                          </text>
                        </g>
                      );
                    }),
                  )}

                {/* band 2 — gateways */}
                {platformVisible &&
                  geo.sites.flatMap((s) =>
                    s.gws.map((g) => {
                      const lines = g.gw.nodes.length
                        ? g.gw.nodes.map((n) => nodeLine(t, n))
                        : [t('networkOverview.noNodeStatus')];
                      const nodesReady = g.gw.nodes.filter((n) => n.ready).length;
                      const sub = g.gw.nodes.length
                        ? t('hybridTopology.gatewaySub', {
                            transport: g.gw.transport,
                            ready: nodesReady,
                            total: g.gw.nodes.length,
                          })
                        : `${g.gw.transport} · ${t('networkOverview.noNodeStatus')}`;
                      return (
                        <g
                          key={`gw-${g.gw.name}`}
                          transform={`translate(${g.x}, ${GW_Y})`}
                          className={`sc-hstopo__node sc-hstopo__node--${g.gw.state}`}
                          {...nodeHandlers({
                            onActivate: onSelect ? () => onSelect('CloudGateway', g.gw.cr) : undefined,
                          })}
                        >
                          <title>
                            {[
                              `CloudGateway ${g.gw.name} — ${stateLabel(t, g.gw.state)}`,
                              `${t('fields.transport')}: ${transportLabel(t, g.gw)}`,
                              ...lines,
                              g.gw.state !== 'ready' ? displayText(t, g.gw.message) : '',
                            ]
                              .filter(Boolean)
                              .join('\n')}
                          </title>
                          <rect width={g.w} height={GW_H} rx={3} />
                          <text x={10} y={17} className="sc-hstopo__node-kind">
                            {t('hybridTopology.gatewayKind')}
                          </text>
                          <text x={10} y={34} className="sc-hstopo__node-name">
                            {trunc(g.gw.name, Math.floor(g.w / 7.4))}
                          </text>
                          <text x={10} y={48} className="sc-hstopo__node-sub">
                            {trunc(sub, Math.floor(g.w / 6.2))}
                          </text>
                          <StateGlyph state={g.gw.state} x={16} y={57} />
                          <text x={28} y={61} className="sc-hstopo__node-state">
                            {stateLabel(t, g.gw.state)}
                          </text>
                        </g>
                      );
                    }),
                  )}

                {/* gateway -> site membership */}
                {platformVisible &&
                  geo.sites.flatMap((s) =>
                    s.gws.map((g) => (
                      <path
                        key={`gs-${g.gw.name}`}
                        d={`M ${g.cx} ${GW_Y + GW_H} C ${g.cx} ${SITE_Y - 16}, ${s.cx} ${
                          GW_Y + GW_H + 16
                        }, ${s.cx} ${SITE_Y}`}
                        className={`sc-hstopo__edge sc-hstopo__edge--${s.site.state}`}
                      />
                    )),
                  )}

                {/* band 3 — sites */}
                {geo.sites.map((s) => {
                  const label = stateLabel(t, s.site.state, 'hybridTopology.noDetails');
                  return (
                    <g
                      key={`site-${s.site.name}`}
                      transform={`translate(${s.x}, ${SITE_Y})`}
                      className={`sc-hstopo__node sc-hstopo__node--site sc-hstopo__node--${s.site.state}`}
                      {...nodeHandlers({
                        onActivate:
                          onSelect && s.site.cr
                            ? () => onSelect('CloudInfrastructure', s.site.cr as K8sResource)
                            : undefined,
                      })}
                    >
                      <title>
                        {`CloudInfrastructure ${s.site.name}${s.site.type ? ` (${s.site.type})` : ''} — ${label}` +
                          (s.site.displayName ? `\n${s.site.displayName}` : '')}
                      </title>
                      <rect width={SITE_W} height={SITE_H} rx={3} />
                      <text x={10} y={17} className="sc-hstopo__node-kind">
                        {trunc(t('hybridTopology.siteKind', { type: s.site.type || '—' }), 30)}
                      </text>
                      <text x={10} y={35} className="sc-hstopo__node-name">
                        {trunc(s.site.name, 24)}
                      </text>
                      <text x={10} y={49} className="sc-hstopo__node-sub">
                        {trunc(s.site.displayName, 28)}
                      </text>
                      <StateGlyph state={s.site.state} x={16} y={58} />
                      <text x={28} y={62} className="sc-hstopo__node-state">
                        {label}
                      </text>
                    </g>
                  );
                })}

                {/* placement -> site connectors, routed through the lane */}
                {geo.groups.flatMap((g) =>
                  g.cols.flatMap((c) =>
                    c.chips.map((chip) => {
                      const cx = geo.siteCx.get(chip.p.siteName);
                      if (cx === undefined) return null;
                      const cy = chip.y + CHIP_H / 2;
                      const laneY = GROUPS_Y - 18;
                      const d =
                        `M ${c.cardX} ${cy} H ${chip.gutterX} V ${laneY} ` +
                        `C ${chip.gutterX} ${SITE_Y + SITE_H + 20}, ${cx} ${laneY - 20}, ${cx} ${
                          SITE_Y + SITE_H
                        }`;
                      return (
                        <path
                          key={`pc-${chip.p.key}`}
                          d={d}
                          className={`sc-hstopo__link sc-hstopo__link--${chip.p.state}`}
                        />
                      );
                    }),
                  ),
                )}

                {/* bands 4 & 5 — entity groups, networks and their placements */}
                {geo.groups.map((g) => (
                  <g key={`grp-${g.group.entity}`}>
                    <rect
                      x={g.x}
                      y={GROUPS_Y}
                      width={g.width}
                      height={g.height}
                      rx={4}
                      className="sc-hstopo__group"
                    />
                    <text x={g.x + GROUP_PAD} y={GROUPS_Y + 15} className="sc-hstopo__group-label">
                      {trunc(t('hybridTopology.entityGroup', { entity: g.group.entity }), 36)}
                    </text>

                    {g.cols.map((c) => (
                      <NetworkColumn key={`col-${c.net.key}`} col={c} onSelect={onSelect} />
                    ))}
                  </g>
                ))}
              </g>
            </svg>
          </div>
          <TopologyLegend platformVisible={platformVisible} />
        </>
      )}
    </div>
  );
}

function NetworkColumn({
  col: c,
  onSelect,
}: {
  col: ColLayout;
  onSelect?: HybridTopologyProps['onSelect'];
}): React.ReactElement {
  const { t } = useTranslation();
  const net = c.net;
  const state = stateLabel(t, net.state);
  const placementsReady = t('hybridTopology.placementsReady', {
    ready: net.placementsReady,
    total: net.placements.length,
  });
  const badge =
    net.vpnTarget !== undefined ? t('hybridTopology.rtBadge', { target: net.vpnTarget }) : t('hybridTopology.rtNone');

  return (
    <g>
      <g
        transform={`translate(${c.cardX}, ${c.y})`}
        className={`sc-hstopo__node sc-hstopo__node--vrf sc-hstopo__node--${net.state}`}
        {...nodeHandlers({
          onActivate: onSelect ? () => onSelect('HybridNetwork', net.cr) : undefined,
        })}
      >
        <title>
          {[
            `${kindTerm(t, 'HybridNetwork')} ${net.namespace}/${net.name} — ${state}`,
            `${t('hybridTopology.routeTarget')}: ${net.routeTarget || t('hybridTopology.noRouteTarget')}`,
            net.mtu !== undefined ? t('networkOverview.mtuValue', { mtu: net.mtu }) : '',
            egressText(net.cr, t),
            `${t('detail.placements')}: ${placementsReady}`,
            net.description,
            net.state !== 'ready' ? displayText(t, net.message) : '',
          ]
            .filter(Boolean)
            .join('\n')}
        </title>
        <rect width={CARD_W} height={NET_H} rx={3} />
        <text x={10} y={16} className="sc-hstopo__node-kind">
          {t('hybridTopology.networkKind')}
          {net.mtu !== undefined ? ` · ${t('networkOverview.mtuValue', { mtu: net.mtu })}` : ''}
        </text>
        <text x={10} y={34} className="sc-hstopo__node-name">
          {trunc(net.name, 24)}
        </text>
        {/* The route target identifies the network across the core: loudest thing here. */}
        <g className={`sc-hstopo__rt${net.duplicateTarget ? ' sc-hstopo__rt--dup' : ''}`}>
          <rect x={10} y={41} width={net.duplicateTarget ? 150 : 86} height={20} rx={3} />
          <text x={18} y={55} className="sc-hstopo__rt-text">
            {net.duplicateTarget
              ? trunc(`${badge} ✖ ${t('hybridTopology.duplicate')}`, 22)
              : trunc(badge, 12)}
          </text>
        </g>
        <text x={102} y={55} className="sc-hstopo__node-sub">
          {net.duplicateTarget ? '' : trunc(egressShort(t, net.cr), 18)}
        </text>
        <StateGlyph state={net.state} x={16} y={70} />
        <text x={28} y={74} className="sc-hstopo__node-state">
          {trunc(net.state === 'absent' ? t('hybridTopology.notPlaced') : `${state} · ${placementsReady}`, 32)}
        </text>
      </g>

      {c.chips.map((chip) => {
        const p = chip.p;
        const pState = stateLabel(t, p.state);
        return (
          <g
            key={`chip-${p.key}`}
            transform={`translate(${c.cardX}, ${chip.y})`}
            className={`sc-hstopo__node sc-hstopo__node--chip sc-hstopo__node--${p.state}`}
            {...nodeHandlers({
              onActivate: onSelect ? () => onSelect('NetworkPlacement', p.cr) : undefined,
            })}
          >
            <title>
              {[
                `${kindTerm(t, 'NetworkPlacement')} ${p.name} — ${pState}`,
                `${t('fields.backendName')}: ${objectLabel(t, { kind: p.backendKind, name: p.backendName })}`,
                `${t('hybridTopology.site')}: ${p.siteName || t('hybridTopology.unresolved')}`,
                p.cloudGateway ? `${t('detail.cloudGateway')}: ${p.cloudGateway}` : '',
                `${t('fields.prefixes')}: ${p.prefixes.join(', ') || '—'}`,
                p.l3Vni !== undefined ? `${t('detail.l3Vni')}: ${p.l3Vni}` : '',
                p.l2Vni !== undefined ? `${t('detail.l2Vni')}: ${p.l2Vni}` : '',
                p.namespaces.length ? `${t('detail.siteNamespaces')}: ${p.namespaces.join(', ')}` : '',
                p.state !== 'ready' ? displayText(t, p.message) : '',
              ]
                .filter(Boolean)
                .join('\n')}
            </title>
            <rect width={CARD_W} height={CHIP_H} rx={3} />
            <text x={10} y={15} className="sc-hstopo__node-kind">
              {t('hybridTopology.placementKind')}
            </text>
            <text x={10} y={30} className="sc-hstopo__chip-name">
              {trunc(objectLabel(t, { kind: p.backendKind, name: p.backendName }, { dense: true }), 28)}
            </text>
            <text x={10} y={43} className="sc-hstopo__chip-prefix">
              {trunc(p.prefixes.join(', ') || t('hybridTopology.noPrefixes'), 30)}
            </text>
            <text x={10} y={56} className="sc-hstopo__node-sub">
              {trunc(vniText(t, p), 34)}
            </text>
            <text x={10} y={69} className="sc-hstopo__node-sub">
              {trunc(siteText(t, p), 34)}
            </text>
            <StateGlyph state={p.state} x={16} y={77} />
            <text x={28} y={81} className="sc-hstopo__node-state">
              {pState}
            </text>
          </g>
        );
      })}
    </g>
  );
}

function FabricFacts({ x, width, fabric }: { x: number; width: number; fabric: TopoFabric }): React.ReactElement {
  const { t } = useTranslation();
  const facts: Array<[string, string]> = [
    [
      t('fields.borderGatewayNodes'),
      fabric.nodesTotal > 0 ? t('detail.countOfReady', { count: fabric.nodesReady, total: fabric.nodesTotal }) : '—',
    ],
    [t('fields.domainAsn'), fabric.asn !== undefined ? String(fabric.asn) : '—'],
    [t('fields.overlayMtu'), fabric.overlayMtu !== undefined ? String(fabric.overlayMtu) : '—'],
  ];
  // Cap the fact columns so a very wide canvas (many tenants) does not
  // smear three short facts across a metre of band.
  const colW = Math.min(Math.max(width / facts.length, 90), 230);
  const span = Math.max(Math.min(width, colW * facts.length), 120);
  const gap = 24;
  const poolW = fabric.pools.length
    ? Math.max((span - gap * (fabric.pools.length - 1)) / fabric.pools.length, 100)
    : 0;

  return (
    <g>
      {facts.map(([k, v], i) => (
        <g key={k} transform={`translate(${x + i * colW}, 0)`}>
          <text y={20} className="sc-hstopo__fact-k">
            {trunc(k, Math.floor(colW / 6.2))}
          </text>
          <text y={36} className="sc-hstopo__fact-v">
            {trunc(v, Math.floor(colW / 6.4))}
          </text>
        </g>
      ))}
      {/* Pool use is a magnitude, so a single-hue bar — never a status colour. */}
      {fabric.pools.map((p, i) => {
        const pct = p.size ? Math.min((p.allocated / p.size) * 100, 100) : 0;
        const range = p.start !== undefined && p.end !== undefined ? ` ${p.start}–${p.end}` : '';
        const label = `${t(p.key === 'l2vni' ? 'fields.l2vniPool' : 'fields.vniPool')}${range}`;
        const value =
          p.size !== undefined
            ? `${t('networkOverview.poolValue', { allocated: p.allocated, total: p.size })} (${Math.round(pct)}%)`
            : t('networkOverview.poolValue', { allocated: p.allocated, total: '—' });
        return (
          <g key={p.key} transform={`translate(${x + i * (poolW + gap)}, 50)`}>
            <text y={10} className="sc-hstopo__fact-k">
              {trunc(label, Math.floor(poolW / 6.2))}
            </text>
            <rect y={16} width={poolW} height={8} rx={4} className="sc-hstopo__pool-track" />
            <rect
              y={16}
              width={Math.max((poolW * pct) / 100, pct > 0 ? 3 : 0)}
              height={8}
              rx={4}
              className="sc-hstopo__pool-fill"
            />
            <text y={36} className="sc-hstopo__fact-v">
              {trunc(value, Math.floor(poolW / 6))}
            </text>
          </g>
        );
      })}
    </g>
  );
}

function TopologyLegend({ platformVisible }: { platformVisible: boolean }): React.ReactElement {
  const { t } = useTranslation();
  const states: NetState[] = ['ready', 'pending', 'failed', 'absent'];
  return (
    <div className="sc-hstopo__legends">
      <ul className="sc-netov__legend" aria-label={t('networkOverview.legend')}>
        {states.map((s) => (
          <li key={s} className="sc-hstopo__legend-item">
            <svg width={16} height={16} viewBox="-8 -8 16 16" aria-hidden="true">
              <StateGlyph state={s} x={0} y={0} />
            </svg>
            {stateLabel(t, s, 'hybridTopology.legendAbsent')}
          </li>
        ))}
      </ul>
      <ul className="sc-netov__legend" aria-label={t('hybridTopology.lineLegend')}>
        {platformVisible && (
          <>
            <li className="sc-hstopo__legend-item">
              <svg width={28} height={12} viewBox="0 0 28 12" aria-hidden="true">
                <path d="M 2 6 H 26" className="sc-hstopo__tunnel sc-hstopo__tunnel--ready" />
                <path d="M 2 6 H 26" className="sc-hstopo__edge sc-hstopo__edge--ready" />
              </svg>
              {t('hybridTopology.legendTunnel')}
            </li>
            <li className="sc-hstopo__legend-item">
              <svg width={28} height={12} viewBox="0 0 28 12" aria-hidden="true">
                <path d="M 2 6 H 26" className="sc-hstopo__edge sc-hstopo__edge--ready" />
              </svg>
              {t('hybridTopology.legendUnderlay')}
            </li>
          </>
        )}
        <li className="sc-hstopo__legend-item">
          <svg width={28} height={12} viewBox="0 0 28 12" aria-hidden="true">
            <path d="M 2 6 H 26" className="sc-hstopo__link sc-hstopo__link--ready" />
          </svg>
          {t('hybridTopology.legendPlacement')}
        </li>
      </ul>
    </div>
  );
}

/** The required non-colour, non-geometry view of exactly the same model. */
function TopologyTable({ model, showEntity }: { model: TopologyModel; showEntity: boolean }): React.ReactElement {
  const { t } = useTranslation();

  const gatewayRows: Array<{
    key: string;
    site: TopoSite;
    gw?: TopoGateway;
    node?: GatewayNodeView;
  }> = [];
  for (const s of model.sites) {
    if (s.gateways.length === 0) {
      gatewayRows.push({ key: s.name, site: s });
      continue;
    }
    for (const gw of s.gateways) {
      if (gw.nodes.length === 0) gatewayRows.push({ key: `${s.name}/${gw.name}`, site: s, gw });
      gw.nodes.forEach((node, i) => gatewayRows.push({ key: `${s.name}/${gw.name}/${node.name || i}`, site: s, gw, node }));
    }
  }

  const rows: Array<{
    key: string;
    entity: string;
    network: string;
    rt: string;
    dup: boolean;
    egress: string;
    placement: string;
    backend: string;
    site: string;
    prefixes: string;
    l3: string;
    l2: string;
    namespaces: string;
    state: NetState;
  }> = [];
  for (const g of model.groups) {
    for (const n of g.networks) {
      const base = {
        entity: g.entity,
        network: n.name,
        rt: n.routeTarget || '—',
        dup: n.duplicateTarget,
        egress: egressText(n.cr, t),
      };
      if (n.placements.length === 0) {
        rows.push({
          ...base,
          key: n.key,
          placement: '—',
          backend: '—',
          site: '—',
          prefixes: '—',
          l3: '—',
          l2: '—',
          namespaces: '—',
          state: n.state,
        });
        continue;
      }
      for (const p of n.placements) {
        rows.push({
          ...base,
          key: p.key,
          placement: p.name,
          backend: objectLabel(t, { kind: p.backendKind, name: p.backendName }, { dense: true }),
          site: p.siteName || t('hybridTopology.unresolved'),
          prefixes: p.prefixes.join(', ') || '—',
          l3: p.l3Vni !== undefined ? String(p.l3Vni) : '—',
          l2: p.l2Vni !== undefined ? String(p.l2Vni) : '—',
          namespaces: p.namespaces.join(', ') || '—',
          state: p.state,
        });
      }
    }
  }

  const tunnelCell = (gw?: TopoGateway, node?: GatewayNodeView): string => {
    if (!gw || gw.transport !== 'wireguard') return '—';
    if (!node) return t(`networkOverview.tunnel_${gw.tunnel || 'down'}`);
    return node.tunnel ? t(`detail.${node.tunnel === 'up' ? 'up' : 'down'}`) : '—';
  };
  const count = (v?: number): string => (v !== undefined ? String(v) : '—');

  return (
    <div className="sc-netov__matrix-wrap">
      {model.platformVisible && (
        <table className="sc-netov__table">
          <caption className="pf-v6-screen-reader">{t('hybridTopology.gatewayTableCaption')}</caption>
          <thead>
            <tr>
              <th scope="col">{t('hybridTopology.site')}</th>
              <th scope="col">{t('detail.siteType')}</th>
              <th scope="col">{t('detail.cloudGateway')}</th>
              <th scope="col">{t('fields.transport')}</th>
              <th scope="col">{t('detail.state')}</th>
              <th scope="col">{t('detail.node')}</th>
              <th scope="col">{t('fields.nodeUnderlayAddress')}</th>
              <th scope="col">{t('detail.siteSessions')}</th>
              <th scope="col">{t('detail.coreSessions')}</th>
              <th scope="col">{t('detail.upstreamSessions')}</th>
              <th scope="col">{t('detail.tunnel')}</th>
            </tr>
          </thead>
          <tbody>
            {gatewayRows.map(({ key, site, gw, node }) => (
              <tr key={key}>
                <td>{site.name}</td>
                <td>{site.type || '—'}</td>
                <td>{gw?.name ?? t('hybridTopology.noGateway')}</td>
                <td>{gw ? transportLabel(t, gw) : '—'}</td>
                <td>
                  {gw
                    ? stateLabel(t, gw.state)
                    : stateLabel(t, site.state, 'hybridTopology.noDetails')}
                </td>
                <td>
                  {node ? node.name || '—' : gw ? t('networkOverview.noNodeStatus') : '—'}
                  {node?.ready === false ? ` (${t('detail.notReady')})` : ''}
                </td>
                <td>{node?.address || '—'}</td>
                <td>{count(node?.site)}</td>
                <td>{count(node?.core)}</td>
                <td>{count(node?.upstream)}</td>
                <td>{tunnelCell(gw, node)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <table className={`sc-netov__table${model.platformVisible ? ' sc-hstopo__table2' : ''}`}>
        <caption className="pf-v6-screen-reader">{t('hybridTopology.networkTableCaption')}</caption>
        <thead>
          <tr>
            {showEntity && <th scope="col">{t('networkOverview.entity')}</th>}
            <th scope="col">{t('networkOverview.network')}</th>
            <th scope="col">{t('hybridTopology.routeTarget')}</th>
            <th scope="col">{t('fields.egressInternet')}</th>
            <th scope="col">{t('kinds.NetworkPlacement')}</th>
            <th scope="col">{t('fields.backendName')}</th>
            <th scope="col">{t('hybridTopology.site')}</th>
            <th scope="col">{t('fields.prefixes')}</th>
            <th scope="col">{t('detail.l3Vni')}</th>
            <th scope="col">{t('detail.l2Vni')}</th>
            <th scope="col">{t('detail.siteNamespaces')}</th>
            <th scope="col">{t('detail.state')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              {showEntity && <td>{r.entity}</td>}
              <td>{r.network}</td>
              <td>
                {r.rt}
                {r.dup ? ` (${t('hybridTopology.duplicate')})` : ''}
              </td>
              <td>{r.egress}</td>
              <td>{r.placement}</td>
              <td>{r.backend}</td>
              <td>{r.site}</td>
              <td>{r.prefixes}</td>
              <td>{r.l3}</td>
              <td>{r.l2}</td>
              <td>{r.namespaces}</td>
              <td>{stateLabel(t, r.state)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default HybridTopology;
