import React from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import { useNavigate } from 'react-router-dom';
import {
  type HybridSovereignKind,
  type K8sResource,
  NetworkOverview,
  PageHeader,
  useNetworkOverviewCRs,
  useTranslation,
} from '@hybridsovereign/shared';

export interface TenantNetworkOverviewPageProps {
  namespace: string;
}

/**
 * Tenant-scoped Network Overview.
 *
 * The fabric, its gateways and the attached sites are platform-owned and shown
 * read-only for context; only this entity's HybridNetworks appear as rows, so a
 * tenant sees where their own networks land without seeing other tenants'.
 */
const TENANT_DETAIL: Partial<Record<HybridSovereignKind, string>> = {
  HybridNetwork: '/networks',
  NetworkPlacement: '/placements',
};

export function TenantNetworkOverviewPage({
  namespace,
}: TenantNetworkOverviewPageProps): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { items, loading, error, refresh } = useNetworkOverviewCRs(namespace);
  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const base = TENANT_DETAIL[kind];
    if (base) navigate(`${base}/${encodeURIComponent(item.metadata.name)}`);
  };

  return (
    <>
      <PageHeader
        title={t('nav.networkOverview')}
        subtitle={`Entity namespace ${namespace}`}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refresh} isDisabled={loading}>
            {t('common.refresh')}
          </Button>
        }
      />
      <PageSection>
        <NetworkOverview
          entityNamespace={namespace}
          resources={items}
          resourcesLoading={loading}
          resourcesError={error}
          onSelect={onSelect}
        />
      </PageSection>
    </>
  );
}

export default TenantNetworkOverviewPage;
