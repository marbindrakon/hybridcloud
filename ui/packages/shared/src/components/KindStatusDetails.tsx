import React, { useState } from 'react';
import {
  Alert,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Label,
  LabelGroup,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { CheckCircleIcon, ExclamationCircleIcon, InProgressIcon, MinusCircleIcon } from '@patternfly/react-icons';
import type {
  CloudGateway,
  CloudInfrastructure,
  CloudVirt,
  HybridFabric,
  HybridNetwork,
  HybridSovereignKind,
  K8sResource,
  NetworkPlacement,
} from '../types';
import { useTranslation } from '../i18n';
import { displayText, objectLabel } from '../i18n/vocabulary';
import { formatRelativeTime } from '../list/listColumns';
import { deletionBlockers } from '../status/phase';
import { TENANT_HIDDEN_PATHS, TENANT_SECRET_KEY_RE } from '../security/tenantHiddenPaths';
import { ToolSignInPanel } from './ToolSignInPanel';
import { Ipv6CloudInitSnippet } from './Ipv6CloudInitSnippet';
import { useSiteIpv6Caps } from '../hooks/ipv6';
import { familyUnavailable, networkIpv6Mode, placementSubnets } from '../forms/ipv6';
import { PART_REASONS, REASONS } from '../status/reasons.generated';

/** Status keys each kind's panel renders (left out of the generic status grid). */
export const KIND_STATUS_KEYS: Partial<Record<HybridSovereignKind, string[]>> = {
  HybridFabric: ['nodes', 'allocated', 'available', 'ledger'],
  CloudGateway: ['nodes', 'wireguard', 'site'],
  CloudInfrastructure: ['capabilities', 'site'],
  HybridNetwork: ['addressSpace', 'egress', 'fabric', 'mtu', 'placementCount', 'placementsReady', 'ipv6'],
  NetworkPlacement: [
    'site',
    'cloudGateway',
    'l3Vni',
    'l2Vni',
    'mtu',
    'prefixes',
    'subnets',
    'ipv6',
    'namespaces',
    'backendReady',
    'gatewayReady',
    'validated',
    'lastValidatedAt',
  ],
  // access: shown by the way-in panel (groups that can sign in)
  CloudVirt: ['site', 'namespaces', 'dns', 'storageClass', 'access'],
  // loginUrl / orgUrl: shown by the sign-in panel
  AAPOrg: ['loginUrl', 'orgUrl'],
  QuayOrg: ['loginUrl', 'orgUrl'],
  VaultKV: ['loginUrl'],
  Vault: ['loginUrl'],
};

/** Status keys shown by the common blocks on every kind. */
export const COMMON_STATUS_KEYS = ['waitingFor', 'deletionBlockedBy', 'realization', 'conditions', 'edaJobs'];

/** Paths never shown on tenant pages (one list for the UI: security/tenantHiddenPaths.ts). */
export { TENANT_HIDDEN_PATHS };

/** Copy of the object without the paths tenants must not see. */
export function redactForTenant<T extends K8sResource>(kind: HybridSovereignKind, item: T): T {
  const paths = TENANT_HIDDEN_PATHS[kind];
  if (!paths?.length) return item;
  const copy = JSON.parse(JSON.stringify(item)) as T;
  for (const path of paths) {
    const parts = path.split('.');
    let cur: Record<string, unknown> | undefined = copy as unknown as Record<string, unknown>;
    for (let i = 0; i < parts.length - 1 && cur; i += 1) {
      const next: unknown = cur[parts[i]];
      cur = next && typeof next === 'object' ? (next as Record<string, unknown>) : undefined;
    }
    if (cur) delete cur[parts[parts.length - 1]];
  }
  // Status keys that look like Vault paths or secret references never reach a tenant page, at any depth.
  const scrub = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(scrub);
    if (!v || typeof v !== 'object') return v;
    const out: Record<string, unknown> = {};
    for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
      if (TENANT_SECRET_KEY_RE.test(k)) continue;
      out[k] = scrub(val);
    }
    return out;
  };
  const withStatus = copy as unknown as { status?: unknown };
  if (withStatus.status) withStatus.status = scrub(withStatus.status);
  return copy;
}

function Dash(): React.ReactElement {
  return <span className="sc-text-muted">—</span>;
}

function Fact({ label, children }: { label: string; children: React.ReactNode }): React.ReactElement {
  return (
    <DescriptionListGroup>
      <DescriptionListTerm>{label}</DescriptionListTerm>
      <DescriptionListDescription>
        {children === undefined || children === null || children === '' ? <Dash /> : children}
      </DescriptionListDescription>
    </DescriptionListGroup>
  );
}

function Facts({ children }: { children: React.ReactNode }): React.ReactElement {
  return (
    <DescriptionList isCompact isHorizontal columnModifier={{ default: '1Col', lg: '2Col' }} className="sc-mb">
      {children}
    </DescriptionList>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }): React.ReactElement {
  return (
    <section style={{ marginBottom: '1.25rem' }}>
      <h3 className="sc-netov__title" style={{ marginBottom: '0.5rem' }}>
        {title}
      </h3>
      {children}
    </section>
  );
}

/** ready / not ready label (never colour alone: icon + text). */
function ReadyLabel({ ready }: { ready?: boolean }): React.ReactElement {
  const { t } = useTranslation();
  if (ready === undefined) return <Dash />;
  return ready ? (
    <Label color="green" icon={<CheckCircleIcon />} isCompact>
      {t('detail.ready')}
    </Label>
  ) : (
    <Label color="orange" icon={<InProgressIcon />} isCompact>
      {t('detail.notReady')}
    </Label>
  );
}

/** up / down label for tunnels and peers. */
function UpDownLabel({ up }: { up?: boolean | string }): React.ReactElement {
  const { t } = useTranslation();
  if (up === undefined || up === '') return <Dash />;
  const isUp = up === true || up === 'up';
  return isUp ? (
    <Label color="green" icon={<CheckCircleIcon />} isCompact>
      {t('detail.up')}
    </Label>
  ) : (
    <Label color="red" icon={<ExclamationCircleIcon />} isCompact>
      {t('detail.down')}
    </Label>
  );
}

function YesNo({ value }: { value?: boolean }): React.ReactElement {
  const { t } = useTranslation();
  if (value === undefined) return <Dash />;
  return <>{value ? t('common.yes') : t('common.no')}</>;
}

function Mono({ children }: { children?: React.ReactNode }): React.ReactElement {
  if (children === undefined || children === null || children === '') return <Dash />;
  return <code style={{ fontSize: '0.8125rem', wordBreak: 'break-all' }}>{children}</code>;
}

function List({ items }: { items?: Array<string | number> | null }): React.ReactElement {
  if (!items || items.length === 0) return <Dash />;
  return (
    <LabelGroup numLabels={8}>
      {items.map((i) => (
        <Label key={String(i)} isCompact color="grey">
          {String(i)}
        </Label>
      ))}
    </LabelGroup>
  );
}

function Link({ href }: { href?: string }): React.ReactElement {
  if (!href) return <Dash />;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" style={{ wordBreak: 'break-all' }}>
      {href}
    </a>
  );
}

function RelativeTime({ iso }: { iso?: string }): React.ReactElement {
  if (!iso) return <Dash />;
  const rel = formatRelativeTime(iso);
  return <span title={rel.title}>{rel.label}</span>;
}

/* ------------------------------------------------------------------ common blocks */

/** waitingFor / deletionBlockedBy, shown the same way on every kind. */
export function StatusWaitBlocks({ item }: { item: K8sResource }): React.ReactElement | null {
  const { t } = useTranslation();
  const waiting = Array.isArray(item.status?.waitingFor) ? item.status?.waitingFor ?? [] : [];
  const blocked = deletionBlockers(item).map((b) =>
    objectLabel(t, { kind: b.kind, name: b.name ?? '', namespace: b.namespace, detail: b.reason }, { dense: true }),
  );
  if (!waiting.length && !blocked.length) return null;
  return (
    <div style={{ display: 'grid', gap: '0.5rem', marginBottom: '1rem' }}>
      {waiting.length > 0 && (
        <Alert variant="info" isInline isPlain title={t('detail.waitingFor')}>
          <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
            {waiting.map((w) => (
              <li key={w}>{objectLabel(t, String(w), { dense: true })}</li>
            ))}
          </ul>
        </Alert>
      )}
      {blocked.length > 0 && (
        <Alert variant="warning" isInline isPlain title={t('detail.deletionBlockedBy')}>
          <ul style={{ margin: 0, paddingLeft: '1.25rem' }}>
            {blocked.map((w, i) => (
              <li key={`${w}-${i}`}>{w}</li>
            ))}
          </ul>
        </Alert>
      )}
    </div>
  );
}

/** status.realization rendered as-is (informational; depends on the platform). */
export function RealizationBlock({ item }: { item: K8sResource }): React.ReactElement | null {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const realization = (item.status as { realization?: unknown } | undefined)?.realization;
  if (realization === undefined || realization === null) return null;
  return (
    <ExpandableSection
      toggleText={t('detail.realization')}
      isExpanded={open}
      onToggle={(_e, v) => setOpen(v)}
      style={{ marginBottom: '1rem' }}
    >
      <p className="sc-text-muted">{t('detail.realizationHelp')}</p>
      <pre className="sc-yaml">{JSON.stringify(realization, null, 2)}</pre>
    </ExpandableSection>
  );
}

/* ------------------------------------------------------------------ per kind */

function HybridFabricDetails({ item }: { item: HybridFabric }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? ({} as HybridFabric['spec']);
  const st = item.status ?? {};
  const statusNodes = st.nodes ?? [];
  const specNodes = spec.borderGateway?.nodes ?? [];
  const nodeNames = Array.from(new Set([...specNodes.map((n) => n.name), ...statusNodes.map((n) => n.name ?? '')])).filter(Boolean);
  const readyCount = statusNodes.filter((n) => n.ready).length;
  const allocated = st.allocated ?? {};
  const available = st.available ?? {};
  const pools = Array.from(new Set([...Object.keys(allocated), ...Object.keys(available)])).sort();
  const entities = (spec.entityRefs ?? []).map((r) => r.name);

  return (
    <>
      <Facts>
        <Fact label={t('fields.domainAsn')}>{spec.domainAsn}</Fact>
        <Fact label={t('fields.overlayMtu')}>{spec.overlayMtu}</Fact>
        <Fact label={t('fields.vniPool')}>
          {spec.vniPool ? `${spec.vniPool.start}–${spec.vniPool.end}` : undefined}
        </Fact>
        <Fact label={t('fields.l2vniPool')}>
          {spec.l2vniPool ? `${spec.l2vniPool.start}–${spec.l2vniPool.end}` : undefined}
        </Fact>
        <Fact label={t('detail.nodesReady')}>
          {t('detail.countOf', { count: readyCount, total: Math.max(specNodes.length, statusNodes.length) })}
        </Fact>
        <Fact label={t('detail.ledger')}>{st.ledger ? <Mono>{st.ledger}</Mono> : undefined}</Fact>
        <Fact label={t('fields.entityRefs')}>
          {entities.length ? <List items={entities} /> : t('form.allEntities')}
        </Fact>
      </Facts>

      <Section title={t('detail.borderGatewayNodes')}>
        <Table aria-label={t('detail.borderGatewayNodes')} variant="compact">
          <Thead>
            <Tr>
              <Th modifier="wrap">{t('common.name')}</Th>
              <Th modifier="wrap">{t('fields.nodeAddress')}</Th>
              <Th modifier="wrap">{t('detail.ready')}</Th>
              <Th modifier="wrap">{t('detail.lastApplied')}</Th>
              <Th modifier="wrap">{t('common.message')}</Th>
            </Tr>
          </Thead>
          <Tbody>
            {nodeNames.map((name) => {
              const s = statusNodes.find((n) => n.name === name);
              const sp = specNodes.find((n) => n.name === name);
              return (
                <Tr key={name}>
                  <Td dataLabel={t('common.name')}>{name}</Td>
                  <Td dataLabel={t('fields.nodeAddress')}>{sp?.address ? <Mono>{sp.address}</Mono> : <Dash />}</Td>
                  <Td dataLabel={t('detail.ready')}>
                    <ReadyLabel ready={s?.ready} />
                  </Td>
                  <Td dataLabel={t('detail.lastApplied')}>
                    <RelativeTime iso={s?.lastAppliedAt} />
                  </Td>
                  <Td dataLabel={t('common.message')}>{s?.message ? displayText(t, s.message) : <Dash />}</Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>
      </Section>

      <Section title={t('detail.poolUse')}>
        {pools.length === 0 ? (
          <p className="sc-text-muted">{t('detail.noPoolData')}</p>
        ) : (
          <Table aria-label={t('detail.poolUse')} variant="compact">
            <Thead>
              <Tr>
                <Th modifier="wrap">{t('detail.pool')}</Th>
                <Th modifier="wrap">{t('detail.allocated')}</Th>
                <Th modifier="wrap">{t('detail.available')}</Th>
                <Th modifier="wrap">{t('detail.use')}</Th>
              </Tr>
            </Thead>
            <Tbody>
              {pools.map((p) => {
                const a = typeof allocated[p] === 'number' ? allocated[p] : undefined;
                const v = typeof available[p] === 'number' ? available[p] : undefined;
                const total = (a ?? 0) + (v ?? 0);
                const pct = total > 0 ? Math.round(((a ?? 0) / total) * 100) : 0;
                return (
                  <Tr key={p}>
                    <Td dataLabel={t('detail.pool')}>
                      <Mono>{p}</Mono>
                    </Td>
                    <Td dataLabel={t('detail.allocated')}>{a ?? <Dash />}</Td>
                    <Td dataLabel={t('detail.available')}>{v ?? <Dash />}</Td>
                    <Td dataLabel={t('detail.use')}>
                      <PoolBar allocated={a ?? 0} total={total} label={p} pct={pct} />
                    </Td>
                  </Tr>
                );
              })}
            </Tbody>
          </Table>
        )}
      </Section>
    </>
  );
}

/** Magnitude bar (single hue), with the numbers as text. */
export function PoolBar({
  allocated,
  total,
  label,
  pct,
}: {
  allocated: number;
  total: number;
  label: string;
  pct: number;
}): React.ReactElement {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', minWidth: '8rem' }}>
      <div
        className="sc-netov__pool-track"
        role="meter"
        aria-valuenow={allocated}
        aria-valuemin={0}
        aria-valuemax={total || allocated}
        aria-label={label}
        style={{ flex: 1 }}
      >
        <span className="sc-netov__pool-fill" style={{ width: `${Math.max(pct, total ? 1 : 0)}%` }} />
      </div>
      <span className="sc-text-muted" style={{ fontSize: '0.75rem' }}>
        {pct}%
      </span>
    </div>
  );
}

function CloudGatewayDetails({ item }: { item: CloudGateway }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? ({} as CloudGateway['spec']);
  const st = item.status ?? {};
  const transport = spec.transport || 'none';
  const wireguard = transport === 'wireguard';
  const nodes = st.nodes ?? [];
  const wgNodes = st.wireguard?.nodes ?? [];

  const handshake = (epoch?: number): React.ReactNode => {
    if (!epoch) return t('detail.never');
    const iso = new Date(epoch * 1000).toISOString();
    return <RelativeTime iso={iso} />;
  };

  return (
    <>
      <Facts>
        <Fact label={t('fields.fabricRef')}>{spec.fabricRef?.name}</Fact>
        <Fact label={t('fields.site')}>{spec.cloudRef?.name}</Fact>
        <Fact label={t('detail.siteType')}>{st.site?.type}</Fact>
        <Fact label={t('detail.siteEvpn')}>
          <YesNo value={st.site?.evpn} />
        </Fact>
        <Fact label={t('fields.transport')}>
          {wireguard ? t('fields.transportWireguard') : t('fields.transportNone')}
        </Fact>
        {wireguard ? (
          <Fact label={t('fields.tunnelCidr')}>{spec.siteUnderlay?.cidr ? <Mono>{spec.siteUnderlay.cidr}</Mono> : undefined}</Fact>
        ) : (
          <>
            <Fact label={t('fields.siteUnderlayVlan')}>{spec.siteUnderlay?.vlan}</Fact>
            <Fact label={t('fields.siteUnderlayCidr')}>
              {spec.siteUnderlay?.cidr ? <Mono>{spec.siteUnderlay.cidr}</Mono> : undefined}
            </Fact>
          </>
        )}
        <Fact label={t('fields.bfd')}>
          <YesNo value={spec.bfd} />
        </Fact>
        <Fact label={t('fields.upstreamRouteReflectors')}>
          <List items={spec.siteUnderlay?.upstreamRouteReflectors} />
        </Fact>
      </Facts>

      <Section title={t('detail.gatewayNodes')}>
        {nodes.length === 0 ? (
          <p className="sc-text-muted">{t('detail.noNodeStatus')}</p>
        ) : (
          <Table aria-label={t('detail.gatewayNodes')} variant="compact">
            <Thead>
              <Tr>
                <Th modifier="wrap">{t('common.name')}</Th>
                <Th modifier="wrap">{t('fields.nodeUnderlayAddress')}</Th>
                <Th modifier="wrap">{t('detail.ready')}</Th>
                <Th modifier="wrap">{t('detail.siteSessions')}</Th>
                <Th modifier="wrap">{t('detail.coreSessions')}</Th>
                <Th modifier="wrap">{t('detail.upstreamSessions')}</Th>
                {wireguard && <Th modifier="wrap">{t('detail.tunnel')}</Th>}
                <Th modifier="wrap">{t('common.message')}</Th>
              </Tr>
            </Thead>
            <Tbody>
              {nodes.map((n, i) => (
                <Tr key={n.name ?? i}>
                  <Td dataLabel={t('common.name')} modifier="nowrap">{n.name || <Dash />}</Td>
                  <Td dataLabel={t('fields.nodeUnderlayAddress')} modifier="nowrap">
                    <Mono>{n.address}</Mono>
                  </Td>
                  <Td dataLabel={t('detail.ready')}>
                    <ReadyLabel ready={n.ready} />
                  </Td>
                  <Td dataLabel={t('detail.siteSessions')}>{n.siteSessions ?? <Dash />}</Td>
                  <Td dataLabel={t('detail.coreSessions')}>{n.coreSessions ?? <Dash />}</Td>
                  <Td dataLabel={t('detail.upstreamSessions')}>{n.upstreamSessions ?? <Dash />}</Td>
                  {wireguard && (
                    <Td dataLabel={t('detail.tunnel')}>
                      <UpDownLabel up={n.tunnel} />
                    </Td>
                  )}
                  <Td dataLabel={t('common.message')}>{n.message ? displayText(t, n.message) : <Dash />}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Section>

      {(wireguard || wgNodes.length > 0) && (
        <Section title={t('detail.wireguard')}>
          {wgNodes.length === 0 ? (
            <p className="sc-text-muted">{t('detail.noWireguardStatus')}</p>
          ) : (
            <>
              <Table aria-label={t('detail.wireguardNodes')} variant="compact" className="sc-mb">
                <Thead>
                  <Tr>
                    <Th modifier="wrap">{t('common.name')}</Th>
                    <Th modifier="wrap">{t('fields.wireguardPublicKey')}</Th>
                    <Th modifier="wrap">{t('fields.wireguardListenPort')}</Th>
                    <Th modifier="wrap">{t('detail.tunnelAddress')}</Th>
                  </Tr>
                </Thead>
                <Tbody>
                  {wgNodes.map((n, i) => (
                    <Tr key={n.name ?? i}>
                      <Td dataLabel={t('common.name')}>{n.name || <Dash />}</Td>
                      <Td dataLabel={t('fields.wireguardPublicKey')}>
                        <Mono>{n.publicKey}</Mono>
                      </Td>
                      <Td dataLabel={t('fields.wireguardListenPort')}>{n.listenPort ?? <Dash />}</Td>
                      <Td dataLabel={t('detail.tunnelAddress')}>
                        <Mono>{n.address}</Mono>
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
              <Table aria-label={t('detail.wireguardPeers')} variant="compact">
                <Thead>
                  <Tr>
                    <Th modifier="wrap">{t('detail.node')}</Th>
                    <Th modifier="wrap">{t('detail.peer')}</Th>
                    <Th modifier="wrap">{t('fields.wireguardEndpoint')}</Th>
                    <Th modifier="wrap">{t('detail.lastHandshake')}</Th>
                    <Th modifier="wrap">{t('detail.state')}</Th>
                  </Tr>
                </Thead>
                <Tbody>
                  {wgNodes.flatMap((n, i) =>
                    (n.peers ?? []).map((p, j) => (
                      <Tr key={`${n.name ?? i}-${p.publicKey ?? j}`}>
                        <Td dataLabel={t('detail.node')}>{n.name || <Dash />}</Td>
                        <Td dataLabel={t('detail.peer')}>
                          <Mono>{p.publicKey}</Mono>
                        </Td>
                        <Td dataLabel={t('fields.wireguardEndpoint')}>
                          <Mono>{p.endpoint}</Mono>
                        </Td>
                        <Td dataLabel={t('detail.lastHandshake')}>{handshake(p.latestHandshake)}</Td>
                        <Td dataLabel={t('detail.state')}>
                          <UpDownLabel up={p.up} />
                        </Td>
                      </Tr>
                    )),
                  )}
                </Tbody>
              </Table>
            </>
          )}
        </Section>
      )}
    </>
  );
}

function CloudInfrastructureDetails({ item }: { item: CloudInfrastructure }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? ({} as CloudInfrastructure['spec']);
  const st = item.status ?? {};
  const caps = Object.entries(st.capabilities ?? {});
  const entities = (spec.entityRefs ?? []).map((r) => r.name);
  return (
    <>
      <Facts>
        <Fact label={t('fields.cloudInfraType')}>{spec.type}</Fact>
        <Fact label={t('fields.displayName')}>{spec.displayName}</Fact>
        <Fact label={t('fields.entityRefs')}>
          {entities.length ? <List items={entities} /> : t('form.allEntities')}
        </Fact>
        <Fact label={t('detail.endpoint')}>
          <Link href={typeof st.site?.endpoint === 'string' ? st.site.endpoint : undefined} />
        </Fact>
        <Fact label={t('fields.region')}>{typeof st.site?.region === 'string' ? st.site.region : undefined}</Fact>
        <Fact label={t('detail.version')}>{typeof st.site?.version === 'string' ? st.site.version : undefined}</Fact>
      </Facts>
      <Section title={t('detail.capabilities')}>
        {caps.length === 0 ? (
          <p className="sc-text-muted">{t('detail.noCapabilities')}</p>
        ) : (
          <LabelGroup numLabels={12}>
            {caps.map(([k, v]) => (
              <Label
                key={k}
                isCompact
                color={v ? 'green' : 'grey'}
                icon={v ? <CheckCircleIcon /> : <MinusCircleIcon />}
              >
                {k}
              </Label>
            ))}
          </LabelGroup>
        )}
      </Section>
    </>
  );
}

const EGRESS_COLOR: Record<string, 'green' | 'orange' | 'grey' | 'red'> = {
  ready: 'green',
  pending: 'orange',
  removing: 'orange',
  unavailable: 'red',
};

/** status.egress.internet state as a label (icon + text). */
export function EgressStateLabel({ state }: { state?: string | null }): React.ReactElement {
  const { t } = useTranslation();
  if (!state) return <Dash />;
  const color = EGRESS_COLOR[state] ?? 'grey';
  const Icon = state === 'ready' ? CheckCircleIcon : state === 'unavailable' ? ExclamationCircleIcon : InProgressIcon;
  return (
    <Label color={color} icon={<Icon />} isCompact>
      {t(`detail.egress_${state}`, { defaultValue: state })}
    </Label>
  );
}

/** "internet egress ready (addresses)" / "internet egress off" for one network, as plain text. */
export function egressText(n: HybridNetwork, t: (k: string, o?: Record<string, unknown>) => string): string {
  const wanted = n.spec?.egress?.internet === true;
  const state = n.status?.egress?.internet ?? '';
  if (!wanted && !state) return t('networkOverview.egressOff');
  const sources = Array.isArray(n.status?.egress?.sourceAddresses) ? n.status?.egress?.sourceAddresses ?? [] : [];
  const label = state ? t(`detail.egress_${state}`, { defaultValue: state }) : t('networkOverview.egressRequested');
  return sources.length
    ? t('networkOverview.egressWithSources', { state: label, sources: sources.join(', ') })
    : t('networkOverview.egressState', { state: label });
}

/** Condition reasons about a network's address space, shown on its IPv6 row. */
const NETWORK_SPACE_REASONS = ['WaitingForAddressSpace', 'IPv6ModeInUse', 'PrefixReserved', 'PrefixInUse', 'PrefixInvalid', 'FamilyUnavailable'];

/** A status reason in plain words (ipv6.reason.<Reason>), with the platform's message when it adds one. */
export function ipv6ReasonText(
  t: (k: string, o?: Record<string, unknown>) => string,
  reason: string | undefined,
  message?: string,
  scope?: 'ipv4' | 'translation',
): string {
  if (!reason) return message ?? '';
  // the catalogue's words, else the status contract's summary (English) of the reason. The unscoped words describe
  // IPv6 internet access; an IPv4 egress row or the translation row uses its own words, else the job's message.
  const words = scope
    ? t(`ipv6.reasonScoped.${scope}.${reason}`, { defaultValue: '' })
    : t(`ipv6.reason.${reason}`, { defaultValue: '' }) || PART_REASONS[reason]?.summary || REASONS[reason]?.summary || '';
  if (!words) return message || reason;
  // the job's message often repeats the reason's words: show it only when it adds something
  const norm = (x: string) => x.trim().replace(/[.\s]+$/, '').toLowerCase();
  if (!message || norm(words).includes(norm(message))) return words;
  if (norm(message).includes(norm(words))) return message;
  return `${words} ${message}`;
}

/** What the network asked for from the platform: "the fabric's default size", "/56", "2001:db8::/56 (pinned)". */
function platformRequestText(spec: HybridNetwork['spec'], t: (k: string, o?: Record<string, unknown>) => string): string {
  const q = (spec?.platformPrefixes ?? []).find((x) => Number(x?.family) === 6);
  if (!q) return '';
  if (q.prefix) return t('ipv6.requestPinned', { prefix: q.prefix });
  return q.prefixLength ? `/${q.prefixLength}` : t('ipv6.requestDefault');
}

function HybridNetworkDetails({ item }: { item: HybridNetwork }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? {};
  const st = item.status ?? {};
  const requested = spec.egress?.internet === true;
  const sources = Array.isArray(st.egress?.sourceAddresses) ? st.egress?.sourceAddresses ?? [] : [];
  const space = Array.isArray(st.addressSpace) ? st.addressSpace : [];
  const held = new Set(space.map((a) => a.prefix));
  const asked = (spec.prefixes ?? []).filter((p) => !held.has(p));
  const { mode, reported } = networkIpv6Mode(item);
  const platformBlocks = space.filter((a) => a.source === 'Platform' && Number(a.family) === 6);
  const platformAsk = platformRequestText(spec, t);
  const spaceCondition = (st.conditions ?? []).find((c) => c?.reason && NETWORK_SPACE_REASONS.includes(c.reason) && c.status !== 'True');
  const families = Array.isArray(st.egress?.families) ? st.egress?.families ?? [] : [];
  const translation = st.egress?.ipv4Translation;
  const modeLabel = t(`ipv6.mode_${mode}`);
  return (
    <>
      <Section title={t('detail.addressSpace')}>
        {space.length === 0 ? (
          <List items={spec.prefixes} />
        ) : (
          <Table aria-label={t('detail.addressSpace')} variant="compact" className="sc-mb">
            <Thead>
              <Tr>
                <Th modifier="wrap">{t('detail.prefix')}</Th>
                <Th modifier="wrap">{t('detail.family')}</Th>
                <Th modifier="wrap">{t('ipv6.source')}</Th>
                <Th modifier="wrap">{t('detail.allocations')}</Th>
                <Th modifier="wrap">{t('detail.largestFree')}</Th>
              </Tr>
            </Thead>
            <Tbody>
              {space.map((a) => (
                <Tr key={a.prefix}>
                  <Td dataLabel={t('detail.prefix')}>
                    <Mono>{a.prefix}</Mono>
                  </Td>
                  <Td dataLabel={t('detail.family')}>{`IPv${a.family}`}</Td>
                  <Td dataLabel={t('ipv6.source')}>{t(`ipv6.source_${a.source === 'Platform' ? 'Platform' : 'Tenant'}`)}</Td>
                  <Td dataLabel={t('detail.allocations')}>{a.allocations ?? 0}</Td>
                  <Td dataLabel={t('detail.largestFree')}>
                    {a.largestFree != null ? `/${a.largestFree}` : t('detail.spaceFull')}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
        {space.length > 0 && asked.length > 0 && (
          <p className="sc-text-muted">{t('detail.addressSpacePending', { prefixes: asked.join(', ') })}</p>
        )}
      </Section>
      <Section title={t('ipv6.title')}>
        <Facts>
          <Fact label={t('ipv6.choiceLabel')}>{reported ? modeLabel : t('ipv6.modeFromSpec', { mode: modeLabel })}</Fact>
          {mode === 'Platform' && <Fact label={t('ipv6.platformRequested')}>{platformAsk}</Fact>}
          {mode === 'Platform' && (
            <Fact label={t('ipv6.platformBlock')}>
              {st.ipv6?.block?.prefix ? (
                <>
                  <Mono>{st.ipv6.block.prefix}</Mono>
                  {st.ipv6.block.source ? ` (${t(`ipv6.blockSource_${st.ipv6.block.source}`, { defaultValue: st.ipv6.block.source })})` : ''}
                </>
              ) : platformBlocks.length ? (
                <List items={platformBlocks.map((a) => a.prefix)} />
              ) : (
                t('ipv6.blockNotAssigned')
              )}
            </Fact>
          )}
          {st.ipv6?.holdDownUntil && (
            <Fact label={t('ipv6.holdDown')}>
              {t('ipv6.holdDownUntil', { time: st.ipv6.holdDownUntil })}
            </Fact>
          )}
          {spaceCondition && <Fact label={t('ipv6.why')}>{ipv6ReasonText(t, spaceCondition.reason, spaceCondition.message)}</Fact>}
          {mode !== 'None' && (
            <Fact label={t('ipv6.internet')}>
              {(() => {
                const f6 = families.find((f) => Number(f.family) === 6);
                if (!f6) return requested ? undefined : t('ipv6.internetOff');
                return (
                  <>
                    <EgressStateLabel state={f6.state} /> {ipv6ReasonText(t, f6.reason, f6.message)}
                  </>
                );
              })()}
            </Fact>
          )}
        </Facts>
      </Section>
      <Facts>
        <Fact label={t('fields.fabricRef')}>{st.fabric || spec.fabricRef?.name}</Fact>
        <Fact label={t('detail.effectiveMtu')}>{st.mtu}</Fact>
        <Fact label={t('detail.requestedMtu')}>{spec.mtu ?? t('detail.fabricDefault')}</Fact>
        <Fact label={t('detail.placements')}>
          {st.placementCount !== undefined
            ? t('detail.countOfReady', { count: st.placementsReady ?? 0, total: st.placementCount })
            : undefined}
        </Fact>
      </Facts>
      <Section title={t('fields.egressInternet')}>
        <Facts>
          <Fact label={t('detail.requested')}>{requested ? t('common.enabled') : t('common.disabled')}</Fact>
          <Fact label={t('detail.state')}>
            <EgressStateLabel state={st.egress?.internet} />
          </Fact>
          <Fact label={t('detail.sourceAddresses')}>
            <List items={sources} />
          </Fact>
          {translation?.state && (
            <Fact label={t('ipv6.translation')}>
              <EgressStateLabel state={translation.state} /> {ipv6ReasonText(t, translation.reason, translation.message, 'translation')}
            </Fact>
          )}
        </Facts>
        {families.length > 0 && (
          <Table aria-label={t('ipv6.egressFamilies')} variant="compact" className="sc-mb">
            <Thead>
              <Tr>
                <Th modifier="wrap">{t('detail.family')}</Th>
                <Th modifier="wrap">{t('detail.state')}</Th>
                <Th modifier="wrap">{t('ipv6.egressMode')}</Th>
                <Th modifier="wrap">{t('ipv6.why')}</Th>
              </Tr>
            </Thead>
            <Tbody>
              {families.map((f, i) => (
                <Tr key={`${f.family}-${i}`}>
                  <Td dataLabel={t('detail.family')}>{`IPv${f.family}`}</Td>
                  <Td dataLabel={t('detail.state')}>
                    <EgressStateLabel state={f.state} />
                  </Td>
                  <Td dataLabel={t('ipv6.egressMode')}>{f.mode ? t(`ipv6.egressMode_${f.mode}`, { defaultValue: f.mode }) : <Dash />}</Td>
                  <Td dataLabel={t('ipv6.why')}>{f.reason || f.message ? ipv6ReasonText(t, f.reason, f.message, Number(f.family) === 6 ? undefined : 'ipv4') : <Dash />}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </Section>
    </>
  );
}

/** What a placement asked for, per family: "IPv4 /24", "IPv4 10.60.5.0/24 (pinned)". */
export function placementRequestText(
  spec: Partial<NetworkPlacement['spec']> | undefined,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const parts: string[] = [];
  for (const [family, req] of [
    [4, spec?.ipv4],
    [6, spec?.ipv6],
  ] as const) {
    if (!req) continue;
    const source = (req as { source?: string }).source;
    parts.push(
      source === 'Provider'
        ? t('ipv6.requestProvider')
        : req.prefix
          ? t('detail.requestPinned', { family, prefix: req.prefix })
          : req.prefixLength
            ? t('detail.requestSize', { family, length: req.prefixLength })
            : t('ipv6.requestSiteDefault', { family }),
    );
  }
  return parts.join(', ');
}

/** A placement's layer 3 and layer 2 VNIs: status.realization, else the top-level status fields. */
export function placementVnis(item: NetworkPlacement | undefined): { l3?: number; l2?: number } {
  const real = (item?.status?.realization ?? {}) as { l3Vni?: unknown; l2Vni?: unknown };
  const pick = (a: unknown, b: unknown): number | undefined =>
    typeof a === 'number' ? a : typeof b === 'number' ? b : undefined;
  return { l3: pick(real.l3Vni, item?.status?.l3Vni), l2: pick(real.l2Vni, item?.status?.l2Vni) };
}

/** One subnet row: "10.60.1.0/24 (gateway 10.60.1.1, from the network)". */
function subnetText(
  s: { prefix?: string; gateway?: string; source?: string },
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const parts = [
    s.gateway ? t('ipv6.gatewayIs', { gateway: s.gateway }) : '',
    s.source ? t(`ipv6.subnetSource_${s.source}`, { defaultValue: s.source }) : '',
  ].filter(Boolean);
  return parts.length ? `${s.prefix} (${parts.join(', ')})` : s.prefix ?? '';
}

function NetworkPlacementDetails({ item }: { item: NetworkPlacement }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? ({} as NetworkPlacement['spec']);
  const st = item.status ?? {};
  const vnis = placementVnis(item);
  const subnets = placementSubnets(item);
  const refused = familyUnavailable(item);
  const v6 = st.ipv6;
  const siteCaps = useSiteIpv6Caps(item.metadata?.namespace, { enabled: subnets.some((x) => Number(x.family) === 6) });
  return (
    <>
      {refused && (
        <Alert variant="warning" isInline title={t('ipv6.familyUnavailableTitle')} className="sc-mb">
          {[t('ipv6.familyUnavailableBody'), refused.message, t('ipv6.familyUnavailableFix')].filter(Boolean).join(' ')}
        </Alert>
      )}
      <Facts>
        <Fact label={t('fields.network')}>{spec.network}</Fact>
        <Fact label={t('fields.backendName')}>
          {spec.backend ? objectLabel(t, { kind: spec.backend.kind, name: spec.backend.name }, { dense: true }) : undefined}
        </Fact>
        <Fact label={t('detail.placementRequest')}>{placementRequestText(spec, t)}</Fact>
        {[4, 6].map((family) => {
          const rows = subnets.filter((x) => Number(x.family) === family && x.prefix);
          const askedFor = family === 4 ? !!spec.ipv4 : !!spec.ipv6;
          if (!rows.length && !askedFor) return null;
          return (
            <Fact key={family} label={t('ipv6.subnetOfFamily', { family })}>
              {rows.length ? <List items={rows.map((r) => subnetText(r, t))} /> : undefined}
            </Fact>
          );
        })}
        {v6?.addressing && <Fact label={t('ipv6.addressing')}>{v6.addressing}</Fact>}
        {v6?.internet && (
          <Fact label={t('ipv6.internet')}>
            <EgressStateLabel state={v6.internet.state} />{' '}
            {v6.internet.path ? t(`ipv6.path_${v6.internet.path}`, { defaultValue: v6.internet.path }) : ''}{' '}
            {ipv6ReasonText(t, v6.internet.reason, v6.internet.message)}
          </Fact>
        )}
        {v6?.translation?.pref64 && (
          <Fact label={t('ipv6.translationPrefix')}>
            <Mono>{v6.translation.pref64}</Mono>
          </Fact>
        )}
        {v6?.translation?.resolvers?.length ? (
          <Fact label={t('ipv6.resolvers')}>
            <List items={v6.translation.resolvers} />
          </Fact>
        ) : null}
        <Fact label={t('fields.site')}>{st.site}</Fact>
        <Fact label={t('detail.cloudGateway')}>{st.cloudGateway}</Fact>
        <Fact label={t('detail.l3Vni')}>{vnis.l3}</Fact>
        <Fact label={t('detail.l2Vni')}>{vnis.l2}</Fact>
        <Fact label={t('detail.effectiveMtu')}>{st.mtu}</Fact>
        <Fact label={t('detail.siteNamespaces')}>
          <List items={st.namespaces} />
        </Fact>
        <Fact label={t('detail.backendReady')}>
          <ReadyLabel ready={st.backendReady} />
        </Fact>
        <Fact label={t('detail.gatewayReady')}>
          <ReadyLabel ready={st.gatewayReady} />
        </Fact>
        <Fact label={t('detail.validated')}>
          <YesNo value={st.validated} />
        </Fact>
        <Fact label={t('detail.lastValidated')}>
          <RelativeTime iso={st.lastValidatedAt} />
        </Fact>
      </Facts>
      <Ipv6CloudInitSnippet item={item} caps={siteCaps.capsFor(st.site)} />
    </>
  );
}

function CloudVirtDetails({ item }: { item: CloudVirt }): React.ReactElement {
  const { t } = useTranslation();
  const spec = item.spec ?? ({} as CloudVirt['spec']);
  const st = item.status ?? {};
  const specNs = spec.namespaces ?? [];
  const statusNs = st.namespaces ?? [];
  const nsNames = Array.from(new Set([...specNs.map((n) => n.name), ...statusNs.map((n) => n.name ?? '')])).filter(
    Boolean,
  );
  const quota = spec.quota ?? {};
  const quotaText = [
    quota.cpu !== undefined ? `${t('detail.cpu')} ${quota.cpu}` : '',
    quota.memory !== undefined ? `${t('detail.memory')} ${quota.memory}` : '',
    quota.storage !== undefined ? `${t('detail.storage')} ${quota.storage}` : '',
    quota.virtualMachines !== undefined ? `${t('detail.vms')} ${quota.virtualMachines}` : '',
  ]
    .filter(Boolean)
    .join(' · ');
  return (
    <>
      <Facts>
        <Fact label={t('fields.site')}>{st.site?.name || spec.cloudRef?.name}</Fact>
        <Fact label={t('detail.siteConsole')}>
          <Link
            href={
              typeof st.endpoints?.console === 'string'
                ? st.endpoints.console
                : typeof st.site?.console === 'string'
                  ? st.site.console
                  : undefined
            }
          />
        </Fact>
        <Fact label={t('detail.endpoint')}>
          <Link href={typeof st.site?.endpoint === 'string' ? st.site.endpoint : undefined} />
        </Fact>
        <Fact label={t('detail.dnsZone')}>{st.dns?.zone ? <Mono>{st.dns.zone}</Mono> : undefined}</Fact>
        <Fact label={t('fields.storageClass')}>{st.storageClass || spec.storageClass}</Fact>
        <Fact label={t('fields.quota')}>{quotaText || t('detail.noQuota')}</Fact>
      </Facts>
      <Section title={t('fields.vmNamespaces')}>
        <Table aria-label={t('fields.vmNamespaces')} variant="compact">
          <Thead>
            <Tr>
              <Th modifier="wrap">{t('common.name')}</Th>
              <Th modifier="wrap">{t('detail.siteNamespace')}</Th>
              <Th modifier="wrap">{t('fields.namespaceNetwork')}</Th>
              <Th modifier="wrap">{t('detail.vms')}</Th>
              <Th modifier="wrap">{t('detail.ready')}</Th>
              <Th modifier="wrap">{t('common.message')}</Th>
            </Tr>
          </Thead>
          <Tbody>
            {nsNames.map((name) => {
              const s = statusNs.find((n) => n.name === name);
              const sp = specNs.find((n) => n.name === name);
              const network = s?.network || sp?.network;
              return (
                <Tr key={name}>
                  <Td dataLabel={t('common.name')}>{name}</Td>
                  <Td dataLabel={t('detail.siteNamespace')}>
                    <Mono>{s?.namespace}</Mono>
                  </Td>
                  <Td dataLabel={t('fields.namespaceNetwork')}>{network || t('form.siteDefaultNetwork')}</Td>
                  <Td dataLabel={t('detail.vms')}>
                    {(() => {
                      const vms = st.virtualMachines?.namespaces?.find((v) => (s?.namespace && v.namespace === s.namespace) || v.name === name);
                      return typeof vms?.count === 'number' ? vms.count : <Dash />;
                    })()}
                  </Td>
                  <Td dataLabel={t('detail.ready')}>
                    <ReadyLabel ready={s?.ready} />
                  </Td>
                  <Td dataLabel={t('common.message')}>{s?.message ? displayText(t, s.message) : <Dash />}</Td>
                </Tr>
              );
            })}
          </Tbody>
        </Table>
      </Section>
    </>
  );
}

export interface KindStatusDetailsProps {
  kind: HybridSovereignKind;
  item: K8sResource;
}

/** Kind-specific summary of a network object's spec and status (Overview tab). */
export function KindStatusDetails({ kind, item }: KindStatusDetailsProps): React.ReactElement | null {
  switch (kind) {
    case 'HybridFabric':
      return <HybridFabricDetails item={item as HybridFabric} />;
    case 'CloudGateway':
      return <CloudGatewayDetails item={item as CloudGateway} />;
    case 'CloudInfrastructure':
      return <CloudInfrastructureDetails item={item as CloudInfrastructure} />;
    case 'HybridNetwork':
      return <HybridNetworkDetails item={item as HybridNetwork} />;
    case 'NetworkPlacement':
      return <NetworkPlacementDetails item={item as NetworkPlacement} />;
    case 'CloudVirt':
      return <CloudVirtDetails item={item as CloudVirt} />;
    case 'AAPOrg':
    case 'QuayOrg':
    case 'VaultKV':
    case 'Vault':
      return <ToolSignInPanel kind={kind} item={item} />;
    default:
      return null;
  }
}
