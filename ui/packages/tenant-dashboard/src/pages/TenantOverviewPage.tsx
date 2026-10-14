import React, { useMemo } from 'react';
import {
  Title,
  Card,
  CardTitle,
  CardBody,
  CardHeader,
  Button,
  Spinner,
  Alert,
  Label,
  Flex,
  FlexItem,
  Progress,
  ProgressSize,
} from '@patternfly/react-core';
import {
  SyncIcon,
  ExclamationTriangleIcon,
  TopologyIcon,
} from '@patternfly/react-icons';
import { Table, Thead, Tr, Th, Tbody, Td } from '@patternfly/react-table';
import { Link, useNavigate } from 'react-router-dom';
import {
  EntityTopology,
  PageHeader,
  HealthDonut,
  InventoryCard,
  StatusBadge,
  KindIcon,
  normalizeHealth,
  useK8sResourceList,
  K8sResource,
  useTranslation,
  TENANT_OVERVIEW_KINDS,
  HybridSovereignKind,
} from '@hybridsovereign/shared';

interface TenantOverviewPageProps {
  namespace: string;
}

const KIND_PATH: Partial<Record<HybridSovereignKind, string>> = {
  Team: '/teams',
  Project: '/projects',
  PlatformOpenshift: '/platforms',
  Assignment: '/assignments',
  CloudOSO: '/cloudoso',
  CloudAWS: '/cloudaws',
  CloudVirt: '/cloudvirt',
  OpenStackMigration: '/migrations',
  Persona: '/personas',
  Rbac: '/rbac',
  Vault: '/vaults',
  VaultKV: '/vaultkvs',
  AAPOrg: '/aaporgs',
  QuayOrg: '/quayorgs',
  HybridNetwork: '/networks',
  NetworkPlacement: '/placements',
};

function bucket(items: K8sResource[]) {
  let ready = 0;
  let failed = 0;
  let pending = 0;
  for (const i of items) {
    const h = normalizeHealth(i.status?.ready, i.status?.status);
    if (h === 'ready') ready += 1;
    else if (h === 'failed') failed += 1;
    else pending += 1;
  }
  return { ready, failed, pending, total: items.length };
}

export function TenantOverviewPage({ namespace }: TenantOverviewPageProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const opts = { namespace };

  const teams = useK8sResourceList<K8sResource>('Team', opts);
  const projects = useK8sResourceList<K8sResource>('Project', opts);
  const platforms = useK8sResourceList<K8sResource>('PlatformOpenshift', opts);
  const assignments = useK8sResourceList<K8sResource>('Assignment', opts);
  const cloudoso = useK8sResourceList<K8sResource>('CloudOSO', opts);
  const cloudaws = useK8sResourceList<K8sResource>('CloudAWS', opts);
  const cloudvirt = useK8sResourceList<K8sResource>('CloudVirt', opts);
  const migrations = useK8sResourceList<K8sResource>('OpenStackMigration', opts);
  const personas = useK8sResourceList<K8sResource>('Persona', opts);
  const rbacs = useK8sResourceList<K8sResource>('Rbac', opts);
  const vaults = useK8sResourceList<K8sResource>('Vault', opts);
  const vaultkvs = useK8sResourceList<K8sResource>('VaultKV', opts);
  const aaporgs = useK8sResourceList<K8sResource>('AAPOrg', opts);
  const quayorgs = useK8sResourceList<K8sResource>('QuayOrg', opts);
  const hybridnetworks = useK8sResourceList<K8sResource>('HybridNetwork', opts);
  const networkplacements = useK8sResourceList<K8sResource>('NetworkPlacement', opts);

  const hookByKind: Record<string, { items: K8sResource[]; loading: boolean; error: Error | null; refresh: () => void }> = {
    Team: teams,
    Project: projects,
    PlatformOpenshift: platforms,
    Assignment: assignments,
    CloudOSO: cloudoso,
    CloudAWS: cloudaws,
    CloudVirt: cloudvirt,
    OpenStackMigration: migrations,
    Persona: personas,
    Rbac: rbacs,
    Vault: vaults,
    VaultKV: vaultkvs,
    AAPOrg: aaporgs,
    QuayOrg: quayorgs,
    HybridNetwork: hybridnetworks,
    NetworkPlacement: networkplacements,
  };

  const lists: Record<string, K8sResource[]> = Object.fromEntries(
    TENANT_OVERVIEW_KINDS.map((k) => [k, hookByKind[k]?.items ?? []]),
  );

  const all = useMemo(
    () => TENANT_OVERVIEW_KINDS.flatMap((k) => hookByKind[k]?.items ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    TENANT_OVERVIEW_KINDS.map((k) => hookByKind[k]?.items),
  );
  const overall = bucket(all);
  const loading = TENANT_OVERVIEW_KINDS.some((k) => hookByKind[k]?.loading);
  const firstError = TENANT_OVERVIEW_KINDS.map((k) => hookByKind[k]?.error).find(Boolean) ?? null;
  const failedItems = all
    .filter((i) => normalizeHealth(i.status?.ready, i.status?.status) === 'failed')
    .slice(0, 12);

  const refreshAll = () => {
    TENANT_OVERVIEW_KINDS.forEach((k) => hookByKind[k]?.refresh());
  };

  const kindRows = TENANT_OVERVIEW_KINDS.map((kind) => ({
    kind,
    label: t(`kinds.${kind}`),
    path: KIND_PATH[kind] ?? '/',
  }));

  return (
    <>
      <PageHeader
        title={t('pages.overviewTitle')}
        subtitle={`Entity namespace ${namespace}`}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refreshAll}>
            {t('common.refresh')}
          </Button>
        }
      />

      {firstError && (
        <Alert variant="warning" isInline title={t('common.k8sUnavailable')} className="sc-mb">
          {firstError.message}
        </Alert>
      )}

      {loading && all.length === 0 ? (
        <Spinner />
      ) : (
        <>
          <div className="sc-overview-top">
            <Card className="sc-panel">
              <CardTitle>{t('pages.platformHealth')}</CardTitle>
              <CardBody>
                <HealthDonut ready={overall.ready} failed={overall.failed} pending={overall.pending} />
              </CardBody>
            </Card>
            <div className="sc-inventory-grid">
              {(['Team', 'PlatformOpenshift', 'Assignment', 'HybridNetwork'] as HybridSovereignKind[]).map(
                (kind) => (
                  <InventoryCard
                    key={kind}
                    title={t(`kinds.${kind}`)}
                    count={lists[kind]?.length ?? 0}
                    hint={`${bucket(lists[kind] ?? []).ready} ${t('status.ready')}`}
                    kind={kind}
                    href={KIND_PATH[kind]}
                  />
                ),
              )}
            </div>
          </div>

          <Title headingLevel="h2" size="lg" className="sc-section-title">
            {t('pages.selfService')}
          </Title>
          <div className="sc-card-grid sc-mb">
            {[
              { labelKey: 'nav.teams', path: '/create/team', kind: 'Team' as const },
              { labelKey: 'nav.projects', path: '/create/project', kind: 'Project' as const },
              { labelKey: 'nav.hybridNetworks', path: '/create/hybridnetwork', kind: 'HybridNetwork' as const },
              { labelKey: 'nav.networkPlacements', path: '/create/networkplacement', kind: 'NetworkPlacement' as const },
              { labelKey: 'nav.assignments', path: '/create/assignment', kind: 'Assignment' as const },
            ].map((a) => (
              <Card
                key={a.path}
                isSelectable
                isCompact
                className="sc-action-card"
                onClick={() => navigate(a.path)}
              >
                <CardTitle>
                  <KindIcon kind={a.kind} size="sm" />
                  {t(a.labelKey)}
                </CardTitle>
              </Card>
            ))}
          </div>

          {failedItems.length > 0 && (
            <Card className="sc-failed-panel sc-mb">
              <CardHeader>
                <Flex alignItems={{ default: 'alignItemsCenter' }} spaceItems={{ default: 'spaceItemsSm' }}>
                  <FlexItem>
                    <ExclamationTriangleIcon className="sc-failed-panel__icon" />
                  </FlexItem>
                  <FlexItem>
                    <CardTitle component="span">
                      {t('pages.failedResources')} <Label color="orange">{failedItems.length}</Label>
                    </CardTitle>
                  </FlexItem>
                </Flex>
              </CardHeader>
              <CardBody>
                <div className="sc-table-wrap">
                  <Table variant="compact" aria-label={t('pages.failedResources')}>
                    <Thead>
                      <Tr>
                        <Th>{t('common.resources')}</Th>
                        <Th>{t('common.name')}</Th>
                        <Th>{t('common.status')}</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {failedItems.map((item) => (
                        <Tr key={`${item.kind}/${item.metadata.name}`}>
                          <Td>{item.kind}</Td>
                          <Td>{item.metadata.name}</Td>
                          <Td>
                            <StatusBadge
                              status={item.status?.status}
                              ready={item.status?.ready}
                              message={item.status?.message}
                            />
                          </Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </div>
              </CardBody>
            </Card>
          )}

          <Title headingLevel="h2" size="lg" className="sc-section-title">
            {t('pages.inventory')}
          </Title>
          <div className="sc-table-wrap sc-mb">
            <Table variant="compact" aria-label={t('pages.inventory')}>
              <Thead>
                <Tr>
                  <Th>{t('common.resources')}</Th>
                  <Th>{t('pages.totalResources')}</Th>
                  <Th>{t('pages.readyCount')}</Th>
                  <Th>{t('pages.failedCount')}</Th>
                  <Th>{t('pages.platformHealth')}</Th>
                  <Th />
                </Tr>
              </Thead>
              <Tbody>
                {kindRows.map((row) => {
                  const b = bucket(lists[row.kind] ?? []);
                  const health = b.total === 0 ? 0 : Math.round((b.ready / b.total) * 100);
                  return (
                    <Tr key={row.kind}>
                      <Td>
                        <span className="sc-kind-cell">
                          <KindIcon kind={row.kind} size="sm" />
                          {row.label}
                        </span>
                      </Td>
                      <Td>{b.total}</Td>
                      <Td>
                        <span className="sc-text-success">{b.ready}</span>
                      </Td>
                      <Td>
                        <span className={b.failed ? 'sc-text-danger' : undefined}>{b.failed}</span>
                      </Td>
                      <Td>
                        <Progress value={health} size={ProgressSize.sm} aria-label={`${row.label} health`} />
                      </Td>
                      <Td>
                        <Link to={row.path}>{t('common.view')}</Link>
                      </Td>
                    </Tr>
                  );
                })}
              </Tbody>
            </Table>
          </div>

          <Title headingLevel="h2" size="lg" className="sc-section-title">
            <TopologyIcon className="sc-inline-icon" /> {t('common.topology')}
          </Title>
          <Card className="sc-panel">
            <CardBody>
              <EntityTopology entityNamespace={namespace} />
            </CardBody>
          </Card>
        </>
      )}
    </>
  );
}
