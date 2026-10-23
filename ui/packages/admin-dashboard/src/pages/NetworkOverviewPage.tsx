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
  objectPath,
  useBreadcrumbs,
  useDocumentTitle,
} from '@hybridsovereign/shared';

/** Standalone-dashboard twin of the console plugin's fabric map. */
export default function NetworkOverviewPage(): React.ReactElement {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { items, loading, error, refresh } = useOverviewCRs(0);

  const crumbs = useBreadcrumbs('fabric-map');
  useDocumentTitle('fabric-map');
  const onSelect = (kind: HybridSovereignKind, item: K8sResource) => {
    const href = objectPath('admin-dashboard', kind, item.metadata.namespace, item.metadata.name);
    if (href) navigate(href);
  };

  return (
    <>
      <PageHeader
        title={t('nav.fabricMap')}
        subtitle={t('pages.networkOverviewDesc')}
        breadcrumbs={crumbs}
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
