import React from 'react';
import { Button, PageSection } from '@patternfly/react-core';
import { SyncIcon } from '@patternfly/react-icons';
import { useNavigate } from 'react-router-dom';
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
};

export default function NetworkOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { items, loading, error, refresh } = useOverviewCRs(0);

  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const base = DETAIL_PATH[kind];
    // HybridNetwork has no standalone detail route; only drill into what exists.
    if (!base) return;
    navigate(`${base}/${encodeURIComponent(item.metadata.name)}`);
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
