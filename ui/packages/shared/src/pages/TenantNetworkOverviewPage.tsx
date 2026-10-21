import React from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import { useNavigate } from 'react-router-dom';
import { type HybridSovereignKind, type K8sResource } from '../types';
import { NetworkOverview } from '../components/NetworkOverview';
import { PageHeader } from '../components/PageHeader';
import { useNetworkOverviewCRs } from '../hooks/networkOverviewCrs';
import { useTranslation } from '../i18n';
import { useTenantScope } from '../scope/ScopeProvider';
import { useNav, useBreadcrumbs, useDocumentTitle } from '../nav/useNav';

/**
 * Tenant-scoped network map.
 *
 * The fabric, its gateways and the attached sites are platform-owned and shown
 * read-only for context; only this tenant's networks appear as rows, so a
 * tenant sees where their own networks land without seeing other tenants'.
 */
export function TenantNetworkOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const nav = useNav();
  const { namespace } = useTenantScope();
  const crumbs = useBreadcrumbs('network-map');
  useDocumentTitle('network-map');
  const { items, loading, error, refresh } = useNetworkOverviewCRs(namespace ?? '');
  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const href = nav.routeFor(kind, item.metadata.name);
    if (href) navigate(href);
  };

  return (
    <>
      <PageHeader
        title={t('nav.networkMap')}
        breadcrumbs={crumbs}
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

