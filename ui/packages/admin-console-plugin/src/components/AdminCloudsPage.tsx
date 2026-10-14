import '../consoleK8sBootstrap';
import * as React from 'react';
import { PageSection } from '@patternfly/react-core';
import { PageHeader, useTranslation } from '@hybridsovereign/shared';
import { AdminResourceListPage } from './AdminResourceListPage';
import AdminCloudInfrastructuresPage from './AdminCloudInfrastructuresPage';
import '@hybridsovereign/shared/styles/openshift.css';

/** Cloud environments: platform-owned infrastructure first, then the tenant cloud projects on it. */
const AdminCloudsPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <AdminCloudInfrastructuresPage />
        <div style={{ marginTop: '1.5rem' }}>
          <PageHeader
            title={t('pages.cloudProjectsSection')}
            subtitle={t('pages.cloudProjectsSectionSubtitle')}
            showLanguageToggle={false}
          />
          <AdminResourceListPage
            kind="CloudOSO"
            title={t('pages.cloudProjectsSection')}
            secondaryKind="CloudAWS"
            tertiaryKind="CloudVirt"
            listPath="/hybridsovereign/clouds/cloudoso"
            secondaryListPath="/hybridsovereign/clouds/cloudaws"
            tertiaryListPath="/hybridsovereign/clouds/cloudvirt"
            hideHeader
          />
        </div>
      </div>
    </PageSection>
  );
};

export default AdminCloudsPage;
