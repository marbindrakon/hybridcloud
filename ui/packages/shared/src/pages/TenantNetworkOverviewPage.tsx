import React, { useState } from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import { useNavigate } from 'react-router-dom';
import { type HybridSovereignKind, type K8sResource } from '../types';
import { NetworkOverview } from '../components/NetworkOverview';
import { AttachNetworkDialog, type AttachEnvironmentRef } from '../components/AttachNetworkDialog';
import { usePermission } from '../access/PermissionProvider';
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
  // Empty map cells open the one Attach dialog with the network and the environment fixed (OD25).
  const [attach, setAttach] = useState<{ network: string; environment: AttachEnvironmentRef } | null>(null);
  const canAttach = usePermission('NetworkPlacement', 'create', { namespace });
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
          onAttach={
            namespace && canAttach.allowed
              ? (network, environment) => setAttach({ network, environment: environment as AttachEnvironmentRef })
              : undefined
          }
        />
      </PageSection>
      <AttachNetworkDialog
        isOpen={!!attach}
        namespace={namespace ?? ''}
        network={attach?.network}
        environment={attach?.environment}
        onClose={() => setAttach(null)}
        onAttached={refresh}
      />
    </>
  );
}

