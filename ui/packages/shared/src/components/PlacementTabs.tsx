import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, Button, EmptyState, EmptyStateBody, Spinner } from '@patternfly/react-core';
import { LinkIcon } from '@patternfly/react-icons';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { useTranslation } from '../i18n';
import { useK8sResourceList } from '../hooks/k8s';
import { useRouteFor } from '../nav/useNav';
import { GatedButton } from '../access/GatedButton';
import { summaryOf, phaseOf } from '../status/phase';
import type { K8sResource, NetworkPlacement, NetworkPlacementBackendKind } from '../types';
import { StatusBadge } from './StatusBadge';
import { AttachNetworkDialog, type AttachEnvironmentRef } from './AttachNetworkDialog';

/** Kinds a network can be attached to (environment pages show the Networks tab). */
export const ENVIRONMENT_KINDS: NetworkPlacementBackendKind[] = ['CloudVirt', 'CloudOSO', 'CloudAWS', 'CloudGCP'];

export function isEnvironmentKind(kind: string): kind is NetworkPlacementBackendKind {
  return (ENVIRONMENT_KINDS as string[]).includes(kind);
}

/** The address block of a placement: allocated prefix, else what was asked for. */
export function placementBlock(p: NetworkPlacement): string {
  const allocated = (p.status?.prefixes ?? []).find((x) => !x.includes(':'));
  if (allocated) return allocated;
  const req = p.spec?.ipv4;
  if (req?.prefix) return req.prefix;
  if (req?.prefixLength) return `/${req.prefixLength}`;
  return '';
}

function NameLink({ kind, name, from }: { kind: string; name: string; from?: string }): React.ReactElement {
  const routeFor = useRouteFor();
  const href = routeFor(kind, name);
  // ?from=<Kind>/<name>: the placement page's breadcrumb leads back to the page it was opened from (N5).
  return href ? <Link to={from ? `${href}?from=${encodeURIComponent(from)}` : href}>{name}</Link> : <>{name}</>;
}

function PlacementTable({
  rows,
  other,
  otherLabel,
  from,
}: {
  rows: NetworkPlacement[];
  other: (p: NetworkPlacement) => { kind: string; name: string; label: string };
  otherLabel: string;
  from: string;
}): React.ReactElement {
  const { t } = useTranslation();
  return (
    <Table variant="compact" aria-label={t('attach.placementsTable')}>
      <Thead>
        <Tr>
          <Th>{t('kindsOne.NetworkPlacement')}</Th>
          <Th>{otherLabel}</Th>
          <Th>{t('attach.block')}</Th>
          <Th>{t('common.status')}</Th>
        </Tr>
      </Thead>
      <Tbody>
        {rows.map((p) => {
          const o = other(p);
          const phase = phaseOf(p);
          return (
            <Tr key={p.metadata.name}>
              <Td dataLabel={t('kindsOne.NetworkPlacement')}>
                <NameLink kind="NetworkPlacement" name={p.metadata.name} from={from} />
              </Td>
              <Td dataLabel={otherLabel}>
                {o.label} <NameLink kind={o.kind} name={o.name} />
              </Td>
              <Td dataLabel={t('attach.block')}>
                <code>{placementBlock(p) || '—'}</code>
              </Td>
              <Td dataLabel={t('common.status')}>
                <StatusBadge item={p} />
                {phase !== 'Ready' && <div className="sc-text-muted sc-status-summary">{summaryOf(p)}</div>}
              </Td>
            </Tr>
          );
        })}
      </Tbody>
    </Table>
  );
}

/**
 * Networks tab of an environment (CloudVirt, CloudOSO, CloudAWS, CloudGCP):
 * the networks attached to it and the Attach network action (OD25). For an
 * OpenShift Virtualization environment, a namespace that names a network
 * without a placement says so and offers to attach it.
 */
export function EnvironmentNetworksTab({
  kind,
  name,
  namespace,
  item,
  readOnly,
}: {
  kind: NetworkPlacementBackendKind;
  name: string;
  namespace: string;
  item?: K8sResource;
  readOnly?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const placements = useK8sResourceList<NetworkPlacement>('NetworkPlacement', { namespace, pollWhileInFlight: true });
  const [attach, setAttach] = useState<{ network?: string } | null>(null);
  const env: AttachEnvironmentRef = useMemo(() => ({ kind, name }), [kind, name]);
  const rows = placements.items.filter((p) => p.spec?.backend?.kind === kind && p.spec?.backend?.name === name);
  const attachedNetworks = new Set(rows.map((p) => p.spec.network));
  const wanted =
    kind === 'CloudVirt'
      ? Array.from(
          new Set(
            ((item?.spec as { namespaces?: { name: string; network?: string }[] } | undefined)?.namespaces ?? [])
              .map((n) => n.network)
              .filter((n): n is string => !!n && !attachedNetworks.has(n)),
          ),
        )
      : [];

  return (
    <div style={{ paddingTop: '1rem' }}>
      {!readOnly && (
        <div style={{ marginBottom: '1rem' }}>
          <GatedButton kind="NetworkPlacement" verb="create" namespace={namespace} variant="primary" icon={<LinkIcon />} onClick={() => setAttach({})}>
            {t('attach.attachNetwork')}
          </GatedButton>
        </div>
      )}
      {wanted.map((n) => (
        <Alert
          key={n}
          variant="info"
          isInline
          isPlain
          className="sc-mb"
          title={t('attach.waitingForNetwork', { network: n })}
          actionLinks={
            readOnly ? undefined : (
              <Button variant="link" isInline onClick={() => setAttach({ network: n })}>
                {t('attach.attach')}
              </Button>
            )
          }
        />
      ))}
      {placements.loading && !placements.items.length ? (
        <Spinner size="md" aria-label={t('common.loading')} />
      ) : placements.error ? (
        <Alert variant="warning" isInline title={placements.error.message} />
      ) : rows.length === 0 ? (
        <EmptyState titleText={t('attach.noNetworks')} headingLevel="h3">
          <EmptyStateBody>{t('attach.noNetworksBody')}</EmptyStateBody>
        </EmptyState>
      ) : (
        <PlacementTable
          rows={rows}
          otherLabel={t('kindsOne.HybridNetwork')}
          other={(p) => ({ kind: 'HybridNetwork', name: p.spec.network, label: '' })}
          from={`${kind}/${name}`}
        />
      )}
      <AttachNetworkDialog
        isOpen={!!attach}
        namespace={namespace}
        environment={env}
        network={attach?.network}
        onClose={() => setAttach(null)}
        onAttached={() => placements.refresh()}
      />
    </div>
  );
}

/** Environments tab of a network: where it is attached, and Attach to environment (OD25). */
export function NetworkEnvironmentsTab({
  network,
  namespace,
  readOnly,
}: {
  network: string;
  namespace: string;
  readOnly?: boolean;
}): React.ReactElement {
  const { t } = useTranslation();
  const placements = useK8sResourceList<NetworkPlacement>('NetworkPlacement', { namespace, pollWhileInFlight: true });
  const [open, setOpen] = useState(false);
  const rows = placements.items.filter((p) => p.spec?.network === network);
  return (
    <div style={{ paddingTop: '1rem' }}>
      {!readOnly && (
        <div style={{ marginBottom: '1rem' }}>
          <GatedButton kind="NetworkPlacement" verb="create" namespace={namespace} variant="primary" icon={<LinkIcon />} onClick={() => setOpen(true)}>
            {t('attach.attachToEnvironment')}
          </GatedButton>
        </div>
      )}
      {placements.loading && !placements.items.length ? (
        <Spinner size="md" aria-label={t('common.loading')} />
      ) : placements.error ? (
        <Alert variant="warning" isInline title={placements.error.message} />
      ) : rows.length === 0 ? (
        <EmptyState titleText={t('attach.notAttached')} headingLevel="h3">
          <EmptyStateBody>{t('attach.notAttachedBody')}</EmptyStateBody>
        </EmptyState>
      ) : (
        <PlacementTable
          rows={rows}
          otherLabel={t('attach.environment')}
          other={(p) => ({ kind: p.spec.backend.kind, name: p.spec.backend.name, label: t(`kindsOne.${p.spec.backend.kind}`) })}
          from={`HybridNetwork/${network}`}
        />
      )}
      <AttachNetworkDialog
        isOpen={open}
        namespace={namespace}
        network={network}
        onClose={() => setOpen(false)}
        onAttached={() => placements.refresh()}
      />
    </div>
  );
}
