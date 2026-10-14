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
  useK8sResourceList,
  useEntityNamespace,
  NamespaceContextBar,
  K8sResource,
  HybridSovereignKind,
  configureK8sClient,
  SelfServiceFormType,
  ResourceListTable,
  filterResourcesByQuery,
  useTranslation,
} from '@hybridsovereign/shared';
import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import '@hybridsovereign/shared/styles/openshift.css';

configureK8sClient({
  baseUrl: '/api/kubernetes',
  fetchFn: consoleFetch as unknown as typeof fetch,
  apiStyle: 'raw',
});

const KIND_META: Partial<
  Record<HybridSovereignKind, { path: string; form?: SelfServiceFormType }>
> = {
  Team: { path: 'teams', form: 'team' },
  Project: { path: 'projects', form: 'project' },
  PlatformOpenshift: { path: 'platforms', form: 'platformopenshift' },
  CloudOSO: { path: 'cloudoso', form: 'cloudoso' },
  CloudAWS: { path: 'cloudaws', form: 'cloudaws' },
  CloudVirt: { path: 'cloudvirt', form: 'cloudvirt' },
  OpenStackMigration: { path: 'migrations', form: 'migration' },
  Assignment: { path: 'assignments', form: 'assignment' },
  Vault: { path: 'vaults', form: 'vault' },
  VaultKV: { path: 'vaultkvs', form: 'vaultkv' },
  AAPOrg: { path: 'aaporgs', form: 'aaporg' },
  QuayOrg: { path: 'quayorgs', form: 'quayorg' },
  Persona: { path: 'personas', form: 'persona' },
  Rbac: { path: 'rbac', form: 'rbac' },
  HybridNetwork: { path: 'networks', form: 'hybridnetwork' },
  NetworkPlacement: { path: 'placements', form: 'networkplacement' },
};

export function makeTenantKindPage(kind: HybridSovereignKind, title: string): React.FC {
  const meta = KIND_META[kind] ?? { path: kind.toLowerCase() };
  const listPath = `/hybridsovereign/tenant/${meta.path}`;

  const Page: React.FC = () => {
    const { t } = useTranslation();
    const navigate = useNavigate();
    const { namespace, entities, selectEntity, entity } = useEntityNamespace();
    const [search, setSearch] = React.useState('');
    const [statusFilter, setStatusFilter] = React.useState<StatusFilter>('all');
    const { items, loading, error, refresh } = useK8sResourceList<K8sResource>(kind, {
      namespace,
    });
    const kindTitle = t(`kinds.${kind}`, { defaultValue: title });
    const filtered = React.useMemo(() => {
      return filterResourcesByQuery(items, kind, search, false).filter((item) => {
        if (statusFilter === 'all') return true;
        return normalizeHealth(item.status?.ready, item.status?.status) === statusFilter;
      });
    }, [items, kind, search, statusFilter]);

    return (
      <PageSection className="sc-console-page">
        <div className="sc-page">
          <NamespaceContextBar
            namespace={namespace}
            entityName={entity?.metadata.name}
            entities={entities}
            onSelectEntity={selectEntity}
          />
          <PageHeader
            title={kindTitle}
            subtitle={`${kindTitle} — ${namespace || '—'}`}
            breadcrumbs={[
              { label: t('nav.sovereignCloud') },
              { label: t('nav.tenancy') },
              { label: kindTitle },
            ]}
            actions={
              meta.form && namespace ? (
                <Button
                  variant="primary"
                  size="sm"
                  icon={<PlusCircleIcon />}
                  onClick={() => navigate(`/hybridsovereign/tenant/create/${meta.form}`)}
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
            showNamespace={false}
            linkMode="anchor"
            detailHref={(item) => `${listPath}/${encodeURIComponent(item.metadata.name)}`}
            enableDelete
            onDeleted={refresh}
          />
        </div>
      </PageSection>
    );
  };
  Page.displayName = `Tenant${kind}Page`;
  return Page;
}

const TenantTeamsPage = makeTenantKindPage('Team', 'Teams');
export default TenantTeamsPage;
