import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, PageSection, Tabs, Tab, TabTitleText } from '@patternfly/react-core';
import { PlusCircleIcon } from '@patternfly/react-icons';
import { PageHeader, useTranslation } from '@hybridsovereign/shared';
import { AdminResourceListPage } from './AdminResourceListPage';
import '@hybridsovereign/shared/styles/openshift.css';

const AdminServicesPage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = React.useState<string | number>(0);

  const createForTab =
    activeTab === 0
      ? { label: 'AAP Org', path: '/hybridsovereign/create/aaporg' }
      : activeTab === 1
        ? { label: 'Quay Org', path: '/hybridsovereign/create/quayorg' }
        : { label: 'Vault', path: '/hybridsovereign/create/vault' };

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <PageHeader
          title={t('nav.serviceUrls')}
          subtitle="Platform service resources and configuration health"
          breadcrumbs={[
            { label: t('nav.sovereignCloud') },
            { label: 'Platform' },
            { label: t('nav.serviceUrls') },
          ]}
          actions={
            <Button
              variant="primary"
              icon={<PlusCircleIcon />}
              onClick={() => navigate(createForTab.path)}
            >
              {t('common.create')} {createForTab.label}
            </Button>
          }
        />
        <Tabs activeKey={activeTab} onSelect={(_e, key) => setActiveTab(key)} mountOnEnter unmountOnExit>
          <Tab eventKey={0} title={<TabTitleText>AAP</TabTitleText>}>
            <AdminResourceListPage
              kind="AAPOrg"
              title="AAP Organizations"
              secondaryKind="AAPConfig"
              listPath="/hybridsovereign/services/aaporgs"
              secondaryListPath="/hybridsovereign/services/aapconfigs"
              hideHeader
              enabled={activeTab === 0}
            />
          </Tab>
          <Tab eventKey={1} title={<TabTitleText>Quay</TabTitleText>}>
            <AdminResourceListPage
              kind="QuayOrg"
              title="Quay Organizations"
              secondaryKind="QuayConfig"
              listPath="/hybridsovereign/services/quayorgs"
              secondaryListPath="/hybridsovereign/services/quayconfigs"
              hideHeader
              enabled={activeTab === 1}
            />
          </Tab>
          <Tab eventKey={2} title={<TabTitleText>Vault</TabTitleText>}>
            <AdminResourceListPage
              kind="Vault"
              title="Vault Instances"
              secondaryKind="VaultKV"
              listPath="/hybridsovereign/services/vaults"
              secondaryListPath="/hybridsovereign/services/vaultkvs"
              hideHeader
              enabled={activeTab === 2}
            />
          </Tab>
        </Tabs>
      </div>
    </PageSection>
  );
};

export default AdminServicesPage;
