import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import {
  HybridSovereignKind,
  useK8sResourceList,
  K8sResource,
  PageHeader,
  FilterToolbar,
  StatusFilter,
  normalizeHealth,
  ResourceListTable,
  filterResourcesByQuery,
  useTranslation,
} from '@hybridsovereign/shared';

interface ResourceListPageProps {
  kind: HybridSovereignKind;
  title: string;
  subtitle?: string;
  secondaryKind?: HybridSovereignKind;
  tertiaryKind?: HybridSovereignKind;
  /** Base path for primary kind detail links (e.g. /teams) */
  listPath: string;
  /** Base path for secondary kind detail links */
  secondaryListPath?: string;
  tertiaryListPath?: string;
  createPath?: string;
  /** When false, skip the API fetch (e.g. inactive service tab) */
  enabled?: boolean;
  /** Hide page header when embedded in a parent tab strip */
  hideHeader?: boolean;
  /** List the primary kind in this namespace only (platform kinds live in sovereign-cloud) */
  namespace?: string;
}

function matchesStatus(item: K8sResource, statusFilter: StatusFilter): boolean {
  if (statusFilter === 'all') return true;
  return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
}

/** Build detail URL — Entity and platform networking CRs use /path/:name; others use /path/:namespace/:name */
export function adminDetailHref(
  listPath: string,
  kind: HybridSovereignKind,
  item: K8sResource,
): string {
  const name = encodeURIComponent(item.metadata.name);
  if (
    kind === 'Entity' ||
    kind === 'CloudInfrastructure' ||
    kind === 'HybridFabric' ||
    kind === 'CloudGateway' ||
    kind === 'TransportLink' ||
    kind === 'UIHealthChecker'
  ) {
    return `${listPath}/${name}`;
  }
  const ns = encodeURIComponent(item.metadata.namespace ?? 'default');
  return `${listPath}/${ns}/${name}`;
}

export function ResourceListPage({
  kind,
  title,
  subtitle,
  secondaryKind,
  tertiaryKind,
  listPath,
  secondaryListPath,
  tertiaryListPath,
  createPath,
  enabled = true,
  hideHeader = false,
  namespace,
}: ResourceListPageProps): React.ReactElement {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const primary = useK8sResourceList<K8sResource>(kind, { enabled, namespace });
  const secondary = useK8sResourceList<K8sResource>(secondaryKind ?? kind, {
    enabled: enabled && !!secondaryKind,
  });
  const tertiary = useK8sResourceList<K8sResource>(tertiaryKind ?? kind, {
    enabled: enabled && !!tertiaryKind,
  });

  const primaryFiltered = useMemo(() => {
    return filterResourcesByQuery(primary.items, kind, search, true).filter((i) =>
      matchesStatus(i, statusFilter),
    );
  }, [primary.items, kind, search, statusFilter]);

  const secondaryFiltered = useMemo(() => {
    if (!secondaryKind) return [];
    return filterResourcesByQuery(secondary.items, secondaryKind, search, true).filter((i) =>
      matchesStatus(i, statusFilter),
    );
  }, [secondary.items, secondaryKind, search, statusFilter]);

  const tertiaryFiltered = useMemo(() => {
    if (!tertiaryKind) return [];
    return filterResourcesByQuery(tertiary.items, tertiaryKind, search, true).filter((i) =>
      matchesStatus(i, statusFilter),
    );
  }, [tertiary.items, tertiaryKind, search, statusFilter]);

  const refresh = () => {
    primary.refresh();
    if (secondaryKind) secondary.refresh();
    if (tertiaryKind) tertiary.refresh();
  };

  const secondaryPath = secondaryListPath ?? listPath;
  const tertiaryPath = tertiaryListPath ?? listPath;

  return (
    <>
      {!hideHeader && (
        <PageHeader
          title={title}
          subtitle={subtitle}
          breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: title }]}
          showLanguageToggle={false}
          actions={
            createPath ? (
              <Button variant="primary" icon={<PlusCircleIcon />} onClick={() => navigate(createPath)}>
                {t('common.create')}
              </Button>
            ) : undefined
          }
        />
      )}
      <FilterToolbar
        search={search}
        onSearchChange={setSearch}
        statusFilter={statusFilter}
        onStatusFilterChange={setStatusFilter}
        onRefresh={refresh}
        searchPlaceholder={t('common.filterResources')}
      />
      <ResourceListTable
        kind={kind}
        items={primaryFiltered}
        loading={primary.loading}
        error={primary.error}
        showNamespace
        linkMode="router"
        detailHref={(item) => adminDetailHref(listPath, kind, item)}
      />
      {secondaryKind && (
        <div style={{ marginTop: '1rem' }}>
          <PageHeader
            title={t(`kinds.${secondaryKind}`, { defaultValue: secondaryKind })}
            showLanguageToggle={false}
          />
          <ResourceListTable
            kind={secondaryKind}
            items={secondaryFiltered}
            loading={secondary.loading}
            error={secondary.error}
            showNamespace
            linkMode="router"
            detailHref={(item) => adminDetailHref(secondaryPath, secondaryKind, item)}
          />
        </div>
      )}
      {tertiaryKind && (
        <div style={{ marginTop: '1rem' }}>
          <PageHeader
            title={t(`kinds.${tertiaryKind}`, { defaultValue: tertiaryKind })}
            showLanguageToggle={false}
          />
          <ResourceListTable
            kind={tertiaryKind}
            items={tertiaryFiltered}
            loading={tertiary.loading}
            error={tertiary.error}
            showNamespace
            linkMode="router"
            detailHref={(item) => adminDetailHref(tertiaryPath, tertiaryKind, item)}
          />
        </div>
      )}
    </>
  );
}
