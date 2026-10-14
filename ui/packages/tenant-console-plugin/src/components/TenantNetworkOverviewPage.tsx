import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';
import { PageSection } from '@patternfly/react-core';
import {
  type HybridSovereignKind,
  type K8sResource,
  NetworkOverview,
  PageHeader,
  NamespaceContextBar,
  useEntityNamespace,
  useNetworkOverviewCRs,
  useTranslation,
} from '@hybridsovereign/shared';
import '@hybridsovereign/shared/styles/openshift.css';

/**
 * Tenant-scoped Network Overview.
 *
 * The fabric, its gateways and the attached sites are platform-owned and shown
 * read-only for context; only the selected entity's HybridNetworks appear as
 * rows, so a tenant never sees another tenant's networks.
 */
const TENANT_DETAIL: Partial<Record<HybridSovereignKind, string>> = {
  HybridNetwork: '/hybridsovereign/tenant/networks',
  NetworkPlacement: '/hybridsovereign/tenant/placements',
};

const TenantNetworkOverviewPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { namespace, entities, selectEntity, entity } = useEntityNamespace();
  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const base = TENANT_DETAIL[kind];
    if (base) navigate(`${base}/${encodeURIComponent(item.metadata.name)}`);
  };
  const { items, loading, error } = useNetworkOverviewCRs(namespace);

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <NamespaceContextBar
          namespace={namespace}
          entityName={entity?.metadata.name}
          billingId={(entity?.spec as { billingID?: string } | undefined)?.billingID}
          entities={entities}
          onSelectEntity={selectEntity}
        />
        <PageHeader
          title={t('nav.networkOverview')}
          subtitle={t('pages.networkOverviewDesc')}
        />
        <NetworkOverview
          entityNamespace={namespace}
          resources={items}
          resourcesLoading={loading}
          resourcesError={error}
          onSelect={onSelect}
        />
      </div>
    </PageSection>
  );
};

export default TenantNetworkOverviewPage;
