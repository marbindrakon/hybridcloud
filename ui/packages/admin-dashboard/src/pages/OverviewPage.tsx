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
  objectPath,
  pathFor,
  useBreadcrumbs,
  useDocumentTitle,
  UnavailableState,
  phaseOf,
} from '@hybridsovereign/shared';

function overviewItemHref(item: K8sResource): string | null {
  return objectPath('admin-dashboard', item.kind || '', item.metadata.namespace, item.metadata.name) ?? null;
}

function bucket(items: K8sResource[]) {
  let ready = 0;
  let failed = 0;
  let pending = 0;
  for (const i of items) {
    const h = normalizeHealth(i);
    if (h === 'ready') ready += 1;
    else if (h === 'failed') failed += 1;
    else pending += 1;
  }
  return { ready, failed, pending, total: items.length };
}

export function OverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const { items, loading, error, refresh, forbiddenKinds } = useOverviewCRs();
  const crumbs = useBreadcrumbs('platform-overview');
  useDocumentTitle('platform-overview');
  const H = 'admin-dashboard' as const;
  const ALL = { kind: 'all' as const };
  const GLOBAL = { kind: 'global' as const };

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
        .filter((i) => normalizeHealth(i) === 'failed')
        .slice(0, 10),
    [items],
  );

  return (
    <>
      <PageHeader
        title={t('pages.overviewTitle')}
        breadcrumbs={crumbs}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refresh}>
            {t('common.refresh')}
          </Button>
        }
      />

      {error && <UnavailableState error={error} onRetry={refresh} />}

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
                href={pathFor(H, 'tenants', GLOBAL)}
                locked={forbiddenKinds.includes('Entity')}
                status="success"
              />
              <InventoryCard
                title={t('nav.teams')}
                count={(byKind.get('Team') ?? []).length}
                hint={`${bucket(byKind.get('Team') ?? []).ready} ready`}
                kind="Team"
                href={pathFor(H, 'teams', ALL)}
                locked={forbiddenKinds.includes('Team')}
              />
              <InventoryCard
                title={t('nav.cloudInfrastructure')}
                count={(byKind.get('CloudInfrastructure') ?? []).length}
                hint={`${bucket(byKind.get('CloudInfrastructure') ?? []).ready} ready`}
                kind="CloudInfrastructure"
                href={pathFor(H, 'sites', GLOBAL)}
                locked={forbiddenKinds.includes('CloudInfrastructure')}
                status={bucket(byKind.get('CloudInfrastructure') ?? []).failed ? 'danger' : 'default'}
              />
              <InventoryCard
                title="Platforms"
                count={platforms.length}
                hint={`${bucket(platforms).ready} ready`}
                kind="PlatformOpenshift"
                href={pathFor(H, 'clusters', ALL)}
                locked={forbiddenKinds.includes('PlatformOpenshift')}
                status={bucket(platforms).failed ? 'danger' : 'default'}
              />
              <InventoryCard
                title={t('nav.assignments')}
                count={(byKind.get('Assignment') ?? []).length}
                hint={`${bucket(byKind.get('Assignment') ?? []).ready} ready`}
                kind="Assignment"
                href={pathFor(H, 'assignments', ALL)}
                locked={forbiddenKinds.includes('Assignment')}
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
                  const healthy = normalizeHealth(p) === 'ready';
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
                              {t(`phase.${phaseOf(p)}`)}
                            </Label>
                          </FlexItem>
                        </Flex>
                      </CardHeader>
                      <CardBody>
                        <dl className="sc-meta-list">
                          <div>
                            <dt>{t('common.namespace')}</dt>
                            <dd>{p.metadata.namespace ?? '—'}</dd>
                          </div>
                          <div>
                            <dt>{t('common.status')}</dt>
                            <dd>{t(`phase.${phaseOf(p)}`)}</dd>
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
                    <Link to={pathFor(H, 'access-groups', ALL)}>View all</Link>
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
                              <StatusBadge item={item} />
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
