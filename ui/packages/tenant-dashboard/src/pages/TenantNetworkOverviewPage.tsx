import React from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import {
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
 * tenant sees where their own VRFs land without seeing other tenants'.
 */
export function TenantNetworkOverviewPage({
  namespace,
}: TenantNetworkOverviewPageProps): React.ReactElement {
  const { t } = useTranslation();
  const { items, loading, error, refresh } = useNetworkOverviewCRs(namespace);

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
        />
      </PageSection>
    </>
  );
}

export default TenantNetworkOverviewPage;
