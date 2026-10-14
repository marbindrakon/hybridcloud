import '../consoleK8sBootstrap';
import * as React from 'react';
import { PageSection } from '@patternfly/react-core';
import {
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
 * rows, so a tenant never sees another tenant's VRFs.
 */
const TenantNetworkOverviewPage: React.FC = () => {
  const { t } = useTranslation();
  const { namespace, entities, selectEntity, entity } = useEntityNamespace();
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
        />
      </div>
    </PageSection>
  );
};

export default TenantNetworkOverviewPage;
