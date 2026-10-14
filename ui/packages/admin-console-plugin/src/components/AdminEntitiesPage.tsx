import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';
import { PageSection, Button } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import {
  PageHeader,
  FilterToolbar,
  StatusFilter,
  normalizeHealth,
  OperatorStatus,
  HybridSovereignKind,
  KIND_PLURALS,
  useK8sResourceList,
  K8sResource,
  ResourceListTable,
  filterResourcesByQuery,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

type SovereignResource = K8sResource & {
  status?: OperatorStatus;
};

const ENTITY_NS = 'sovereign-cloud';

const AdminEntitiesPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [search, setSearch] = React.useState('');
  const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');
  const { items, loading, error, refresh } = useK8sResourceList<SovereignResource>('Entity', {
    namespace: ENTITY_NS,
  });

  const filtered = React.useMemo(() => {
    return filterResourcesByQuery(items, 'Entity', search, true).filter((item) => {
      if (statusFilter === 'all') return true;
      return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
    });
  }, [items, search, statusFilter]);

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <PageHeader
          title={t('nav.entities')}
          subtitle={t('pages.entitiesSubtitle')}
          breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: t('nav.entities') }]}
          actions={
            <Button
              variant="primary"
              icon={<PlusCircleIcon />}
              onClick={() => navigate('/hybridsovereign/create/entity')}
            >
              {t('common.create')}
            </Button>
          }
        />
        <FilterToolbar
          search={search}
          onSearchChange={setSearch}
          statusFilter={statusFilter}
          onStatusFilterChange={setStatusFilter}
          onRefresh={refresh}
          searchPlaceholder={t('common.filterResources')}
        />
        <ResourceListTable
          kind="Entity"
          items={filtered}
          loading={loading}
          error={error}
          showNamespace
          linkMode="anchor"
          detailHref={(item) =>
            `/hybridsovereign/entities/${encodeURIComponent(item.metadata?.name ?? '')}`
          }
          enableDelete
          onDeleted={refresh}
        />
      </div>
    </PageSection>
  );
};

export default AdminEntitiesPage;

/** Reusable list page for other admin kinds — one-shot fetch, manual refresh only */
export const makeKindListPage = (
  kind: HybridSovereignKind,
  title: string,
  opts?: { createKind?: string; listPath?: string; namespace?: string },
): React.FC => {
  const listPath = opts?.listPath ?? `/hybridsovereign/${KIND_PLURALS[kind] ?? kind.toLowerCase()}`;
  const Page: React.FC = () => {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const [search, setSearch] = React.useState('');
    const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');
    const { items, loading, error, refresh } = useK8sResourceList<SovereignResource>(kind, {
      ...(kind === 'Entity' ? { namespace: ENTITY_NS } : opts?.namespace ? { namespace: opts.namespace } : {}),
    });
    const kindTitle = t(`kinds.${kind}`, { defaultValue: title });

    const filtered = React.useMemo(() => {
      return filterResourcesByQuery(items, kind, search, true).filter((item) => {
        if (statusFilter === 'all') return true;
        return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
      });
    }, [items, search, statusFilter]);

    const detailHref = (item: SovereignResource) => {
      const n = item.metadata?.name ?? '';
      const ns = item.metadata?.namespace ?? '';
      if (kind === 'Entity') {
        return `/hybridsovereign/entities/${encodeURIComponent(n)}`;
      }
      if (
        kind === 'CloudInfrastructure' || kind === 'HybridFabric' ||
        kind === 'CloudGateway' ||
        kind === 'TransportLink' ||
        kind === 'UIHealthChecker'
      ) {
        return `${listPath}/${encodeURIComponent(n)}`;
      }
      return `${listPath}/${encodeURIComponent(ns)}/${encodeURIComponent(n)}`;
    };

    return (
      <PageSection className="sc-console-page">
        <div className="sc-page">
          <PageHeader
            title={kindTitle}
            breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: kindTitle }]}
            actions={
              opts?.createKind ? (
                <Button
                  variant="primary"
                  icon={<PlusCircleIcon />}
                  onClick={() => navigate(`/hybridsovereign/create/${opts.createKind}`)}
                >
                  {t('common.create')}
                </Button>
              ) : undefined
            }
          />
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
            items={filtered}
            loading={loading}
            error={error}
            showNamespace
            linkMode="anchor"
            detailHref={detailHref}
            enableDelete
            onDeleted={refresh}
          />
        </div>
      </PageSection>
    );
  };
  return Page;
};
