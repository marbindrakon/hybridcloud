import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, PageSection } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import {
  HybridSovereignKind,
  PageHeader,
  FilterToolbar,
  StatusFilter,
  normalizeHealth,
  useK8sResourceList,
  K8sResource,
  ResourceListTable,
  filterResourcesByQuery,
  useTranslation,
} from '@hybridsovereign/shared';
import { consoleAdminDetailHref } from '../adminPaths';
import '@hybridsovereign/shared/styles/openshift.css';

const ENTITY_NS = 'sovereign-cloud';

function matchesStatus(item: K8sResource, statusFilter: StatusFilter): boolean {
  if (statusFilter === 'all') return true;
  return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
}

export interface AdminResourceListPageProps {
  kind: HybridSovereignKind;
  title: string;
  subtitle?: string;
  secondaryKind?: HybridSovereignKind;
  tertiaryKind?: HybridSovereignKind;
  listPath: string;
  secondaryListPath?: string;
  tertiaryListPath?: string;
  createPath?: string;
  enabled?: boolean;
  hideHeader?: boolean;
}

export const AdminResourceListPage: React.FC<AdminResourceListPageProps> = ({
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
}) => {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [search, setSearch] = React.useState('');
  const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');

  const primary = useK8sResourceList<K8sResource>(kind, {
    enabled,
    ...(kind === 'Entity' ? { namespace: ENTITY_NS } : {}),
  });
  const secondary = useK8sResourceList<K8sResource>(secondaryKind ?? kind, {
    enabled: enabled && !!secondaryKind,
  });
  const tertiary = useK8sResourceList<K8sResource>(tertiaryKind ?? kind, {
    enabled: enabled && !!tertiaryKind,
  });

  const primaryFiltered = React.useMemo(
    () =>
      filterResourcesByQuery(primary.items, kind, search, true).filter((i) =>
        matchesStatus(i, statusFilter),
      ),
    [primary.items, kind, search, statusFilter],
  );

  const secondaryFiltered = React.useMemo(() => {
    if (!secondaryKind) return [];
    return filterResourcesByQuery(secondary.items, secondaryKind, search, true).filter((i) =>
      matchesStatus(i, statusFilter),
    );
  }, [secondary.items, secondaryKind, search, statusFilter]);

  const tertiaryFiltered = React.useMemo(() => {
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
    <PageSection className="sc-console-page">
      <div className="sc-page">
        {!hideHeader && (
          <PageHeader
            title={title}
            subtitle={subtitle}
            breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: title }]}
            actions={
              createPath ? (
                <Button
                  variant="primary"
                  icon={<PlusCircleIcon />}
                  onClick={() => navigate(createPath)}
                >
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
          linkMode="anchor"
          detailHref={(item) => consoleAdminDetailHref(listPath, kind, item)}
          enableDelete
          onDeleted={refresh}
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
              linkMode="anchor"
              detailHref={(item) => consoleAdminDetailHref(secondaryPath, secondaryKind, item)}
              enableDelete
              onDeleted={refresh}
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
              linkMode="anchor"
              detailHref={(item) => consoleAdminDetailHref(tertiaryPath, tertiaryKind, item)}
              enableDelete
              onDeleted={refresh}
            />
          </div>
        )}
      </div>
    </PageSection>
  );
};
