import '../consoleK8sBootstrap';
import * as React from 'react';
import { useMemo, Component, type ReactNode } from 'react';
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
  PageSection,
} from '@patternfly/react-core';
import {
  SyncIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ExternalLinkAltIcon,
  TopologyIcon,
} from '@patternfly/react-icons';
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
  useTranslation,
} from '@hybridsovereign/shared';
import { overviewItemHref } from '../adminPaths';
import '@hybridsovereign/shared/styles/openshift.css';

/** Keep Overview usable if topology/subtree throws (React #306 / undefined element). */
class SectionErrorBoundary extends Component<
  { title: string; children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <Alert variant="warning" isInline title={this.props.title}>
          {this.state.error.message || 'Section failed to render'}
        </Alert>
      );
    }
    return this.props.children;
  }
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

const AdminOverviewPage: React.FC = () => {
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
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <PageHeader
          title={t('pages.overviewTitle')}
          subtitle="Platform health and CR topology — OpenShift Console native"
          breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: t('pages.overviewTitle') }]}
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
                  href="/hybridsovereign/entities"
                  status="success"
                />
                <InventoryCard
                  title={t('nav.teams')}
                  count={(byKind.get('Team') ?? []).length}
                  hint={`${bucket(byKind.get('Team') ?? []).ready} ready`}
                  kind="Team"
                  href="/hybridsovereign/teams"
                />
                <InventoryCard
                  title="Platforms"
                  count={platforms.length}
                  hint={`${bucket(platforms).ready} ready`}
                  kind="PlatformOpenshift"
                  href="/hybridsovereign/platforms"
                  status={bucket(platforms).failed ? 'danger' : 'default'}
                />
                <InventoryCard
                  title={t('nav.assignments')}
                  count={(byKind.get('Assignment') ?? []).length}
                  hint={`${bucket(byKind.get('Assignment') ?? []).ready} ready`}
                  kind="Assignment"
                  href="/hybridsovereign/assignments"
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
                    const href =
                      overviewItemHref(p) ??
                      `/hybridsovereign/platforms/${encodeURIComponent(p.metadata.namespace ?? 'default')}/${encodeURIComponent(p.metadata.name)}`;
                    return (
                      <Card key={p.metadata.name} className="sc-cluster-card" isCompact>
                        <CardHeader>
                          <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }}>
                            <FlexItem>
                              <KindIcon kind="PlatformOpenshift" size="sm" />
                            </FlexItem>
                            <FlexItem>
                              <a href={href} className="sc-cluster-card__name">
                                {p.metadata.name} <ExternalLinkAltIcon />
                              </a>
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
                        Failed custom resources <Label color="orange">{overall.failed}</Label>
                      </CardTitle>
                    </FlexItem>
                    <FlexItem align={{ default: 'alignEnd' }}>
                      <a href="/hybridsovereign/operators">View all</a>
                    </FlexItem>
                  </Flex>
                </CardHeader>
                <CardBody>
                  <div className="sc-table-wrap">
                    <table className="pf-v5-c-table pf-m-compact" role="grid" aria-label="Failed resources">
                      <thead>
                        <tr>
                          <th>Kind</th>
                          <th>Name</th>
                          <th>Namespace</th>
                          <th>Message</th>
                        </tr>
                      </thead>
                      <tbody>
                        {failedItems.map((item) => {
                          const href = overviewItemHref(item);
                          return (
                            <tr key={`${item.kind}/${item.metadata.namespace}/${item.metadata.name}`}>
                              <td data-label="Kind">{item.kind}</td>
                              <td data-label={t('common.name')}>
                                {href ? (
                                  <a className="sc-resource-link" href={href}>
                                    <KindIcon kind={item.kind || 'Unknown'} size="sm" />
                                    {item.metadata.name}
                                  </a>
                                ) : (
                                  item.metadata.name
                                )}
                              </td>
                              <td data-label={t('common.namespace')}>{item.metadata.namespace ?? '—'}</td>
                              <td data-label="Message">
                                <StatusBadge
                                  status={item.status?.status}
                                  ready={item.status?.ready}
                                  message={item.status?.message}
                                />
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
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
                <SectionErrorBoundary title="Live topology unavailable">
                  <EntityTopology
                    resources={items}
                    resourcesLoading={loading}
                    resourcesError={error}
                  />
                </SectionErrorBoundary>
              </CardBody>
            </Card>

            <div className="sc-inventory-grid" style={{ marginTop: '1rem' }}>
              <InventoryCard
                title={t('nav.cloudInfrastructure')}
                count={(byKind.get('CloudInfrastructure') ?? []).length}
                hint={`${bucket(byKind.get('CloudInfrastructure') ?? []).ready} ready`}
                kind="CloudInfrastructure"
                href="/hybridsovereign/clouds/infrastructure"
                status={bucket(byKind.get('CloudInfrastructure') ?? []).failed ? 'danger' : 'default'}
              />
            </div>
          </>
        )}
      </div>
    </PageSection>
  );
};

export default AdminOverviewPage;
