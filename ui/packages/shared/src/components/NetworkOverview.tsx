import React, { useMemo, useState } from 'react';
import { Alert, Button, Spinner } from '@patternfly/react-core';
import {
  CheckCircleIcon,
  ExclamationCircleIcon,
  MinusCircleIcon,
  InProgressIcon,
  TableIcon,
  TopologyIcon,
} from '@patternfly/react-icons';
import type {
  CloudGateway,
  CloudInfrastructure,
  HybridFabric,
  HybridNetwork,
  HybridSovereignKind,
  K8sResource,
  NetworkPlacement,
} from '../types';
import { useOverviewCRs } from '../hooks/overviewCrs';
import { useTranslation } from '../i18n';
import { normalizeHealth } from './StatusBadge';
import { PoolBar } from './KindStatusDetails';

/**
 * Network Overview — the hybrid fabric as one picture.
 *
 * Three views of the same data, in dependency order:
 *   1. Fabric summary  — each HybridFabric: ASN, overlay MTU, border gateway nodes ready, pool use.
 *   2. Fabric map      — the fabric at the centre, one spoke per CloudGateway to its
 *                        CloudInfrastructure: site type, transport, sessions per node, tunnel state.
 *   3. Networks        — per entity, rows are HybridNetworks (with their internet egress), columns
 *                        are the entity's cloud environments (CloudVirt, CloudOSO). A cell is one
 *                        NetworkPlacement: its state and prefixes.
 *
 * Accessibility: state is never colour alone — every cell and edge carries an icon and a text
 * label, there is a legend, and the network matrix has a table view.
 */

export type NetState = 'ready' | 'pending' | 'failed' | 'absent';

export interface NetworkOverviewProps {
  /** Scope to one tenant: only that entity's networks and environments are shown. */
  entityNamespace?: string;
  /** Pre-fetched CRs, to avoid a second aggregate fetch. */
  resources?: K8sResource[];
  resourcesLoading?: boolean;
  resourcesError?: Error | null;
  /** Called when a gateway, network or placement is activated, for drill-down navigation. */
  onSelect?: (kind: HybridSovereignKind, item: K8sResource) => void;
}

interface NodeSessions {
  name: string;
  ready?: boolean;
  site?: number;
  core?: number;
  upstream?: number;
  tunnel?: string;
}

interface Spoke {
  gateway: CloudGateway;
  siteName: string;
  siteType: string;
  transport: string;
  state: NetState;
  /** wireguard only: up when every node's tunnel is up, down when one is down */
  tunnel: '' | 'up' | 'down' | 'partial';
  nodes: NodeSessions[];
}

interface Backend {
  /** `${kind}/${name}` within the entity */
  id: string;
  kind: string;
  name: string;
  siteName: string;
  item?: K8sResource;
}

interface Cell {
  state: NetState;
  prefixes: string[];
  placement?: NetworkPlacement;
}

interface EntityGroup {
  namespace: string;
  entity: string;
  networks: HybridNetwork[];
  backends: Backend[];
}

const STATE_ICON: Record<NetState, { icon: React.ComponentType<{ className?: string }>; cls: string }> = {
  ready: { icon: CheckCircleIcon, cls: 'ready' },
  pending: { icon: InProgressIcon, cls: 'pending' },
  failed: { icon: ExclamationCircleIcon, cls: 'failed' },
  absent: { icon: MinusCircleIcon, cls: 'absent' },
};

function itemsOfKind<T extends K8sResource>(items: K8sResource[], kind: HybridSovereignKind): T[] {
  return items.filter((i) => (i.kind || '') === kind) as T[];
}

function stateOf(item: K8sResource): NetState {
  const health = normalizeHealth(item.status?.ready, item.status?.status);
  if (health === 'ready') return 'ready';
  if (health === 'failed') return 'failed';
  return 'pending';
}

function refName(item: K8sResource, key: string): string {
  const ref = (item.spec as Record<string, unknown> | undefined)?.[key];
  if (ref && typeof ref === 'object') {
    const n = (ref as { name?: unknown }).name;
    return typeof n === 'string' ? n : '';
  }
  return '';
}

function entityOf(ns: string): string {
  return ns.replace(/^entity-/, '');
}

/** Geometry for the fabric map. */
const MAP_W = 760;
const HUB_R = 54;
const SPOKE_W = 230;
const SPOKE_BASE_H = 58;
const NODE_LINE_H = 13;
const SPOKE_GAP = 18;

export function NetworkOverview({
  entityNamespace,
  resources,
  resourcesLoading,
  resourcesError,
  onSelect,
}: NetworkOverviewProps): React.ReactElement {
  const { t } = useTranslation();
  const [asTable, setAsTable] = useState(false);
  const useParent = resources !== undefined;
  const overview = useOverviewCRs(0, { enabled: !useParent });

  // A tenant scope still shows the platform half (fabric, gateways, sites) when it is readable;
  // only the tenant's own networks and environments are drawn below it.
  const items = useParent ? (resources as K8sResource[]) : overview.items;
  const loading = useParent ? !!resourcesLoading : overview.loading;
  const error = useParent ? resourcesError ?? null : overview.error;

  const model = useMemo(() => {
    const inScope = (i: K8sResource) => !entityNamespace || i.metadata.namespace === entityNamespace;
    const fabrics = itemsOfKind<HybridFabric>(items, 'HybridFabric');
    const gateways = itemsOfKind<CloudGateway>(items, 'CloudGateway');
    const infra = itemsOfKind<CloudInfrastructure>(items, 'CloudInfrastructure');
    const networks = itemsOfKind<HybridNetwork>(items, 'HybridNetwork').filter(inScope);
    const placements = itemsOfKind<NetworkPlacement>(items, 'NetworkPlacement').filter(inScope);
    const environments = [
      ...itemsOfKind<K8sResource>(items, 'CloudVirt'),
      ...itemsOfKind<K8sResource>(items, 'CloudOSO'),
    ].filter(inScope);

    const infraByName = new Map(infra.map((i) => [i.metadata.name, i]));

    // One spoke per CloudGateway, grouped by the fabric it joins.
    const spokesByFabric = new Map<string, Spoke[]>();
    for (const gw of gateways) {
      const siteName = refName(gw, 'cloudRef');
      const site = infraByName.get(siteName);
      const transport = gw.spec?.transport || 'none';
      const nodes: NodeSessions[] = (gw.status?.nodes ?? []).map((n) => ({
        name: n.name ?? '',
        ready: n.ready,
        site: n.siteSessions,
        core: n.coreSessions,
        upstream: n.upstreamSessions,
        tunnel: n.tunnel,
      }));
      let tunnel: Spoke['tunnel'] = '';
      if (transport === 'wireguard') {
        const states = nodes.map((n) => n.tunnel).filter(Boolean);
        if (states.length === 0) tunnel = 'down';
        else if (states.every((s) => s === 'up')) tunnel = 'up';
        else if (states.every((s) => s !== 'up')) tunnel = 'down';
        else tunnel = 'partial';
      }
      const gwState = stateOf(gw);
      const spoke: Spoke = {
        gateway: gw,
        siteName: siteName || '—',
        siteType: (gw.status?.site?.type as string | undefined) || site?.spec?.type || '—',
        transport,
        // The edge is ready only when the gateway is ready and, for a tunnel, every node's tunnel is up.
        state: gwState === 'ready' && transport === 'wireguard' && tunnel !== 'up' ? 'pending' : gwState,
        tunnel,
        nodes,
      };
      const fabricName = refName(gw, 'fabricRef');
      spokesByFabric.set(fabricName, [...(spokesByFabric.get(fabricName) ?? []), spoke]);
    }
    const fabricNames = Array.from(
      new Set([...fabrics.map((f) => f.metadata.name), ...Array.from(spokesByFabric.keys())]),
    ).sort();

    // Environments (columns) and networks (rows), per entity namespace.
    const groups = new Map<string, EntityGroup>();
    const group = (ns: string): EntityGroup => {
      let g = groups.get(ns);
      if (!g) {
        g = { namespace: ns, entity: entityOf(ns), networks: [], backends: [] };
        groups.set(ns, g);
      }
      return g;
    };
    const addBackend = (ns: string, kind: string, name: string, item?: K8sResource) => {
      if (!kind || !name) return;
      const g = group(ns);
      const id = `${kind}/${name}`;
      if (g.backends.some((b) => b.id === id)) return;
      g.backends.push({ id, kind, name, siteName: item ? refName(item, 'cloudRef') : '', item });
    };
    for (const n of networks) group(n.metadata.namespace ?? '').networks.push(n);
    for (const e of environments) addBackend(e.metadata.namespace ?? '', e.kind || '', e.metadata.name, e);
    for (const p of placements) {
      addBackend(p.metadata.namespace ?? '', p.spec?.backend?.kind ?? '', p.spec?.backend?.name ?? '');
    }
    const entityGroups = Array.from(groups.values())
      .filter((g) => g.networks.length > 0 || g.backends.length > 0)
      .sort((a, b) => a.namespace.localeCompare(b.namespace));
    for (const g of entityGroups) {
      g.networks.sort((a, b) => a.metadata.name.localeCompare(b.metadata.name));
      g.backends.sort(
        (a, b) => a.siteName.localeCompare(b.siteName) || a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name),
      );
    }

    const cells = new Map<string, Cell>();
    for (const p of placements) {
      const net = p.spec?.network ?? '';
      const id = `${p.spec?.backend?.kind ?? ''}/${p.spec?.backend?.name ?? ''}`;
      if (!net || id === '/') continue;
      cells.set(`${p.metadata.namespace}|${net}|${id}`, {
        state: stateOf(p),
        prefixes: Array.isArray(p.spec?.prefixes) ? p.spec.prefixes : [],
        placement: p,
      });
    }

    return { fabrics, fabricNames, spokesByFabric, entityGroups, cells, networks };
  }, [items, entityNamespace]);

  const cellFor = (net: HybridNetwork, backendId: string): Cell =>
    model.cells.get(`${net.metadata.namespace}|${net.metadata.name}|${backendId}`) ?? {
      state: 'absent',
      prefixes: [],
    };

  if (loading && model.fabrics.length === 0 && model.networks.length === 0) {
    return <Spinner aria-label={t('networkOverview.loading')} />;
  }

  if (error && model.fabrics.length === 0 && model.networks.length === 0) {
    return (
      <Alert variant="warning" title={t('networkOverview.unableToLoad')} isInline>
        {error.message}
      </Alert>
    );
  }

  if (model.fabricNames.length === 0 && model.entityGroups.length === 0) {
    return (
      <Alert variant="info" title={t('networkOverview.emptyTitle')} isInline>
        {t('networkOverview.emptyBody')}
      </Alert>
    );
  }

  const showEntity = !entityNamespace;

  return (
    <div className="sc-netov" aria-label={t('nav.networkOverview')}>
      {model.fabricNames.map((name) => {
        const fabric = model.fabrics.find((f) => f.metadata.name === name);
        const spokes = model.spokesByFabric.get(name) ?? [];
        return (
          <React.Fragment key={name || '-'}>
            {fabric && <FabricSummary fabric={fabric} spokes={spokes} />}
            {spokes.length > 0 && (
              <section className="sc-netov__section">
                <h3 className="sc-netov__title">
                  {t('networkOverview.fabricMap')} {name && <code>{name}</code>}
                </h3>
                <p className="sc-netov__hint">{t('networkOverview.fabricMapHint')}</p>
                <FabricMap spokes={spokes} fabricName={name} onSelect={onSelect} />
                <Legend />
              </section>
            )}
          </React.Fragment>
        );
      })}

      <section className="sc-netov__section">
        <div className="sc-netov__section-head">
          <div>
            <h3 className="sc-netov__title">{t('networkOverview.networksTitle')}</h3>
            <p className="sc-netov__hint">{t('networkOverview.networksHint')}</p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={asTable ? <TopologyIcon /> : <TableIcon />}
            onClick={() => setAsTable((v) => !v)}
          >
            {asTable ? t('networkOverview.matrixView') : t('networkOverview.tableView')}
          </Button>
        </div>

        {model.entityGroups.length === 0 ? (
          <Alert variant="info" title={t('networkOverview.noNetworks')} isInline />
        ) : asTable ? (
          <PlacementTable groups={model.entityGroups} cellFor={cellFor} showEntity={showEntity} />
        ) : (
          <>
            {model.entityGroups.map((g) => (
              <div key={g.namespace} style={{ marginBottom: '1rem' }}>
                {showEntity && (
                  <h4 className="sc-netov__title" style={{ fontSize: '0.875rem' }}>
                    {t('networkOverview.entity')} <code>{g.entity}</code>
                  </h4>
                )}
                <PlacementMatrix group={g} cellFor={cellFor} onSelect={onSelect} />
              </div>
            ))}
            <Legend />
          </>
        )}
      </section>
    </div>
  );
}

function FabricSummary({ fabric, spokes }: { fabric: HybridFabric; spokes: Spoke[] }): React.ReactElement {
  const { t } = useTranslation();
  const st = fabric.status ?? {};
  const state = stateOf(fabric);
  const Meta = STATE_ICON[state];
  const specNodes = fabric.spec?.borderGateway?.nodes ?? [];
  const statusNodes = st.nodes ?? [];
  const nodesReady = statusNodes.filter((n) => n.ready).length;
  const nodesTotal = Math.max(specNodes.length, statusNodes.length);
  const attached = spokes.filter((s) => s.state === 'ready').length;
  const allocated = st.allocated ?? {};
  const available = st.available ?? {};
  const pools = Array.from(new Set([...Object.keys(allocated), ...Object.keys(available)])).sort();

  return (
    <section className="sc-netov__section sc-netov__fabric">
      <div className="sc-netov__fabric-head">
        <h3 className="sc-netov__title">
          {t('networkOverview.fabric')} <code>{fabric.metadata.name}</code>
        </h3>
        <span className={`sc-netov__state sc-netov__state--${Meta.cls}`}>
          <Meta.icon /> {t(`networkOverview.state_${state}`)}
        </span>
      </div>
      <dl className="sc-netov__facts">
        <Fact label={t('fields.domainAsn')} value={fabric.spec?.domainAsn !== undefined ? String(fabric.spec.domainAsn) : '—'} />
        <Fact
          label={t('fields.overlayMtu')}
          value={fabric.spec?.overlayMtu !== undefined ? String(fabric.spec.overlayMtu) : '—'}
        />
        <Fact
          label={t('networkOverview.borderGatewayNodesReady')}
          value={t('detail.countOf', { count: nodesReady, total: nodesTotal })}
        />
        <Fact
          label={t('networkOverview.sitesAttached')}
          value={t('detail.countOf', { count: attached, total: spokes.length })}
        />
      </dl>
      {pools.length > 0 && (
        <div className="sc-netov__pools" style={{ display: 'grid', gap: '0.375rem' }}>
          {pools.map((p) => {
            const a = typeof allocated[p] === 'number' ? allocated[p] : 0;
            const v = typeof available[p] === 'number' ? available[p] : 0;
            const total = a + v;
            const pct = total > 0 ? Math.round((a / total) * 100) : 0;
            return (
              <div key={p} className="sc-netov__pool">
                <div className="sc-netov__pool-label">
                  <span>
                    {t('networkOverview.pool')} <code>{p}</code>
                  </span>
                  <span className="sc-netov__pool-value">
                    {t('networkOverview.poolValue', { allocated: a, total })}
                  </span>
                </div>
                <PoolBar allocated={a} total={total} pct={pct} label={p} />
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function Fact({ label, value }: { label: string; value: string }): React.ReactElement {
  return (
    <div className="sc-netov__fact">
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}

function short(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function FabricMap({
  spokes,
  fabricName,
  onSelect,
}: {
  spokes: Spoke[];
  fabricName: string;
  onSelect?: NetworkOverviewProps['onSelect'];
}): React.ReactElement {
  const { t } = useTranslation();
  const heights = spokes.map((s) => SPOKE_BASE_H + Math.max(s.nodes.length, 1) * NODE_LINE_H);
  const tops: number[] = [];
  let y = 24;
  for (const h of heights) {
    tops.push(y);
    y += h + SPOKE_GAP;
  }
  const height = Math.max(y + 8, 2 * HUB_R + 48);
  const hubX = 110;
  const hubY = height / 2;
  const spokeX = MAP_W - SPOKE_W - 24;

  const nodeLine = (n: NodeSessions): string => {
    const parts = [
      t('networkOverview.sessionsSite', { count: n.site ?? 0 }),
      t('networkOverview.sessionsCore', { count: n.core ?? 0 }),
    ];
    if (n.upstream) parts.push(t('networkOverview.sessionsUpstream', { count: n.upstream }));
    if (n.tunnel) parts.push(t('networkOverview.tunnelState', { state: t(`detail.${n.tunnel === 'up' ? 'up' : 'down'}`) }));
    return `${n.name}: ${parts.join(' · ')}`;
  };

  return (
    <div className="sc-netov__map-wrap">
      <svg
        className="sc-netov__map"
        viewBox={`0 0 ${MAP_W} ${height}`}
        role="img"
        aria-label={t('networkOverview.mapAria', { count: spokes.length })}
      >
        {spokes.map((s, i) => {
          const cy = tops[i] + heights[i] / 2;
          const x1 = hubX + HUB_R;
          const mx = (x1 + spokeX) / 2;
          const meta = STATE_ICON[s.state];
          const label =
            s.transport === 'wireguard'
              ? `${s.transport} · ${t(`networkOverview.tunnel_${s.tunnel || 'down'}`)}`
              : s.transport;
          return (
            <g key={s.gateway.metadata.name}>
              <path
                d={`M ${x1} ${hubY} C ${mx} ${hubY}, ${mx} ${cy}, ${spokeX} ${cy}`}
                className={`sc-netov__edge sc-netov__edge--${meta.cls}`}
              />
              <text x={mx} y={(hubY + cy) / 2 - 6} textAnchor="middle" className="sc-netov__edge-label">
                {label}
              </text>
            </g>
          );
        })}

        <g transform={`translate(${hubX - HUB_R}, ${hubY - HUB_R})`}>
          <circle cx={HUB_R} cy={HUB_R} r={HUB_R} className="sc-netov__hub" />
          <text x={HUB_R} y={HUB_R - 4} textAnchor="middle" className="sc-netov__hub-kind">
            {t('networkOverview.hubLabel')}
          </text>
          <text x={HUB_R} y={HUB_R + 12} textAnchor="middle" className="sc-netov__hub-name">
            {short(fabricName || '—', 17)}
          </text>
        </g>

        {spokes.map((s, i) => {
          const meta = STATE_ICON[s.state];
          const select = onSelect ? () => onSelect('CloudGateway', s.gateway) : undefined;
          const lines = s.nodes.length ? s.nodes.map(nodeLine) : [t('networkOverview.noNodeStatus')];
          return (
            <g
              key={`n-${s.gateway.metadata.name}`}
              transform={`translate(${spokeX}, ${tops[i]})`}
              className={`sc-netov__node sc-netov__node--${meta.cls}`}
              role={select ? 'button' : undefined}
              tabIndex={select ? 0 : undefined}
              onClick={select}
              onKeyDown={
                select
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') select();
                    }
                  : undefined
              }
            >
              <title>
                {`${s.siteName} (${s.siteType}) — ${t(`networkOverview.state_${s.state}`)}\n${lines.join('\n')}`}
              </title>
              <rect width={SPOKE_W} height={heights[i]} rx={3} />
              <text x={12} y={18} className="sc-netov__node-kind">
                {`${s.siteType} · ${s.transport}`}
              </text>
              <text x={12} y={35} className="sc-netov__node-name">
                {short(s.siteName, 26)}
              </text>
              <text x={12} y={49} className="sc-netov__node-state">
                {t(`networkOverview.state_${s.state}`)}
              </text>
              {lines.map((line, j) => (
                <text key={j} x={12} y={49 + (j + 1) * NODE_LINE_H} className="sc-netov__node-state">
                  {short(line, 40)}
                </text>
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** "egress: ready (addresses)" / "egress: off" for one network. */
function egressText(n: HybridNetwork, t: (k: string, o?: Record<string, unknown>) => string): string {
  const wanted = n.spec?.egress?.internet === true;
  const state = n.status?.egress?.internet ?? '';
  if (!wanted && !state) return t('networkOverview.egressOff');
  const sources = Array.isArray(n.status?.egress?.sourceAddresses) ? n.status?.egress?.sourceAddresses ?? [] : [];
  const label = state ? t(`detail.egress_${state}`, { defaultValue: state }) : t('networkOverview.egressRequested');
  return sources.length
    ? t('networkOverview.egressWithSources', { state: label, sources: sources.join(', ') })
    : t('networkOverview.egressState', { state: label });
}

function PlacementMatrix({
  group,
  cellFor,
  onSelect,
}: {
  group: EntityGroup;
  cellFor: (n: HybridNetwork, id: string) => Cell;
  onSelect?: NetworkOverviewProps['onSelect'];
}): React.ReactElement {
  const { t } = useTranslation();
  if (group.networks.length === 0) {
    return <Alert variant="info" title={t('networkOverview.noNetworks')} isInline />;
  }
  if (group.backends.length === 0) {
    return <Alert variant="info" title={t('networkOverview.noEnvironments')} isInline />;
  }
  return (
    <div className="sc-netov__matrix-wrap">
      <table className="sc-netov__matrix">
        <caption className="pf-v6-screen-reader">{t('networkOverview.matrixCaption')}</caption>
        <thead>
          <tr>
            <th scope="col" className="sc-netov__rowhead">
              {t('networkOverview.network')}
            </th>
            {group.backends.map((b) => (
              <th key={b.id} scope="col" className="sc-netov__colhead">
                <span className="sc-netov__colhead-kind">{b.kind}</span>
                <span className="sc-netov__colhead-name" title={b.name}>
                  {b.name}
                </span>
                {b.siteName && <span className="sc-netov__colhead-site">{b.siteName}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {group.networks.map((n) => {
            const mtu = n.status?.mtu ?? n.spec?.mtu;
            return (
              <tr key={`${n.metadata.namespace}/${n.metadata.name}`}>
                <th scope="row" className="sc-netov__rowhead">
                  <button
                    type="button"
                    className="sc-netov__rowlink"
                    onClick={onSelect ? () => onSelect('HybridNetwork', n) : undefined}
                    disabled={!onSelect}
                  >
                    {n.metadata.name}
                  </button>
                  <span className="sc-netov__rowmeta">
                    {mtu !== undefined ? `${t('networkOverview.mtuValue', { mtu })} · ` : ''}
                    {egressText(n, t)}
                  </span>
                </th>
                {group.backends.map((b) => {
                  const c = cellFor(n, b.id);
                  const meta = STATE_ICON[c.state];
                  const label = t(`networkOverview.state_${c.state}`);
                  const prefixes = c.prefixes.join(', ');
                  const select =
                    onSelect && c.placement ? () => onSelect('NetworkPlacement', c.placement as K8sResource) : undefined;
                  return (
                    <td key={b.id} className={`sc-netov__cell sc-netov__cell--${meta.cls}`}>
                      <span
                        className="sc-netov__cell-inner"
                        title={`${n.metadata.name} → ${b.kind} ${b.name}: ${label}${prefixes ? `\n${prefixes}` : ''}`}
                        role={select ? 'button' : undefined}
                        tabIndex={select ? 0 : undefined}
                        onClick={select}
                        onKeyDown={
                          select
                            ? (e) => {
                                if (e.key === 'Enter' || e.key === ' ') select();
                              }
                            : undefined
                        }
                        style={select ? { cursor: 'pointer' } : undefined}
                      >
                        <meta.icon />
                        <span className="sc-netov__cell-label">{label}</span>
                        {c.state !== 'absent' && prefixes && <span className="sc-netov__cell-prefix">{prefixes}</span>}
                      </span>
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PlacementTable({
  groups,
  cellFor,
  showEntity,
}: {
  groups: EntityGroup[];
  cellFor: (n: HybridNetwork, id: string) => Cell;
  showEntity: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const rows: Array<{
    key: string;
    entity: string;
    net: string;
    egress: string;
    backend: string;
    placement: string;
    prefixes: string;
    state: NetState;
  }> = [];
  for (const g of groups) {
    for (const n of g.networks) {
      for (const b of g.backends) {
        const c = cellFor(n, b.id);
        if (c.state === 'absent') continue;
        rows.push({
          key: `${g.namespace}/${n.metadata.name}/${b.id}`,
          entity: g.entity,
          net: n.metadata.name,
          egress: egressText(n, t),
          backend: `${b.kind}/${b.name}`,
          placement: c.placement?.metadata.name ?? '—',
          prefixes: c.prefixes.join(', ') || '—',
          state: c.state,
        });
      }
    }
  }

  if (rows.length === 0) {
    return <Alert variant="info" title={t('networkOverview.noPlacements')} isInline />;
  }

  return (
    <div className="sc-netov__matrix-wrap">
      <table className="sc-netov__table">
        <thead>
          <tr>
            {showEntity && <th scope="col">{t('networkOverview.entity')}</th>}
            <th scope="col">{t('networkOverview.network')}</th>
            <th scope="col">{t('fields.egressInternet')}</th>
            <th scope="col">{t('fields.backendName')}</th>
            <th scope="col">{t('kinds.NetworkPlacement')}</th>
            <th scope="col">{t('fields.prefixes')}</th>
            <th scope="col">{t('detail.state')}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const meta = STATE_ICON[r.state];
            return (
              <tr key={r.key}>
                {showEntity && <td>{r.entity}</td>}
                <td>{r.net}</td>
                <td>{r.egress}</td>
                <td>{r.backend}</td>
                <td>{r.placement}</td>
                <td>{r.prefixes}</td>
                <td>
                  <span className={`sc-netov__state sc-netov__state--${meta.cls}`}>
                    <meta.icon /> {t(`networkOverview.state_${r.state}`)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Legend(): React.ReactElement {
  const { t } = useTranslation();
  return (
    <ul className="sc-netov__legend" aria-label={t('networkOverview.legend')}>
      {(Object.keys(STATE_ICON) as NetState[]).map((k) => {
        const m = STATE_ICON[k];
        return (
          <li key={k} className={`sc-netov__state sc-netov__state--${m.cls}`}>
            <m.icon /> {t(`networkOverview.state_${k}`)}
          </li>
        );
      })}
    </ul>
  );
}

export default NetworkOverview;
