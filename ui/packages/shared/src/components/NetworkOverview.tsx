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
import { HybridSovereignKind, K8sResource } from '../types';
import { useOverviewCRs } from '../hooks/overviewCrs';
import { normalizeHealth } from './StatusBadge';

/**
 * Network Overview — the hybrid fabric as one picture.
 *
 * Three views of the same data, in dependency order:
 *   1. Fabric summary  — each HybridFabric: ASN, border gateway, VNI pool use.
 *   2. Fabric map      — border gateway at the centre, one spoke per site
 *                        (CloudGateway -> CloudInfrastructure), link state on
 *                        the edge. This is the "across clouds" picture.
 *   3. VRF x backend   — rows are tenant HybridNetworks, columns are the cloud
 *                        projects they are placed on. A cell is one
 *                        NetworkPlacement.
 *
 * Accessibility: cell state is never colour alone — every cell and edge
 * carries an icon and a text label, there is always a legend, and the whole
 * matrix has a table view (the amber "pending" swatch is below 3:1 against the
 * surface, so the label is what carries the meaning).
 */

export type NetState = 'ready' | 'pending' | 'failed' | 'absent';

export interface NetworkOverviewProps {
  /** Scope to one tenant: only that entity's HybridNetworks are shown. */
  entityNamespace?: string;
  /** Pre-fetched Overview CRs, to avoid a second aggregate fetch. */
  resources?: K8sResource[];
  resourcesLoading?: boolean;
  resourcesError?: Error | null;
  /** Called when a node or row is activated, for drill-down navigation. */
  onSelect?: (kind: HybridSovereignKind, item: K8sResource) => void;
}

interface Spoke {
  gateway: K8sResource;
  siteName: string;
  siteType: string;
  state: NetState;
  tunnel: string;
  detail: string;
}

interface Backend {
  /** `${kind}/${name}` */
  id: string;
  kind: string;
  name: string;
  siteName: string;
}

interface Cell {
  state: NetState;
  prefixes: string[];
  placementName?: string;
}

const STATE_META: Record<
  NetState,
  { label: string; icon: React.ComponentType<{ className?: string }>; cls: string }
> = {
  ready: { label: 'Ready', icon: CheckCircleIcon, cls: 'ready' },
  pending: { label: 'Pending', icon: InProgressIcon, cls: 'pending' },
  failed: { label: 'Failed', icon: ExclamationCircleIcon, cls: 'failed' },
  absent: { label: 'Not placed', icon: MinusCircleIcon, cls: 'absent' },
};

function itemsOfKind(items: K8sResource[], kind: HybridSovereignKind): K8sResource[] {
  return items.filter((i) => (i.kind || '') === kind);
}

function stateOf(item: K8sResource): NetState {
  const health = normalizeHealth(st(item).ready as boolean | undefined, st(item).status as string | undefined);
  if (health === 'ready') return 'ready';
  if (health === 'failed') return 'failed';
  if (health === 'unknown') return 'pending';
  return 'pending';
}

function cloudRefName(item: K8sResource): string {
  const ref = (item.spec as { cloudRef?: { name?: string } } | undefined)?.cloudRef;
  return typeof ref?.name === 'string' ? ref.name : '';
}

/**
 * Per-kind status fields (vni, bgwEndpoint, placements, ...) are declared on the
 * specific *Status interfaces, but these lists arrive as the generic
 * K8sResource whose status is the base OperatorStatus. Read them as a bag.
 */
function st(item: K8sResource): Record<string, unknown> {
  return (item.status ?? {}) as unknown as Record<string, unknown>;
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' ? v : undefined;
}

/** Geometry for the radial fabric map. */
const MAP_W = 760;
const HUB_R = 54;
const SPOKE_W = 150;
const SPOKE_H = 56;
const ROW_GAP = 76;

export function NetworkOverview({
  entityNamespace,
  resources,
  resourcesLoading,
  resourcesError,
  onSelect,
}: NetworkOverviewProps): React.ReactElement {
  const [asTable, setAsTable] = useState(false);
  const useParent = resources !== undefined;
  const overview = useOverviewCRs(0, { enabled: !useParent });

  // Tenant scope still needs the platform half (fabric, gateways, sites) from
  // sovereign-cloud, so the aggregate fetch is used either way and the tenant's
  // own HybridNetworks are filtered out of it below.
  const items = useParent ? (resources as K8sResource[]) : overview.items;
  const loading = useParent ? !!resourcesLoading : overview.loading;
  const error = useParent ? resourcesError ?? null : overview.error;

  const model = useMemo(() => {
    const fabrics = itemsOfKind(items, 'HybridFabric');
    const gateways = itemsOfKind(items, 'CloudGateway');
    const links = itemsOfKind(items, 'TransportLink');
    const infra = itemsOfKind(items, 'CloudInfrastructure');
    const placements = itemsOfKind(items, 'NetworkPlacement');

    let networks = itemsOfKind(items, 'HybridNetwork');
    if (entityNamespace) {
      networks = networks.filter((n) => n.metadata.namespace === entityNamespace);
    }

    const infraByName = new Map(infra.map((i) => [i.metadata.name, i]));
    const linkByGateway = new Map(
      links.map((l) => [str((l.spec as { cloudGatewayRef?: string })?.cloudGatewayRef), l]),
    );

    // One spoke per CloudGateway: a site attached to the fabric.
    const spokes: Spoke[] = gateways.map((gw) => {
      const siteName = cloudRefName(gw) || str(st(gw).cloudInfrastructure);
      const site = infraByName.get(siteName);
      const link = linkByGateway.get(gw.metadata.name);
      const transportReady = st(gw).transportReady === true;
      const gwState = stateOf(gw);
      // The edge is only "ready" when the gateway AND its tunnel are up.
      const state: NetState = gwState === 'ready' && !transportReady ? 'pending' : gwState;
      const tunnel =
        str((link?.spec as { tunnelType?: string } | undefined)?.tunnelType) ||
        str((gw.spec as { transport?: { type?: string } } | undefined)?.transport?.type) ||
        'none';
      const peers = num(st(gw).peerCount);
      return {
        gateway: gw,
        siteName: siteName || '(unbound)',
        siteType: str((site?.spec as { type?: string } | undefined)?.type) || 'unknown',
        state,
        tunnel,
        detail: [
          `transport ${transportReady ? 'up' : 'down'}`,
          peers !== undefined ? `${peers} peer${peers === 1 ? '' : 's'}` : '',
          str(st(gw).gatewayAddress),
        ]
          .filter(Boolean)
          .join(' · '),
      };
    });

    // Columns: every cloud project that carries at least one placement, plus
    // every project on an attached site, so empty sites are still visible.
    const backendMap = new Map<string, Backend>();
    const addBackend = (kind: string, name: string) => {
      if (!kind || !name) return;
      const id = `${kind}/${name}`;
      if (backendMap.has(id)) return;
      const project = items.find((i) => (i.kind || '') === kind && i.metadata.name === name);
      backendMap.set(id, {
        id,
        kind,
        name,
        siteName: project ? cloudRefName(project) : '',
      });
    };
    for (const p of placements) {
      const b = (p.spec as { backend?: { kind?: string; name?: string } } | undefined)?.backend;
      addBackend(str(b?.kind), str(b?.name));
    }
    for (const n of networks) {
      const sp = (st(n).placements ?? []) as Array<{ backendKind?: string; backendName?: string }>;
      for (const p of sp) addBackend(str(p.backendKind), str(p.backendName));
    }
    const backends = Array.from(backendMap.values()).sort(
      (a, b) => a.siteName.localeCompare(b.siteName) || a.name.localeCompare(b.name),
    );

    // Cells: prefer the NetworkPlacement CR, fall back to the summary the
    // HybridNetwork operator writes into status.placements[].
    const cells = new Map<string, Cell>();
    for (const p of placements) {
      const spec = p.spec as
        | { network?: string; backend?: { kind?: string; name?: string }; prefixes?: string[] }
        | undefined;
      const netName = str(spec?.network);
      const id = `${str(spec?.backend?.kind)}/${str(spec?.backend?.name)}`;
      if (!netName || id === '/') continue;
      cells.set(`${p.metadata.namespace}|${netName}|${id}`, {
        state: stateOf(p),
        prefixes: Array.isArray(spec?.prefixes) ? (spec?.prefixes as string[]) : [],
        placementName: p.metadata.name,
      });
    }
    for (const n of networks) {
      const sp = (st(n).placements ?? []) as Array<{
        name?: string;
        backendKind?: string;
        backendName?: string;
        prefixes?: string[];
        ready?: boolean;
      }>;
      for (const p of sp) {
        const id = `${str(p.backendKind)}/${str(p.backendName)}`;
        const key = `${n.metadata.namespace}|${n.metadata.name}|${id}`;
        if (cells.has(key) || id === '/') continue;
        cells.set(key, {
          state: p.ready ? 'ready' : 'pending',
          prefixes: Array.isArray(p.prefixes) ? p.prefixes : [],
          placementName: str(p.name),
        });
      }
    }

    return { fabrics, spokes, backends, networks, cells, infra };
  }, [items, entityNamespace]);

  const cellFor = (net: K8sResource, backendId: string): Cell =>
    model.cells.get(`${net.metadata.namespace}|${net.metadata.name}|${backendId}`) ?? {
      state: 'absent',
      prefixes: [],
    };

  if (loading && model.fabrics.length === 0 && model.networks.length === 0) {
    return <Spinner aria-label="Loading network overview" />;
  }

  if (error && model.fabrics.length === 0) {
    return (
      <Alert variant="warning" title="Unable to load the network overview" isInline>
        {error.message || 'Failed to list Hybrid Sovereign networking resources.'}
      </Alert>
    );
  }

  if (model.fabrics.length === 0 && model.networks.length === 0) {
    return (
      <Alert variant="info" title="No hybrid networking configured" isInline>
        No HybridFabric or HybridNetwork found
        {entityNamespace ? ` in ${entityNamespace}` : ''}. A platform admin creates the fabric and
        its gateways; tenants then place HybridNetworks on cloud projects.
      </Alert>
    );
  }

  return (
    <div className="sc-netov" aria-label="Network overview">
      {model.fabrics.map((f) => (
        <FabricSummary key={f.metadata.name} fabric={f} spokes={model.spokes} />
      ))}

      {model.spokes.length > 0 && (
        <section className="sc-netov__section">
          <h3 className="sc-netov__title">Fabric map</h3>
          <p className="sc-netov__hint">
            The border gateway and every site attached to it. Tenant clusters are deliberately not
            fabric members — only cloud projects carry VRFs.
          </p>
          <FabricMap spokes={model.spokes} fabric={model.fabrics[0]} onSelect={onSelect} />
          <Legend />
        </section>
      )}

      <section className="sc-netov__section">
        <div className="sc-netov__section-head">
          <div>
            <h3 className="sc-netov__title">Networks across clouds</h3>
            <p className="sc-netov__hint">
              Each row is a tenant VRF; each column a cloud project it can be placed on. A cell is
              one NetworkPlacement.
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={asTable ? <TopologyIcon /> : <TableIcon />}
            onClick={() => setAsTable((v) => !v)}
          >
            {asTable ? 'Matrix view' : 'Table view'}
          </Button>
        </div>

        {model.networks.length === 0 ? (
          <Alert variant="info" title="No tenant networks" isInline>
            No HybridNetwork {entityNamespace ? `in ${entityNamespace}` : 'on this hub'} yet.
          </Alert>
        ) : asTable ? (
          <PlacementTable networks={model.networks} backends={model.backends} cellFor={cellFor} />
        ) : (
          <>
            <PlacementMatrix
              networks={model.networks}
              backends={model.backends}
              cellFor={cellFor}
              onSelect={onSelect}
            />
            <Legend />
          </>
        )}
      </section>
    </div>
  );
}

function FabricSummary({
  fabric,
  spokes,
}: {
  fabric: K8sResource;
  spokes: Spoke[];
}): React.ReactElement {
  const fs = st(fabric);
  const allocated = num(fs.allocatedVniCount) ?? 0;
  const available = num(fs.availableVniCount) ?? 0;
  const total = allocated + available;
  const pct = total > 0 ? Math.round((allocated / total) * 100) : 0;
  const asn =
    num((fabric.spec as { domainAsn?: number } | undefined)?.domainAsn) ?? num(fs.domainAsn);
  const state = stateOf(fabric);
  const Meta = STATE_META[state];
  const attached = spokes.filter((s) => s.state === 'ready').length;

  return (
    <section className="sc-netov__section sc-netov__fabric">
      <div className="sc-netov__fabric-head">
        <h3 className="sc-netov__title">
          Fabric <code>{fabric.metadata.name}</code>
        </h3>
        <span className={`sc-netov__state sc-netov__state--${Meta.cls}`}>
          <Meta.icon /> {Meta.label}
        </span>
      </div>
      <dl className="sc-netov__facts">
        <Fact label="Domain ASN" value={asn !== undefined ? String(asn) : '—'} />
        <Fact label="Border gateway" value={str(fs.bgwEndpoint) || str(fs.bgwAddress) || '—'} />
        <Fact
          label="Sites attached"
          value={`${attached} of ${spokes.length}`}
        />
        <Fact label="BGP peers" value={String(num(fs.bgwPeerCount) ?? 0)} />
      </dl>
      <div className="sc-netov__pool">
        <div className="sc-netov__pool-label">
          <span>VNI pool</span>
          <span className="sc-netov__pool-value">
            {allocated} allocated / {total || '—'}
          </span>
        </div>
        {/* Magnitude, so a single-hue bar, not a status colour. */}
        <div
          className="sc-netov__pool-track"
          role="meter"
          aria-valuenow={allocated}
          aria-valuemin={0}
          aria-valuemax={total || allocated}
          aria-label="VNI pool allocation"
        >
          <span className="sc-netov__pool-fill" style={{ width: `${Math.max(pct, total ? 1 : 0)}%` }} />
        </div>
      </div>
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

function FabricMap({
  spokes,
  fabric,
  onSelect,
}: {
  spokes: Spoke[];
  fabric?: K8sResource;
  onSelect?: NetworkOverviewProps['onSelect'];
}): React.ReactElement {
  const height = Math.max(spokes.length, 1) * ROW_GAP + 48;
  const hubX = 110;
  const hubY = height / 2;
  const spokeX = MAP_W - SPOKE_W - 24;

  return (
    <div className="sc-netov__map-wrap">
      <svg
        className="sc-netov__map"
        viewBox={`0 0 ${MAP_W} ${height}`}
        role="img"
        aria-label={`Fabric map: ${spokes.length} site${spokes.length === 1 ? '' : 's'} attached`}
      >
        {spokes.map((s, i) => {
          const y = 24 + i * ROW_GAP + SPOKE_H / 2;
          const x1 = hubX + HUB_R;
          const mx = (x1 + spokeX) / 2;
          const meta = STATE_META[s.state];
          return (
            <g key={s.gateway.metadata.name}>
              <path
                d={`M ${x1} ${hubY} C ${mx} ${hubY}, ${mx} ${y}, ${spokeX} ${y}`}
                className={`sc-netov__edge sc-netov__edge--${meta.cls}`}
              />
              <text
                x={mx}
                y={(hubY + y) / 2 - 6}
                textAnchor="middle"
                className="sc-netov__edge-label"
              >
                {s.tunnel}
              </text>
            </g>
          );
        })}

        <g transform={`translate(${hubX - HUB_R}, ${hubY - HUB_R})`}>
          <circle cx={HUB_R} cy={HUB_R} r={HUB_R} className="sc-netov__hub" />
          <text x={HUB_R} y={HUB_R - 4} textAnchor="middle" className="sc-netov__hub-kind">
            Border GW
          </text>
          <text x={HUB_R} y={HUB_R + 12} textAnchor="middle" className="sc-netov__hub-name">
            {fabric
              ? fabric.metadata.name.length > 17
                ? `${fabric.metadata.name.slice(0, 16)}…`
                : fabric.metadata.name
              : 'fabric'}
          </text>
        </g>

        {spokes.map((s, i) => {
          const y = 24 + i * ROW_GAP;
          const meta = STATE_META[s.state];
          return (
            <g
              key={`n-${s.gateway.metadata.name}`}
              transform={`translate(${spokeX}, ${y})`}
              className={`sc-netov__node sc-netov__node--${meta.cls}`}
              role={onSelect ? 'button' : undefined}
              tabIndex={onSelect ? 0 : undefined}
              onClick={onSelect ? () => onSelect('CloudGateway', s.gateway) : undefined}
              onKeyDown={
                onSelect
                  ? (e) => {
                      if (e.key === 'Enter' || e.key === ' ') onSelect('CloudGateway', s.gateway);
                    }
                  : undefined
              }
            >
              <title>{`${s.siteName} (${s.siteType}) — ${meta.label}\n${s.detail}`}</title>
              <rect width={SPOKE_W} height={SPOKE_H} rx={3} />
              <text x={12} y={20} className="sc-netov__node-kind">
                {s.siteType}
              </text>
              <text x={12} y={37} className="sc-netov__node-name">
                {s.siteName.length > 17 ? `${s.siteName.slice(0, 16)}…` : s.siteName}
              </text>
              <text x={12} y={50} className="sc-netov__node-state">
                {meta.label}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function PlacementMatrix({
  networks,
  backends,
  cellFor,
  onSelect,
}: {
  networks: K8sResource[];
  backends: Backend[];
  cellFor: (n: K8sResource, id: string) => Cell;
  onSelect?: NetworkOverviewProps['onSelect'];
}): React.ReactElement {
  if (backends.length === 0) {
    return (
      <Alert variant="info" title="No placements yet" isInline>
        These networks exist as VRFs but are not placed on any cloud project.
      </Alert>
    );
  }
  return (
    <div className="sc-netov__matrix-wrap">
      <table className="sc-netov__matrix">
        <caption className="pf-v6-screen-reader">
          Tenant networks by cloud project; each cell gives the placement state
        </caption>
        <thead>
          <tr>
            <th scope="col" className="sc-netov__rowhead">
              Network (VRF)
            </th>
            {backends.map((b) => (
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
          {networks.map((n) => {
            const vni = num(st(n).vni);
            const rt = str(st(n).canonicalRt);
            const entity = str(st(n).entity);
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
                    {entity && <>{entity} · </>}
                    {vni !== undefined ? `VNI ${vni}` : 'VNI —'}
                    {rt && <> · RT {rt}</>}
                  </span>
                </th>
                {backends.map((b) => {
                  const c = cellFor(n, b.id);
                  const meta = STATE_META[c.state];
                  const prefixes = c.prefixes.length ? c.prefixes.join(', ') : 'no prefixes';
                  return (
                    <td key={b.id} className={`sc-netov__cell sc-netov__cell--${meta.cls}`}>
                      <span
                        className="sc-netov__cell-inner"
                        title={`${n.metadata.name} on ${b.name}: ${meta.label}${
                          c.state === 'absent' ? '' : `\n${prefixes}`
                        }`}
                      >
                        <meta.icon />
                        <span className="sc-netov__cell-label">{meta.label}</span>
                        {c.state !== 'absent' && c.prefixes.length > 0 && (
                          <span className="sc-netov__cell-prefix">{c.prefixes[0]}</span>
                        )}
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
  networks,
  backends,
  cellFor,
}: {
  networks: K8sResource[];
  backends: Backend[];
  cellFor: (n: K8sResource, id: string) => Cell;
}): React.ReactElement {
  const rows: Array<{
    net: string;
    entity: string;
    vni: string;
    rt: string;
    backend: string;
    state: NetState;
    prefixes: string;
    placement: string;
  }> = [];
  for (const n of networks) {
    for (const b of backends) {
      const c = cellFor(n, b.id);
      if (c.state === 'absent') continue;
      rows.push({
        net: n.metadata.name,
        entity: str(st(n).entity),
        vni: num(st(n).vni) !== undefined ? String(num(st(n).vni)) : '—',
        rt: str(st(n).canonicalRt) || '—',
        backend: `${b.kind}/${b.name}`,
        state: c.state,
        prefixes: c.prefixes.join(', ') || '—',
        placement: c.placementName ?? '—',
      });
    }
  }

  if (rows.length === 0) {
    return (
      <Alert variant="info" title="No placements yet" isInline>
        These networks exist as VRFs but are not placed on any cloud project.
      </Alert>
    );
  }

  return (
    <div className="sc-netov__matrix-wrap">
      <table className="sc-netov__table">
        <thead>
          <tr>
            <th scope="col">Network</th>
            <th scope="col">Entity</th>
            <th scope="col">VNI</th>
            <th scope="col">Route target</th>
            <th scope="col">Backend</th>
            <th scope="col">Placement</th>
            <th scope="col">Prefixes</th>
            <th scope="col">State</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const meta = STATE_META[r.state];
            return (
              <tr key={`${r.net}-${r.backend}`}>
                <td>{r.net}</td>
                <td>{r.entity || '—'}</td>
                <td>{r.vni}</td>
                <td>{r.rt}</td>
                <td>{r.backend}</td>
                <td>{r.placement}</td>
                <td>{r.prefixes}</td>
                <td>
                  <span className={`sc-netov__state sc-netov__state--${meta.cls}`}>
                    <meta.icon /> {meta.label}
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
  return (
    <ul className="sc-netov__legend" aria-label="State legend">
      {(Object.keys(STATE_META) as NetState[]).map((k) => {
        const m = STATE_META[k];
        return (
          <li key={k} className={`sc-netov__state sc-netov__state--${m.cls}`}>
            <m.icon /> {m.label}
          </li>
        );
      })}
    </ul>
  );
}

export default NetworkOverview;
