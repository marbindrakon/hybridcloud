import '../consoleK8sBootstrap';
import * as React from 'react';
import { PageSection } from '@patternfly/react-core';
import { useTranslation } from '@hybridsovereign/shared';
import { AdminResourceListPage } from './AdminResourceListPage';
import '@hybridsovereign/shared/styles/openshift.css';

const AdminOperatorsPage: React.FC = () => {
  const { t } = useTranslation();

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <AdminResourceListPage
          kind="Rbac"
          title={t('nav.operators')}
          subtitle={t('pages.operatorsSubtitle')}
          secondaryKind="RbacConfig"
          listPath="/hybridsovereign/operators/rbacs"
          secondaryListPath="/hybridsovereign/operators/rbacconfigs"
          createPath="/hybridsovereign/create/rbac"
        />
      </div>
    </PageSection>
  );
};

export default AdminOperatorsPage;
