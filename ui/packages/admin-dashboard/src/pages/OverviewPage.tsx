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
} from '@patternfly/react-core';
import {
  SyncIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ExternalLinkAltIcon,
  TopologyIcon,
} from '@patternfly/react-icons';
import { Table, Thead, Tr, Th, Tbody, Td } from '@patternfly/react-table';
import { Link } from 'react-router-dom';
import {
  EntityTopology,
  PageHeader,
  HealthDonut,
  InventoryCard,
  StatusBadge,
  KindIcon,
  normalizeHealth,
  useOverviewCRs,
  K8sResource,
  HybridSovereignKind,
  useTranslation,
} from '@hybridsovereign/shared';
import { adminDetailHref } from './ResourceListPage';

const KIND_LIST_PATH: Partial<Record<string, string>> = {
  Entity: '/entities',
  Team: '/teams',
  Project: '/projects',
  Assignment: '/assignments',
  PlatformOpenshift: '/platforms',
  CloudOSO: '/clouds/cloudoso',
  CloudAWS: '/clouds/cloudaws',
  CloudVirt: '/clouds/cloudvirt',
  CloudInfrastructure: '/clouds/infrastructure',
  Persona: '/personas',
  Rbac: '/operators/rbacs',
  RbacConfig: '/operators/rbacconfigs',
  AAPOrg: '/services/aaporgs',
  AAPConfig: '/services/aapconfigs',
  QuayOrg: '/services/quayorgs',
  QuayConfig: '/services/quayconfigs',
  Vault: '/services/vaults',
  VaultKV: '/services/vaultkvs',
  HybridFabric: '/networking/fabrics',
  CloudGateway: '/networking/gateways',
  TransportLink: '/networking/transport',
  HybridNetwork: '/networks',
  NetworkPlacement: '/placements',
  UIHealthChecker: '/networking/uihealth',
};

function overviewItemHref(item: K8sResource): string | null {
  const kind = (item.kind || '') as HybridSovereignKind;
  const listPath = KIND_LIST_PATH[kind];
  if (!listPath) return null;
  return adminDetailHref(listPath, kind, item);
}

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

export function OverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const { items, loading, error, refresh } = useOverviewCRs();

  const byKind = useMemo(() => {
    const map = new Map<string, K8sResource[]>();
    for (const item of items) {
      const k = item.kind || 'Unknown';
      const list = map.get(k) ?? [];
      list.push(item);
      map.set(k, list);
    }
    return map;
  }, [items]);

  const overall = useMemo(() => bucket(items), [items]);
  const platforms = byKind.get('PlatformOpenshift') ?? [];
  const failedItems = useMemo(
    () =>
      items
        .filter((i) => normalizeHealth(i.status?.ready, i.status?.status) === 'failed')
        .slice(0, 10),
    [items],
  );

  return (
    <>
      <PageHeader
        title={t('pages.overviewTitle')}
        showLanguageToggle={false}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refresh}>
            {t('common.refresh')}
          </Button>
        }
      />

      {error && (
        <Alert variant="warning" isInline title={t('pages.overviewUnable')} className="sc-mb">
          {error.message}
        </Alert>
      )}

      {loading && items.length === 0 ? (
        <Spinner />
      ) : (
        <>
          <div className="sc-overview-top">
            <Card className="sc-panel">
              <CardTitle>Platform health</CardTitle>
              <CardBody>
                <HealthDonut
                  ready={overall.ready}
                  failed={overall.failed}
                  pending={overall.pending}
                  title="Platform"
                />
              </CardBody>
            </Card>

            <div className="sc-inventory-grid">
              <InventoryCard
                title={t('nav.entities')}
                count={(byKind.get('Entity') ?? []).length}
                hint={`${bucket(byKind.get('Entity') ?? []).ready} ready`}
                kind="Entity"
                href="/entities"
                status="success"
              />
              <InventoryCard
                title={t('nav.teams')}
                count={(byKind.get('Team') ?? []).length}
                hint={`${bucket(byKind.get('Team') ?? []).ready} ready`}
                kind="Team"
                href="/teams"
              />
              <InventoryCard
                title={t('nav.cloudInfrastructure')}
                count={(byKind.get('CloudInfrastructure') ?? []).length}
                hint={`${bucket(byKind.get('CloudInfrastructure') ?? []).ready} ready`}
                kind="CloudInfrastructure"
                href="/clouds/infrastructure"
                status={bucket(byKind.get('CloudInfrastructure') ?? []).failed ? 'danger' : 'default'}
              />
              <InventoryCard
                title="Platforms"
                count={platforms.length}
                hint={`${bucket(platforms).ready} ready`}
                kind="PlatformOpenshift"
                href="/platforms"
                status={bucket(platforms).failed ? 'danger' : 'default'}
              />
              <InventoryCard
                title={t('nav.assignments')}
                count={(byKind.get('Assignment') ?? []).length}
                hint={`${bucket(byKind.get('Assignment') ?? []).ready} ready`}
                kind="Assignment"
                href="/assignments"
                status={bucket(byKind.get('Assignment') ?? []).failed ? 'danger' : 'default'}
              />
            </div>
          </div>

          {platforms.length > 0 && (
            <>
              <Title headingLevel="h2" size="lg" className="sc-section-title">
                OpenShift clusters
              </Title>
              <div className="sc-cluster-grid sc-mb">
                {platforms.slice(0, 6).map((p) => {
                  const healthy = normalizeHealth(p.status?.ready, p.status?.status) === 'ready';
                  return (
                    <Card key={p.metadata.name} className="sc-cluster-card" isCompact>
                      <CardHeader>
                        <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }}>
                          <FlexItem>
                            <KindIcon kind="PlatformOpenshift" size="sm" />
                          </FlexItem>
                          <FlexItem>
                            <Link
                              to={
                                overviewItemHref(p) ??
                                `/platforms/${encodeURIComponent(p.metadata.namespace ?? 'default')}/${encodeURIComponent(p.metadata.name)}`
                              }
                              className="sc-cluster-card__name"
                            >
                              {p.metadata.name} <ExternalLinkAltIcon />
                            </Link>
                          </FlexItem>
                          <FlexItem align={{ default: 'alignEnd' }}>
                            <Label color={healthy ? 'green' : 'red'} icon={healthy ? <CheckCircleIcon /> : undefined}>
                              {healthy ? 'Healthy' : 'Degraded'}
                            </Label>
                          </FlexItem>
                        </Flex>
                      </CardHeader>
                      <CardBody>
                        <dl className="sc-meta-list">
                          <div>
                            <dt>Namespace</dt>
                            <dd>{p.metadata.namespace ?? '—'}</dd>
                          </div>
                          <div>
                            <dt>Status</dt>
                            <dd>{p.status?.status ?? (healthy ? 'ready' : 'unknown')}</dd>
                          </div>
                        </dl>
                      </CardBody>
                    </Card>
                  );
                })}
              </div>
            </>
          )}

          {failedItems.length > 0 && (
            <Card className="sc-failed-panel sc-mb">
              <CardHeader>
                <Flex alignItems={{ default: 'alignItemsCenter' }} spaceItems={{ default: 'spaceItemsSm' }}>
                  <FlexItem>
                    <ExclamationTriangleIcon className="sc-failed-panel__icon" />
                  </FlexItem>
                  <FlexItem>
                    <CardTitle component="span">
                      Failed custom resources{' '}
                      <Label color="orange">{overall.failed}</Label>
                    </CardTitle>
                  </FlexItem>
                  <FlexItem align={{ default: 'alignEnd' }}>
                    <Link to="/operators">View all</Link>
                  </FlexItem>
                </Flex>
              </CardHeader>
              <CardBody>
                <div className="sc-table-wrap">
                  <Table variant="compact" aria-label="Failed resources">
                    <Thead>
                      <Tr>
                        <Th>Kind</Th>
                        <Th>Name</Th>
                        <Th>Namespace</Th>
                        <Th>Message</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {failedItems.map((item) => {
                        const href = overviewItemHref(item);
                        return (
                          <Tr key={`${item.kind}/${item.metadata.namespace}/${item.metadata.name}`}>
                            <Td dataLabel="Kind">{item.kind}</Td>
                            <Td dataLabel={t('common.name')}>
                              {href ? (
                                <Link className="sc-resource-link" to={href}>
                                  <KindIcon kind={item.kind || 'Unknown'} size="sm" />
                                  {item.metadata.name}
                                </Link>
                              ) : (
                                item.metadata.name
                              )}
                            </Td>
                            <Td dataLabel={t('common.namespace')}>{item.metadata.namespace ?? '—'}</Td>
                            <Td dataLabel="Message">
                              <StatusBadge
                                status={item.status?.status}
                                ready={item.status?.ready}
                                message={item.status?.message}
                              />
                            </Td>
                          </Tr>
                        );
                      })}
                    </Tbody>
                  </Table>
                </div>
              </CardBody>
            </Card>
          )}

          <Flex alignItems={{ default: 'alignItemsCenter' }} className="sc-section-title">
            <FlexItem>
              <Title headingLevel="h2" size="lg">
                <TopologyIcon className="sc-inline-icon" /> Live topology
              </Title>
            </FlexItem>
          </Flex>
          <Card className="sc-panel">
            <CardBody>
              <EntityTopology
                resources={items}
                resourcesLoading={loading}
                resourcesError={error}
              />
            </CardBody>
          </Card>
        </>
      )}
    </>
  );
}
