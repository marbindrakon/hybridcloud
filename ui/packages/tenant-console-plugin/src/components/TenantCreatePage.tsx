import '../consoleK8sBootstrap';
import * as React from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Alert, PageSection } from '@patternfly/react-core';
import {
  CreateResourceForm,
  NamespaceContextBar,
  SelfServiceFormType,
  useEntityNamespace,
  configureK8sClient,
  useTranslation,
} from '@hybridsovereign/shared';
import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import '@hybridsovereign/shared/styles/openshift.css';

configureK8sClient({
  baseUrl: '/api/kubernetes',
  fetchFn: consoleFetch as unknown as typeof fetch,
  apiStyle: 'raw',
});

const LIST_PATH: Record<string, string> = {
  team: '/hybridsovereign/tenant/teams',
  project: '/hybridsovereign/tenant/projects',
  assignment: '/hybridsovereign/tenant/assignments',
  cloudoso: '/hybridsovereign/tenant/cloudoso',
  cloudaws: '/hybridsovereign/tenant/cloudaws',
  cloudvirt: '/hybridsovereign/tenant/cloudvirt',
  platformopenshift: '/hybridsovereign/tenant/platforms',
  migration: '/hybridsovereign/tenant/migrations',
  persona: '/hybridsovereign/tenant/personas',
  rbac: '/hybridsovereign/tenant/rbac',
  vault: '/hybridsovereign/tenant/vaults',
  vaultkv: '/hybridsovereign/tenant/vaultkvs',
  aaporg: '/hybridsovereign/tenant/aaporgs',
  quayorg: '/hybridsovereign/tenant/quayorgs',
  hybridnetwork: '/hybridsovereign/tenant/networks',
  networkplacement: '/hybridsovereign/tenant/placements',
};

const TenantCreatePage: React.FC = () => {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { formType } = useParams<{ formType: string }>();
  const { namespace, entities, selectEntity, entity } = useEntityNamespace();
  const type = (formType ?? 'team') as SelfServiceFormType;
  const listPath = LIST_PATH[type] ?? '/hybridsovereign/tenant/overview';

  return (
    <PageSection className="sc-console-page">
      <div className="sc-page">
        <NamespaceContextBar
          namespace={namespace}
          entityName={entity?.metadata.name}
          entities={entities}
          onSelectEntity={selectEntity}
        />
        {!namespace ? (
          <Alert variant="info" title="Select an entity" isInline>
            Choose an entity namespace before creating a resource.
          </Alert>
        ) : (
          <CreateResourceForm
            formType={type}
            namespace={namespace}
            listPath={listPath}
            onSuccess={(path) => navigate(path)}
            onCancel={() => navigate(listPath)}
          />
        )}
      </div>
    </PageSection>
  );
};

export default TenantCreatePage;
