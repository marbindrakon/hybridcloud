import React from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import { useNavigate } from 'react-router-dom';
import { adminDetailHref } from './ResourceListPage';
import {
  NetworkOverview,
  PageHeader,
  useOverviewCRs,
  useTranslation,
  type HybridSovereignKind,
  type K8sResource,
} from '@hybridsovereign/shared';

/** Standalone-dashboard twin of the console plugin's Network Overview. */
const DETAIL_PATH: Partial<Record<HybridSovereignKind, string>> = {
  CloudGateway: '/networking/gateways',
  HybridFabric: '/networking/fabrics',
  CloudInfrastructure: '/clouds/infrastructure',
  HybridNetwork: '/networking/networks',
  NetworkPlacement: '/networking/placements',
  CloudVirt: '/clouds/cloudvirt',
  CloudOSO: '/clouds/cloudoso',
};

export default function NetworkOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { items, loading, error, refresh } = useOverviewCRs(0);

  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const base = DETAIL_PATH[kind];
    if (!base) return;
    navigate(adminDetailHref(base, kind, item));
  };

  return (
    <>
      <PageHeader
        title={t('nav.networkOverview')}
        subtitle={t('pages.networkOverviewDesc')}
        actions={
          <Button variant="secondary" icon={<SyncIcon />} onClick={refresh} isDisabled={loading}>
            {t('common.refresh')}
          </Button>
        }
      />
      <PageSection>
        <NetworkOverview
          resources={items}
          resourcesLoading={loading}
          resourcesError={error}
          onSelect={onSelect}
        />
      </PageSection>
    </>
  );
}
