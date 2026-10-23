import React from 'react';
import { Tab, Tabs, TabTitleText } from '@patternfly/react-core';
import { PageHeader } from '../components/PageHeader';
import { useTranslation } from '../i18n';
import { useBreadcrumbs, useDocumentTitle } from '../nav/useNav';
import { ScopedListPage } from './ScopedListPage';

/**
 * Platform integrations: identity provider connections (RbacConfig),
 * automation connections (AAPConfig) and registry connections (QuayConfig),
 * which replace the old "Operators" and "Service URLs" pages.
 */
export function IntegrationsPage(): React.ReactElement {
  const { t } = useTranslation();
  const [tab, setTab] = React.useState<string | number>('rbac');
  const crumbs = useBreadcrumbs('integrations');
  useDocumentTitle('integrations');
  return (
    <>
      <PageHeader title={t('nav.integrations')} breadcrumbs={crumbs} />
      <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} mountOnEnter unmountOnExit>
        <Tab eventKey="rbac" title={<TabTitleText>{t('kinds.RbacConfig')}</TabTitleText>}>
          <ScopedListPage entryId="integration-rbac" hideHeader />
        </Tab>
        <Tab eventKey="aap" title={<TabTitleText>{t('kinds.AAPConfig')}</TabTitleText>}>
          <ScopedListPage entryId="integration-aap" hideHeader />
        </Tab>
        <Tab eventKey="quay" title={<TabTitleText>{t('kinds.QuayConfig')}</TabTitleText>}>
          <ScopedListPage entryId="integration-quay" hideHeader />
        </Tab>
      </Tabs>
    </>
  );
}
