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
  HybridFabric,
  HybridNetwork,
  HybridSovereignKind,
  K8sResource,
  NetworkPlacement,
} from '../types';
import { useOverviewCRs } from '../hooks/overviewCrs';
import { useTranslation } from '../i18n';
import { HybridTopology, describeGateway, stateOf, type NetState } from './HybridTopology';
import { PoolBar, egressText } from './KindStatusDetails';
import { attachBlock } from '../forms/attachable';

/**
 * Network Overview — the hybrid fabric as one picture.
 *
 * Three views of the same data, in dependency order:
 *   1. Fabric summary  — each HybridFabric: ASN, overlay MTU, border gateway nodes ready, pool use.
 *   2. Topology        — HybridTopology: the fabric, each CloudGateway with its transport (sessions
 *                        and tunnel state per node), the sites, then each entity's HybridNetworks and
 *                        their placements, with a line from every placement to the site it lands on.
 *                        One per fabric when there are several.
 *   3. Networks        — per entity, rows are HybridNetworks (with their internet egress), columns
 *                        are the entity's cloud environments (CloudVirt, CloudOSO). A cell is one
 *                        NetworkPlacement: its state and prefixes.
 *
 * Accessibility: state is never colour alone — every cell carries an icon and a text label, there
 * is a legend, and the topology and the network matrix each have a table view.
 */

export interface NetworkOverviewProps {
  /** Scope to one tenant: only that entity's networks and environments are shown. */
  entityNamespace?: string;
  /** Pre-fetched CRs, to avoid a second aggregate fetch. */
  resources?: K8sResource[];
  resourcesLoading?: boolean;
  resourcesError?: Error | null;
  /** Called when a gateway, network or placement is activated, for drill-down navigation. */
  onSelect?: (kind: HybridSovereignKind, item: K8sResource) => void;
  /**
   * Offer "Attach" on the empty cells of the network map (network not on that
   * environment yet): called with the network, the environment and the tenant
   * namespace. The page opens the one Attach dialog (OD25).
   */
  onAttach?: (network: string, environment: { kind: string; name: string }, namespace: string) => void;
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

export function NetworkOverview({
  entityNamespace,
  resources,
  resourcesLoading,
  resourcesError,
  onSelect,
  onAttach,
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
    const networks = itemsOfKind<HybridNetwork>(items, 'HybridNetwork').filter(inScope);
    const placements = itemsOfKind<NetworkPlacement>(items, 'NetworkPlacement').filter(inScope);
    const environments = [
      ...itemsOfKind<K8sResource>(items, 'CloudVirt'),
      ...itemsOfKind<K8sResource>(items, 'CloudOSO'),
    ].filter(inScope);

    // Each CloudGateway's state (a WireGuard gateway counts once its tunnel is up on every node),
    // grouped by the fabric it joins.
    const gatewaysByFabric = new Map<string, NetState[]>();
    for (const gw of gateways) {
      const fabricName = refName(gw, 'fabricRef');
      gatewaysByFabric.set(fabricName, [...(gatewaysByFabric.get(fabricName) ?? []), describeGateway(gw).state]);
    }
    const fabricNames = Array.from(
      new Set([...fabrics.map((f) => f.metadata.name), ...Array.from(gatewaysByFabric.keys())]),
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
        prefixes: Array.isArray(p.status?.prefixes) ? p.status.prefixes : [],
        placement: p,
      });
    }

    return { fabrics, fabricNames, gatewaysByFabric, entityGroups, cells, networks };
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
  // One topology per fabric when there are several; otherwise one, which a tenant that cannot read
  // the platform kinds still gets for its own networks.
  const multiFabric = model.fabricNames.length > 1;
  const topology = (fabricName?: string) => (
    <section className="sc-netov__section">
      <HybridTopology items={items} entityNamespace={entityNamespace} fabricName={fabricName} onSelect={onSelect} />
    </section>
  );

  return (
    <div className="sc-netov" aria-label={t('nav.networkOverview')}>
      {model.fabricNames.map((name) => {
        const fabric = model.fabrics.find((f) => f.metadata.name === name);
        return (
          <React.Fragment key={name || '-'}>
            {fabric && <FabricSummary fabric={fabric} gateways={model.gatewaysByFabric.get(name) ?? []} />}
            {multiFabric && topology(name)}
          </React.Fragment>
        );
      })}

      {!multiFabric && (model.fabricNames.length > 0 || model.networks.length > 0) && topology()}

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
                <PlacementMatrix group={g} cellFor={cellFor} onSelect={onSelect} onAttach={onAttach} />
              </div>
            ))}
            <Legend />
          </>
        )}
      </section>
    </div>
  );
}

function FabricSummary({ fabric, gateways }: { fabric: HybridFabric; gateways: NetState[] }): React.ReactElement {
  const { t } = useTranslation();
  const st = fabric.status ?? {};
  const state = stateOf(fabric);
  const Meta = STATE_ICON[state];
  const specNodes = fabric.spec?.borderGateway?.nodes ?? [];
  const statusNodes = st.nodes ?? [];
  const nodesReady = statusNodes.filter((n) => n.ready).length;
  const nodesTotal = Math.max(specNodes.length, statusNodes.length);
  const attached = gateways.filter((s) => s === 'ready').length;
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
          value={t('detail.countOf', { count: attached, total: gateways.length })}
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

function PlacementMatrix({
  group,
  cellFor,
  onSelect,
  onAttach,
}: {
  group: EntityGroup;
  cellFor: (n: HybridNetwork, id: string) => Cell;
  onSelect?: NetworkOverviewProps['onSelect'];
  onAttach?: NetworkOverviewProps['onAttach'];
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
                <span className="sc-netov__colhead-kind">{t(`kindsOne.${b.kind}`, { defaultValue: b.kind })}</span>
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
                  if (!c.placement && c.state === 'absent' && onAttach && attachBlock(b.kind, b.item, n.metadata.name)) {
                    // GT-F3: an environment none of whose namespaces names the network cannot take it.
                    const why = t('attach.notAttachable', { network: n.metadata.name, environment: b.name });
                    return (
                      <td key={b.id} className={`sc-netov__cell sc-netov__cell--${meta.cls}`}>
                        <span className="sc-netov__cell-inner sc-text-muted" title={why} aria-label={why}>
                          {t('attach.cannotAttach')}
                        </span>
                      </td>
                    );
                  }
                  if (!c.placement && c.state === 'absent' && onAttach) {
                    const attachLabel = t('attach.attachCell', { network: n.metadata.name, environment: b.name });
                    return (
                      <td key={b.id} className={`sc-netov__cell sc-netov__cell--${meta.cls}`}>
                        <Button
                          variant="link"
                          isInline
                          size="sm"
                          aria-label={attachLabel}
                          title={attachLabel}
                          onClick={() => onAttach(n.metadata.name, { kind: b.kind, name: b.name }, group.namespace)}
                        >
                          {t('attach.attach')}
                        </Button>
                      </td>
                    );
                  }
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
