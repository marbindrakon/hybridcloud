import React, { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import {
  HybridSovereignKind,
  useK8sResourceList,
  useCanI,
  K8sResource,
  KIND_PLURALS,
  PageHeader,
  FilterToolbar,
  StatusFilter,
  normalizeHealth,
  ResourceListTable,
  filterResourcesByQuery,
  useTranslation,
} from '@hybridsovereign/shared';

type FormType =
  | 'team'
  | 'project'
  | 'assignment'
  | 'cloudoso'
  | 'cloudaws'
  | 'cloudvirt'
  | 'migration'
  | 'persona'
  | 'rbac'
  | 'vault'
  | 'vaultkv'
  | 'aaporg'
  | 'quayorg'
  | 'hybridnetwork'
  | 'networkplacement';

interface TenantResourcePageProps {
  kind: HybridSovereignKind;
  title: string;
  namespace: string;
  listPath: string;
  formType?: FormType;
}

export function TenantResourcePage({
  kind,
  title,
  namespace,
  listPath,
  formType,
}: TenantResourcePageProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const plural = KIND_PLURALS[kind];
  const { allowed: canCreate } = useCanI(namespace, plural, 'create');
  const { items, loading, error, refresh } = useK8sResourceList<K8sResource>(kind, {
    namespace,
  });

  const filtered = useMemo(() => {
    const byQuery = filterResourcesByQuery(items, kind, search, false);
    return byQuery.filter((item) => {
      if (statusFilter === 'all') return true;
      return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
    });
  }, [items, kind, search, statusFilter]);

  return (
    <>
      <PageHeader
        title={title}
        subtitle={`Namespaced ${kind} resources in ${namespace}`}
        breadcrumbs={[{ label: t('nav.sovereignCloud') }, { label: 'Tenancy' }, { label: title }]}
        actions={
          formType && canCreate ? (
            <Button
              variant="primary"
              icon={<PlusCircleIcon />}
              onClick={() => navigate(`/create/${formType}`)}
            >
              Create
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
        showNamespace={false}
        linkMode="router"
        detailHref={(item) => `${listPath}/${encodeURIComponent(item.metadata.name)}`}
      />
    </>
  );
}
