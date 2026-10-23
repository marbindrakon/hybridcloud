import React, { useMemo } from 'react';
import {
  Title,
  Card,
  CardTitle,
  CardBody,
  CardHeader,
  Button,
  Spinner,
  Label,
  Tooltip,
  Flex,
  FlexItem,
  Progress,
  ProgressSize,
} from '@patternfly/react-core';
import {
  SyncIcon,
  ExclamationTriangleIcon,
  TopologyIcon,
  LockIcon,
} from '@patternfly/react-icons';
import { Table, Thead, Tr, Th, Tbody, Td } from '@patternfly/react-table';
import { Link, useNavigate } from 'react-router-dom';
import { EntityTopology } from '../components/EntityTopology';
import { PageHeader } from '../components/PageHeader';
import { HealthDonut } from '../components/HealthDonut';
import { InventoryCard } from '../components/InventoryCard';
import { StatusBadge, normalizeHealth } from '../components/StatusBadge';
import { GettingStartedCard } from '../components/GettingStartedCard';
import { TenantHealthBanner } from '../components/TenantHealthBanner';
import { KindIcon } from '../icons';
import { useK8sResourceList } from '../hooks/k8s';
import { K8sResource, TENANT_OVERVIEW_KINDS, HybridSovereignKind } from '../types';
import { useTranslation } from '../i18n';
import { classifyError } from '../hooks/errors';
import { UnavailableState } from '../access/ForbiddenState';
import { usePermission } from '../access/PermissionProvider';
import { useSharedObjects } from '../access/sharedWithYou';
import { useTenantScope } from '../scope/ScopeProvider';
import { useNav, useBreadcrumbs, useDocumentTitle } from '../nav/useNav';
import { tenantLabel } from '../scope/resolveIdentity';
import { kindTerm } from '../i18n/vocabulary';

/** Kinds of the overview that still have a page (Persona has none). */
const OVERVIEW_KINDS = TENANT_OVERVIEW_KINDS.filter((k) => k !== 'Persona');

/** A self-service card that is disabled, with the role named, when the user may not create the kind. */
function SelfServiceCard({ kind, labelKey, onOpen }: { kind: HybridSovereignKind; labelKey: string; onOpen: () => void }): React.ReactElement {
  const { t } = useTranslation();
  const { allowed, loading } = usePermission(kind, 'create');
  const card = (
    <Card
      isSelectable={allowed}
      isCompact
      isDisabled={!allowed}
      className={`sc-action-card${allowed ? '' : ' sc-action-card--disabled'}`}
      onClick={allowed ? onOpen : undefined}
    >
      <CardTitle>
        <KindIcon kind={kind} size="sm" />
        {t(labelKey)}
      </CardTitle>
    </Card>
  );
  if (allowed) return card;
  return <Tooltip content={loading ? t('access.checking') : t('access.notAllowed')}>{card}</Tooltip>;
}

/** Link to an object's page, or its name when the kind has no page here. */
function ObjectLink({ kind, name }: { kind: string; name: string }): React.ReactElement {
  const nav = useNav();
  const href = nav.routeFor(kind, name);
  return href ? <Link to={href}>{name}</Link> : <>{name}</>;
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

export function TenantOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const nav = useNav();
  const { scope, namespace: scopeNamespace } = useTenantScope();
  const namespace = scopeNamespace ?? '';
  const crumbs = useBreadcrumbs('home');
  useDocumentTitle('home');
  const opts = { namespace, enabled: !!namespace };
  const kindPath = (kind: HybridSovereignKind): string => nav.routeFor(kind) ?? nav.pathFor('home');

  const teams = useK8sResourceList<K8sResource>('Team', opts);
  const projects = useK8sResourceList<K8sResource>('Project', opts);
  const platforms = useK8sResourceList<K8sResource>('PlatformOpenshift', opts);
  const assignments = useK8sResourceList<K8sResource>('Assignment', opts);
  const cloudoso = useK8sResourceList<K8sResource>('CloudOSO', opts);
  const cloudaws = useK8sResourceList<K8sResource>('CloudAWS', opts);
  const cloudgcp = useK8sResourceList<K8sResource>('CloudGCP', opts);
  const cloudvirt = useK8sResourceList<K8sResource>('CloudVirt', opts);
  const migrations = useK8sResourceList<K8sResource>('OpenStackMigration', opts);
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
    CloudGCP: cloudgcp,
    CloudVirt: cloudvirt,
    OpenStackMigration: migrations,
    Rbac: rbacs,
    Vault: vaults,
    VaultKV: vaultkvs,
    AAPOrg: aaporgs,
    QuayOrg: quayorgs,
    HybridNetwork: hybridnetworks,
    NetworkPlacement: networkplacements,
  };

  // A kind the user may not list answers 403. Objects of it the user holds by name (team setup
  // and app project grants) are read one by one and shown (GT-F4); a kind with none is locked,
  // never shown as zero (UX-057).
  const forbiddenKinds: HybridSovereignKind[] = OVERVIEW_KINDS.filter((k) => classifyError(hookByKind[k]?.error) === 'forbidden');
  const shared = useSharedObjects(forbiddenKinds, namespace, !!namespace && forbiddenKinds.length > 0);
  const sharedOf = (k: HybridSovereignKind): K8sResource[] => shared.byKind[k] ?? [];
  const isShared = (k: HybridSovereignKind) => forbiddenKinds.includes(k) && sharedOf(k).length > 0;
  const isLocked = (k: HybridSovereignKind) => forbiddenKinds.includes(k) && !isShared(k) && !shared.loading;

  const lists: Record<string, K8sResource[]> = Object.fromEntries(
    OVERVIEW_KINDS.map((k) => [k, forbiddenKinds.includes(k) ? sharedOf(k) : hookByKind[k]?.items ?? []]),
  );

  const all = useMemo(
    () => OVERVIEW_KINDS.flatMap((k) => lists[k] ?? []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [...OVERVIEW_KINDS.map((k) => hookByKind[k]?.items), shared.byKind],
  );
  const sharedItems = forbiddenKinds.flatMap((k) => sharedOf(k));
  const overall = bucket(all);
  const loading = OVERVIEW_KINDS.some((k) => hookByKind[k]?.loading) || shared.loading;
  const lockedKinds = OVERVIEW_KINDS.filter((k) => isLocked(k));
  const firstError =
    OVERVIEW_KINDS.map((k) => hookByKind[k]?.error).find(
      (e) => !!e && classifyError(e) !== 'forbidden',
    ) ?? null;
  const failedItems = all
    .filter((i) => normalizeHealth(i) === 'failed')
    .slice(0, 12);

  const refreshAll = () => {
    OVERVIEW_KINDS.forEach((k) => hookByKind[k]?.refresh());
  };

  const kindRows = OVERVIEW_KINDS.map((kind) => ({
    kind,
    label: kindTerm(t, kind, 'many'),
    path: kindPath(kind),
  }));

  return (
    <>
      <PageHeader
        title={scope.kind === 'tenant' ? tenantLabel(scope.tenant) : t('pages.overviewTitle')}
        subtitle={`${t('scope.tenantNamespace')}: ${namespace}`}
        breadcrumbs={crumbs}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refreshAll}>
            {t('common.refresh')}
          </Button>
        }
      />

      {scope.kind === 'tenant' && <TenantHealthBanner namespace={namespace} />}
      {scope.kind === 'tenant' && <GettingStartedCard />}

      {firstError && <UnavailableState error={firstError} onRetry={refreshAll} />}

      {loading && all.length === 0 ? (
        <Spinner />
      ) : (
        <>
          <div className="sc-overview-top">
            <Card className="sc-panel">
              <CardTitle>{t('pages.platformHealth')}</CardTitle>
              <CardBody>
                <HealthDonut ready={overall.ready} failed={overall.failed} pending={overall.pending} />
                {lockedKinds.length > 0 && (
                  <p className="sc-muted">
                    <LockIcon /> {t('access.kindsNotVisible', { count: lockedKinds.length })}
                  </p>
                )}
              </CardBody>
            </Card>
            <div className="sc-inventory-grid">
              {(['Team', 'PlatformOpenshift', 'Assignment', 'HybridNetwork'] as HybridSovereignKind[]).map(
                (kind) => (
                  <InventoryCard
                    key={kind}
                    title={kindTerm(t, kind, 'many')}
                    count={lists[kind]?.length ?? 0}
                    hint={`${bucket(lists[kind] ?? []).ready} ${t('status.ready')}`}
                    kind={kind}
                    locked={isLocked(kind)}
                    href={kindPath(kind)}
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
              { labelKey: 'nav.networks', path: nav.createPathFor('hybridnetwork') ?? '', kind: 'HybridNetwork' as const },
              { labelKey: 'nav.envVm', path: nav.createPathFor('cloudvirt') ?? '', kind: 'CloudVirt' as const },
              { labelKey: 'nav.teams', path: nav.createPathFor('team') ?? '', kind: 'Team' as const },
              { labelKey: 'nav.projects', path: nav.createPathFor('project') ?? '', kind: 'Project' as const },
              { labelKey: 'nav.assignments', path: nav.createPathFor('assignment') ?? '', kind: 'Assignment' as const },
            ].map((a) => (
              <SelfServiceCard key={a.path} kind={a.kind} labelKey={a.labelKey} onOpen={() => navigate(a.path)} />
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
                          <Td>{kindTerm(t, item.kind)}</Td>
                          <Td>
                            <ObjectLink kind={item.kind} name={item.metadata.name} />
                          </Td>
                          <Td>
                            <StatusBadge item={item} />
                          </Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </div>
              </CardBody>
            </Card>
          )}

          {sharedItems.length > 0 && (
            <Card className="sc-panel sc-mb">
              <CardTitle>{t('pages.sharedWithYouTitle')}</CardTitle>
              <CardBody>
                <p className="sc-text-muted">{t('pages.sharedWithYouBody')}</p>
                <div className="sc-table-wrap">
                  <Table variant="compact" aria-label={t('pages.sharedWithYouTitle')}>
                    <Thead>
                      <Tr>
                        <Th>{t('common.resources')}</Th>
                        <Th>{t('common.name')}</Th>
                        <Th>{t('common.status')}</Th>
                      </Tr>
                    </Thead>
                    <Tbody>
                      {sharedItems.map((item) => (
                        <Tr key={`${item.kind}/${item.metadata.name}`}>
                          <Td>
                            <span className="sc-kind-cell">
                              <KindIcon kind={item.kind} size="sm" />
                              {kindTerm(t, item.kind)}
                            </span>
                          </Td>
                          <Td>
                            <ObjectLink kind={item.kind} name={item.metadata.name} />
                          </Td>
                          <Td>
                            <StatusBadge item={item} />
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
                  if (isLocked(row.kind)) {
                    return (
                      <Tr key={row.kind}>
                        <Td>
                          <span className="sc-kind-cell">
                            <KindIcon kind={row.kind} size="sm" />
                            {row.label}
                          </span>
                        </Td>
                        <Td colSpan={5}>
                          <span className="sc-muted">
                            <LockIcon /> {t('access.noAccess')}
                          </span>
                        </Td>
                      </Tr>
                    );
                  }
                  return (
                    <Tr key={row.kind}>
                      <Td>
                        <span className="sc-kind-cell">
                          <KindIcon kind={row.kind} size="sm" />
                          {row.label}
                        </span>
                      </Td>
                      <Td>
                        {b.total}
                        {isShared(row.kind) && (
                          <>
                            {' '}
                            <Label isCompact color="blue">
                              {t('pages.sharedWithYouLabel')}
                            </Label>
                          </>
                        )}
                      </Td>
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
